"""底層資產 + 月度選擇權 collar 結合回測。

模擬：
- 每 21 個交易日進入一次新的月度結構
- 4-leg collar：賣 call (+3.6%) + 買 put (-1.9%) + 賣 put (-7.8%) + 買 put (-13.7%)
- IV 用過去 20 日 log return 算的 HV
- 每月底結算（intrinsic at expiry）

比較變體：
1. 純 underlying (B&H)
2. 純 underlying + 月度 4-leg collar
3. trim&add only（現行 SOP）
4. trim&add + 月度 4-leg collar
5. trim&add + 月度純 buy put（保護傘）
6. trim&add + 月度純 sell call（covered call）
"""
from __future__ import annotations
import argparse
import math
import sys
from datetime import date
from typing import Callable, List, Tuple


def fetch_yahoo_ohlc(ticker: str, days: int):
    import yfinance as yf
    if days <= 366:    period = '1y'
    elif days <= 732:  period = '2y'
    elif days <= 1830: period = '5y'
    else:              period = 'max'
    df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    rows = []
    for idx, row in df.iterrows():
        c, h, l, o = row.get('Close'), row.get('High'), row.get('Low'), row.get('Open')
        if any(v is None or v != v for v in (c, h, l, o)):
            continue
        rows.append((idx.date(), float(o), float(h), float(l), float(c)))
    rows.sort(key=lambda x: x[0])
    return ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
            [r[3] for r in rows], [r[4] for r in rows])


def bs_call_put(S, K, sigma, T, r=0.0):
    """Black-Scholes European call/put pricing。"""
    if sigma <= 0 or T <= 0 or S <= 0 or K <= 0:
        return max(0, S - K), max(0, K - S)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + sigma ** 2 / 2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    call = S * N(d1) - K * math.exp(-r * T) * N(d2)
    put  = K * math.exp(-r * T) * N(-d2) - S * N(-d1)
    return call, put


def calc_hv(closes_window, periods_per_year=252):
    """Annualized HV from log returns。"""
    if len(closes_window) < 5:
        return 0.20
    rets = [math.log(closes_window[i] / closes_window[i-1])
            for i in range(1, len(closes_window))
            if closes_window[i-1] > 0]
    if not rets:
        return 0.20
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / len(rets)
    return math.sqrt(var * periods_per_year)


def calc_ma(closes, period):
    out = []
    for i in range(len(closes)):
        if i < period - 1:
            out.append(None); continue
        out.append(sum(closes[i - period + 1:i + 1]) / period)
    return out


def collar_cycle_return(closes_slice, sell_call_pct, buy_put_pct,
                        sell_put_pct=None, sell_put_size=0,
                        buy_disaster_put_pct=None, buy_disaster_size=0,
                        cycle_len=21):
    """單一週期的 collar P&L（換算成相對 underlying 的 return）。
    `closes_slice`：21+ 天 closes，[0] 是入場日、[-1] 是結算日
    回傳 (collar_return, hv_used)"""
    if len(closes_slice) < 2:
        return 0.0, 0.20
    S0, S1 = closes_slice[0], closes_slice[-1]
    hv = calc_hv(closes_slice[:cycle_len + 1])
    T = cycle_len / 252
    total = 0.0

    # 賣 call
    if sell_call_pct is not None and sell_call_pct > 0:
        K = S0 * (1 + sell_call_pct)
        prem, _ = bs_call_put(S0, K, hv, T)
        payoff = -max(0, S1 - K)
        total += (prem + payoff) / S0

    # 買 put (主保護)
    if buy_put_pct is not None and buy_put_pct > 0:
        K = S0 * (1 - buy_put_pct)
        _, prem = bs_call_put(S0, K, hv, T)
        payoff = max(0, K - S1)
        total += (-prem + payoff) / S0

    # 賣 put (短 put 收 premium)
    if sell_put_pct is not None and sell_put_size > 0:
        K = S0 * (1 - sell_put_pct)
        _, prem = bs_call_put(S0, K, hv, T)
        payoff = -max(0, K - S1)
        total += sell_put_size * (prem + payoff) / S0

    # 買災難 put
    if buy_disaster_put_pct is not None and buy_disaster_size > 0:
        K = S0 * (1 - buy_disaster_put_pct)
        _, prem = bs_call_put(S0, K, hv, T)
        payoff = max(0, K - S1)
        total += buy_disaster_size * (-prem + payoff) / S0

    return total, hv


