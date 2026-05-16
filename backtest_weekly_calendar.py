"""Weekly Iron Condor + Calendar Spread 5 年回測。

2. Weekly Iron Condor：DTE 5 天、靠 theta 收割
3. Calendar Spread：賣近月 + 買遠月、賺 IV 結構與 theta 差

對照組：Monthly Iron Condor（已知基準）+ B&H
"""
from __future__ import annotations
import argparse
import math
import sys


def fetch_yahoo_ohlc(ticker, days):
    import yfinance as yf
    if days <= 366: period = '1y'
    elif days <= 732: period = '2y'
    elif days <= 1830: period = '5y'
    else: period = 'max'
    df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    rows = []
    for idx, row in df.iterrows():
        c = row.get('Close')
        if c is None or c != c: continue
        rows.append((idx.date(), float(c)))
    rows.sort(key=lambda x: x[0])
    return [r[0] for r in rows], [r[1] for r in rows]


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


def strike_for_delta(S, sigma, T, delta_target, is_call):
    z_map = {0.10: 1.282, 0.15: 1.036, 0.20: 0.842, 0.25: 0.674, 0.30: 0.524}
    z = z_map.get(round(delta_target, 2), 1.282)
    sigT = sigma * math.sqrt(T)
    drift = sigma * sigma * T / 2
    if is_call:
        return S * math.exp(z * sigT + drift)
    else:
        return S * math.exp(-z * sigT + drift)


def round_strike(v, inc=100):
    return round(v / inc) * inc


# ─── 策略 2: Weekly Iron Condor ───────────────
def run_weekly_iron_condor(closes, dates, iv_premium=0.10, dte=5,
                            short_delta=0.20, long_delta=0.10,
                            early_exit_factor=0.5):
    """每週進新單 (DTE 5 天)，持有到結算
       early_exit_factor: 若 cycle pnl 為負，乘上此係數模擬「跌一半就跑」
    """
    n = len(closes)
    cycle = dte
    eq = [1.0]
    cur = 1.0
    n_trades = 0
    pnls = []
    i = 0
    while i < n - 1:
        next_i = min(i + cycle, n - 1)
        S0 = closes[i]
        S1 = closes[next_i]
        sigma = calc_hv(closes[:i+1]) * (1 + iv_premium)
        T = cycle / 252

        # 4 個 strike
        K_sp = round_strike(strike_for_delta(S0, sigma, T, short_delta, False))
        K_bp = round_strike(strike_for_delta(S0, sigma, T, long_delta, False))
        K_sc = round_strike(strike_for_delta(S0, sigma, T, short_delta, True))
        K_bc = round_strike(strike_for_delta(S0, sigma, T, long_delta, True))

        _, sp = bs_call_put(S0, K_sp, sigma, T)
        _, bp = bs_call_put(S0, K_bp, sigma, T)
        sc, _ = bs_call_put(S0, K_sc, sigma, T)
        bc, _ = bs_call_put(S0, K_bc, sigma, T)
        credit = (sp - bp) + (sc - bc)

        put_int = max(0, K_sp - S1) - max(0, K_bp - S1)
        call_int = max(0, S1 - K_sc) - max(0, S1 - K_bc)
        pnl = credit - put_int - call_int

        # early exit 模擬
        if pnl < 0:
            pnl *= early_exit_factor

        cycle_ret = pnl / S0
        cur *= (1 + cycle_ret)
        pnls.append(pnl)
        n_trades += 1

        for j in range(i+1, next_i+1):
            frac = (j - i) / (next_i - i)
            eq.append(eq[i] * (1 + cycle_ret * frac))
        i = next_i

    return eq, {'n_trades': n_trades, 'avg_pnl': sum(pnls)/len(pnls) if pnls else 0,
                'win_rate': sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0}


