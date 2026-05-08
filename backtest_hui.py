"""
backtest_hui.py — 輝哥比例式價差單回測

每月 roll：
  - 買 1×N BP at delta -0.10 (long put hedge)
  - 賣 2×N SP at strike = BP - sp_offset (short put credit)
  - 可選：賣 1×N SC at delta +0.10 (covered call)

對比：naked / put_only (1×BP) / 輝哥 ratio / 輝哥 full

CLI：
  python3 backtest_hui.py                      # 合成 252 天
  python3 backtest_hui.py --shioaji --days 365 # 真實 1 年
  python3 backtest_hui.py --sp-offset 800      # 改 SP 距離
"""
import argparse
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Tuple, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as B   # noqa: E402

TXO_MULTIPLIER = 50


@dataclass
class HuiResult:
    dates: List[date] = field(default_factory=list)
    tx:    List[float] = field(default_factory=list)
    equity_naked:      List[float] = field(default_factory=list)
    equity_put_only:   List[float] = field(default_factory=list)
    equity_hui_ratio:  List[float] = field(default_factory=list)   # 1×BP + 2×SP
    equity_hui_full:   List[float] = field(default_factory=list)   # 上述 + 1×SC
    rolls: List[dict] = field(default_factory=list)


def _short_put_buyback(S, K, T, sigma):
    """Short put buyback cost = current BS put price × qty × multiplier。回傳點數。"""
    if T <= 0:
        return max(0, K - S)
    return B.bs_price(S, K, T, sigma, is_put=True, r=0.0)


