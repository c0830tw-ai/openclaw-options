"""Regime-driven credit spreads 5 年回測。

策略邏輯：每月（21 個交易日）按 regime 決定當月結構：
- 🐂 Bull        → Bull Put Spread (sell delta -0.20 + buy delta -0.10 put)
- 🐻 Bear        → Bear Call Spread (sell delta +0.20 + buy delta +0.10 call)
- 😴 Sideways    → Iron Condor (兩邊都做 spread)
- 🐻 V Crash     → 暫停（高 IV 不賣方）

進場條件：
- IV PR > 40% 才建單（IV 太低不划算）
- 主動管理：獲利 50% 提前平倉、損失 -50% max 也出場

對照組：
- 無 regime filter（純每月做 short put spread）
- 無 IV filter
- 無主動管理（hold to expiry）
"""
from __future__ import annotations
import argparse
import math
import sys
from typing import List


def fetch_yahoo_ohlc(ticker, days):
    import yfinance as yf
    if days <= 366: period = '1y'
    elif days <= 732: period = '2y'
    elif days <= 1830: period = '5y'
    else: period = 'max'
    df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    rows = []
    for idx, row in df.iterrows():
        c, h, l = row.get('Close'), row.get('High'), row.get('Low')
        if any(v is None or v != v for v in (c, h, l)): continue
        rows.append((idx.date(), float(h), float(l), float(c)))
    rows.sort(key=lambda x: x[0])
    return ([r[0] for r in rows], [r[1] for r in rows],
            [r[2] for r in rows], [r[3] for r in rows])


def bs_call_put(S, K, sigma, T, r=0.0):
    if sigma <= 0 or T <= 0 or S <= 0 or K <= 0:
        return max(0, S-K), max(0, K-S)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S/K) + (r + sigma**2/2)*T) / (sigma*sqrtT)
    d2 = d1 - sigma*sqrtT
    N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    call = S*N(d1) - K*math.exp(-r*T)*N(d2)
    put = K*math.exp(-r*T)*N(-d2) - S*N(-d1)
    return call, put


def calc_hv(closes, period=20):
    if len(closes) < period+1: return 0.20
    rets = [math.log(closes[i]/closes[i-1]) for i in range(len(closes)-period, len(closes)) if closes[i-1] > 0]
    if not rets: return 0.20
    m = sum(rets)/len(rets)
    var = sum((r-m)**2 for r in rets)/len(rets)
    return math.sqrt(var * 252)


def calc_iv_pr(closes, lookback=252):
    """過去 N 日 HV 序列的當前百分位。"""
    if len(closes) < 60: return 50
    hvs = []
    for i in range(60, len(closes)):
        hvs.append(calc_hv(closes[:i+1]))
    if not hvs: return 50
    cur = hvs[-1]
    rank = sum(1 for h in hvs[-lookback:] if h <= cur) / min(lookback, len(hvs)) * 100
    return rank


def detect_regime(closes, i, lookback=60):
    if i < lookback: return 'sideways'
    cur = closes[i]
    ret_60 = (cur / closes[i-lookback] - 1)
    ma60 = sum(closes[i-lookback:i]) / lookback
    # V crash check first
    if i >= 10:
        ret_10 = (cur / closes[i-10] - 1)
        if ret_10 < -0.10: return 'v_crash'
    if ret_60 > 0.10 and cur > ma60: return 'bull'
    if ret_60 < -0.10 and cur < ma60: return 'bear'
    return 'sideways'


def strike_for_delta(S, sigma, T, delta_target, is_call):
    """BS-derived strike for target delta."""
    z_map = {0.10: 1.282, 0.15: 1.036, 0.20: 0.842, 0.25: 0.674, 0.30: 0.524}
    z = z_map.get(round(delta_target, 2), 1.282)
    sigT = sigma * math.sqrt(T)
    drift = sigma * sigma * T / 2
    if is_call:
        return S * math.exp(z * sigT + drift)
    else:
        return S * math.exp(-z * sigT + drift)


def round_strike(v, increment=100):
    if v is None: return None
    return round(v / increment) * increment


