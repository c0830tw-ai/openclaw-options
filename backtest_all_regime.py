"""
backtest_all_regime.py — 多情境 × 8 策略整合矩陣回測

對每個 regime window（牛/熊/盤整）跑全部 8 種策略，
輸出該情境最佳策略 + 完整 ranking 矩陣

CLI：
  python3 backtest_all_regime.py --shioaji --quarters 4
  python3 backtest_all_regime.py --shioaji
"""
import argparse
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as B            # noqa: E402
import backtest_hui as BH       # noqa: E402
import backtest_calendar as BC  # noqa: E402
import backtest_regime as BR    # noqa: E402
import backtest_all as BA       # noqa: E402


def evaluate_window(prices, hedge_lots=5, sp_offset=500):
    """對單一 price slice 跑 8 策略，回傳 list of stats。"""
    if not prices or len(prices) < 30:
        return []

    res_main = B.run_backtest(prices, hedge_lots=hedge_lots, sell_call=True)
    res_hui  = BH.run_hui_backtest(prices, hedge_lots=hedge_lots, sp_offset=sp_offset)
    cal_rev  = BC.run_calendar_backtest(prices, hedge_lots=hedge_lots, variant='reverse')
    cal_std  = BC.run_calendar_backtest(prices, hedge_lots=hedge_lots, variant='standard')
    ic_dates, ic_naked, ic_eq = BA.run_iron_condor(prices, hedge_lots=hedge_lots)

    base = res_main.equity_naked[0]
    n = len(res_main.dates)

    rows = [
        ('naked',       res_main.equity_naked),
        ('put-only',    res_main.equity_put_only),
        ('collar',      res_main.equity_collar),
        ('hui-ratio',   res_hui.equity_hui_ratio),
        ('hui-full',    res_hui.equity_hui_full),
        ('cal-reverse', [r['calendar'] for r in cal_rev]),
        ('cal-std',     [r['calendar'] for r in cal_std]),
        ('iron-condor', ic_eq),
    ]
    out = []
    for name, eq in rows:
        if not eq: continue
        s = BA._stats_from_eq(eq, base, n)
        out.append({'name': name, **s})
    out.sort(key=lambda r: r['calmar'], reverse=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', help='TX CSV')
    ap.add_argument('--shioaji', action='store_true')
    ap.add_argument('--days', type=int, default=400)
    ap.add_argument('--lots', type=int, default=5)
    ap.add_argument('--quarters', type=int, default=0)
    args = ap.parse_args()

    if args.csv:        prices = B.load_csv(args.csv)
    elif args.shioaji:  prices = B.fetch_tx_history_shioaji(days=args.days)
    else:               prices = B.synthetic_prices(days=args.days)

    if not prices:
        print('無資料', file=sys.stderr); return 1

    print(f'[all_regime] {len(prices)} 天 · 跑 8 策略 / regime', file=sys.stderr)

    # 切 regime
    if args.quarters > 0:
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
                'n_days': e - s + 1, 'return_pct': round(ret, 2),
            })
    else:
        windows = BR.detect_regimes(prices)

    if not windows:
        print('無有效 regime window', file=sys.stderr); return 1

    LABEL = {'bull': '🐂 牛', 'bear': '🐻 熊', 'side': '😴 盤整'}

    # 完整矩陣 + per-regime 冠軍
    print()
    print('━' * 100)
    summaries = []
    for w in windows:
        slice_p = prices[w['start_idx']:w['end_idx'] + 1]
        stats = evaluate_window(slice_p, hedge_lots=args.lots)
        if not stats: continue
        best = stats[0]
        summaries.append({**w, 'best': best, 'stats': stats})
        print(f'{LABEL.get(w["regime"], w["regime"]):>6}  '
              f'{w["start_date"]} → {w["end_date"]} ({w["n_days"]}d, {w["return_pct"]:+.2f}%)')
        print(f'{"  排名":>6}  {"策略":>14}  │ {"報酬":>9} {"DD":>9} {"Calmar":>8}')
        for i, r in enumerate(stats, 1):
            mark = ' 🏆' if i == 1 else ('  ⭐' if i == 2 else '   ')
            print(f'  {i:>4}  {r["name"]:>14}{mark}│ '
                  f'{r["total_ret"]:>+8.2f}% {r["mdd"]:>+8.2f}% {r["calmar"]:>+8.2f}')
        print('─' * 100)

    # 跨 regime 冠軍對照
    print()
    print('━' * 80)
    print('📊 各情境最佳策略對照')
    print('━' * 80)
    for s in summaries:
        b = s['best']
        print(f'  {LABEL.get(s["regime"], s["regime"]):<8}'
              f'│ TX {s["return_pct"]:>+6.2f}% │ best: {b["name"]:>14} '
              f'({b["total_ret"]:>+6.2f}% / DD {b["mdd"]:>+6.2f}% / Calmar {b["calmar"]:+.2f})')
    print('━' * 80)

    # 全期勝出次數統計（哪些策略最常奪冠 / 進前 3）
    if len(summaries) >= 2:
        win_count = {}
        top3_count = {}
        for s in summaries:
            for i, r in enumerate(s['stats'][:3]):
                top3_count[r['name']] = top3_count.get(r['name'], 0) + 1
                if i == 0:
                    win_count[r['name']] = win_count.get(r['name'], 0) + 1
        print()
        print('💡 跨情境統計：')
        print(f'   奪冠次數：' + ', '.join(f'{k}={v}' for k, v in sorted(win_count.items(), key=lambda x: -x[1])))
        print(f'   進前 3：'   + ', '.join(f'{k}={v}' for k, v in sorted(top3_count.items(), key=lambda x: -x[1])))
        print()
        print('   建議：根據 RegimeAdvisor 偵測當前情境，套用對應冠軍策略')


if __name__ == '__main__':
    sys.exit(main() or 0)
