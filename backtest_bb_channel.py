"""BB 通道策略回測：BB↓ 分批進、BB↑ 後 trail、5% 停損。

跑 3 個 timeframe 對照：
- 30 分 K（60 天樣本）
- 60 分 K（2 年樣本）
- 日 K（5 年樣本）
"""
from __future__ import annotations
import math
import sys
from typing import List


def fetch_yahoo_intraday(ticker, period, interval):
    import yfinance as yf
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    rows = []
    for idx, row in df.iterrows():
        c = row.get('Close'); h = row.get('High'); l = row.get('Low')
        if any(v is None or v != v for v in (c, h, l)):
            continue
        rows.append((idx, float(h), float(l), float(c)))
    rows.sort(key=lambda x: x[0])
    return ([r[0] for r in rows], [r[1] for r in rows],
            [r[2] for r in rows], [r[3] for r in rows])


def calc_bb(closes, period=20, std_mult=2.0):
    mid, low, up = [], [], []
    for i in range(len(closes)):
        if i < period - 1:
            mid.append(None); low.append(None); up.append(None); continue
        window = closes[i - period + 1:i + 1]
        m = sum(window) / period
        std = math.sqrt(sum((x - m) ** 2 for x in window) / period)
        mid.append(m)
        low.append(m - std_mult * std)
        up.append(m + std_mult * std)
    return mid, low, up


def run_bb_channel(highs, lows, closes, dates, bb_period=20,
                   stop_loss_pct=0.05, trail_pct=0.05, batches=3):
    """
    策略：
    - 收盤觸 BB↓ → 用「剩餘現金 × 1/n_remaining_batches」買進
    - 觸 BB↑ → 開始 trail stop
    - 從平均成本跌 stop_loss_pct → 全停損
    - trail 啟動後從歷史高跌 trail_pct → 移動停利全出
    """
    n = len(closes)
    _, bb_low, bb_up = calc_bb(closes, bb_period)
    cash = 1.0
    units = 0.0
    avg_cost = 0.0
    fraction_invested = 0.0   # 0 ~ 1.0
    high_since_entry = 0.0
    trail_active = False
    n_buys = 0
    n_sells = 0
    eq = []

    for i in range(n):
        c = closes[i]
        # 進場：觸 BB↓ 且未滿倉
        if bb_low[i] is not None and c <= bb_low[i] and fraction_invested < 0.999:
            target_increase = min(1.0 / batches, 1.0 - fraction_invested)
            # 用「起始資本 × target_increase」量化買入
            invest_cash = min(target_increase, cash)
            if invest_cash > 1e-9 and c > 0:
                buy_units = invest_cash / c
                new_units = units + buy_units
                if new_units > 1e-9:
                    avg_cost = (avg_cost * units + c * buy_units) / new_units
                units = new_units
                cash -= invest_cash
                fraction_invested = min(1.0, fraction_invested + target_increase)
                n_buys += 1

        if units > 0:
            high_since_entry = max(high_since_entry, c)
            # 觸 BB↑ → 啟動 trail
            if bb_up[i] is not None and c >= bb_up[i]:
                trail_active = True
            # 停損
            exit_now = False
            if c <= avg_cost * (1 - stop_loss_pct):
                exit_now = True
            elif trail_active and c <= high_since_entry * (1 - trail_pct):
                exit_now = True
            if exit_now:
                cash += units * c
                units = 0
                avg_cost = 0
                fraction_invested = 0
                trail_active = False
                high_since_entry = 0
                n_sells += 1
        eq.append(units * c + cash)

    final_value = units * closes[-1] + cash
    total = final_value - 1
    peak = eq[0]; mdd = 0.0
    for v in eq:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, n) if eq[i - 1] > 0]
    if rets:
        mr = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mr) ** 2 for r in rets) / len(rets))
    else:
        mr, sd = 0, 0
    return eq, {
        'total_ret': total * 100,
        'mdd': mdd * 100,
        'mean_ret': mr,
        'std_ret': sd,
        'n_buys': n_buys,
        'n_sells': n_sells,
        'final_fraction': fraction_invested,
    }


