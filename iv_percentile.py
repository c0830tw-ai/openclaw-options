"""
iv_percentile.py — ATM IV 歷史百分位排名

每次 shioaji_collar refresh 時 append 當日 ATM IV 到 iv_history.json，
然後算當前 IV 在過去 252 天的百分位 / 分類。

若紀錄不足（<30 天），可用 --backfill 從 Shioaji TX 抓 1 年計算
trailing 20-day HV 當代理（IV proxy），先有歷史可比。

CLI：
  python3 iv_percentile.py --backfill         # 用 TX HV proxy 灌 1 年資料
  python3 iv_percentile.py                     # 印當前狀態
"""
import argparse
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
HISTORY_FILE = _HERE / 'iv_history.json'


def load_history() -> List[Dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return []


def save_history(history: List[Dict[str, Any]]) -> None:
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')


def append_today(iv: float, source: str = 'live') -> int:
    """寫入今日 IV（覆寫同日）。回傳累積筆數。"""
    if iv is None or iv <= 0:
        return len(load_history())
    today = datetime.now().date().isoformat()
    h = load_history()
    h = [r for r in h if r.get('date') != today]
    h.append({'date': today, 'iv': float(iv), 'source': source})
    h.sort(key=lambda r: r['date'])
    save_history(h)
    return len(h)


def _classify(pct: Optional[float]) -> Tuple[str, str]:
    """回傳 (label, view)。"""
    if pct is None:
        return '—', '資料不足'
    if pct >= 85: return '極高 IV', '賣方有利、買方避建倉'
    if pct >= 70: return '偏高 IV', '賣方略佳'
    if pct >= 30: return '中性 IV', '無明顯偏好'
    if pct >= 15: return '偏低 IV', '買方略佳'
    return '極低 IV', '買方有利、IV crush 後低點'


def summary(current_iv: float, lookback_days: int = 252) -> Optional[Dict[str, Any]]:
    """產出當前 IV 在過去 lookback_days 內的百分位。
    紀錄不足 30 天時 enough_data=False。"""
    if current_iv is None or current_iv <= 0:
        return None
    h = load_history()
    if not h:
        return {
            'enough_data': False, 'history_n': 0,
            'current_iv':  round(current_iv, 4),
            'message': '無歷史資料；請執行 python3 iv_percentile.py --backfill',
        }
    cutoff = (datetime.now().date() - timedelta(days=lookback_days)).isoformat()
    recent = [r['iv'] for r in h if r.get('date', '') >= cutoff and r.get('iv') is not None]
    if not recent:
        return {'enough_data': False, 'history_n': 0, 'current_iv': round(current_iv, 4)}

    less = sum(1 for v in recent if v < current_iv)
    pct  = less / len(recent) * 100
    label, view = _classify(pct if len(recent) >= 30 else None)

    sorted_iv = sorted(recent)
    n = len(sorted_iv)

    return {
        'enough_data':    n >= 30,
        'current_iv':     round(current_iv, 4),
        'current_iv_pct': round(current_iv * 100, 1),
        'percentile':     round(pct, 1),
        'history_n':      n,
        'lookback_days':  lookback_days,
        'min_pct':        round(min(recent) * 100, 1),
        'max_pct':        round(max(recent) * 100, 1),
        'median_pct':     round(sorted_iv[n // 2] * 100, 1),
        'p25_pct':        round(sorted_iv[n // 4] * 100, 1),
        'p75_pct':        round(sorted_iv[n * 3 // 4] * 100, 1),
        'label':          label,
        'view':           view,
    }


# ── Backfill from TX HV ───────────────────────────────────
def backfill_from_tx(period: int = 20, days: int = 400) -> int:
    """用 Shioaji TX 1 年抓日 K，算 rolling N 日 HV，當代理 IV history 灌入。"""
    sys.path.insert(0, str(_HERE))
    import backtest as B
    prices = dict(B.fetch_tx_history_shioaji(days=days))
    if len(prices) < period + 5:
        print(f'[iv_percentile] TX 資料不夠 ({len(prices)} 天 < {period+5})', file=sys.stderr)
        return 0

    items = sorted(prices.items())
    history = []
    for i in range(period, len(items)):
        window = [p for _, p in items[i - period:i + 1]]
        rets = [math.log(window[j] / window[j - 1]) for j in range(1, len(window))
                if window[j - 1] > 0]
        if len(rets) < period - 1:
            continue
        mean = sum(rets) / len(rets)
        var  = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
        hv   = math.sqrt(var * 252)
        date_str = items[i][0].isoformat() if hasattr(items[i][0], 'isoformat') else str(items[i][0])
        history.append({'date': date_str, 'iv': round(hv, 4), 'source': 'tx_hv_proxy'})

    save_history(history)
    return len(history)


# ── CLI ───────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--backfill', action='store_true',
                    help='從 Shioaji TX 1 年資料計算 HV proxy 當作 IV history')
    ap.add_argument('--days',  type=int, default=400)
    args = ap.parse_args()

    if args.backfill:
        n = backfill_from_tx(days=args.days)
        print(f'[iv_percentile] backfilled {n} 筆 → {HISTORY_FILE.name}', file=sys.stderr)
        h = load_history()
        if h:
            ivs = [r['iv'] for r in h]
            print(f'  範圍：{min(ivs)*100:.1f}% ~ {max(ivs)*100:.1f}%（中位 {sorted(ivs)[len(ivs)//2]*100:.1f}%）')
        return 0

    h = load_history()
    print(f'history entries: {len(h)}')
    if h:
        latest = h[-1]
        print(f'latest: {latest["date"]} IV={latest["iv"]*100:.1f}% (source={latest.get("source", "?")})')
        s = summary(latest['iv'])
        if s and s.get('enough_data'):
            print()
            print(f"  當前 IV  : {s['current_iv_pct']:.1f}%  → 百分位 {s['percentile']:.0f} pctile")
            print(f"  分類     : {s['label']} ({s['view']})")
            print(f"  歷史範圍 : {s['min_pct']:.1f}% ~ {s['max_pct']:.1f}%（中位 {s['median_pct']:.1f}%）")
            print(f"  IQR      : {s['p25_pct']:.1f}% ~ {s['p75_pct']:.1f}%")
    return 0


if __name__ == '__main__':
    sys.exit(main())
