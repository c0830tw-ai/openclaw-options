"""
backtest_regime.py — 多區間回測：自動偵測牛/熊/盤整 → 各區間獨立 sweep

流程：
  1. 抓 1 年 TX 歷史
  2. 計算 60-day 累積報酬，分類 bull (>+10%) / bear (<-10%) / sideways
  3. 找連續同類窗口（長度 ≥30 天）
  4. 對每個窗口跑 backtest_optimize.sweep()
  5. 比對 best params 是否因情境而異

CLI：
  python3 backtest_regime.py --shioaji
  python3 backtest_regime.py --csv tx.csv
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import List, Tuple, Dict, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as B            # noqa: E402
import backtest_optimize as BO  # noqa: E402

REGIME_WINDOW = 30     # 看回 N 天累積報酬
BULL_THRESHOLD = 7     # %
BEAR_THRESHOLD = -7    # %
MIN_WINDOW    = 25     # 至少 25 天才算一個 regime window


def detect_regimes(prices: List[Tuple[date, float]]) -> List[Dict[str, Any]]:
    """從 (date, price) 序列回傳 [{regime, start, end, n, return_pct}, ...]。"""
    if len(prices) < REGIME_WINDOW + 5:
        return []

    # 標記每天 regime
    labels = []
    for i in range(len(prices)):
        if i < REGIME_WINDOW:
            labels.append('init')
            continue
        ret = (prices[i][1] - prices[i - REGIME_WINDOW][1]) / prices[i - REGIME_WINDOW][1] * 100
        if ret >= BULL_THRESHOLD:    labels.append('bull')
        elif ret <= BEAR_THRESHOLD:  labels.append('bear')
        else:                         labels.append('side')

    # 找連續窗口
    windows = []
    cur_label = None
    cur_start = None
    for i, label in enumerate(labels):
        if label == 'init':
            continue
        if label != cur_label:
            if cur_label and i - cur_start >= MIN_WINDOW:
                windows.append({
                    'regime':     cur_label,
                    'start_idx':  cur_start,
                    'end_idx':    i - 1,
                    'start_date': prices[cur_start][0],
                    'end_date':   prices[i - 1][0],
                    'n_days':     i - cur_start,
                    'price_start': prices[cur_start][1],
                    'price_end':   prices[i - 1][1],
                    'return_pct': round((prices[i - 1][1] - prices[cur_start][1]) / prices[cur_start][1] * 100, 2),
                })
            cur_label = label
            cur_start = i
    # 最後一段
    if cur_label and len(labels) - cur_start >= MIN_WINDOW:
        last = len(labels) - 1
        windows.append({
            'regime':     cur_label,
            'start_idx':  cur_start,
            'end_idx':    last,
            'start_date': prices[cur_start][0],
            'end_date':   prices[last][0],
            'n_days':     last - cur_start + 1,
            'price_start': prices[cur_start][1],
            'price_end':   prices[last][1],
            'return_pct': round((prices[last][1] - prices[cur_start][1]) / prices[cur_start][1] * 100, 2),
        })

    return windows


def sweep_regime(prices_slice: List[Tuple[date, float]], hedge_lots: int = 5) -> List[Dict[str, Any]]:
    """跑單一 regime window 的 sweep。"""
    return BO.sweep(prices_slice, hedge_lots=hedge_lots)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', help='TX history CSV')
    ap.add_argument('--shioaji', action='store_true')
    ap.add_argument('--days', type=int, default=400)
    ap.add_argument('--lots', type=int, default=5)
    ap.add_argument('--quarters', type=int, default=0,
                    help='強制等分為 N 段（取代 auto-detect）')
    args = ap.parse_args()

    if args.csv:
        prices = B.load_csv(args.csv)
    elif args.shioaji:
        prices = B.fetch_tx_history_shioaji(days=args.days)
    else:
        prices = B.synthetic_prices(days=args.days)

    print(f'[regime] {len(prices)} 個交易日', file=sys.stderr)

    if args.quarters > 0:
        # 等分模式
        n = args.quarters
        chunk = len(prices) // n
        windows = []
        for i in range(n):
            s = i * chunk
            e = (i + 1) * chunk - 1 if i < n - 1 else len(prices) - 1
            ret = (prices[e][1] - prices[s][1]) / prices[s][1] * 100
            label = 'bull' if ret > 5 else 'bear' if ret < -5 else 'side'
            windows.append({
                'regime': label, 'start_idx': s, 'end_idx': e,
                'start_date': prices[s][0], 'end_date': prices[e][0],
                'n_days': e - s + 1,
                'price_start': prices[s][1], 'price_end': prices[e][1],
                'return_pct': round(ret, 2),
            })
        print(f'[regime] 等分 {n} 段', file=sys.stderr)
    else:
        windows = detect_regimes(prices)
        print(f'[regime] auto-detect 找到 {len(windows)} 個 regime windows', file=sys.stderr)

    if not windows:
        print('沒有足夠長度的 regime window（需 ≥30 天）', file=sys.stderr)
        return 1

    REGIME_LABEL = {'bull': '🐂 牛市', 'bear': '🐻 熊市', 'side': '😴 盤整'}

    # 印 regime 摘要
    print()
    print('━' * 70)
    print(f'{"Regime":>8}  {"start":>10} → {"end":>10}  {"n":>4}d  {"return":>8}')
    print('─' * 70)
    for w in windows:
        print(f'  {REGIME_LABEL.get(w["regime"], w["regime"]):<10}'
              f'  {w["start_date"]} → {w["end_date"]}'
              f'  {w["n_days"]:>4}d  {w["return_pct"]:>+7.2f}%')
    print('━' * 70)

    # 對每個 window sweep
    all_results = []
    for w in windows:
        slice_p = prices[w['start_idx']:w['end_idx'] + 1]
        if len(slice_p) < 30:
            continue
        print()
        print(f'═══ {REGIME_LABEL.get(w["regime"], w["regime"])}'
              f'  {w["start_date"]} → {w["end_date"]} ({w["n_days"]}d, {w["return_pct"]:+.2f}%) ═══',
              file=sys.stderr)
        rows = sweep_regime(slice_p, hedge_lots=args.lots)
        if not rows:
            continue
        best = rows[0]
        all_results.append({**w, 'best': best, 'top3': rows[:3]})

    # 跨 regime 比較
    print()
    print('━' * 90)
    print('各 regime 最佳組合對比')
    print('━' * 90)
    print(f'{"Regime":>8}  {"DTE":>4} {"Δ":>5} {"策略":>9} │ {"Return":>8} {"vs裸":>8} {"MaxDD":>8} {"Calmar":>8}')
    print('─' * 90)
    for r in all_results:
        b = r['best']
        print(f'  {REGIME_LABEL.get(r["regime"], r["regime"]):<10}'
              f'{b["dte"]:>4} {b["delta"]:>5.2f} {b["strategy"]:>9} │ '
              f'{b["return_pct"]:>+7.2f}% {b["vs_naked"]:>+7.2f}% '
              f'{b["mdd_pct"]:>+7.2f}% {b["calmar"]:>+8.2f}')
    print('━' * 90)

    # 觀察：各 regime 的 best DTE 是否一致
    if len(all_results) >= 2:
        dtes = [r['best']['dte'] for r in all_results]
        deltas = [r['best']['delta'] for r in all_results]
        strats = [r['best']['strategy'] for r in all_results]
        print()
        print('💡 觀察：')
        if len(set(dtes)) == 1:
            print(f'   所有 regime best DTE 都是 {dtes[0]}')
        else:
            print(f'   best DTE 因 regime 而異：{dict(zip([r["regime"] for r in all_results], dtes))}')
        if len(set(deltas)) == 1:
            print(f'   所有 regime best delta 都是 {deltas[0]}')
        else:
            print(f'   best delta 因 regime 而異：{dict(zip([r["regime"] for r in all_results], deltas))}')
        if len(set(strats)) == 1:
            print(f'   所有 regime 都偏好 {strats[0]} 策略')
        else:
            print(f'   策略偏好因 regime 而異：{dict(zip([r["regime"] for r in all_results], strats))}')
        print('━' * 90)

    return 0


if __name__ == '__main__':
    sys.exit(main())