def run_hui_backtest(prices, hedge_lots=5, notional_qty=5,
                     dte_target=30, delta_target=0.10, sp_offset=500):
    """執行 4 策略並行回測。"""
    if not prices:
        raise ValueError('empty prices')

    pxs = [p[1] for p in prices]
    hv_arr = B._rolling_hv(pxs)

    res = HuiResult()
    cur_put       = None   # 1× long put（put_only / ratio / full 都有）
    cur_sp_2x     = None   # 2× short put（ratio / full）
    cur_call      = None   # 1× short call（full）

    put_paid     = 0.0     # put_only & ratio 累積（買 put 支出 - 平倉收回 - 結算 payoff）
    sp_cash      = 0.0     # ratio 用：累積收 sp credit - 買回 - 結算 assigned
    call_cash    = 0.0     # full 用：call credit - 買回 - assigned
    last_roll_month = None

    for i, (dt, S) in enumerate(prices):
        hv = max(0.10, hv_arr[i])

        # === 到期處理 ===
        if cur_put and dt >= cur_put.expiry:
            payoff = max(0, cur_put.strike - S) * cur_put.qty * TXO_MULTIPLIER
            put_paid -= payoff
            res.rolls.append({'date': dt.isoformat(), 'action': 'long_put_expire',
                              'strike': cur_put.strike, 'payoff': round(payoff)})
            cur_put = None
        if cur_sp_2x and dt >= cur_sp_2x.expiry:
            assigned = max(0, cur_sp_2x.strike - S) * cur_sp_2x.qty * TXO_MULTIPLIER
            sp_cash -= assigned
            res.rolls.append({'date': dt.isoformat(), 'action': 'short_put_expire_assigned',
                              'strike': cur_sp_2x.strike, 'assigned': round(assigned)})
            cur_sp_2x = None
        if cur_call and dt >= cur_call.expiry:
            assigned = max(0, S - cur_call.strike) * cur_call.qty * TXO_MULTIPLIER
            call_cash -= assigned
            res.rolls.append({'date': dt.isoformat(), 'action': 'short_call_expire_assigned',
                              'strike': cur_call.strike, 'assigned': round(assigned)})
            cur_call = None

        # === 月初 roll ===
        if dt.month != (last_roll_month or 0):
            # close existing
            if cur_put:
                T_left = max(1, (cur_put.expiry - dt).days)
                v = cur_put.value_at(S, T_left, hv)
                put_paid -= v
                cur_put = None
            if cur_sp_2x:
                T_left = max(1, (cur_sp_2x.expiry - dt).days)
                buyback = _short_put_buyback(S, cur_sp_2x.strike, T_left/365, hv) * cur_sp_2x.qty * TXO_MULTIPLIER
                sp_cash -= buyback
                cur_sp_2x = None
            if cur_call:
                T_left = max(1, (cur_call.expiry - dt).days)
                buyback = cur_call.buyback_value(S, T_left, hv)
                call_cash -= buyback
                cur_call = None

            # open new
            expiry = dt + timedelta(days=dte_target)
            T = dte_target / 365
            bp_K = B._find_put_strike_at_delta(S, T, hv, target_delta=delta_target)
            bp_prem = B.bs_price(S, bp_K, T, hv, is_put=True, r=0.0)
            put_paid += bp_prem * hedge_lots * TXO_MULTIPLIER
            cur_put = B.PutPosition(entry_date=dt, expiry=expiry, strike=bp_K,
                                     qty=hedge_lots, entry_premium=bp_prem, iv_at_entry=hv)
            res.rolls.append({'date': dt.isoformat(), 'action': 'long_put_open',
                              'strike': bp_K, 'premium': round(bp_prem, 1)})

            sp_K = bp_K - sp_offset
            sp_prem = B.bs_price(S, sp_K, T, hv, is_put=True, r=0.0)
            sp_cash += sp_prem * (hedge_lots * 2) * TXO_MULTIPLIER
            cur_sp_2x = B.PutPosition(entry_date=dt, expiry=expiry, strike=sp_K,
                                       qty=hedge_lots * 2, entry_premium=sp_prem, iv_at_entry=hv)
            res.rolls.append({'date': dt.isoformat(), 'action': 'short_put_2x_open',
                              'strike': sp_K, 'premium': round(sp_prem, 1)})

            sc_K = B._find_call_strike_at_delta(S, T, hv, target_delta=delta_target)
            sc_prem = B.bs_price(S, sc_K, T, hv, is_put=False, r=0.0)
            call_cash += sc_prem * hedge_lots * TXO_MULTIPLIER
            cur_call = B.CallPosition(entry_date=dt, expiry=expiry, strike=sc_K,
                                       qty=hedge_lots, entry_premium=sc_prem, iv_at_entry=hv)
            res.rolls.append({'date': dt.isoformat(), 'action': 'short_call_open',
                              'strike': sc_K, 'premium': round(sc_prem, 1)})

            last_roll_month = dt.month

        # === Mark-to-market ===
        long_v = S * notional_qty * TXO_MULTIPLIER
        bp_v = 0.0
        if cur_put:
            T_left = max(0, (cur_put.expiry - dt).days)
            bp_v = cur_put.value_at(S, T_left, hv)

        sp_liab = 0.0
        if cur_sp_2x:
            T_left = max(0, (cur_sp_2x.expiry - dt).days)
            sp_liab = _short_put_buyback(S, cur_sp_2x.strike, T_left/365, hv) * cur_sp_2x.qty * TXO_MULTIPLIER

        sc_liab = 0.0
        if cur_call:
            T_left = max(0, (cur_call.expiry - dt).days)
            sc_liab = cur_call.buyback_value(S, T_left, hv)

        equity_naked     = long_v
        equity_put_only  = long_v + bp_v - put_paid
        equity_hui_ratio = long_v + bp_v + sp_cash - sp_liab - put_paid
        equity_hui_full  = equity_hui_ratio + call_cash - sc_liab

        res.dates.append(dt)
        res.tx.append(S)
        res.equity_naked.append(equity_naked)
        res.equity_put_only.append(equity_put_only)
        res.equity_hui_ratio.append(equity_hui_ratio)
        res.equity_hui_full.append(equity_hui_full)

    return res


