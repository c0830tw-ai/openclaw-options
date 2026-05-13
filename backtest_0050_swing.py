"""0050 swing 進出場訊號回測：比較 BB / MA20 / RSI / buy-hold。

資料源：Yahoo `0050.TW` auto_adjust（還原權息）
比較策略：
  1. BB↓ 進 / BB↑ 出
  2. MA20 cross-up 進 / cross-down 出
  3. RSI<30→上穿 30 進 / RSI>70 出
  4. Buy-and-hold（對照組）

用法：
  python3 backtest_0050_swing.py --days 1825
  python3 backtest_0050_swing.py --days 1095 --save 0050_swing.csv
"""
from __future__ import annotations
import argparse
import csv
import math
import sys
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Tuple


def fetch_yahoo_adj(ticker: str, days: int) -> List[Tuple[date, float]]:
    import yfinance as yf
    if days <= 366:    period = '1y'
    elif days <= 732:  period = '2y'
    elif days <= 1830: period = '5y'
    else:              period = 'max'
    df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    out: List[Tuple[date, float]] = []
    for idx, row in df.iterrows():
        c = row.get('Close')
        if c is None or c != c:
            continue
        out.append((idx.date(), float(c)))
    out.sort(key=lambda x: x[0])
    return out


def calc_bb(closes: List[float], period: int = 20, std_mult: float = 2.0):
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


def calc_ma(closes: List[float], period: int) -> List:
    out = []
    for i in range(len(closes)):
        if i < period - 1:
            out.append(None); continue
        out.append(sum(closes[i - period + 1:i + 1]) / period)
    return out


def calc_rsi(closes: List[float], period: int = 14) -> List:
    n = len(closes)
    out: List = [None] * n
    if n < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, n):
        chg = closes[i] - closes[i - 1]
        gains.append(max(0.0, chg))
        losses.append(max(0.0, -chg))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, n):
        if i > period:
            avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
            avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        if avg_l == 0:
            out[i] = 100.0
        else:
            rs = avg_g / avg_l
            out[i] = 100 - 100 / (1 + rs)
    return out


def run_swing(closes: List[float], dates: List[date],
              entry_fn: Callable[[int], bool],
              exit_fn: Callable[[int], bool]) -> Tuple[List[float], List[dict]]:
    """跑 swing 策略，回傳 (逐日 equity, trades list)。
    未持倉時 equity 維持不變。"""
    n = len(closes)
    eq = [1.0]
    in_pos = False
    units = 0.0
    cash = 1.0
    entry_i = None
    trades: List[dict] = []
    for i in range(1, n):
        if in_pos:
            cur = units * closes[i]
            if exit_fn(i):
                trades.append({
                    'entry_date': dates[entry_i],
                    'exit_date':  dates[i],
                    'entry_price': closes[entry_i],
                    'exit_price':  closes[i],
                    'return':      closes[i] / closes[entry_i] - 1,
                    'days':        i - entry_i,
                })
                cash = cur
                units = 0
                in_pos = False
                entry_i = None
                eq.append(cash)
                continue
            eq.append(cur)
        else:
            if entry_fn(i):
                units = cash / closes[i]
                cash = 0
                in_pos = True
                entry_i = i
                eq.append(units * closes[i])
                continue
            eq.append(cash)
    # 結尾若仍持倉，標記未平倉
    if in_pos:
        trades.append({
            'entry_date': dates[entry_i],
            'exit_date':  dates[-1],
            'entry_price': closes[entry_i],
            'exit_price':  closes[-1],
            'return':      closes[-1] / closes[entry_i] - 1,
            'days':        n - 1 - entry_i,
            'open':        True,
        })
    return eq, trades


