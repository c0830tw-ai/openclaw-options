#!/usr/bin/env python3
"""backtest_overbought.py — 過熱訊號 + 賣方勝率回測（接續連紅K研究）

(2) 乖離率 / 站上 BB 上軌 → 隔日 P(跌) 與未來 1/3/5 日報酬
(4) 賣方策略：過熱時賣 call（+buf%）在 5 個交易日後「價外作廢」(OTM) 的勝率，
    並用 HV 高/低（中位數切）當 IV 高/低 regime 代理。
資料重用 backtest_redblack.fetch_daily_ohlc（TAIEX 日 OHLC）。
"""
import math
import statistics as st
import sys
from backtest_redblack import fetch_daily_ohlc

HOLD = 5          # 賣方持有交易日（週選量級）
BUFS = (0.02, 0.03, 0.04)


def _ma(xs, i, n):
    return sum(xs[i - n + 1:i + 1]) / n


def _sd(xs, i, n):
    seg = xs[i - n + 1:i + 1]
    return st.pstdev(seg)


def _hv(C, i, n=20):
    rets = [math.log(C[k] / C[k - 1]) for k in range(i - n + 1, i + 1)]
    return st.pstdev(rets) * math.sqrt(252) * 100


def analyze(src, rows):
    dates = [d for d, _ in rows]
    O = [v[0] for _, v in rows]; H = [v[1] for _, v in rows]
    L = [v[2] for _, v in rows]; C = [v[3] for _, v in rows]
    n = len(rows)
    print(f'\n=== 過熱訊號 + 賣方勝率 回測 ===')
    print(f'來源 {src} | 樣本 {n} 日 | {dates[0]} ~ {dates[-1]}\n')

    # 預計算指標（從 i>=20 起有效）
    ma20 = [None] * n; bbU = [None] * n; bias = [None] * n; hv20 = [None] * n
    for i in range(20, n):
        m = _ma(C, i, 20); s = _sd(C, i, 20)
        ma20[i] = m; bbU[i] = m + 2 * s
        bias[i] = (C[i] - m) / m * 100
        hv20[i] = _hv(C, i, 20)

    def fwd_ret(i, k):
        return (C[i + k] - C[i]) / C[i] * 100 if i + k < n else None

    # ---------- (2) 過熱訊號 → 未來報酬 ----------
    print('── (2) 過熱訊號 → 未來報酬（收對收）──')
    # 基準
    base_d1 = [fwd_ret(i, 1) for i in range(20, n - 1)]
    base_neg = sum(1 for r in base_d1 if r is not None and r < 0) / len(base_d1)
    print(f'基準: 隔日下跌率 {base_neg*100:.1f}% | 隔日均報酬 {st.mean([r for r in base_d1 if r is not None]):+.2f}%')

    def report(name, sigfn):
        idx = [i for i in range(20, n - 1) if sigfn(i)]
        if len(idx) < 15:
            print(f'  {name}: 樣本不足 ({len(idx)})'); return
        for k in (1, 3, 5):
            rs = [fwd_ret(i, k) for i in idx if fwd_ret(i, k) is not None]
            negr = sum(1 for r in rs if r < 0) / len(rs)
            mean = st.mean(rs); med = st.median(rs)
            tag = f'未來{k}日'
            extra = f' | P(跌) {negr*100:.0f}%' if k == 1 else ''
            print(f'  {name} [{tag}] n={len(rs)}: 均 {mean:+.2f}% 中位 {med:+.2f}%{extra}')

    report('站上BB上軌  ', lambda i: bbU[i] and C[i] > bbU[i])
    report('乖離 ≥ +3%  ', lambda i: bias[i] is not None and bias[i] >= 3)
    report('乖離 ≥ +5%  ', lambda i: bias[i] is not None and bias[i] >= 5)
    report('乖離 ≥ +7%  ', lambda i: bias[i] is not None and bias[i] >= 7)

    # ---------- (4) 賣方勝率：過熱時賣 call ----------
    print(f'\n── (4) 賣 call 勝率：持有 {HOLD} 交易日，到期收盤 < 履約=贏（OTM 作廢）──')
    hv_vals = [hv20[i] for i in range(20, n) if hv20[i] is not None]
    hv_med = st.median(hv_vals)
    print(f'HV20 中位 {hv_med:.1f}%（HV≥中位=「高IV代理」）\n')

    def sell_call_winrate(buf, cond):
        win = tot = touch = 0
        for i in range(20, n - HOLD):
            if not cond(i):
                continue
            K = C[i] * (1 + buf)
            tot += 1
            if C[i + HOLD] < K:
                win += 1
            if max(H[i + 1:i + HOLD + 1]) >= K:
                touch += 1
        return win, tot, touch

    conds = [
        ('全樣本(基準)', lambda i: True),
        ('站上BB上軌',   lambda i: bbU[i] and C[i] > bbU[i]),
        ('乖離≥+5%',     lambda i: bias[i] is not None and bias[i] >= 5),
        ('過熱+HV高',    lambda i: bbU[i] and C[i] > bbU[i] and hv20[i] and hv20[i] >= hv_med),
        ('過熱+HV低',    lambda i: bbU[i] and C[i] > bbU[i] and hv20[i] and hv20[i] < hv_med),
    ]
    for buf in BUFS:
        print(f'  賣 call 履約 = 現價 +{buf*100:.0f}%：')
        for name, c in conds:
            w, t, tch = sell_call_winrate(buf, c)
            if t < 15:
                print(f'    {name:<12} 樣本不足 ({t})'); continue
            wr = w / t; se = math.sqrt(wr * (1 - wr) / t)
            print(f'    {name:<12} 勝率 {wr*100:.1f}% ±{se*100:.1f}  (n={t}, 期間曾被觸及 {tch/t*100:.0f}%)')
        print()


if __name__ == '__main__':
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    src, rows = fetch_daily_ohlc(days)
    if len(rows) < 60:
        print(f'資料太少（{len(rows)}）', file=sys.stderr); sys.exit(1)
    analyze(src, rows)
