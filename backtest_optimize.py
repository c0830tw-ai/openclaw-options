"""
backtest_optimize.py — 參數最佳化掃描

對 DTE × delta × strategy(put-only/collar) 三維組合跑 backtest，
按風險調整報酬（return / |maxDD|）排序，找歷史最佳設定。

CLI：
  python3 backtest_optimize.py                # 合成 252 天
  python3 backtest_optimize.py --shioaji      # 真實 TX 1 年
  python3 backtest_optimize.py --csv tx.csv   # 自帶 CSV
"""
import argparse
import sys
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as B   # noqa: E402

# 掃描範圍
DTE_VALUES   = [15, 21, 30, 45]
DELTA_VALUES = [0.05, 0.08, 0.10, 0.15]
STRATEGIES   = [('put_only', False), ('collar', True)]


def sweep(prices, hedge_lots: int = 5):
    """跑所有組合，回傳 list of dict 排序好。"""
    rows = []
    total = len(DTE_VALUES) * len(DELTA_VALUES) * len(STRATEGIES)
    i = 0
    for dte in DTE_VALUES:
        for delta in DELTA_VALUES:
            for label, sell_call in STRATEGIES:
                i += 1
                print(f'[{i}/{total}] DTE={dte} delta={delta:.2f} {label}...',
                      file=sys.stderr, flush=True)
                res = B.run_backtest(
                    prices,
                    hedge_lots=hedge_lots,
                    sell_call=sell_call,
                    dte_target=dte,
                    delta_target=delta,
                )
                if not res.equity_collar:
                    continue

                base = res.equity_naked[0]
                eq = res.equity_collar if sell_call else res.equity_put_only
                ret  = (eq[-1] - base) / base * 100

                # max drawdown
                peak = eq[0]
                mdd = 0.0
                for v in eq:
                    peak = max(peak, v)
                    if peak > 0:
                        mdd = min(mdd, (v - peak) / peak * 100)

                # naked baseline
                naked_ret = (res.equity_naked[-1] - base) / base * 100
                naked_mdd = 0.0
                peak = res.equity_naked[0]
                for v in res.equity_naked:
                    peak = max(peak, v)
                    if peak > 0:
                        naked_mdd = min(naked_mdd, (v - peak) / peak * 100)

                # risk-adj：return / |mdd|（簡易 calmar 比）
                calmar = ret / abs(mdd) if mdd < 0 else float('inf')

                rows.append({
                    'dte':        dte,
                    'delta':      delta,
                    'strategy':   label,
                    'return_pct': round(ret, 2),
                    'naked_ret':  round(naked_ret, 2),
                    'vs_naked':   round(ret - naked_ret, 2),
                    'mdd_pct':    round(mdd, 2),
                    'naked_mdd':  round(naked_mdd, 2),
                    'dd_protect': round(naked_mdd - mdd, 2),  # 正數=保護更多
                    'calmar':     round(calmar, 3),
                    'put_paid':   round(res.put_cash_paid[-1]) if res.put_cash_paid else 0,
                    'call_cash':  round(res.call_cash[-1])     if res.call_cash else 0,
                })

    rows.sort(key=lambda r: r['calmar'], reverse=True)
    return rows


def print_table(rows, top: int = 12):
    print()
    print('━' * 90)
    print('參數掃描結果（按 Calmar 比 = return / |maxDD| 排序）')
    print('━' * 90)
    print(f'{"DTE":>4} {"Δ":>5} {"策略":>9} │ {"Return":>8} {"vs裸":>8} │ '
          f'{"MaxDD":>8} {"DD保護":>8} │ {"Calmar":>8}')
    print('─' * 90)
    for r in rows[:top]:
        sd = '+' if r['dd_protect'] > 0 else ''
        print(f'{r["dte"]:>4} {r["delta"]:>5.2f} {r["strategy"]:>9} │ '
              f'{r["return_pct"]:>+7.2f}% {r["vs_naked"]:>+7.2f}% │ '
              f'{r["mdd_pct"]:>+7.2f}% {sd}{r["dd_protect"]:>+6.2f}% │ '
              f'{r["calmar"]:>+8.2f}')
    print('━' * 90)

    # 推薦
    if rows:
        best = rows[0]
        print()
        print('🏆 最佳組合（Calmar 最高）：')
        print(f'   DTE={best["dte"]}d  delta={best["delta"]:.2f}  策略={best["strategy"]}')
        print(f'   報酬 {best["return_pct"]:+.2f}% (裸長 {best["naked_ret"]:+.2f}%)')
        print(f'   MaxDD {best["mdd_pct"]:+.2f}% (裸長 {best["naked_mdd"]:+.2f}%)')
        if best['dd_protect'] > 0:
            print(f'   ↳ 比裸長部位多保護 {best["dd_protect"]:.2f}% drawdown')
        print()
        print('💡 觀察：')
        # delta 趨勢
        put_only_rows = [r for r in rows if r['strategy'] == 'put_only']
        if put_only_rows:
            best_pure_put = max(put_only_rows, key=lambda r: r['calmar'])
            print(f'   put-only 最佳：DTE={best_pure_put["dte"]} delta={best_pure_put["delta"]:.2f} '
                  f'(Calmar {best_pure_put["calmar"]:+.2f})')
        collar_rows = [r for r in rows if r['strategy'] == 'collar']
        if collar_rows:
            best_collar = max(collar_rows, key=lambda r: r['calmar'])
            print(f'   collar 最佳：DTE={best_collar["dte"]} delta={best_collar["delta"]:.2f} '
                  f'(Calmar {best_collar["calmar"]:+.2f})')
        print('━' * 90)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', help='Path to TX history CSV')
    ap.add_argument('--shioaji', action='store_true', help='Fetch real TX history')
    ap.add_argument('--days', type=int, default=252, help='Synthetic days')
    ap.add_argument('--lots', type=int, default=5, help='Put hedge lots')
    ap.add_argument('--top', type=int, default=12, help='Show top N rows')
    ap.add_argument('--save', help='Save full results to CSV')
    args = ap.parse_args()

    if args.csv:
        prices = B.load_csv(args.csv)
    elif args.shioaji:
        prices = B.fetch_tx_history_shioaji(days=args.days + 30)
    else:
        prices = B.synthetic_prices(days=args.days)

    print(f'[optimize] {len(prices)} 個交易日 · 掃 '
          f'{len(DTE_VALUES)}×{len(DELTA_VALUES)}×{len(STRATEGIES)} 組合', file=sys.stderr)
    rows = sweep(prices, hedge_lots=args.lots)
    print_table(rows, top=args.top)

    if args.save:
        import csv
        with open(args.save, 'w') as f:
            if rows:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
        print(f'[optimize] saved → {args.save}', file=sys.stderr)


if __name__ == '__main__':
    main()
