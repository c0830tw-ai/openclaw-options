"""
event_analysis.py — 歷史事件 P&L 解析

對每個過去發生的事件（FOMC / CPI / 法說），用 TX 歷史價格計算：
  - T-1 → T （事件當日）變動
  - T → T+1 （隔日）變動
  - T-1 → T+3 （事件後 3 天）變動
聚合按 event_type 分群，看：
  - 平均 / 中位 TX 變動
  - 跳空頻率（>±1.5% 視為大跳空）
  - 「方向偏好」（FOMC 平均偏跌？CPI 偏漲？）

CLI：
  python3 event_analysis.py            # 抓 Shioaji TX 1 年 + events，寫 event_history.json
  python3 event_analysis.py --print    # 印出彙整不寫檔
"""
import argparse
import json
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
OUT_PATH = _HERE / 'event_history.json'


def _find_price_around(prices: Dict[date, float], target: date,
                        offset_days: int) -> Optional[Tuple[date, float]]:
    """從 target+offset_days 出發找最近一個交易日（往前/往後最多 5 天）。"""
    direction = 1 if offset_days >= 0 else -1
    base = target + timedelta(days=offset_days)
    for delta in range(0, 7):
        check = base + timedelta(days=delta * direction)
        if check in prices:
            return check, prices[check]
        check2 = base + timedelta(days=delta * (-direction))
        if check2 in prices:
            return check2, prices[check2]
    return None


def analyze_one(event: Dict[str, Any], prices: Dict[date, float]) -> Optional[Dict[str, Any]]:
    """單事件 P&L。回傳 None 若價格資料不足。"""
    try:
        ev_date = datetime.strptime(event['date'], '%Y-%m-%d').date()
    except (KeyError, ValueError):
        return None
    if ev_date >= datetime.now().date():
        return None

    t_minus1 = _find_price_around(prices, ev_date, -1)
    t_zero   = _find_price_around(prices, ev_date,  0)
    t_plus1  = _find_price_around(prices, ev_date,  1)
    t_plus3  = _find_price_around(prices, ev_date,  3)
    if not (t_minus1 and t_zero):
        return None

    def _pct(p_from, p_to):
        if not (p_from and p_to) or p_from[1] == 0:
            return None
        return (p_to[1] - p_from[1]) / p_from[1] * 100

    pct_T   = _pct(t_minus1, t_zero)
    pct_T1  = _pct(t_zero,   t_plus1) if t_plus1 else None
    pct_T3  = _pct(t_minus1, t_plus3) if t_plus3 else None

    return {
        'date':    event['date'],
        'name':    event.get('name', ''),
        'type':    event.get('type', 'other'),
        'tx_at_T': round(t_zero[1], 1),
        'tx_change_T_pct':  round(pct_T,  2) if pct_T  is not None else None,
        'tx_change_T1_pct': round(pct_T1, 2) if pct_T1 is not None else None,
        'tx_change_T3_pct': round(pct_T3, 2) if pct_T3 is not None else None,
    }


def aggregate_by_type(per_event: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按 type 聚合：平均 / 中位 / 跳空頻率。"""
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for ev in per_event:
        by_type.setdefault(ev['type'], []).append(ev)

    out = {}
    for t, evs in by_type.items():
        ts  = [e['tx_change_T_pct']  for e in evs if e['tx_change_T_pct']  is not None]
        t1s = [e['tx_change_T1_pct'] for e in evs if e['tx_change_T1_pct'] is not None]
        if not ts:
            continue
        big_jumps = sum(1 for v in ts if abs(v) > 1.5)
        bull_days = sum(1 for v in ts if v > 0)
        out[t] = {
            'n':                     len(evs),
            'avg_T_pct':             round(statistics.mean(ts), 2),
            'median_T_pct':          round(statistics.median(ts), 2),
            'avg_T1_pct':            round(statistics.mean(t1s), 2) if t1s else None,
            'big_jump_rate_pct':     round(big_jumps / len(ts) * 100, 1),
            'bull_day_rate_pct':     round(bull_days / len(ts) * 100, 1),
            'min_T_pct':             round(min(ts), 2),
            'max_T_pct':             round(max(ts), 2),
        }
    return out


def analyze(events: List[Dict[str, Any]], prices: Dict[date, float]) -> Dict[str, Any]:
    """主入口：events list + price dict → 聚合 + per-event。"""
    per_event = []
    for ev in events:
        r = analyze_one(ev, prices)
        if r is not None:
            per_event.append(r)
    per_event.sort(key=lambda e: e['date'], reverse=True)

    return {
        'generated_at':  datetime.now().isoformat(timespec='seconds'),
        'event_count':   len(per_event),
        'price_points':  len(prices),
        'first_date':    per_event[-1]['date'] if per_event else None,
        'last_date':     per_event[0]['date']  if per_event else None,
        'aggregates':    aggregate_by_type(per_event),
        'events':        per_event,
    }


# ── CLI ───────────────────────────────────────────────────
def _load_events_combined() -> List[Dict[str, Any]]:
    """讀 events.json + events_auto.json，過濾掉未來事件。"""
    out = []
    today = datetime.now().date().isoformat()
    for fname in ('events.json', 'events_auto.json'):
        p = _HERE / fname
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            continue
        for ev in d.get('events') or []:
            if ev.get('date') and ev['date'] < today:
                out.append(ev)
    # dedup by (date, type)
    seen = set()
    deduped = []
    for ev in out:
        key = (ev.get('date'), ev.get('type'))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)
    return deduped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=400, help='Days of TX history to fetch')
    ap.add_argument('--print', dest='print_only', action='store_true', help='Only print, do not write')
    args = ap.parse_args()

    print(f'[event_analysis] fetching {args.days} days of TX history...', file=sys.stderr)
    sys.path.insert(0, str(_HERE))
    import backtest as B
    tx_history = B.fetch_tx_history_shioaji(days=args.days)
    prices = dict(tx_history)
    print(f'[event_analysis] got {len(prices)} price points', file=sys.stderr)

    events = _load_events_combined()
    print(f'[event_analysis] {len(events)} past events to analyze', file=sys.stderr)

    result = analyze(events, prices)
    print(f'[event_analysis] analyzed {result["event_count"]} events with valid TX windows', file=sys.stderr)

    # 印彙整
    print()
    print('━' * 60)
    print(f'歷史事件 TX 影響彙整（{result["first_date"]} → {result["last_date"]}）')
    print('━' * 60)
    print(f'{"類型":>10} │ {"次數":>4} │ {"平均 T%":>8} │ {"中位 T%":>8} │ {"跳空率":>6} │ {"漲日率":>6}')
    for t, agg in result['aggregates'].items():
        print(f'{t:>10} │ {agg["n"]:>4} │ {agg["avg_T_pct"]:>+7.2f}% │ {agg["median_T_pct"]:>+7.2f}% │ {agg["big_jump_rate_pct"]:>5.1f}% │ {agg["bull_day_rate_pct"]:>5.1f}%')
    print()
    print('近 5 個事件：')
    for e in result['events'][:5]:
        t = e.get('tx_change_T_pct')
        t1 = e.get('tx_change_T1_pct')
        t_str  = f'{t:+.2f}%'  if t  is not None else '—'
        t1_str = f'{t1:+.2f}%' if t1 is not None else '—'
        print(f'  {e["date"]} {e["name"][:30]:30s} T{t_str:>8s}  T+1{t1_str:>8s}')
    print('━' * 60)

    if not args.print_only:
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'[event_analysis] wrote → {OUT_PATH}', file=sys.stderr)


if __name__ == '__main__':
    main()