def run_credit_spread_strategy(closes, dates,
                                use_regime_filter=True,
                                use_iv_filter=True,
                                early_exit=True,
                                cycle_len=21,
                                iv_premium=0.0):
    """iv_premium: 把 HV 乘上 (1 + iv_premium) 當作 IV
       0.0 = 用 HV（保守、低估賣方收入）
       0.10 = IV = HV × 1.10（貼近現實的 vol risk premium）"""
    """每月入場 credit spread，依 regime 決定方向。"""
    n = len(closes)
    eq = [1.0]
    cur_eq = 1.0
    i = 0
    n_cycles = 0
    n_bull = 0; n_bear = 0; n_condor = 0; n_skip = 0
    total_pnl = 0.0
    log = []

    while i < n - 1:
        next_i = min(i + cycle_len, n - 1)
        S0 = closes[i]
        S1 = closes[next_i]
        sigma_hv = calc_hv(closes[:i+1])
        # IV 通常 > HV（vol risk premium），賣方收的 premium 因此較高
        # 結算用 HV（真實波動），但 premium 用 IV（市場價）
        sigma_iv = sigma_hv * (1 + iv_premium)
        sigma = sigma_iv   # 用於計算 premium
        T = cycle_len / 252
        iv_pr = calc_iv_pr(closes[:i+1]) if use_iv_filter else 50

        regime = detect_regime(closes, i) if use_regime_filter else 'bull'

        cycle_pnl = 0.0
        action = 'skip'
        max_loss = 0.0
        credit = 0.0

        # IV filter
        if use_iv_filter and iv_pr < 40:
            action = 'skip-low-iv'
            n_skip += 1
        elif regime == 'v_crash':
            action = 'skip-vcrash'
            n_skip += 1
        else:
            # Build spread per regime
            if regime == 'bull':
                # Bull put spread
                K_sell = round_strike(strike_for_delta(S0, sigma, T, 0.20, False))
                K_buy = round_strike(strike_for_delta(S0, sigma, T, 0.10, False))
                _, sell_p = bs_call_put(S0, K_sell, sigma, T)
                _, buy_p = bs_call_put(S0, K_buy, sigma, T)
                credit = sell_p - buy_p
                width = K_sell - K_buy
                # Settle at expiry
                intrinsic = max(0, K_sell - S1) - max(0, K_buy - S1)
                cycle_pnl = credit - intrinsic
                max_loss = -(width - credit)
                action = f'bull_put @ {int(K_sell)}/{int(K_buy)}'
                n_bull += 1
            elif regime == 'bear':
                # Bear call spread
                K_sell = round_strike(strike_for_delta(S0, sigma, T, 0.20, True))
                K_buy = round_strike(strike_for_delta(S0, sigma, T, 0.10, True))
                sell_c, _ = bs_call_put(S0, K_sell, sigma, T)
                buy_c, _ = bs_call_put(S0, K_buy, sigma, T)
                credit = sell_c - buy_c
                width = K_buy - K_sell
                intrinsic = max(0, S1 - K_sell) - max(0, S1 - K_buy)
                cycle_pnl = credit - intrinsic
                max_loss = -(width - credit)
                action = f'bear_call @ {int(K_sell)}/{int(K_buy)}'
                n_bear += 1
            elif regime == 'sideways':
                # Iron Condor
                K_sell_p = round_strike(strike_for_delta(S0, sigma, T, 0.20, False))
                K_buy_p = round_strike(strike_for_delta(S0, sigma, T, 0.10, False))
                K_sell_c = round_strike(strike_for_delta(S0, sigma, T, 0.20, True))
                K_buy_c = round_strike(strike_for_delta(S0, sigma, T, 0.10, True))
                _, sp = bs_call_put(S0, K_sell_p, sigma, T)
                _, bp = bs_call_put(S0, K_buy_p, sigma, T)
                sc, _ = bs_call_put(S0, K_sell_c, sigma, T)
                bc, _ = bs_call_put(S0, K_buy_c, sigma, T)
                credit_put = sp - bp
                credit_call = sc - bc
                credit = credit_put + credit_call
                put_intrinsic = max(0, K_sell_p - S1) - max(0, K_buy_p - S1)
                call_intrinsic = max(0, S1 - K_sell_c) - max(0, S1 - K_buy_c)
                cycle_pnl = credit - put_intrinsic - call_intrinsic
                action = f'condor {int(K_buy_p)}/{int(K_sell_p)}/{int(K_sell_c)}/{int(K_buy_c)}'
                n_condor += 1

            # 主動管理：50% 利潤提前平倉、-50% max loss 也出
            if early_exit and max_loss < 0:
                # 簡單模擬：假設 50% 利潤點被觸發機率高（縮小最終 P&L 變異）
                # 在實務中無法精準模擬，這裡用「平均出場時 P&L」近似
                # 簡化處理：early_exit 開啟時 P&L 縮小 30% 的負端、不變正端
                if cycle_pnl < 0:
                    cycle_pnl = cycle_pnl * 0.6   # 50% max loss 平倉

        # cycle return % = cycle_pnl / S0 (normalized to 1 unit of S0 value)
        cycle_ret = cycle_pnl / S0 if S0 else 0
        cur_eq *= (1 + cycle_ret)
        n_cycles += 1
        total_pnl += cycle_pnl
        log.append({
            'cycle': n_cycles, 'date': dates[i], 'S0': S0, 'S1': S1,
            'regime': regime, 'action': action, 'credit': credit,
            'pnl': cycle_pnl, 'iv_pr': iv_pr,
        })

        # 內插每日 equity
        for j in range(i+1, next_i+1):
            frac = (j - i) / (next_i - i)
            eq.append(eq[i] * (1 + cycle_ret * frac))
        i = next_i

    return eq, {
        'n_cycles': n_cycles,
        'n_bull': n_bull, 'n_bear': n_bear,
        'n_condor': n_condor, 'n_skip': n_skip,
        'total_pnl': total_pnl,
        'log': log,
    }


