"""經典「獲利 X% 啟動 → 回檔 Y% of profit」移動停利回測。

公式：
    stop = high × (1 - trail_pct) + entry × trail_pct
         = high - (high - entry) × trail_pct

舉例 (entry=100, trail_pct=0.30):
- High 110 → stop 107 (賺 10 回檔 3)
- High 120 → stop 114 (賺 20 回檔 6)
- High 130 → stop 121 (賺 30 回檔 9)
"""
from __future__ import annotations
import math
import sys
from datetime import date


def fetch_yahoo_adj(ticker, days):
    import yfinance as yf
    if days <= 366: period = '1y'
    elif days <= 732: period = '2y'
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


def calc_ma(closes, period):
    out = []
    for i in range(len(closes)):
        if i < period - 1:
            out.append(None); continue
        out.append(sum(closes[i - period + 1:i + 1]) / period)
    return out


def run_profit_trail(closes, dates, activation_pct, trail_profit_pct, re_entry='ma20'):
    """
    activation_pct: 達到 +X% 才啟動 trail
    trail_profit_pct: 回檔多少 % of unrealized profit 觸發出場
                     (0.30 = 回檔 30% of profit)
    re_entry: 出場後何時重新進場 - 'ma20' / 'never' / 'next_day'

    回傳 (eq, stats)
    """
    n = len(closes)
    if n < 25: return [1.0]*n, {'n_trades': 0, 'win_rate': 0}
    ma20 = calc_ma(closes, 20)

    holding = True   # 一開始就持有
    entry_price = closes[0]
    high_since_entry = closes[0]
    activated = False
    units = 1.0
    cash = 0.0
    n_trades = 0
    n_wins = 0
    pnls = []

    eq = [1.0]
    base = closes[0]

    for i in range(1, n):
        c = closes[i]
        if holding:
            high_since_entry = max(high_since_entry, c)
            # 啟動 trail？
            if not activated and c >= entry_price * (1 + activation_pct):
                activated = True
            # 計算 stop
            if activated:
                stop = high_since_entry - (high_since_entry - entry_price) * trail_profit_pct
                if c < stop:
                    # 出場
                    cash = units * c
                    pnl = c / entry_price - 1
                    pnls.append(pnl)
                    if pnl > 0: n_wins += 1
                    n_trades += 1
                    holding = False
                    units = 0
                    activated = False
                    high_since_entry = 0
                    entry_price = 0
        else:
            # 等重進場訊號
            if re_entry == 'ma20':
                if ma20[i] is not None and ma20[i-1] is not None:
                    if c > ma20[i] and closes[i-1] <= ma20[i-1]:   # cross-up
                        units = cash / c
                        cash = 0
                        entry_price = c
                        high_since_entry = c
                        holding = True
            elif re_entry == 'next_day':
                units = cash / c
                cash = 0
                entry_price = c
                high_since_entry = c
                holding = True
            # never → 不重進場
        eq.append((units * c + cash) / base)

    win_rate = n_wins / n_trades if n_trades else 0
    avg_pnl = sum(pnls) / len(pnls) if pnls else 0
    return eq, {'n_trades': n_trades, 'win_rate': win_rate, 'avg_pnl_pct': avg_pnl*100}


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


def run_one_segment(ticker, start, end, label):
    dates, closes = fetch_yahoo_adj(ticker, 3650)
    idx_lo = next((i for i, d in enumerate(dates) if d >= start), None)
    idx_hi = None
    for i in range(len(dates)-1, -1, -1):
        if dates[i] <= end:
            idx_hi = i; break
    if idx_lo is None or idx_hi is None or idx_hi - idx_lo < 30:
        return None
    seg_d = dates[idx_lo:idx_hi+1]
    seg_c = closes[idx_lo:idx_hi+1]
    base = seg_c[0]; end_v = seg_c[-1]
    move = (end_v/base - 1)*100

    print()
    print('━' * 100)
    print(f'{label}（{seg_d[0]} → {seg_d[-1]}, {len(seg_d)} 天, B&H {move:+.1f}%）')
    print('━' * 100)
    print(f'  {"變體":<35} │ {"總報酬":>8} {"年化":>8} {"DD":>7} {"Sharpe":>6} {"次數":>4} {"勝率":>5} {"均報":>7}')
    print('─' * 100)

    variants = [
        # (label, activation, trail_pct)
        ('Activation 5%, Trail 30%',  0.05, 0.30),
        ('Activation 7%, Trail 30%',  0.07, 0.30),
        ('Activation 10%, Trail 30%（你的範例）', 0.10, 0.30),
        ('Activation 10%, Trail 20% (緊)',         0.10, 0.20),
        ('Activation 10%, Trail 50% (鬆)',         0.10, 0.50),
        ('Activation 15%, Trail 30%',  0.15, 0.30),
    ]
    for label2, act, trail in variants:
        eq, meta = run_profit_trail(seg_c, seg_d, act, trail, re_entry='ma20')
        s = stats(eq)
        print(f'  {label2:<35} │ {s["total"]:>+6.2f}% {s["annual"]:>+6.2f}% '
              f'{s["mdd"]:>+5.2f}% {s["sharpe"]:>+5.2f} '
              f'{meta["n_trades"]:>4} {meta["win_rate"]*100:>4.0f}% {meta["avg_pnl_pct"]:>+5.2f}%')
    bh_eq = [c/seg_c[0] for c in seg_c]
    bs = stats(bh_eq)
    print(f'  {"Buy & Hold":<35} │ {bs["total"]:>+6.2f}% {bs["annual"]:>+6.2f}% '
          f'{bs["mdd"]:>+5.2f}% {bs["sharpe"]:>+5.2f}      -   -    -')


def main():
    print('「獲利達 X% 啟動 → 回檔 Y% of profit 移動停利」5 年回測')
    print('=' * 100)

    # 全期 5 年
    for ticker, name in [('0050.TW', '0050 元大台灣 50'), ('2330.TW', '2330 台積電')]:
        run_one_segment(ticker, date(2021, 5, 17), date(2026, 5, 15), f'{name} 5 年全期')

    # Regime 段
    print()
    print('=' * 100)
    print('Regime 段分析（用 0050）')
    print('=' * 100)
    for s, e, lbl in [
        (date(2022, 1, 1),  date(2022, 10, 31), '🐻 2022 升息熊'),
        (date(2020, 2, 1),  date(2020, 4, 30),  '🐻 2020 COVID 崩盤'),
        (date(2018, 5, 1),  date(2019, 5, 31),  '😴 2018-2019 盤整'),
        (date(2025, 7, 1),  date(2026, 5, 15),  '🐂 2025-2026 強牛'),
    ]:
        run_one_segment('0050.TW', s, e, f'0050 {lbl}')


if __name__ == '__main__':
    main()