def run_collar_only(closes, dates, sell_call_pct=0.036, buy_put_pct=0.019,
                    sell_put_pct=0.078, sell_put_size=2.0,
                    buy_disaster_pct=0.137, buy_disaster_size=1.67):
    """純 buy-hold underlying + 月度 4-leg collar。
       size 比例對應 1000 萬部位的 5C/3P/6P/5P → call:put_buy:put_sell:disaster = 1.67:1:2:1.67"""
    n = len(closes)
    cycle_len = 21
    eq = [1.0]
    cur = 1.0
    i = 0
    while i < n - 1:
        next_i = min(i + cycle_len, n - 1)
        seg = closes[i:next_i + 1]
        # Underlying return
        S0, S1 = seg[0], seg[-1]
        u_ret = (S1 - S0) / S0 if S0 else 0

        # Collar return (sizes need to be scaled: relative to 1 unit underlying)
        # 我們用 1 unit underlying，collar 用上面 size 比例
        collar_ret, _ = collar_cycle_return(
            seg,
            sell_call_pct=sell_call_pct * 1,         # collar 對 underlying 比例 1:1
            buy_put_pct=buy_put_pct * 1,
            sell_put_pct=sell_put_pct, sell_put_size=sell_put_size,
            buy_disaster_put_pct=buy_disaster_pct, buy_disaster_size=buy_disaster_size,
            cycle_len=cycle_len,
        )

        cycle_total = u_ret + collar_ret
        # 線性內插每日 equity
        for j in range(i + 1, next_i + 1):
            frac = (j - i) / (next_i - i)
            eq.append(cur * (1 + cycle_total * frac))
        cur = cur * (1 + cycle_total)
        i = next_i
    return eq, {'cycles': (n - 1) // cycle_len}


def run_trimadd_plus_collar(closes, dates,
                             trim_pct, trim_size,
                             add_signal_fn,
                             sell_call_pct=0.036, buy_put_pct=0.019,
                             sell_put_pct=0.078, sell_put_size=2.0,
                             buy_disaster_pct=0.137, buy_disaster_size=1.67):
    """trim&add + 月度 4-leg collar"""
    n = len(closes)
    cycle_len = 21
    units = 1.0; cash = 0.0
    recent_high = closes[0]
    eq = [1.0]; base = closes[0]
    cycle_start = 0
    n_trims = 0; n_adds = 0

    for i in range(1, n):
        # 1. Trim 檢查（fully deployed only）
        if cash < 1e-6:
            recent_high = max(recent_high, closes[i])
            dd = closes[i] / recent_high - 1
            if dd <= -trim_pct:
                trim_units = units * trim_size
                cash += trim_units * closes[i]
                units -= trim_units
                n_trims += 1
        # 2. Add-back
        if cash > 0 and add_signal_fn(i):
            units += cash / closes[i]
            cash = 0
            recent_high = closes[i]
            n_adds += 1
        # 3. Collar 結算（每 cycle_len 天）— 只在 fully deployed 時 collar 有效
        if i - cycle_start >= cycle_len:
            if cash < 1e-6:   # 滿倉時才跑 collar
                seg = closes[cycle_start:i + 1]
                collar_ret, _ = collar_cycle_return(
                    seg,
                    sell_call_pct=sell_call_pct,
                    buy_put_pct=buy_put_pct,
                    sell_put_pct=sell_put_pct, sell_put_size=sell_put_size,
                    buy_disaster_put_pct=buy_disaster_pct, buy_disaster_size=buy_disaster_size,
                    cycle_len=cycle_len,
                )
                # Collar return 加到 units（等同 equity 提升）
                units *= (1 + collar_ret)
            cycle_start = i
        eq.append((units * closes[i] + cash) / base)
    return eq, {'n_trims': n_trims, 'n_adds': n_adds}


def stats(eq):
    if len(eq) < 2: return {}
    total = eq[-1] / eq[0] - 1
    n = len(eq)
    annual = (eq[-1] / eq[0]) ** (252 / max(n - 1, 1)) - 1
    peak = eq[0]; mdd = 0.0
    for v in eq:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    rets = [eq[i]/eq[i-1] - 1 for i in range(1, n) if eq[i-1] > 0]
    if rets:
        mr = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mr) ** 2 for r in rets) / len(rets))
        sharpe = (mr * 252) / (sd * math.sqrt(252)) if sd else 0
    else:
        sharpe = 0
    return {'total': total*100, 'annual': annual*100, 'mdd': mdd*100,
            'sharpe': sharpe, 'vol': (sum((r - sum(rets)/len(rets))**2 for r in rets)/len(rets))**0.5 * math.sqrt(252) * 100 if rets else 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ticker', default='0050.TW')
    ap.add_argument('--days', type=int, default=1825)
    args = ap.parse_args()

    print(f'[option-combo] Yahoo {args.ticker} {args.days} 天...', file=sys.stderr)
    dates, opens, highs, lows, closes = fetch_yahoo_ohlc(args.ticker, args.days)
    print(f'[option-combo] 共 {len(dates)} 天: {dates[0]} → {dates[-1]}', file=sys.stderr)

    ma20 = calc_ma(closes, 20)
    def ma20_cross_up(i):
        if i < 1 or ma20[i] is None or ma20[i-1] is None: return False
        return closes[i] > ma20[i] and closes[i-1] <= ma20[i-1]

    results = []

    # 1. 純 underlying buy-hold
    bh_eq = [c / closes[0] for c in closes]
    results.append(('Buy & Hold', bh_eq, {}))

    # 2. 純 underlying + 4-leg collar
    eq, meta = run_collar_only(closes, dates)
    results.append(('B&H + 月度 4-leg collar', eq, meta))

    # 3. 純 underlying + only buy put (保護傘)
    eq, meta = run_collar_only(closes, dates,
                                sell_call_pct=0, buy_put_pct=0.019,
                                sell_put_pct=None, sell_put_size=0,
                                buy_disaster_pct=None, buy_disaster_size=0)
    results.append(('B&H + 月度 long put 保護', eq, meta))

    # 4. 純 underlying + only sell call (covered call)
    eq, meta = run_collar_only(closes, dates,
                                sell_call_pct=0.036, buy_put_pct=0,
                                sell_put_pct=None, sell_put_size=0,
                                buy_disaster_pct=None, buy_disaster_size=0)
    results.append(('B&H + 月度 covered call', eq, meta))

    # 5. trim&add only (現行 SOP - 用 -10% 全出 / MA20 加回作代表)
    from backtest_0050_trim_add import run_trim_add as _ta
    eq, meta = _ta(closes, dates, [(0.10, 1.00)], ma20_cross_up)
    results.append(('Trim -10%×100% / +MA20 (no options)', eq, meta))

    # 6. trim&add + 4-leg collar
    eq, meta = run_trimadd_plus_collar(closes, dates,
                                        trim_pct=0.10, trim_size=1.00,
                                        add_signal_fn=ma20_cross_up)
    results.append(('Trim -10%×100% + 月度 collar', eq, meta))

    # 7. trim&add + only sell call
    eq, meta = run_trimadd_plus_collar(closes, dates,
                                        trim_pct=0.10, trim_size=1.00,
                                        add_signal_fn=ma20_cross_up,
                                        sell_call_pct=0.036, buy_put_pct=0,
                                        sell_put_pct=None, sell_put_size=0,
                                        buy_disaster_pct=None, buy_disaster_size=0)
    results.append(('Trim -10%×100% + 月度 covered call', eq, meta))

    # 8. trim&add + only buy put
    eq, meta = run_trimadd_plus_collar(closes, dates,
                                        trim_pct=0.10, trim_size=1.00,
                                        add_signal_fn=ma20_cross_up,
                                        sell_call_pct=0, buy_put_pct=0.019,
                                        sell_put_pct=None, sell_put_size=0,
                                        buy_disaster_pct=None, buy_disaster_size=0)
    results.append(('Trim -10%×100% + 月度 long put', eq, meta))

    print()
    print('━' * 110)
    print(f'{args.ticker} underlying + 選擇權 結合回測（{dates[0]} → {dates[-1]}, '
          f'{closes[0]:.2f} → {closes[-1]:.2f}, {(closes[-1]/closes[0]-1)*100:+.2f}%）')
    print('━' * 110)
    print(f'  {"策略":<40} │ {"總報酬":>9} {"年化":>9} {"MaxDD":>8} {"Sharpe":>7} {"年化波動":>9}')
    print('─' * 110)
    rows = []
    for name, eq, meta in results:
        s = stats(eq)
        rows.append((name, s))
        print(f'  {name:<40} │ {s["total"]:>+7.2f}% {s["annual"]:>+7.2f}% '
              f'{s["mdd"]:>+7.2f}% {s["sharpe"]:>+7.2f} {s["vol"]:>+7.2f}%')
    print('━' * 110)

    rows.sort(key=lambda r: r[1]['sharpe'], reverse=True)
    print(f'\n📊 依 Sharpe 排名：')
    for i, (n, s) in enumerate(rows, 1):
        bar = '🏆' if i == 1 else ('⭐' if i == 2 else '  ')
        print(f'  {i}. {bar} {n:<40} Sharpe={s["sharpe"]:+.2f}, 報酬 {s["total"]:+.1f}%, DD {s["mdd"]:+.1f}%')


if __name__ == '__main__':
    main()
