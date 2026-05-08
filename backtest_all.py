"""
backtest_all.py — 全策略整合回測對比

跑下列策略並排比較：
  1. naked       — 純長部位
  2. put-only    — 1×BP
  3. collar      — 1×BP + 1×SC
  4. hui-ratio   — 1×BP + 2×SP（輝哥比例式）
  5. hui-full    — 1×BP + 2×SP + 1×SC
  6. cal-reverse — 輝哥水平價差（buy 近 + sell 遠）
  7. cal-std     — 標準水平（buy 遠 + sell 近）
  8. ic          — Iron Condor（4 腳）

CLI：
  python3 backtest_all.py --shioaji --days 365
  python3 backtest_all.py --days 252
"""
import argparse
import math
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as B          # noqa: E402
import backtest_hui as BH     # noqa: E402
import backtest_calendar as BC  # noqa: E402

TXO_MULT = 50


def _stats_from_eq(eq, base, n):
    yrs = n / 252
    ret = (eq[-1] - base) / base * 100
    ann = ((1 + ret/100) ** (1/max(yrs, 0.01)) - 1) * 100
    peak = eq[0]; mdd = 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, (v - peak) / peak * 100)
    cal = ann / abs(mdd) if mdd < 0 else 0
    # Sharpe
    rets = []
    for i in range(1, len(eq)):
        if eq[i-1] > 0:
            rets.append((eq[i] - eq[i-1]) / eq[i-1])
    if len(rets) > 1:
        mu = statistics.mean(rets); sigma = statistics.stdev(rets)
        sharpe = (mu * 252) / (sigma * math.sqrt(252)) if sigma > 0 else 0
    else:
        sharpe = 0
    return {'total_ret': ret, 'annual': ann, 'mdd': mdd, 'calmar': cal, 'sharpe': sharpe}


