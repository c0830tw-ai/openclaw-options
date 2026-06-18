"""MACD 訊號獨立回測 — 看是否值得當主訊號。

測試 4 個變體：
1. MACD cross-up 買 / cross-down 賣
2. Histogram cross-up 0 買 / cross-down 0 賣
3. MACD cross + MA60 filter（只在 MA60 上方買、下方賣）
4. MACD + MA20 filter
"""
from __future__ import annotations
import math
import sys
from datetime import date


def fetch_yahoo(ticker, days):
    import yfinance as yf
    if days <= 366: period = '1y'
    elif days <= 1830: period = '5y'
    elif days <= 3660: period = '10y'
    else: period = 'max'
    df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    rows = []
    for idx, row in df.iterrows():
        c = row.get('Close')
        if c is None or c != c: continue
        rows.append((idx.date(), float(c)))
    rows.sort(key=lambda x: x[0])
    return [r[0] for r in rows], [r[1] for r in rows]


def calc_ema(closes, period):
    if not closes: return []
    k = 2 / (period + 1)
    out = [closes[0]]
    for v in closes[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def calc_macd(closes, fast=12, slow=26, signal=9):
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    sig_line = calc_ema(macd_line, signal)
    hist = [m - s for m, s in zip(macd_line, sig_line)]
    return macd_line, sig_line, hist


def calc_ma(closes, period):
    out = []
    for i in range(len(closes)):
        if i < period - 1:
            out.append(None); continue
        out.append(sum(closes[i - period + 1:i + 1]) / period)
    return out


def run_macd_strategy(closes, dates, entry_fn, exit_fn):
    """Long-only：訊號買進、訊號出場。"""
    n = len(closes)
    eq = [1.0]
    units = 0.0
    cash = 1.0
    n_trades = 0
    pnls = []
    entry_price = 0

    for i in range(1, n):
        c = closes[i]
        if units < 1e-9:   # 空倉
            if entry_fn(i, closes):
                units = cash / c
                cash = 0
                entry_price = c
        else:   # 持有
            if exit_fn(i, closes):
                pnl = c / entry_price - 1
                pnls.append(pnl)
                cash = units * c
                units = 0
                n_trades += 1
        eq.append((units * c + cash) / 1.0)

    # 結尾平倉統計
    if units > 0 and entry_price > 0:
        pnls.append(closes[-1] / entry_price - 1)

    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / len(pnls) if pnls else 0
    return eq, {'n_trades': n_trades, 'win_rate': win_rate,
                'avg_pnl': sum(pnls)/len(pnls) if pnls else 0}


def stats(eq):
    if len(eq) < 2: return {}
    total = eq[-1] / eq[0] - 1
    n = len(eq)
    annual = (eq[-1] / eq[0]) ** (252 / max(n-1, 1)) - 1
    peak = eq[0]; mdd = 0
    for v in eq:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    rets = [eq[i]/eq[i-1]-1 for i in range(1, n) if eq[i-1] > 0]
    if rets:
        mr = sum(rets)/len(rets)
        sd = math.sqrt(sum((r-mr)**2 for r in rets)/len(rets))
        sharpe = (mr * 252) / (sd * math.sqrt(252)) if sd else 0
    else: sharpe = 0
    return {'total': total*100, 'annual': annual*100, 'mdd': mdd*100, 'sharpe': sharpe}


def run_on(ticker, label, start, end):
    dates, closes = fetch_yahoo(ticker, 3650)
    idx_lo = next((i for i, d in enumerate(dates) if d >= start), None)
    idx_hi = None
    for i in range(len(dates)-1, -1, -1):
        if dates[i] <= end: idx_hi = i; break
    if idx_lo is None or idx_hi is None or idx_hi - idx_lo < 30: return None
    seg_d = dates[idx_lo:idx_hi+1]
    seg_c = closes[idx_lo:idx_hi+1]
    base = seg_c[0]; end_v = seg_c[-1]
    move = (end_v/base-1)*100

    macd, sig, hist = calc_macd(seg_c)
    ma20 = calc_ma(seg_c, 20)
    ma60 = calc_ma(seg_c, 60)

    print()
    print('━' * 100)
    print(f'{label}（{seg_d[0]} → {seg_d[-1]}, {len(seg_d)} 天, B&H {move:+.1f}%）')
    print('━' * 100)
    print(f'  {"變體":<35} │ {"總報酬":>8} {"年化":>8} {"MaxDD":>7} {"Sharpe":>6} {"次數":>4} {"勝率":>5}')
    print('─' * 100)

    # 1. MACD cross signal line
    def s1_e(i, c): return macd[i] > sig[i] and macd[i-1] <= sig[i-1]
    def s1_x(i, c): return macd[i] < sig[i] and macd[i-1] >= sig[i-1]

    # 2. Histogram cross 0
    def s2_e(i, c): return hist[i] > 0 and hist[i-1] <= 0
    def s2_x(i, c): return hist[i] < 0 and hist[i-1] >= 0

    # 3. MACD cross + MA60 filter (只在 MA60 上方買)
    def s3_e(i, c):
        if ma60[i] is None: return False
        return macd[i] > sig[i] and macd[i-1] <= sig[i-1] and c[i] > ma60[i]
    def s3_x(i, c):
        if ma60[i] is None: return True
        return macd[i] < sig[i] and macd[i-1] >= sig[i-1]

    # 4. MACD cross + MA20 filter
    def s4_e(i, c):
        if ma20[i] is None: return False
        return macd[i] > sig[i] and macd[i-1] <= sig[i-1] and c[i] > ma20[i]
    def s4_x(i, c):
        if ma20[i] is None: return True
        return macd[i] < sig[i] and macd[i-1] >= sig[i-1]

    variants = [
        ('MACD cross signal',           s1_e, s1_x),
        ('MACD hist cross 0',           s2_e, s2_x),
        ('MACD cross + MA60 filter',    s3_e, s3_x),
        ('MACD cross + MA20 filter',    s4_e, s4_x),
    ]
    for name, e, x in variants:
        eq, meta = run_macd_strategy(seg_c, seg_d, e, x)
        s = stats(eq)
        print(f'  {name:<35} │ {s["total"]:>+6.2f}% {s["annual"]:>+6.2f}% '
              f'{s["mdd"]:>+5.2f}% {s["sharpe"]:>+5.2f} '
              f'{meta["n_trades"]:>4} {meta["win_rate"]*100:>4.0f}%')

    bh_eq = [c/seg_c[0] for c in seg_c]
    bs = stats(bh_eq)
    print(f'  {"Buy & Hold":<35} │ {bs["total"]:>+6.2f}% {bs["annual"]:>+6.2f}% '
          f'{bs["mdd"]:>+5.2f}% {bs["sharpe"]:>+5.2f}    -    -')


def main():
    print('MACD 訊號獨立回測（5 年 + Regime 段）')
    print('=' * 100)

    for tk, name in [('0050.TW', '0050'), ('2330.TW', '2330')]:
        run_on(tk, f'{name} 5 年全期', date(2021, 5, 17), date(2026, 5, 15))

    for s, e, lbl in [
        (date(2022, 1, 1),  date(2022, 10, 31), '🐻 2022 升息熊'),
        (date(2020, 2, 1),  date(2020, 4, 30),  '🐻 2020 COVID 崩盤'),
        (date(2018, 5, 1),  date(2019, 5, 31),  '😴 2018-2019 盤整'),
        (date(2025, 7, 1),  date(2026, 5, 15),  '🐂 2025-2026 強牛'),
    ]:
        run_on('0050.TW', f'0050 {lbl}', s, e)


if __name__ == '__main__':
    main()