def report(res):
    if not res.equity_naked:
        return

    base = res.equity_naked[0]
    n = len(res.dates)
    yrs = n / 252

    def _stats(eq):
        ret = (eq[-1] - base) / base * 100
        ann = ((1 + ret/100) ** (1 / max(yrs, 0.01)) - 1) * 100
        peak = eq[0]
        mdd = 0.0
        for v in eq:
            peak = max(peak, v)
            if peak > 0:
                mdd = min(mdd, (v - peak) / peak * 100)
        calmar = ann / abs(mdd) if mdd < 0 else 0
        return ret, ann, mdd, calmar

    n_ret, n_ann, n_dd, n_cal = _stats(res.equity_naked)
    p_ret, p_ann, p_dd, p_cal = _stats(res.equity_put_only)
    r_ret, r_ann, r_dd, r_cal = _stats(res.equity_hui_ratio)
    f_ret, f_ann, f_dd, f_cal = _stats(res.equity_hui_full)

    print()
    print('━' * 80)
    print(f'回測期間：{res.dates[0]} → {res.dates[-1]}  ({n} 天)')
    print(f'TX: {res.tx[0]:.0f} → {res.tx[-1]:.0f}  ({(res.tx[-1]/res.tx[0]-1)*100:+.2f}%)')
    print('━' * 80)
    print(f'{"":12} │ {"裸長":>10} │ {"put-only":>10} │ {"輝哥 ratio":>10} │ {"輝哥 full":>10}')
    print(f'{"總報酬":12} │ {n_ret:+9.2f}% │ {p_ret:+9.2f}% │ {r_ret:+9.2f}% │ {f_ret:+9.2f}%')
    print(f'{"年化":12} │ {n_ann:+9.2f}% │ {p_ann:+9.2f}% │ {r_ann:+9.2f}% │ {f_ann:+9.2f}%')
    print(f'{"最大回檔":12} │ {n_dd:+9.2f}% │ {p_dd:+9.2f}% │ {r_dd:+9.2f}% │ {f_dd:+9.2f}%')
    print(f'{"Calmar":12} │ {n_cal:+9.2f}  │ {p_cal:+9.2f}  │ {r_cal:+9.2f}  │ {f_cal:+9.2f}')
    print('━' * 80)
    print()
    print('💡 解讀：')
    if r_ret > p_ret:
        print(f'   輝哥 ratio 比 put-only 多 {r_ret - p_ret:+.2f}%（賣 SP credit 補 BP 成本有效）')
    else:
        print(f'   輝哥 ratio 落後 put-only {r_ret - p_ret:+.2f}%（極端跌幅 ×2 短 SP 賠錢）')
    if f_ret > r_ret + 1:
        print(f'   輝哥 full 比 ratio 多 {f_ret - r_ret:+.2f}%（盤整 / 微跌時 covered call 加分）')
    elif f_ret < r_ret - 1:
        print(f'   輝哥 full 落後 ratio {f_ret - r_ret:+.2f}%（牛市 short call 被軋）')
    if r_dd > p_dd + 2:
        print(f'   輝哥 ratio drawdown 多 {abs(r_dd - p_dd):.2f}% — 短 SP 跌破時加倍虧')
    print('━' * 80)


