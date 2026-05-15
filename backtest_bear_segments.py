"""針對特定熊市段測試 trim&add 策略。

Bear segments:
- 2008 financial crisis (10/2007 - 11/2008)
- 2020 COVID crash (02/2020 - 03/2020)
- 2022 升息熊 (01/2022 - 10/2022)
- 2024 mini bear (07/2024 - 08/2024)
"""
from __future__ import annotations
import sys
import math
from datetime import date
sys.path.insert(0, '/Users/chenchinghung/openclaw/options')

import backtest_0050_trim_add as bt


def run_strats_on_segment(ticker: str, start: date, end: date, label: str):
    dates, opens, highs, lows, closes = bt.fetch_yahoo_ohlc(ticker, 3650)
    # 過濾到目標區間
    idx_lo = next((i for i, d in enumerate(dates) if d >= start), None)
    idx_hi = next((i for i, d in enumerate(reversed(dates))
                   if d <= end), None)
    if idx_lo is None or idx_hi is None:
        print(f'{ticker} 沒有 {start}~{end} 的資料，跳過'); return
    idx_hi = len(dates) - 1 - idx_hi
    if idx_hi - idx_lo < 30:
        print(f'{ticker} {label}: 資料太少 ({idx_hi - idx_lo} 天)，跳過'); return

    seg_d = dates[idx_lo:idx_hi + 1]
    seg_c = closes[idx_lo:idx_hi + 1]
    seg_h = highs[idx_lo:idx_hi + 1]
    seg_l = lows[idx_lo:idx_hi + 1]

    base = seg_c[0]; end_v = seg_c[-1]
    drop = (end_v / base - 1) * 100

    # 找最深 DD
    peak = base; max_dd = 0
    for v in seg_c:
        peak = max(peak, v); max_dd = min(max_dd, v / peak - 1)

    print()
    print('━' * 100)
    print(f'{ticker} {label}（{seg_d[0]} → {seg_d[-1]}, {len(seg_d)} 天）')
    print(f'起始 {base:.2f} → 結束 {end_v:.2f} ({drop:+.2f}%)，期間最深 DD {max_dd*100:+.2f}%')
    print('━' * 100)

    strats = bt.build_strategies(seg_c, seg_d, highs=seg_h, lows=seg_l)
    rows = []
    for name, eq, meta in strats:
        s = bt.stats_for(eq, []) if hasattr(bt, 'stats_for') else None
        if s is None:
            # 自己算
            total = eq[-1] / eq[0] - 1
            n = len(eq)
            peak_e = eq[0]; mdd = 0.0
            for v in eq:
                peak_e = max(peak_e, v); mdd = min(mdd, v / peak_e - 1)
            rets = [eq[i]/eq[i-1]-1 for i in range(1, n) if eq[i-1] > 0]
            if rets:
                mr = sum(rets) / len(rets)
                sd = math.sqrt(sum((r-mr)**2 for r in rets) / len(rets))
                sharpe = (mr * 252) / (sd * math.sqrt(252)) if sd else 0
            else:
                sharpe = 0
            s = {'total_ret': total*100, 'mdd': mdd*100, 'sharpe': sharpe,
                 'trades': meta.get('n_trims', 0), 'avg_ret': 0,
                 'avg_days': 0, 'win_rate': 0, 'exposure': 0}
        rows.append((name, s))

    # 排 sharpe
    rows.sort(key=lambda r: r[1]['sharpe'], reverse=True)
    print(f'  {"排名":>4}  {"策略":<32} │ {"報酬":>8} {"MaxDD":>8} {"Sharpe":>7}')
    print('─' * 80)
    for i, (n, s) in enumerate(rows[:8], 1):
        bar = '🏆' if i == 1 else ('⭐' if i == 2 else '  ')
        print(f'  {i:>4}  {bar} {n:<32} │ {s["total_ret"]:>+6.2f}% '
              f'{s["mdd"]:>+6.2f}% {s["sharpe"]:>+6.2f}')


def main():
    segments = [
        # 🐻 熊市段
        ('0050.TW', date(2020,  2,  1), date(2020,  4, 30), '🐻 2020 COVID 崩盤'),
        ('0050.TW', date(2022,  1,  1), date(2022, 10, 31), '🐻 2022 升息熊'),
        ('0050.TW', date(2024,  7,  1), date(2024,  8, 31), '🐻 2024 八月迷你熊'),
        ('2330.TW', date(2020,  2,  1), date(2020,  4, 30), '🐻 2020 COVID 崩盤'),
        ('2330.TW', date(2022,  1,  1), date(2022, 10, 31), '🐻 2022 升息熊'),
        ('2330.TW', date(2024,  7,  1), date(2024,  8, 31), '🐻 2024 八月迷你熊'),
        ('2330.TW', date(2025,  3,  1), date(2025,  6, 30), '🐻 2025 春跌'),
        # 😴 盤整段
        ('0050.TW', date(2018,  5,  1), date(2019,  5, 31), '😴 2018-2019 盤整'),
        ('0050.TW', date(2021,  6,  1), date(2022,  1,  1), '😴 2021 H2 盤整'),
        ('2330.TW', date(2018,  5,  1), date(2019,  5, 31), '😴 2018-2019 盤整'),
        # 🐂 牛市段
        ('0050.TW', date(2020,  5,  1), date(2021,  4, 30), '🐂 2020-2021 V 反彈'),
        ('0050.TW', date(2025,  7,  1), date(2026,  5, 14), '🐂 2025-2026 強牛'),
        ('2330.TW', date(2020,  5,  1), date(2021,  4, 30), '🐂 2020-2021 V 反彈'),
        ('2330.TW', date(2025,  7,  1), date(2026,  5, 14), '🐂 2025-2026 強牛'),
    ]
    for tk, s, e, lbl in segments:
        run_strats_on_segment(tk, s, e, lbl)


if __name__ == '__main__':
    main()