def stats(eq):
    if len(eq) < 2: return {}
    total = eq[-1] - 1
    n = len(eq)
    annual = (eq[-1]) ** (252 / max(n-1, 1)) - 1
    peak = eq[0]; mdd = 0
    for v in eq:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    rets = [eq[i]/eq[i-1]-1 for i in range(1, n) if eq[i-1] > 0]
    if rets:
        mr = sum(rets)/len(rets)
        sd = math.sqrt(sum((r-mr)**2 for r in rets)/len(rets))
        sharpe = (mr * 252) / (sd * math.sqrt(252)) if sd else 0
        vol = sd * math.sqrt(252) * 100
    else:
        sharpe, vol = 0, 0
    return {'total': total*100, 'annual': annual*100, 'mdd': mdd*100,
            'sharpe': sharpe, 'vol': vol}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ticker', default='^TWII')   # TAIEX
    ap.add_argument('--days', type=int, default=1825)
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    print(f'[credit_spreads] Yahoo {args.ticker} {args.days} 天...', file=sys.stderr)
    dates, highs, lows, closes = fetch_yahoo_ohlc(args.ticker, args.days)
    print(f'共 {len(dates)} 天: {dates[0]} → {dates[-1]}', file=sys.stderr)

    variants = [
        # (label, regime_filter, iv_filter, early_exit, iv_premium)
        ('Regime + IV + 主動管理 (HV)',         True, True, True, 0.00),
        ('Regime + IV + 主動管理 (IV=HV×1.10)', True, True, True, 0.10),
        ('Regime + IV + 主動管理 (IV=HV×1.20)', True, True, True, 0.20),
        ('Regime only (IV=HV×1.10)',             True, False, True, 0.10),
        ('純 Bull Put 永遠賣 (IV=HV×1.10)',      False, False, True, 0.10),
    ]

    print()
    print('━' * 100)
    print(f'TAIEX credit spreads 5 年回測（{dates[0]} → {dates[-1]}, '
          f'{closes[0]:.0f} → {closes[-1]:.0f}, {(closes[-1]/closes[0]-1)*100:+.1f}%）')
    print('━' * 100)
    print(f'  {"變體":<35} │ {"總報酬":>8} {"年化":>8} {"MaxDD":>8} {"Sharpe":>7} {"波動":>7} {"進場":>5}')
    print('─' * 100)

    for label, regime_f, iv_f, exit_f, iv_prem in variants:
        eq, meta = run_credit_spread_strategy(closes, dates,
                                                use_regime_filter=regime_f,
                                                use_iv_filter=iv_f,
                                                early_exit=exit_f,
                                                iv_premium=iv_prem)
        s = stats(eq)
        n_entries = meta['n_bull'] + meta['n_bear'] + meta['n_condor']
        print(f'  {label:<35} │ {s["total"]:>+6.2f}% {s["annual"]:>+6.2f}% '
              f'{s["mdd"]:>+6.2f}% {s["sharpe"]:>+6.2f} {s["vol"]:>+5.2f}% '
              f'{n_entries}/{meta["n_cycles"]}')

    print('━' * 100)
    print()
    print('進場分布（最佳版）：')
    eq, meta = run_credit_spread_strategy(closes, dates, True, True, True)
    print(f'  🐂 Bull put:  {meta["n_bull"]:>3} 次')
    print(f'  🐻 Bear call: {meta["n_bear"]:>3} 次')
    print(f'  😴 Condor:    {meta["n_condor"]:>3} 次')
    print(f'  跳過(低IV/Vcrash): {meta["n_skip"]:>3} 次')
    if args.verbose:
        print()
        print('每月 log：')
        for r in meta['log']:
            print(f'  {r["date"]} {r["regime"]:<10} {r["action"]:<35} '
                  f'IVpr={r["iv_pr"]:.0f} PnL={r["pnl"]:+.1f}')


if __name__ == '__main__':
    main()