def run_iron_condor(prices, hedge_lots=5, notional_qty=5,
                     dte_target=30, delta_target=0.10, wing_offset=200):
    """4 腳 Iron Condor: BP高 + SP低 / SC低 + BC高（同月）"""
    if not prices: raise ValueError('empty')
    pxs = [p[1] for p in prices]
    hv_arr = B._rolling_hv(pxs)
    # tracker
    legs = {'bp': None, 'sp': None, 'sc': None, 'bc': None}  # PutPosition / CallPosition
    cash_paid = 0.0   # net cash flow
    last_month = None
    out_dates = []; out_naked = []; out_eq = []

    for i, (dt, S) in enumerate(prices):
        hv = max(0.10, hv_arr[i])

        # 到期 settle
        for k, leg in list(legs.items()):
            if leg and dt >= leg.expiry:
                K = leg.strike
                qty = leg.qty
                payoff = 0
                if k in ('bp', 'sp'):
                    payoff = max(0, K - S) * qty * TXO_MULT
                else:
                    payoff = max(0, S - K) * qty * TXO_MULT
                # bp/bc = long → 收 payoff；sp/sc = short → 付 payoff
                if k in ('bp', 'bc'):
                    cash_paid -= payoff   # long expire 收回
                else:
                    cash_paid += payoff   # short expire 我們要付出
                legs[k] = None

        # 月初 roll
        if dt.month != (last_month or 0):
            for k in list(legs.keys()):
                if legs[k]:
                    leg = legs[k]
                    T_left = max(1, (leg.expiry - dt).days) / 365
                    if k in ('bp', 'sp'):
                        v = B.bs_price(S, leg.strike, T_left, hv, is_put=True, r=0.0) * leg.qty * TXO_MULT
                    else:
                        v = B.bs_price(S, leg.strike, T_left, hv, is_put=False, r=0.0) * leg.qty * TXO_MULT
                    if k in ('bp', 'bc'):
                        cash_paid -= v
                    else:
                        cash_paid += v
                    legs[k] = None

            expiry = dt + timedelta(days=dte_target)
            T = dte_target / 365
            put_K  = B._find_put_strike_at_delta(S, T, hv, target_delta=delta_target)
            call_K = B._find_call_strike_at_delta(S, T, hv, target_delta=delta_target)
            bp_K = put_K + wing_offset
            sp_K = put_K
            sc_K = call_K
            bc_K = call_K - wing_offset

            # 開 4 腳
            for k, K, is_put, side in [
                ('bp', bp_K, True,  'buy'),
                ('sp', sp_K, True,  'sell'),
                ('sc', sc_K, False, 'sell'),
                ('bc', bc_K, False, 'buy'),
            ]:
                prem = B.bs_price(S, K, T, hv, is_put=is_put, r=0.0)
                cost = prem * hedge_lots * TXO_MULT
                if side == 'buy':
                    cash_paid += cost
                    if is_put: legs[k] = B.PutPosition(entry_date=dt, expiry=expiry, strike=K, qty=hedge_lots, entry_premium=prem, iv_at_entry=hv)
                    else:      legs[k] = B.CallPosition(entry_date=dt, expiry=expiry, strike=K, qty=hedge_lots, entry_premium=prem, iv_at_entry=hv)
                else:
                    cash_paid -= cost
                    if is_put: legs[k] = B.PutPosition(entry_date=dt, expiry=expiry, strike=K, qty=hedge_lots, entry_premium=prem, iv_at_entry=hv)
                    else:      legs[k] = B.CallPosition(entry_date=dt, expiry=expiry, strike=K, qty=hedge_lots, entry_premium=prem, iv_at_entry=hv)
            last_month = dt.month

        # MTM
        long_v = S * notional_qty * TXO_MULT
        leg_value = 0.0
        for k, leg in legs.items():
            if not leg: continue
            T_left = max(0, (leg.expiry - dt).days) / 365
            if k in ('bp', 'sp'):
                v = B.bs_price(S, leg.strike, T_left, hv, is_put=True, r=0.0) * leg.qty * TXO_MULT
            else:
                v = B.bs_price(S, leg.strike, T_left, hv, is_put=False, r=0.0) * leg.qty * TXO_MULT
            if k in ('bp', 'bc'):
                leg_value += v
            else:
                leg_value -= v

        eq = long_v + leg_value - cash_paid
        out_dates.append(dt)
        out_naked.append(long_v)
        out_eq.append(eq)

    return out_dates, out_naked, out_eq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', help='TX CSV')
    ap.add_argument('--shioaji', action='store_true')
    ap.add_argument('--days', type=int, default=252)
    ap.add_argument('--lots', type=int, default=5)
    ap.add_argument('--sp-offset', type=int, default=500)
    args = ap.parse_args()

    if args.csv:        prices = B.load_csv(args.csv)
    elif args.shioaji:  prices = B.fetch_tx_history_shioaji(days=args.days + 30)
    else:               prices = B.synthetic_prices(days=args.days)

    if not prices:
        print('無資料', file=sys.stderr); return 1

    print(f'[backtest_all] {len(prices)} 天 · 跑 8 策略', file=sys.stderr)

    # 1. naked / put_only / collar
    res_main = B.run_backtest(prices, hedge_lots=args.lots, sell_call=True)
    base = res_main.equity_naked[0]
    n = len(res_main.dates)

    # 2. hui ratio + full
    res_hui = BH.run_hui_backtest(prices, hedge_lots=args.lots, sp_offset=args.sp_offset)

    # 3. calendar reverse + std
    cal_rev = BC.run_calendar_backtest(prices, hedge_lots=args.lots, variant='reverse')
    cal_std = BC.run_calendar_backtest(prices, hedge_lots=args.lots, variant='standard')

    # 4. Iron condor
    ic_dates, ic_naked, ic_eq = run_iron_condor(prices, hedge_lots=args.lots)

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

    stats = []
    for name, eq in rows:
        if not eq: continue
        s = _stats_from_eq(eq, base, n)
        stats.append({'name': name, **s})

    # Sort by Calmar
    stats.sort(key=lambda r: r['calmar'], reverse=True)

    print()
    print('━' * 90)
    print(f'8 策略整合回測（{prices[0][0]} → {prices[-1][0]}, '
          f'TX {prices[0][1]:.0f} → {prices[-1][1]:.0f}, '
          f'{((prices[-1][1]/prices[0][1])-1)*100:+.2f}%）')
    print('━' * 90)
    print(f'  {"排名":>4}  {"策略":>14}  │ {"總報酬":>10} {"年化":>10} {"MaxDD":>10} {"Calmar":>8} {"Sharpe":>8}')
    print('─' * 90)
    for i, r in enumerate(stats, 1):
        bar = ' 🏆' if i == 1 else ('  ⭐' if i == 2 else '   ')
        print(f'  {i:>4}  {r["name"]:>14}{bar}│ '
              f'{r["total_ret"]:>+9.2f}% {r["annual"]:>+9.2f}% '
              f'{r["mdd"]:>+9.2f}% {r["calmar"]:>+8.2f} {r["sharpe"]:>+8.2f}')
    print('━' * 90)
    print()
    print(f'💡 期間 TX {((prices[-1][1]/prices[0][1])-1)*100:+.1f}%；'
          f'最佳 Calmar = {stats[0]["name"]}，最佳報酬 = {max(stats, key=lambda r: r["total_ret"])["name"]}')


if __name__ == '__main__':
    sys.exit(main() or 0)