def stats_for(eq: List[float], trades: List[dict]) -> dict:
    if len(eq) < 2:
        return {}
    total = eq[-1] / eq[0] - 1
    n = len(eq)
    annual = (eq[-1] / eq[0]) ** (252 / max(n - 1, 1)) - 1
    peak = eq[0]; mdd = 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, n) if eq[i - 1] > 0]
    if rets:
        mr = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mr) ** 2 for r in rets) / len(rets))
        sharpe = (mr * 252) / (sd * math.sqrt(252)) if sd else 0
    else:
        sharpe = 0
    wins = [t for t in trades if t['return'] > 0]
    win_rate = len(wins) / len(trades) if trades else 0
    avg_ret = sum(t['return'] for t in trades) / len(trades) if trades else 0
    avg_days = sum(t['days'] for t in trades) / len(trades) if trades else 0
    days_in_pos = sum(t['days'] for t in trades)
    exposure = days_in_pos / n if n else 0
    return {
        'total_ret': total * 100,
        'annual':    annual * 100,
        'mdd':       mdd * 100,
        'sharpe':    sharpe,
        'trades':    len(trades),
        'win_rate':  win_rate * 100,
        'avg_ret':   avg_ret * 100,
        'avg_days':  avg_days,
        'exposure':  exposure * 100,
    }


def _label_regime(ret_pct: float) -> str:
    if ret_pct > 10:  return '🐂 牛市'
    if ret_pct < -5:  return '🐻 熊市'
    return '😴 盤整'


