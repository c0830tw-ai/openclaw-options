"""
backtest_hui_regime.py — 輝哥比例式 × 多情境分段回測

對每個 regime window（牛/熊/盤整）跑 sp_offset sweep，
看不同市場狀態下哪個 offset 最佳。

CLI：
  python3 backtest_hui_regime.py --shioaji --quarters 4
  python3 backtest_hui_regime.py --shioaji         # auto-detect
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as B            # noqa: E402
import backtest_hui as BH       # noqa: E402
import backtest_regime as BR    # noqa: E402

OFFSETS = [200, 400, 700, 1000, 1500]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', help='TX CSV')
    ap.add_argument('--shioaji', action='store_true')
    ap.add_argument('--days', type=int, default=400)
    ap.add_argument('--lots', type=int, default=5)
    ap.add_argument('--quarters', type=int, default=0,
                    help='強制等分為 N 段，否則 auto-detect')
    args = ap.parse_args()

    if args.csv:        prices = B.load_csv(args.csv)
    elif args.shioaji:  prices = B.fetch_tx_history_shioaji(days=args.days)
    else:               prices = B.synthetic_prices(days=args.days)

    if not prices:
        print('沒抓到資料', file=sys.stderr)
        return 1

    print(f'[hui_regime] {len(prices)} 天 · 跑 {len(OFFSETS)} 個 offset / regime', file=sys.stderr)

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
        print('無有效 regime window', file=sys.stderr)
        return 1

    LABEL = {'bull': '🐂 牛市', 'bear': '🐻 熊市', 'side': '😴 盤整'}

    print()
    print('━' * 100)
    print(f'{"Regime":>10} │ {"window":>22} │ {"offset":>7} │ '
          f'{"ratio":>8} {"vs po":>7} │ {"DD":>8} {"Calmar":>8}')
    print('─' * 100)

    summaries = []
    for w in windows:
        slice_p = prices[w['start_idx']:w['end_idx'] + 1]
        if len(slice_p) < 30:
            continue
        rows = BH.sweep_offsets(slice_p, args.lots, OFFSETS)
        if not rows:
            continue
        # 找 best Calmar
        best = max(rows, key=lambda r: r['ratio_calmar'] if r['ratio_calmar'] else -999)
        for r in rows:
            mark = ' ⭐' if r['sp_offset'] == best['sp_offset'] else ''
            label = LABEL.get(w['regime'], w['regime'])
            window_s = f'{w["start_date"]}→{w["end_date"]}'
            print(f'{label:>10} │ {window_s:>22} │ {r["sp_offset"]:>7} │ '
                  f'{r["ratio_ret"]:>+7.2f}% {r["ratio_vs_po"]:>+6.2f}% │ '
                  f'{r["ratio_dd"]:>+7.2f}% {r["ratio_calmar"]:>+8.2f}{mark}')
        print('─' * 100)
        summaries.append({**w, 'best_offset': best['sp_offset'], 'best': best})

    # 跨 regime 摘要
    print()
    print('━' * 80)
    print('各情境最佳 sp_offset 對比')
    print('━' * 80)
    print(f'{"Regime":>10} │ {"return %":>10} │ {"best offset":>11} │ {"ratio ret":>10} {"DD":>8} {"Calmar":>8}')
    for s in summaries:
        print(f'  {LABEL.get(s["regime"], s["regime"]):<8}'
              f'│ {s["return_pct"]:>+9.2f}% │ {s["best_offset"]:>11} │ '
              f'{s["best"]["ratio_ret"]:>+9.2f}% {s["best"]["ratio_dd"]:>+7.2f}% {s["best"]["ratio_calmar"]:>+8.2f}')
    print('━' * 80)

    if len(summaries) >= 2:
        offsets_used = [s['best_offset'] for s in summaries]
        print()
        print('💡 觀察：')
        if len(set(offsets_used)) == 1:
            print(f'   所有情境最佳 offset 都是 {offsets_used[0]}')
        else:
            print(f'   最佳 offset 因 regime 而異：{dict(zip([s["regime"] for s in summaries], offsets_used))}')
        print('   (offset 大 = SP 深 OTM、安全；offset 小 = 收 credit 多但 DD 風險大)')


if __name__ == '__main__':
    sys.exit(main() or 0)
