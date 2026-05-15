"""0050 持倉「跌多少減碼、回到什麼支撐加碼」回測。

對照組：純買 hold
變體：6 種 trim 觸發 × add-back 訊號組合

資料源：Yahoo `0050.TW` auto_adjust（還原權息）

用法：
  python3 backtest_0050_trim_add.py --days 1825
  python3 backtest_0050_trim_add.py --days 1825 --segments 6
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
    elif days <= 3660: period = '10y'
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


def fetch_yahoo_ohlc(ticker: str, days: int):
    """回傳 (dates, opens, highs, lows, closes)"""
    import yfinance as yf
    if days <= 366:    period = '1y'
    elif days <= 732:  period = '2y'
    elif days <= 1830: period = '5y'
    elif days <= 3660: period = '10y'
    else:              period = 'max'
    df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    rows = []
    for idx, row in df.iterrows():
        c = row.get('Close'); h = row.get('High'); l = row.get('Low'); o = row.get('Open')
        if any(v is None or v != v for v in (c, h, l, o)):
            continue
        rows.append((idx.date(), float(o), float(h), float(l), float(c)))
    rows.sort(key=lambda x: x[0])
    return ([r[0] for r in rows], [r[1] for r in rows],
            [r[2] for r in rows], [r[3] for r in rows], [r[4] for r in rows])


def calc_atr(highs, lows, closes, period=14):
    """Wilder ATR"""
    out = [None]
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        trs.append(tr)
        if i < period:
            out.append(None)
        elif i == period:
            out.append(sum(trs) / period)
        else:
            out.append((out[-1] * (period - 1) + tr) / period)
    return out


def calc_bb(closes, period=20, std_mult=2.0):
    mid, low, up = [], [], []
    for i in range(len(closes)):
        if i < period - 1:
            mid.append(None); low.append(None); up.append(None); continue
        window = closes[i - period + 1:i + 1]
        m = sum(window) / period
        std = math.sqrt(sum((x - m) ** 2 for x in window) / period)
        mid.append(m); low.append(m - std_mult * std); up.append(m + std_mult * std)
    return mid, low, up


def calc_ma(closes, period):
    out = []
    for i in range(len(closes)):
        if i < period - 1:
            out.append(None); continue
        out.append(sum(closes[i - period + 1:i + 1]) / period)
    return out


def calc_ema(closes, period):
    if not closes:
        return []
    k = 2 / (period + 1)
    out = [closes[0]]
    for v in closes[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def run_breakeven_stop(closes, dates, activation_pct, trail_pct, add_signal_fn):
    """保本停利：
       階段 1：from entry，沒漲到 activation_pct → 無 stop（hold）
       階段 2：漲到 activation_pct 啟動 → stop = max(entry_price, high × (1-trail_pct))
       觸發 stop → 全出
       add-back 後重置"""
    n = len(closes)
    units = 1.0; cash = 0.0
    entry_price = closes[0]
    high_since_entry = closes[0]
    activated = False
    n_trims = 0; n_adds = 0
    eq = [1.0]; base = closes[0]
    for i in range(1, n):
        fully = cash < 1e-6
        if fully:
            high_since_entry = max(high_since_entry, closes[i])
            if closes[i] / entry_price - 1 >= activation_pct:
                activated = True
            if activated:
                stop = max(entry_price, high_since_entry * (1 - trail_pct))
                if closes[i] < stop:
                    cash += units * closes[i]
                    units = 0
                    activated = False
                    n_trims += 1
        if cash > 0 and add_signal_fn(i):
            units += cash / closes[i]
            cash = 0
            entry_price = closes[i]
            high_since_entry = closes[i]
            activated = False
            n_adds += 1
        eq.append((units * closes[i] + cash) / base)
    return eq, {'n_trims': n_trims, 'n_adds': n_adds}


def run_atr_trailing(closes, dates, highs, lows, atr_mult, add_signal_fn, atr_period=14):
    """ATR trailing stop：stop = high_since_entry - atr_mult × ATR
       依波動率動態調整 trail 距離"""
    atr = calc_atr(highs, lows, closes, atr_period)
    n = len(closes)
    units = 1.0; cash = 0.0
    high_since_entry = closes[0]
    n_trims = 0; n_adds = 0
    eq = [1.0]; base = closes[0]
    for i in range(1, n):
        fully = cash < 1e-6
        if fully:
            high_since_entry = max(high_since_entry, closes[i])
            if atr[i] is not None:
                stop = high_since_entry - atr_mult * atr[i]
                if closes[i] < stop:
                    cash += units * closes[i]
                    units = 0
                    n_trims += 1
        if cash > 0 and add_signal_fn(i):
            units += cash / closes[i]
            cash = 0
            high_since_entry = closes[i]
            n_adds += 1
        eq.append((units * closes[i] + cash) / base)
    return eq, {'n_trims': n_trims, 'n_adds': n_adds}


def run_trailing_profit_lock(closes: List[float], dates: List[date],
                              activation_pct: float, trim_pct: float, trim_size: float,
                              add_signal_fn) -> Tuple[List[float], dict]:
    """經典移動停利：
       - 入場後價格上漲超過 activation_pct (e.g. +5%) 才啟動追蹤
       - 之後追蹤入場後 high；若回檔 trim_pct 從 high → trim 對應比例
       - add-back 用 add_signal_fn"""
    n = len(closes)
    units = 1.0
    cash = 0.0
    entry_price = closes[0]
    high_since_entry = closes[0]
    activated = False
    n_trims = 0
    n_adds = 0
    eq = [1.0]
    base = closes[0]
    for i in range(1, n):
        fully_deployed = cash < 1e-6
        if fully_deployed:
            high_since_entry = max(high_since_entry, closes[i])
            if closes[i] / entry_price - 1 >= activation_pct:
                activated = True
            if activated:
                dd = closes[i] / high_since_entry - 1
                if dd <= -trim_pct:
                    trim_units = units * trim_size
                    cash += trim_units * closes[i]
                    units -= trim_units
                    n_trims += 1
                    activated = False   # 等下次重新建滿才重新啟動
        if cash > 0 and add_signal_fn(i):
            add_units = cash / closes[i]
            units += add_units
            cash = 0
            entry_price = closes[i]
            high_since_entry = closes[i]
            activated = False
            n_adds += 1
        eq.append((units * closes[i] + cash) / base)
    return eq, {'n_trims': n_trims, 'n_adds': n_adds,
                'final_units': units, 'final_cash': cash}


def run_trim_add(closes: List[float], dates: List[date],
                 trim_levels: List[Tuple[float, float]],
                 add_signal_fn,
                 add_levels=None,
                 trim_signal_fn=None) -> Tuple[List[float], dict]:
    """trim_levels: [(DD threshold, trim fraction), ...]
       add_signal_fn(i): 單階 add-back 訊號 → 加滿回 1.0 unit
       add_levels (optional): [(signal_fn, fraction), ...] — 分階加回
       trim_signal_fn (optional): 訊號式 trim（如跌破均線），提供時忽略 DD threshold；
                                  砍倉比例用 trim_levels[0][1]，可重複觸發（add-back 後 reset）
       回傳 (逐日 equity normalized, stats dict)"""
    n = len(closes)
    units = 1.0
    cash = 0.0
    recent_high = closes[0]
    trim_state = 0
    add_state  = 0
    n_trims = 0
    n_adds = 0
    eq = [1.0]
    base = closes[0]
    laddered_add = add_levels is not None and len(add_levels) > 0
    use_signal_trim = trim_signal_fn is not None
    signal_trim_frac = trim_levels[0][1] if trim_levels else 1.0
    for i in range(1, n):
        # 「滿倉」判定：cash 為 0（沒閒置資金）= 已全部投入
        fully_deployed = cash < 1e-6
        if fully_deployed:
            recent_high = max(recent_high, closes[i])
        # 檢查 trim
        if use_signal_trim:
            # 訊號式 trim：滿倉時若觸發訊號就砍
            if fully_deployed and trim_signal_fn(i):
                trim_units = units * signal_trim_frac
                cash += trim_units * closes[i]
                units -= trim_units
                n_trims += 1
        else:
            dd = closes[i] / recent_high - 1
            while trim_state < len(trim_levels) and dd <= -trim_levels[trim_state][0]:
                trim_frac = trim_levels[trim_state][1]
                trim_units = units * trim_frac
                cash += trim_units * closes[i]
                units -= trim_units
                trim_state += 1
                n_trims += 1
        # 檢查 add-back
        if cash > 0:
            if laddered_add:
                # 分階加回：每個 level 觸發時加該比例（of 原始 cash）
                while add_state < len(add_levels) and add_levels[add_state][0](i):
                    frac = add_levels[add_state][1]
                    # frac 是剩餘 cash 的比例（最後一階用 frac=1.0 確保加滿）
                    add_cash = cash * frac
                    add_units = add_cash / closes[i]
                    units += add_units
                    cash  -= add_cash
                    add_state += 1
                    n_adds += 1
                if cash < 1e-6:
                    cash = 0; trim_state = 0; add_state = 0
                    recent_high = closes[i]
            else:
                if add_signal_fn(i):
                    add_units = cash / closes[i]
                    units += add_units
                    cash = 0
                    recent_high = closes[i]
                    trim_state = 0
                    n_adds += 1
        eq.append((units * closes[i] + cash) / base)
    return eq, {'n_trims': n_trims, 'n_adds': n_adds,
                'final_units': units, 'final_cash': cash}


def stats(eq: List[float]) -> dict:
    if len(eq) < 2: return {}
    total = eq[-1] / eq[0] - 1
    n = len(eq)
    annual = (eq[-1] / eq[0]) ** (252 / max(n - 1, 1)) - 1
    peak = eq[0]; mdd = 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, n) if eq[i - 1] > 0]
    mr = sum(rets) / len(rets) if rets else 0
    sd = math.sqrt(sum((r - mr) ** 2 for r in rets) / len(rets)) if rets else 0
    sharpe = (mr * 252) / (sd * math.sqrt(252)) if sd else 0
    return {'total_ret': total * 100, 'annual': annual * 100,
            'mdd': mdd * 100, 'sharpe': sharpe, 'vol': sd * math.sqrt(252) * 100}


def build_strategies(closes, dates, highs=None, lows=None):
    """組合所有變體；回傳 [(name, eq, meta)]。highs/lows 用於 ATR trailing。"""
    _, bb_low, _ = calc_bb(closes, 20)
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    ma60 = calc_ma(closes, 60)
    ema23 = calc_ema(closes, 23)
    ema10 = calc_ema(closes, 10)

    def ma20_cross_up(i):
        if i < 1 or ma20[i] is None or ma20[i-1] is None: return False
        return closes[i] > ma20[i] and closes[i-1] <= ma20[i-1]
    def ma60_cross_up(i):
        if i < 1 or ma60[i] is None or ma60[i-1] is None: return False
        return closes[i] > ma60[i] and closes[i-1] <= ma60[i-1]
    def bb_low_touch(i):
        return bb_low[i] is not None and closes[i] <= bb_low[i]
    def ema23_break_down(i):
        # 收盤跌破 23 EMA（前一日仍在 EMA 上方）
        if i < 1 or ema23[i] is None or ema23[i-1] is None: return False
        return closes[i] < ema23[i] and closes[i-1] >= ema23[i-1]
    def ema23_cross_up(i):
        if i < 1 or ema23[i] is None or ema23[i-1] is None: return False
        return closes[i] > ema23[i] and closes[i-1] <= ema23[i-1]
    def ma10_cross_up(i):
        if i < 1 or ma10[i] is None or ma10[i-1] is None: return False
        return closes[i] > ma10[i] and closes[i-1] <= ma10[i-1]
    def ema10_cross_up(i):
        if i < 1 or ema10[i] is None or ema10[i-1] is None: return False
        return closes[i] > ema10[i] and closes[i-1] <= ema10[i-1]

    results = []
    # Trim 單階段（50%）+ 不同 add-back
    eq, meta = run_trim_add(closes, dates, [(0.05, 0.50)], ma20_cross_up)
    results.append(('Trim -5%×50% / +MA20', eq, meta))

    eq, meta = run_trim_add(closes, dates, [(0.10, 0.50)], ma20_cross_up)
    results.append(('Trim -10%×50% / +MA20', eq, meta))

    eq, meta = run_trim_add(closes, dates, [(0.15, 0.50)], ma20_cross_up)
    results.append(('Trim -15%×50% / +MA20', eq, meta))

    eq, meta = run_trim_add(closes, dates, [(0.10, 0.50)], bb_low_touch)
    results.append(('Trim -10%×50% / +BB↓', eq, meta))

    eq, meta = run_trim_add(closes, dates, [(0.10, 0.50)], ma60_cross_up)
    results.append(('Trim -10%×50% / +MA60', eq, meta))

    # Ladder：-5%/-10%/-15% 各砍 25%
    eq, meta = run_trim_add(closes, dates,
                            [(0.05, 0.25), (0.10, 0.33), (0.15, 0.50)],
                            ma20_cross_up)
    results.append(('Ladder 5/10/15% / +MA20', eq, meta))

    # ── 新增變體（用戶 5/15 詢問執行方式對比）──
    # Trim 100% (全出) + MA60 加回
    eq, meta = run_trim_add(closes, dates, [(0.10, 1.00)], ma60_cross_up)
    results.append(('Trim -10%×100% / +MA60', eq, meta))

    # Trim 100% (全出) + MA20 加回
    eq, meta = run_trim_add(closes, dates, [(0.10, 1.00)], ma20_cross_up)
    results.append(('Trim -10%×100% / +MA20', eq, meta))

    # Trim 50% + Add laddered (MA20 加 50% + MA60 加剩餘全部)
    eq, meta = run_trim_add(
        closes, dates, [(0.10, 0.50)], None,
        add_levels=[(ma20_cross_up, 0.50), (ma60_cross_up, 1.00)]
    )
    results.append(('Trim 50% / +MA20×½+MA60×½', eq, meta))

    # ★ 新增（用戶 5/15 提議）：-5% 砍 50% + -10% 再砍 100%（即全出）
    # 這是「分階出場」邏輯：先預警出一半、跌深再全出
    eq, meta = run_trim_add(closes, dates, [(0.05, 0.50), (0.10, 1.00)], ma60_cross_up)
    results.append(('Trim 5%/50%+10%/100% / +MA60', eq, meta))

    eq, meta = run_trim_add(closes, dates, [(0.05, 0.50), (0.10, 1.00)], ma20_cross_up)
    results.append(('Trim 5%/50%+10%/100% / +MA20', eq, meta))

    # ★ 新增（用戶 5/15 提議）：跌破 23 EMA 全賣 + 站上 MA60 全買
    # 訊號式 trim（非 DD 觸發），用 trim_levels[0][1] = 1.0 表示全出
    eq, meta = run_trim_add(closes, dates, [(0, 1.00)], ma60_cross_up,
                            trim_signal_fn=ema23_break_down)
    results.append(('EMA23↓ 全出 / +MA60', eq, meta))

    # 同樣訊號 trim 但 MA20 加回（看是否更快）
    eq, meta = run_trim_add(closes, dates, [(0, 1.00)], ma20_cross_up,
                            trim_signal_fn=ema23_break_down)
    results.append(('EMA23↓ 全出 / +MA20', eq, meta))

    # 跌破 EMA23 全出 + 站回 EMA23 全進（同訊號雙向，最敏感）
    eq, meta = run_trim_add(closes, dates, [(0, 1.00)], ema23_cross_up,
                            trim_signal_fn=ema23_break_down)
    results.append(('EMA23↓ 全出 / +EMA23↑', eq, meta))

    # 跌破 EMA23 半出 + 站回 EMA23 補回（保守版）
    eq, meta = run_trim_add(closes, dates, [(0, 0.50)], ema23_cross_up,
                            trim_signal_fn=ema23_break_down)
    results.append(('EMA23↓ 半出 / +EMA23↑', eq, meta))

    # ★ 買回訊號變體（用戶 5/15）：對 0050 冠軍規則 trim 5%/50%+10%/100% 試不同 add-back
    # 比較 MA10 / MA20 / EMA10 / EMA23 / BB↓
    eq, meta = run_trim_add(closes, dates, [(0.05, 0.50), (0.10, 1.00)], ma10_cross_up)
    results.append(('Trim 5%/50%+10%/100% / +MA10', eq, meta))

    eq, meta = run_trim_add(closes, dates, [(0.05, 0.50), (0.10, 1.00)], ema10_cross_up)
    results.append(('Trim 5%/50%+10%/100% / +EMA10', eq, meta))

    eq, meta = run_trim_add(closes, dates, [(0.05, 0.50), (0.10, 1.00)], ema23_cross_up)
    results.append(('Trim 5%/50%+10%/100% / +EMA23', eq, meta))

    eq, meta = run_trim_add(closes, dates, [(0.05, 0.50), (0.10, 1.00)],
                            lambda i: bb_low[i] is not None and closes[i] <= bb_low[i])
    results.append(('Trim 5%/50%+10%/100% / +BB↓ touch', eq, meta))

    # 對 2330 / 00679B 冠軍規則也測 MA10 buy-back
    # Trim -10%×100% / +MA10 (vs 現用 MA20)
    eq, meta = run_trim_add(closes, dates, [(0.10, 1.00)], ma10_cross_up)
    results.append(('Trim -10%×100% / +MA10', eq, meta))

    # EMA23↓ 全出 / +MA10 (vs 現用 MA20)
    eq, meta = run_trim_add(closes, dates, [(0, 1.00)], ma10_cross_up,
                            trim_signal_fn=ema23_break_down)
    results.append(('EMA23↓ 全出 / +MA10', eq, meta))

    # ★ 經典移動停利（用戶 5/15 詢問）
    # 要求先有利潤 +5% 才啟動，跌 -10% trim 全出
    eq, meta = run_trailing_profit_lock(closes, dates,
                                         activation_pct=0.05, trim_pct=0.10,
                                         trim_size=1.00, add_signal_fn=ma20_cross_up)
    results.append(('移動停利 +5% 啟動 / -10% 全出 / +MA20', eq, meta))

    # 經典移動停利 - 更嚴格啟動門檻
    eq, meta = run_trailing_profit_lock(closes, dates,
                                         activation_pct=0.10, trim_pct=0.10,
                                         trim_size=1.00, add_signal_fn=ma20_cross_up)
    results.append(('移動停利 +10% 啟動 / -10% 全出 / +MA20', eq, meta))

    # 經典移動停利 - 半出
    eq, meta = run_trailing_profit_lock(closes, dates,
                                         activation_pct=0.05, trim_pct=0.05,
                                         trim_size=0.50, add_signal_fn=ma20_cross_up)
    results.append(('移動停利 +5% 啟動 / -5% 半出 / +MA20', eq, meta))

    # ★ 保本停利（用戶 5/15 詢問）
    eq, meta = run_breakeven_stop(closes, dates, activation_pct=0.10,
                                   trail_pct=0.05, add_signal_fn=ma20_cross_up)
    results.append(('保本停利 +10%啟動 trail-5% / +MA20', eq, meta))

    eq, meta = run_breakeven_stop(closes, dates, activation_pct=0.05,
                                   trail_pct=0.05, add_signal_fn=ma20_cross_up)
    results.append(('保本停利 +5%啟動 trail-5% / +MA20', eq, meta))

    # ★ ATR trailing（需要 highs/lows，若沒有就跳過）
    if highs is not None and lows is not None:
        eq, meta = run_atr_trailing(closes, dates, highs, lows,
                                     atr_mult=2.0, add_signal_fn=ma20_cross_up)
        results.append(('ATR×2 trailing / +MA20', eq, meta))

        eq, meta = run_atr_trailing(closes, dates, highs, lows,
                                     atr_mult=3.0, add_signal_fn=ma20_cross_up)
        results.append(('ATR×3 trailing / +MA20', eq, meta))

    # 對照組 Buy & Hold
    bh_eq = [c / closes[0] for c in closes]
    results.append(('Buy & Hold', bh_eq, {'n_trims': 0, 'n_adds': 0}))

    return results


def _label_regime(ret_pct: float) -> str:
    if ret_pct > 10:  return '🐂 牛市'
    if ret_pct < -5:  return '🐻 熊市'
    return '😴 盤整'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ticker', default='0050.TW',
                    help='Yahoo ticker, e.g. 0050.TW, 2330.TW, 00679B.TWO')
    ap.add_argument('--days', type=int, default=1825)
    ap.add_argument('--segments', type=int, default=0)
    ap.add_argument('--save', help='CSV 輸出')
    args = ap.parse_args()

    print(f'[trim+add] Yahoo 抓 {args.ticker} OHLC {args.days} 天...', file=sys.stderr)
    dates, opens, highs, lows, closes = fetch_yahoo_ohlc(args.ticker, args.days)
    print(f'[trim+add] 共 {len(dates)} 天: {dates[0]} → {dates[-1]}', file=sys.stderr)

    strats = build_strategies(closes, dates, highs=highs, lows=lows)

    print()
    print('━' * 110)
    print(f'{args.ticker} trim+add 回測（{dates[0]} → {dates[-1]}, '
          f'{closes[0]:.2f} → {closes[-1]:.2f}, {(closes[-1]/closes[0]-1)*100:+.2f}%）')
    print('━' * 110)
    print(f'  {"策略":<26} │ {"總報酬":>9} {"年化":>9} {"MaxDD":>8} {"Sharpe":>7} {"波動":>7} {"trim":>5} {"add":>4}')
    print('─' * 110)
    rows = []
    for name, eq, meta in strats:
        s = stats(eq)
        rows.append((name, s, meta))
        print(f'  {name:<26} │ {s["total_ret"]:>+7.2f}% {s["annual"]:>+7.2f}% '
              f'{s["mdd"]:>+7.2f}% {s["sharpe"]:>+7.2f} {s["vol"]:>+5.2f}% '
              f'{meta["n_trims"]:>5} {meta["n_adds"]:>4}')
    print('━' * 110)

    rows.sort(key=lambda r: r[1]['sharpe'], reverse=True)
    print(f'\n📊 依 Sharpe 排名：')
    for i, (name, s, _) in enumerate(rows, 1):
        bar = '🏆' if i == 1 else ('⭐' if i == 2 else '  ')
        print(f'  {i}. {bar} {name:<26} Sharpe={s["sharpe"]:+.2f}, '
              f'總報酬 {s["total_ret"]:+.1f}%, DD {s["mdd"]:+.1f}%')

    # 分段
    if args.segments and args.segments > 1:
        n = len(dates)
        chunk = n // args.segments
        win, top3 = {}, {}
        for q in range(args.segments):
            s_i = q * chunk
            e_i = (q + 1) * chunk - 1 if q < args.segments - 1 else n - 1
            sl_d  = dates[s_i:e_i + 1]
            sl_c  = closes[s_i:e_i + 1]
            ret_p = (sl_c[-1] / sl_c[0] - 1) * 100 if sl_c[0] else 0
            regime = _label_regime(ret_p)
            seg = build_strategies(sl_c, sl_d)

            seg_rows = []
            for name, eq, meta in seg:
                s = stats(eq)
                seg_rows.append((name, s, meta))
            seg_rows.sort(key=lambda r: r[1]['sharpe'], reverse=True)

            print()
            print(f'### Q{q+1}: {regime}（{sl_d[0]} → {sl_d[-1]}, 0050 {ret_p:+.2f}%）')
            print(f'  {"排名":>4}  {"策略":<26} │ {"總報酬":>8} {"DD":>8} {"Sharpe":>7} {"trim":>5} {"add":>4}')
            print('─' * 80)
            for i, (name, s, meta) in enumerate(seg_rows, 1):
                bar = '🏆' if i == 1 else ('⭐' if i == 2 else '  ')
                print(f'  {i:>4}  {bar} {name:<26} │ {s["total_ret"]:>+6.2f}% '
                      f'{s["mdd"]:>+6.2f}% {s["sharpe"]:>+6.2f} '
                      f'{meta["n_trims"]:>5} {meta["n_adds"]:>4}')
            for j, (name, _, _) in enumerate(seg_rows[:3]):
                top3[name] = top3.get(name, 0) + 1
                if j == 0: win[name] = win.get(name, 0) + 1

        print()
        print('━' * 80)
        print('跨情境統計')
        print('━' * 80)
        print('🏆 奪冠次數:')
        for k, v in sorted(win.items(), key=lambda x: -x[1]):
            print(f'   {k}: {v}')
        print('進前 3 次數:')
        for k, v in sorted(top3.items(), key=lambda x: -x[1]):
            print(f'   {k}: {v}')

    if args.save:
        with open(args.save, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['date', 'close'] + [s[0] for s in strats])
            for i, d in enumerate(dates):
                w.writerow([d, closes[i]] + [s[1][i] for s in strats])
        print(f'\n[trim+add] CSV → {args.save}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
