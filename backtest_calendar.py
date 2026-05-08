"""
backtest_calendar.py — 水平價差 (Calendar Spread) 週度滾動回測

每 N 個交易日開一組新 calendar：
  - 標準 (standard)：buy 遠 + sell 近 (debit)，賺 theta 衰退
  - 反向 (reverse / 輝哥)：buy 近 + sell 遠 (credit)，賺 IV 結構差

近腳到期 → 結算近腳；同日開新近腳（遠腳保留至 far_dte）
直到遠腳也到期 → 重新開整組

CLI：
  python3 backtest_calendar.py                 # 合成 252 天
  python3 backtest_calendar.py --shioaji
  python3 backtest_calendar.py --variant standard
  python3 backtest_calendar.py --variant reverse
"""
import argparse
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Tuple, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as B   # noqa: E402

TXO_MULTIPLIER = 50


@dataclass
class Leg:
    side:    str      # 'buy' / 'sell'
    expiry:  date
    strike:  float
    qty:     int
    entry:   float    # 點數


def _bs_put(S, K, T, sigma):
    return B.bs_price(S, K, T, sigma, is_put=True, r=0.0) if T > 0 else max(0, K - S)


def run_calendar_backtest(prices, hedge_lots=5, notional_qty=5,
                          near_dte=7, far_dte=30, variant='reverse'):
    """每週開一組 calendar，近腳到期 settle，遠腳到期 settle。"""
    if not prices:
        raise ValueError('empty prices')
    pxs = [p[1] for p in prices]
    hv_arr = B._rolling_hv(pxs)

    cur_near: Optional[Leg] = None
    cur_far:  Optional[Leg] = None
    cash = 0.0     # 累積（收 - 付）

    series = []  # (date, equity_naked, equity_calendar)

    for i, (dt, S) in enumerate(prices):
        hv = max(0.10, hv_arr[i])

        # === 到期處理 ===
        if cur_near and dt >= cur_near.expiry:
            payoff = max(0, cur_near.strike - S) * cur_near.qty * TXO_MULTIPLIER
            # Buy → 收 payoff；Sell → 付 payoff
            if cur_near.side == 'buy':  cash += payoff
            else:                        cash -= payoff
            cur_near = None
        if cur_far and dt >= cur_far.expiry:
            payoff = max(0, cur_far.strike - S) * cur_far.qty * TXO_MULTIPLIER
            if cur_far.side == 'buy':   cash += payoff
            else:                        cash -= payoff
            cur_far = None

        # === 開新部位 ===
        # 遠腳：若無 → 開 ATM put，DTE = far_dte
        # 近腳：若無 → 開 ATM put，DTE = near_dte
        # 履約：取 ATM 整百
        atm_K = round(S / 100) * 100

        if cur_far is None:
            far_expiry = dt + timedelta(days=far_dte)
            T_far = far_dte / 365
            far_prem = _bs_put(S, atm_K, T_far, hv)
            far_side = 'buy' if variant == 'standard' else 'sell'
            cost = far_prem * hedge_lots * TXO_MULTIPLIER
            cash += (cost if far_side == 'sell' else -cost)
            cur_far = Leg(side=far_side, expiry=far_expiry, strike=atm_K,
                          qty=hedge_lots, entry=far_prem)

        if cur_near is None:
            near_expiry = dt + timedelta(days=near_dte)
            T_near = near_dte / 365
            # 用 cur_far 同履約以維持 calendar 結構
            K_use = cur_far.strike if cur_far else atm_K
            near_prem = _bs_put(S, K_use, T_near, hv)
            near_side = 'sell' if variant == 'standard' else 'buy'
            cost = near_prem * hedge_lots * TXO_MULTIPLIER
            cash += (cost if near_side == 'sell' else -cost)
            cur_near = Leg(side=near_side, expiry=near_expiry, strike=K_use,
                            qty=hedge_lots, entry=near_prem)

        # === Mark-to-market ===
        long_v = S * notional_qty * TXO_MULTIPLIER

        leg_value = 0.0
        for leg in (cur_near, cur_far):
            if not leg: continue
            T_left = max(0, (leg.expiry - dt).days) / 365
            v = _bs_put(S, leg.strike, T_left, hv) * leg.qty * TXO_MULTIPLIER
            leg_value += v if leg.side == 'buy' else -v   # buy 是資產 / sell 是負債

        equity_naked = long_v
        equity_cal   = long_v + leg_value + cash
        series.append({'date': dt, 'tx': S, 'naked': equity_naked, 'calendar': equity_cal})

    return series