def sweep_offsets(prices, hedge_lots, offsets):
    """跑多個 sp_offset 對比。"""
    rows = []
    for o in offsets:
        res = run_hui_backtest(prices, hedge_lots=hedge_lots, sp_offset=o)
        if not res.equity_naked:
            continue
        base = res.equity_naked[0]
        po_ret = (res.equity_put_only[-1] - base) / base * 100
        rt_ret = (res.equity_hui_ratio[-1] - base) / base * 100
        ft_ret = (res.equity_hui_full[-1]  - base) / base * 100
        # max DD
        def _mdd(eq):
            peak, m = eq[0], 0.0
            for v in eq:
                peak = max(peak, v)
                if peak > 0: m = min(m, (v - peak) / peak * 100)
            return m
        rt_dd = _mdd(res.equity_hui_ratio)
        ft_dd = _mdd(res.equity_hui_full)
        yrs = len(res.dates) / 252
        rt_ann = ((1 + rt_ret/100) ** (1/max(yrs, 0.01)) - 1) * 100
        ft_ann = ((1 + ft_ret/100) ** (1/max(yrs, 0.01)) - 1) * 100
        rows.append({
            'sp_offset':       o,
            'put_only_ret':    round(po_ret, 2),
            'ratio_ret':       round(rt_ret, 2),
            'ratio_vs_po':     round(rt_ret - po_ret, 2),
            'ratio_dd':        round(rt_dd, 2),
            'ratio_calmar':    round(rt_ann / abs(rt_dd), 2) if rt_dd < 0 else 0,
            'full_ret':        round(ft_ret, 2),
            'full_dd':         round(ft_dd, 2),
            'full_calmar':     round(ft_ann / abs(ft_dd), 2) if ft_dd < 0 else 0,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', help='Path to TX CSV')
    ap.add_argument('--shioaji', action='store_true')
    ap.add_argument('--days', type=int, default=252)
    ap.add_argument('--lots', type=int, default=5)
    ap.add_argument('--sp-offset', type=int, default=500, help='SP 履約距 BP 下方點數')
    ap.add_argument('--sweep', action='store_true', help='Sweep 多個 sp_offset')
    ap.add_argument('--save', help='Save equity to CSV')
    args = ap.parse_args()

    if args.csv:        prices = B.load_csv(args.csv)
    elif args.shioaji:  prices = B.fetch_tx_history_shioaji(days=args.days + 30)
    else:               prices = B.synthetic_prices(days=args.days)

    if args.sweep:
        offsets = [200, 300, 400, 500, 700, 1000, 1500, 2000]
        print(f'[backtest_hui] sweep {len(offsets)} 個 sp_offset · {len(prices)} 天', file=sys.stderr)
        rows = sweep_offsets(prices, args.lots, offsets)
        print()
        print('━' * 90)
        print(f'sp_offset sweep ({len(prices)} 天 · TX {prices[0][1]:.0f} → {prices[-1][1]:.0f})')
        print('━' * 90)
        print(f'{"offset":>7} │ {"put-only":>8} │ {"ratio":>8} {"vs po":>8} │ {"DD":>8} {"Calmar":>8} │ {"full":>8} {"DD":>8} {"Calmar":>8}')
        print('─' * 90)
        for r in rows:
            print(f'{r["sp_offset"]:>7} │ {r["put_only_ret"]:>+7.2f}% │ '
                  f'{r["ratio_ret"]:>+7.2f}% {r["ratio_vs_po"]:>+7.2f}% │ '
                  f'{r["ratio_dd"]:>+7.2f}% {r["ratio_calmar"]:>+8.2f} │ '
                  f'{r["full_ret"]:>+7.2f}% {r["full_dd"]:>+7.2f}% {r["full_calmar"]:>+8.2f}')
        print('━' * 90)
        # 最佳 offset by Calmar
        best_r = max(rows, key=lambda r: r['ratio_calmar'])
        best_f = max(rows, key=lambda r: r['full_calmar'])
        print()
        print(f'🏆 ratio Calmar 最高：sp_offset={best_r["sp_offset"]} '
              f'(報酬 {best_r["ratio_ret"]:+.2f}%, DD {best_r["ratio_dd"]:+.2f}%, Calmar {best_r["ratio_calmar"]})')
        print(f'🏆 full  Calmar 最高：sp_offset={best_f["sp_offset"]} '
              f'(報酬 {best_f["full_ret"]:+.2f}%, DD {best_f["full_dd"]:+.2f}%, Calmar {best_f["full_calmar"]})')
        print()
        print('💡 觀察：offset 越大 → SP 越深 OTM → 收 credit 變少但 DD 風險變遠')
        return

    print(f'[backtest_hui] {len(prices)} 天 · sp_offset={args.sp_offset}', file=sys.stderr)
    res = run_hui_backtest(prices, hedge_lots=args.lots, sp_offset=args.sp_offset)
    report(res)

    if args.save:
        import csv
        with open(args.save, 'w') as f:
            w = csv.writer(f)
            w.writerow(['date', 'tx', 'equity_naked', 'equity_put_only',
                         'equity_hui_ratio', 'equity_hui_full'])
            for i, d in enumerate(res.dates):
                w.writerow([d.isoformat(), round(res.tx[i], 1),
                             round(res.equity_naked[i]),
                             round(res.equity_put_only[i]),
                             round(res.equity_hui_ratio[i]),
                             round(res.equity_hui_full[i])])
        print(f'[backtest_hui] saved → {args.save}', file=sys.stderr)


if __name__ == '__main__':
    main()