# ─── 策略 3: Calendar Spread (Put Calendar) ───────
def run_calendar_spread(closes, dates, iv_premium=0.10,
                         short_dte=7, long_dte=30,
                         delta_target=0.10):
    """賣近月 put + 買遠月 put，同履約價。
       每 short_dte 進新單，close at short expiry。
       長腳剩 (long_dte - short_dte) 天，重新 BS 估價。"""
    n = len(closes)
    cycle = short_dte
    eq = [1.0]
    cur = 1.0
    n_trades = 0
    pnls = []
    i = 0
    while i < n - 1:
        next_i = min(i + cycle, n - 1)
        S0 = closes[i]
        S1 = closes[next_i]
        sigma_short = calc_hv(closes[:i+1]) * (1 + iv_premium)
        sigma_long  = calc_hv(closes[:i+1]) * (1 + iv_premium)   # 假設 term structure 平
        T_short = short_dte / 252
        T_long  = long_dte / 252

        # 同履約價（10 delta OTM put）
        K = round_strike(strike_for_delta(S0, sigma_short, T_short, delta_target, False))
        _, short_prem = bs_call_put(S0, K, sigma_short, T_short)
        _, long_prem = bs_call_put(S0, K, sigma_long, T_long)
        # 進場：付 long - 收 short = debit
        debit = long_prem - short_prem

        # 結算：short 到期、long 剩 long_dte-short_dte 天
        sigma_remaining = calc_hv(closes[:next_i+1]) * (1 + iv_premium)
        T_remaining = (long_dte - short_dte) / 252
        _, short_settle = bs_call_put(S1, K, sigma_remaining, 0.001)   # intrinsic only
        _, long_remain  = bs_call_put(S1, K, sigma_remaining, T_remaining)

        # short 我們賣的、to close 要買回（付 short_settle 的 intrinsic）
        # long 我們買的、to close 要賣出（收 long_remain）
        pnl = -debit + (long_remain - short_settle)
        # 註：short_prem 是進場時收的、結算時要平掉 short → 付 max(0, K-S1)
        # 簡化版：直接算 net
        # 重算：
        # 進場：付 long_prem (買), 收 short_prem (賣) → net debit = long_prem - short_prem
        # 出場：收 long_remain (賣), 付 short_settle (買回) → net credit = long_remain - short_settle
        # P&L = - debit + net_credit = (long_remain - long_prem) + (short_prem - short_settle)
        pnl = (long_remain - long_prem) + (short_prem - short_settle)

        cycle_ret = pnl / S0
        cur *= (1 + cycle_ret)
        pnls.append(pnl)
        n_trades += 1

        for j in range(i+1, next_i+1):
            frac = (j - i) / (next_i - i)
            eq.append(eq[i] * (1 + cycle_ret * frac))
        i = next_i

    return eq, {'n_trades': n_trades, 'avg_pnl': sum(pnls)/len(pnls) if pnls else 0,
                'win_rate': sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0}


# ─── 對照：Monthly Iron Condor ─────────────
def run_monthly_iron_condor(closes, dates, iv_premium=0.10, dte=21,
                             short_delta=0.20, long_delta=0.10):
    return run_weekly_iron_condor(closes, dates, iv_premium, dte,
                                    short_delta, long_delta, early_exit_factor=0.6)


def stats(eq):
    if len(eq) < 2: return {}
    total = eq[-1] - 1
    n = len(eq)
    annual = eq[-1] ** (252 / max(n-1, 1)) - 1
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
    ap.add_argument('--ticker', default='^TWII')
    ap.add_argument('--days', type=int, default=1825)
    args = ap.parse_args()

    print(f'[weekly+calendar] Yahoo {args.ticker} {args.days} 天...', file=sys.stderr)
    dates, closes = fetch_yahoo_ohlc(args.ticker, args.days)
    print(f'共 {len(dates)} 天: {dates[0]} → {dates[-1]}', file=sys.stderr)

    print()
    print('━' * 110)
    print(f'TAIEX 短週期選擇權策略 5 年回測（{dates[0]} → {dates[-1]}, '
          f'{closes[0]:.0f} → {closes[-1]:.0f}, {(closes[-1]/closes[0]-1)*100:+.1f}%）')
    print('━' * 110)
    print(f'  {"策略":<40} │ {"總報酬":>8} {"年化":>8} {"MaxDD":>8} {"Sharpe":>7} {"勝率":>5} {"交易":>5}')
    print('─' * 110)

    variants = [
        ('Monthly IC (DTE 21, 對照)', run_monthly_iron_condor, {'iv_premium': 0.10, 'dte': 21}),
        ('Weekly IC (DTE 5, IV×1.10)', run_weekly_iron_condor, {'iv_premium': 0.10, 'dte': 5}),
        ('Weekly IC (DTE 5, IV×1.20)', run_weekly_iron_condor, {'iv_premium': 0.20, 'dte': 5}),
        ('Weekly IC (DTE 7, IV×1.10)', run_weekly_iron_condor, {'iv_premium': 0.10, 'dte': 7}),
        ('Put Calendar 7/30 (IV×1.10)', run_calendar_spread, {'iv_premium': 0.10, 'short_dte': 7, 'long_dte': 30}),
        ('Put Calendar 7/30 (IV×1.20)', run_calendar_spread, {'iv_premium': 0.20, 'short_dte': 7, 'long_dte': 30}),
        ('Put Calendar 14/45 (IV×1.10)', run_calendar_spread, {'iv_premium': 0.10, 'short_dte': 14, 'long_dte': 45}),
    ]

    for label, fn, kwargs in variants:
        eq, meta = fn(closes, dates, **kwargs)
        s = stats(eq)
        print(f'  {label:<40} │ {s["total"]:>+6.2f}% {s["annual"]:>+6.2f}% '
              f'{s["mdd"]:>+6.2f}% {s["sharpe"]:>+6.2f} '
              f'{meta["win_rate"]*100:>4.0f}% {meta["n_trades"]:>4}')

    print('━' * 110)


if __name__ == '__main__':
    main()