def report(series, variant):
    if len(series) < 2:
        print('資料不足'); return
    base = series[0]['naked']
    n = len(series)
    yrs = n / 252

    def _stats(key):
        eq = [r[key] for r in series]
        ret = (eq[-1] - base) / base * 100
        ann = ((1 + ret/100) ** (1/max(yrs, 0.01)) - 1) * 100
        peak = eq[0]; mdd = 0.0
        for v in eq:
            peak = max(peak, v)
            if peak > 0: mdd = min(mdd, (v - peak) / peak * 100)
        cal = ann / abs(mdd) if mdd < 0 else 0
        return ret, ann, mdd, cal

    n_ret, n_ann, n_dd, n_cal = _stats('naked')
    c_ret, c_ann, c_dd, c_cal = _stats('calendar')

    print()
    print('━' * 70)
    print(f'回測：{series[0]["date"]} → {series[-1]["date"]}  ({n} 天)  · variant: {variant}')
    print(f'TX: {series[0]["tx"]:.0f} → {series[-1]["tx"]:.0f}  ({(series[-1]["tx"]/series[0]["tx"]-1)*100:+.2f}%)')
    print('━' * 70)
    print(f'{"":12} │ {"裸長":>10} │ {"+ Calendar":>12}')
    print(f'{"總報酬":12} │ {n_ret:+9.2f}% │ {c_ret:+11.2f}%')
    print(f'{"年化":12} │ {n_ann:+9.2f}% │ {c_ann:+11.2f}%')
    print(f'{"最大回檔":12} │ {n_dd:+9.2f}% │ {c_dd:+11.2f}%')
    print(f'{"Calmar":12} │ {n_cal:+9.2f}  │ {c_cal:+11.2f}')
    print('━' * 70)
    diff = c_ret - n_ret
    if diff > 1:
        print(f'💡 Calendar 多賺 {diff:+.2f}%（{variant} 在這段期間有效）')
    elif diff < -1:
        print(f'💡 Calendar 少賺 {diff:+.2f}%（{variant} 在這段期間拖累）')
    else:
        print(f'💡 Calendar vs 裸長 {diff:+.2f}%（影響微小）')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', help='Path to TX CSV')
    ap.add_argument('--shioaji', action='store_true')
    ap.add_argument('--days', type=int, default=252)
    ap.add_argument('--lots', type=int, default=5)
    ap.add_argument('--near-dte', type=int, default=7)
    ap.add_argument('--far-dte',  type=int, default=30)
    ap.add_argument('--variant', choices=['standard', 'reverse'], default='reverse')
    ap.add_argument('--compare-both', action='store_true', help='跑兩種 variant 並排')
    args = ap.parse_args()

    if args.csv:        prices = B.load_csv(args.csv)
    elif args.shioaji:  prices = B.fetch_tx_history_shioaji(days=args.days + 30)
    else:               prices = B.synthetic_prices(days=args.days)

    if not prices:
        print('無資料', file=sys.stderr); return 1

    if args.compare_both:
        for v in ['standard', 'reverse']:
            print(f'\n>>> {v.upper()} <<<')
            series = run_calendar_backtest(prices, hedge_lots=args.lots,
                                           near_dte=args.near_dte, far_dte=args.far_dte,
                                           variant=v)
            report(series, v)
    else:
        series = run_calendar_backtest(prices, hedge_lots=args.lots,
                                        near_dte=args.near_dte, far_dte=args.far_dte,
                                        variant=args.variant)
        report(series, args.variant)


if __name__ == '__main__':
    sys.exit(main() or 0)