def run_all_strategies_on(closes: List[float], dates: List[date]):
    """在指定區間跑全部 5 個策略，回傳 [(name, eq, trades)]。"""
    _, bb_low, bb_up = calc_bb(closes, 20)
    ma20 = calc_ma(closes, 20)
    rsi = calc_rsi(closes, 14)

    def s1_e(i): return bb_low[i] is not None and closes[i] <= bb_low[i]
    def s1_x(i): return bb_up[i]  is not None and closes[i] >= bb_up[i]
    def s2_e(i): return (ma20[i] is not None and ma20[i-1] is not None
                         and closes[i] > ma20[i] and closes[i-1] <= ma20[i-1])
    def s2_x(i): return (ma20[i] is not None and ma20[i-1] is not None
                         and closes[i] < ma20[i] and closes[i-1] >= ma20[i-1])
    def s3_e(i): return (rsi[i] is not None and rsi[i-1] is not None
                         and rsi[i] > 30 and rsi[i-1] <= 30)
    def s3_x(i): return rsi[i] is not None and rsi[i] > 70
    def s4_e(i): return (bb_low[i] is not None and rsi[i] is not None
                         and closes[i] <= bb_low[i] and rsi[i] < 35)
    def s4_x(i): return ((bb_up[i] is not None and closes[i] >= bb_up[i])
                         or (rsi[i] is not None and rsi[i] > 65))

    eq1, t1 = run_swing(closes, dates, s1_e, s1_x)
    eq2, t2 = run_swing(closes, dates, s2_e, s2_x)
    eq3, t3 = run_swing(closes, dates, s3_e, s3_x)
    eq4, t4 = run_swing(closes, dates, s4_e, s4_x)
    bh_eq = [closes[i] / closes[0] for i in range(len(closes))]
    bh_tr = [{'entry_date': dates[0], 'exit_date': dates[-1],
              'entry_price': closes[0], 'exit_price': closes[-1],
              'return': closes[-1] / closes[0] - 1, 'days': len(closes) - 1}]

    return [
        ('BB↓進 BB↑出',         eq1, t1),
        ('MA20 cross 進出',     eq2, t2),
        ('RSI<30 進 RSI>70 出', eq3, t3),
        ('BB+RSI 雙重',          eq4, t4),
        ('Buy & Hold',           bh_eq, bh_tr),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=1825,
                    help='回測天數（建議 1095=3y 或 1825=5y）')
    ap.add_argument('--segments', type=int, default=0,
                    help='分段數（牛/熊/盤整 regime 分析），0=不分段')
    ap.add_argument('--save', help='CSV 輸出路徑')
    args = ap.parse_args()

    print(f'[swing] Yahoo 抓 0050.TW (auto_adjust) {args.days} 天...', file=sys.stderr)
    series = fetch_yahoo_adj('0050.TW', args.days)
    if len(series) < 60:
        print('資料不足', file=sys.stderr); return 1
    dates = [d for d, _ in series]
    closes = [c for _, c in series]
    print(f'[swing] 共 {len(series)} 天: {dates[0]} → {dates[-1]}', file=sys.stderr)

    # 指標
    _, bb_low, bb_up = calc_bb(closes, 20)
    ma20 = calc_ma(closes, 20)
    rsi = calc_rsi(closes, 14)

    # ─ 策略 1: BB↓ 進 / BB↑ 出 ─
    def s1_entry(i):
        return bb_low[i] is not None and closes[i] <= bb_low[i]
    def s1_exit(i):
        return bb_up[i] is not None and closes[i] >= bb_up[i]
    eq1, t1 = run_swing(closes, dates, s1_entry, s1_exit)

    # ─ 策略 2: MA20 cross-up 進 / cross-down 出 ─
    def s2_entry(i):
        if ma20[i] is None or ma20[i - 1] is None: return False
        return closes[i] > ma20[i] and closes[i - 1] <= ma20[i - 1]
    def s2_exit(i):
        if ma20[i] is None or ma20[i - 1] is None: return False
        return closes[i] < ma20[i] and closes[i - 1] >= ma20[i - 1]
    eq2, t2 = run_swing(closes, dates, s2_entry, s2_exit)

    # ─ 策略 3: RSI<30 反彈進 / RSI>70 出 ─
    def s3_entry(i):
        if rsi[i] is None or rsi[i - 1] is None: return False
        return rsi[i] > 30 and rsi[i - 1] <= 30
    def s3_exit(i):
        return rsi[i] is not None and rsi[i] > 70
    eq3, t3 = run_swing(closes, dates, s3_entry, s3_exit)

    # ─ 策略 4: BB↓ + RSI<35 雙重進 / BB↑ 或 RSI>65 出 ─
    def s4_entry(i):
        if bb_low[i] is None or rsi[i] is None: return False
        return closes[i] <= bb_low[i] and rsi[i] < 35
    def s4_exit(i):
        if bb_up[i] is None or rsi[i] is None: return False
        return closes[i] >= bb_up[i] or rsi[i] > 65
    eq4, t4 = run_swing(closes, dates, s4_entry, s4_exit)

    # ─ 對照組: Buy-Hold ─
    bh_eq = [closes[i] / closes[0] for i in range(len(closes))]
    bh_trades = [{
        'entry_date': dates[0], 'exit_date': dates[-1],
        'entry_price': closes[0], 'exit_price': closes[-1],
        'return': closes[-1] / closes[0] - 1, 'days': len(closes) - 1,
    }]

    strats = [
        ('BB↓進 BB↑出',         eq1, t1),
        ('MA20 cross 進出',     eq2, t2),
        ('RSI<30 進 RSI>70 出', eq3, t3),
        ('BB+RSI 雙重',          eq4, t4),
        ('Buy & Hold',           bh_eq, bh_trades),
    ]

    print()
    print('━' * 110)
    print(f'0050 swing 策略回測（{dates[0]} → {dates[-1]}, '
          f'0050: {closes[0]:.2f} → {closes[-1]:.2f}, {(closes[-1]/closes[0]-1)*100:+.2f}%）')
    print('━' * 110)
    header = f'  {"策略":<22} │ {"總報酬":>9} {"年化":>9} {"MaxDD":>8} {"Sharpe":>7} {"次數":>4} {"勝率":>6} {"均報":>7} {"均天":>6} {"曝險":>6}'
    print(header)
    print('─' * 110)
    rows = []
    for name, eq, tr in strats:
        s = stats_for(eq, tr)
        rows.append((name, s))
        print(f'  {name:<22} │ {s["total_ret"]:>+7.2f}% {s["annual"]:>+7.2f}% '
              f'{s["mdd"]:>+7.2f}% {s["sharpe"]:>+7.2f} {s["trades"]:>4} '
              f'{s["win_rate"]:>5.1f}% {s["avg_ret"]:>+5.2f}% {s["avg_days"]:>5.1f} '
              f'{s["exposure"]:>5.1f}%')
    print('━' * 110)

    # 排名（依 Sharpe）
    rows.sort(key=lambda r: r[1]['sharpe'], reverse=True)
    print(f'\n📊 依 Sharpe 排名（risk-adjusted）：')
    for i, (name, s) in enumerate(rows, 1):
        bar = '🏆' if i == 1 else ('⭐' if i == 2 else '  ')
        print(f'  {i}. {bar} {name:<22} Sharpe={s["sharpe"]:+.2f}, '
              f'總報酬 {s["total_ret"]:+.1f}%, DD {s["mdd"]:+.1f}%')

    # ─── 分段（牛/熊/盤整 regime 分析）───────────
    if args.segments and args.segments > 1:
        n = len(dates)
        chunk = n // args.segments
        win_count: Dict[str, int] = {}
        top3_count: Dict[str, int] = {}
        regime_winners: Dict[str, List[str]] = {'🐂 牛市': [], '🐻 熊市': [], '😴 盤整': []}

        for q in range(args.segments):
            s_i = q * chunk
            e_i = (q + 1) * chunk - 1 if q < args.segments - 1 else n - 1
            sl_dates  = dates[s_i:e_i + 1]
            sl_closes = closes[s_i:e_i + 1]
            ret_pct = (sl_closes[-1] / sl_closes[0] - 1) * 100 if sl_closes[0] else 0
            regime = _label_regime(ret_pct)
            seg_strats = run_all_strategies_on(sl_closes, sl_dates)

            seg_rows = []
            for name, eq, tr in seg_strats:
                s = stats_for(eq, tr)
                seg_rows.append({'name': name, **s})
            seg_rows.sort(key=lambda r: r['sharpe'], reverse=True)

            print()
            print(f'### Q{q+1}: {regime}（{sl_dates[0]} → {sl_dates[-1]}, '
                  f'0050 {ret_pct:+.2f}%）')
            print(f'  {"排名":>4}  {"策略":<22} │ {"總報酬":>9} {"DD":>8} {"Sharpe":>7} {"次數":>4} {"勝率":>6} {"曝險":>6}')
            print('─' * 90)
            for i, r in enumerate(seg_rows, 1):
                bar = '🏆' if i == 1 else ('⭐' if i == 2 else '  ')
                print(f'  {i:>4}  {bar} {r["name"]:<22} │ {r["total_ret"]:>+7.2f}% '
                      f'{r["mdd"]:>+7.2f}% {r["sharpe"]:>+7.2f} '
                      f'{r["trades"]:>4} {r["win_rate"]:>5.1f}% {r["exposure"]:>5.1f}%')

            for j, r in enumerate(seg_rows[:3]):
                top3_count[r['name']] = top3_count.get(r['name'], 0) + 1
                if j == 0:
                    win_count[r['name']] = win_count.get(r['name'], 0) + 1
                    regime_winners[regime].append(r['name'])

        print()
        print('━' * 90)
        print('跨情境統計')
        print('━' * 90)
        print('🏆 奪冠次數:')
        for k, v in sorted(win_count.items(), key=lambda x: -x[1]):
            print(f'   {k}: {v}')
        print('進前 3 次數:')
        for k, v in sorted(top3_count.items(), key=lambda x: -x[1]):
            print(f'   {k}: {v}')
        print('依 regime 看哪個策略贏:')
        for reg, winners in regime_winners.items():
            if winners:
                print(f'   {reg}: {", ".join(winners)}')

    if args.save:
        out_path = Path(args.save)
        with out_path.open('w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['date', 'close'] + [n for n, _, _ in strats])
            for i, d in enumerate(dates):
                w.writerow([d, closes[i]] + [strats[j][1][i] for j in range(len(strats))])
        print(f'\n[swing] CSV → {out_path}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
