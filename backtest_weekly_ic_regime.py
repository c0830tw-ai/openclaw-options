"""Weekly Iron Condor 在不同 regime 段的表現驗證。"""
from __future__ import annotations
import math
import sys
from datetime import date
sys.path.insert(0, '/Users/chenchinghung/openclaw/options')

from backtest_weekly_calendar import (fetch_yahoo_ohlc, run_weekly_iron_condor,
                                       run_calendar_spread, stats)


def run_segment(ticker, start, end, label):
    dates, closes = fetch_yahoo_ohlc(ticker, 3650)
    idx_lo = next((i for i, d in enumerate(dates) if d >= start), None)
    idx_hi = None
    for i in range(len(dates) - 1, -1, -1):
        if dates[i] <= end:
            idx_hi = i
            break
    if idx_lo is None or idx_hi is None or idx_hi - idx_lo < 30:
        print(f'{label}: 資料不足'); return
    seg_d = dates[idx_lo:idx_hi + 1]
    seg_c = closes[idx_lo:idx_hi + 1]
    base = seg_c[0]; end_v = seg_c[-1]
    move = (end_v / base - 1) * 100
    peak = base; mdd = 0
    for v in seg_c:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)

    print()
    print('━' * 100)
    print(f'{label}（{seg_d[0]} → {seg_d[-1]}, {len(seg_d)} 天）')
    print(f'TAIEX {base:.0f} → {end_v:.0f} ({move:+.2f}%), 最深 DD {mdd*100:+.2f}%')
    print('━' * 100)
    print(f'  {"變體":<35} │ {"報酬":>7} {"年化":>7} {"MaxDD":>7} {"Sharpe":>6} {"勝率":>5} {"次數":>4}')
    print('─' * 100)

    tests = [
        ('Monthly IC (DTE 21)',  run_weekly_iron_condor, {'dte': 21, 'iv_premium': 0.10}),
        ('Weekly IC (DTE 5, IV×1.10)',  run_weekly_iron_condor, {'dte': 5, 'iv_premium': 0.10}),
        ('Weekly IC (DTE 5, IV×1.20)',  run_weekly_iron_condor, {'dte': 5, 'iv_premium': 0.20}),
        ('Put Calendar 7/30',     run_calendar_spread, {'short_dte': 7, 'long_dte': 30, 'iv_premium': 0.10}),
    ]
    for name, fn, kw in tests:
        eq, meta = fn(seg_c, seg_d, **kw)
        s = stats(eq)
        print(f'  {name:<35} │ {s["total"]:>+5.2f}% {s["annual"]:>+5.2f}% '
              f'{s["mdd"]:>+5.2f}% {s["sharpe"]:>+5.2f} '
              f'{meta["win_rate"]*100:>4.0f}% {meta["n_trades"]:>4}')


def main():
    segments = [
        ('^TWII', date(2020, 2, 1),  date(2020, 4, 30),  '🐻 2020 COVID 崩盤'),
        ('^TWII', date(2022, 1, 1),  date(2022, 10, 31), '🐻 2022 升息熊'),
        ('^TWII', date(2024, 7, 1),  date(2024, 8, 31),  '🐻 2024 八月迷你熊'),
        ('^TWII', date(2018, 5, 1),  date(2019, 5, 31),  '😴 2018-2019 盤整'),
        ('^TWII', date(2021, 6, 1),  date(2022, 1, 1),   '😴 2021 H2 盤整'),
        ('^TWII', date(2020, 5, 1),  date(2021, 4, 30),  '🐂 2020-2021 V 反彈'),
        ('^TWII', date(2023, 11, 1), date(2024, 8, 30),  '🐂 2023-2024 牛市'),
        ('^TWII', date(2025, 7, 1),  date(2026, 5, 15),  '🐂 2025-2026 強牛'),
    ]
    for tk, s, e, lbl in segments:
        run_segment(tk, s, e, lbl)


if __name__ == '__main__':
    main()