def buy_hold_stats(closes):
    n = len(closes)
    eq = [c / closes[0] for c in closes]
    total = closes[-1] / closes[0] - 1
    peak = eq[0]; mdd = 0
    for v in eq:
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, n) if eq[i - 1] > 0]
    if rets:
        mr = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mr) ** 2 for r in rets) / len(rets))
    else:
        mr, sd = 0, 0
    return {'total_ret': total * 100, 'mdd': mdd * 100, 'mean_ret': mr, 'std_ret': sd}


def annualize_sharpe(mr, sd, bars_per_year):
    if sd == 0: return 0
    return (mr * bars_per_year) / (sd * math.sqrt(bars_per_year))


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--ticker', default='0050.TW')
    args = ap.parse_args()

    print(f'BB 通道策略回測（{args.ticker}）：BB↓ 分批進、BB↑ trail、-5% 停損')
    print('=' * 88)

    configs = [
        ('30 分 K (近 60 天)',   '60d', '30m', 252 * 13),   # 13 個 30-min/day
        ('60 分 K (近 2 年)',     '730d', '60m', 252 * 7),  # 7 個 hour/day (盤中 ~6.5h)
        ('日 K (近 5 年)',        '5y', '1d', 252),
    ]

    for label, period, interval, bars_per_year in configs:
        try:
            dates, highs, lows, closes = fetch_yahoo_intraday(args.ticker, period, interval)
        except Exception as e:
            print(f'\n{label}：抓取失敗 - {e}')
            continue
        if len(closes) < 30:
            print(f'\n{label}：資料太少 ({len(closes)} bars)')
            continue

        # 策略 1: 含 5% 停損
        eq, stats = run_bb_channel(highs, lows, closes, dates, stop_loss_pct=0.05)
        sharpe = annualize_sharpe(stats['mean_ret'], stats['std_ret'], bars_per_year)
        # 策略 2: 拿掉 5% 停損（只有 trail 停利）
        eq2, stats2 = run_bb_channel(highs, lows, closes, dates, stop_loss_pct=0.99)
        sharpe2 = annualize_sharpe(stats2['mean_ret'], stats2['std_ret'], bars_per_year)
        # 策略 3: 停損改 10%
        eq3, stats3 = run_bb_channel(highs, lows, closes, dates, stop_loss_pct=0.10)
        sharpe3 = annualize_sharpe(stats3['mean_ret'], stats3['std_ret'], bars_per_year)

        bh = buy_hold_stats(closes)
        bh_sharpe = annualize_sharpe(bh['mean_ret'], bh['std_ret'], bars_per_year)
        bh_pct = (closes[-1] / closes[0] - 1) * 100

        print(f'\n📊 {label}  ({len(closes)} bars, {dates[0]} → {dates[-1]})')
        print(f'   {args.ticker}: {closes[0]:.2f} → {closes[-1]:.2f} ({bh_pct:+.2f}%)')
        print(f'   ┌────────────────────────────────────────────────────────────┐')
        print(f'   │ 策略                報酬       MaxDD    Sharpe  買/賣       │')
        print(f'   ├────────────────────────────────────────────────────────────┤')
        print(f'   │ BB通道 + 5% 停損    {stats["total_ret"]:>+7.2f}%   {stats["mdd"]:>+6.2f}%   '
              f'{sharpe:>+5.2f}   {stats["n_buys"]}/{stats["n_sells"]:<2}        │')
        print(f'   │ BB通道 + 10% 停損   {stats3["total_ret"]:>+7.2f}%   {stats3["mdd"]:>+6.2f}%   '
              f'{sharpe3:>+5.2f}   {stats3["n_buys"]}/{stats3["n_sells"]:<2}        │')
        print(f'   │ BB通道 (無停損)     {stats2["total_ret"]:>+7.2f}%   {stats2["mdd"]:>+6.2f}%   '
              f'{sharpe2:>+5.2f}   {stats2["n_buys"]}/{stats2["n_sells"]:<2}        │')
        print(f'   │ Buy & Hold          {bh["total_ret"]:>+7.2f}%   {bh["mdd"]:>+6.2f}%   '
              f'{bh_sharpe:>+5.2f}   -            │')
        print(f'   └────────────────────────────────────────────────────────────┘')


if __name__ == '__main__':
    main()
