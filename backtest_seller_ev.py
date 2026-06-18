#!/usr/bin/env python3
"""backtest_seller_ev.py — 賣方期望損益(EV)回測：高 IV 多收的權利金，補不補得回較低勝率？

每個交易日 t 模擬「賣 1 口 call（或 put）」，履約 = 現價 ±buf%，持有 HOLD 交易日。
權利金用 Black-76（依當時 HV20 定價），到期以收盤結算內含價值。
P&L(點) = 權利金 - 到期內含；×50 = NT。
分組比較 HV 高/低（IV regime 代理）與「過熱」條件。
"""
import math
import statistics as st
import sys
from backtest import bs_price, RF, TXO_MULTIPLIER
from backtest_redblack import fetch_daily_ohlc

HOLD = 5
BUFS = (0.02, 0.03)


def analyze(src, rows):
    C = [v[3] for _, v in rows]; H = [v[1] for _, v in rows]
    dates = [d for d, _ in rows]
    n = len(rows)
    print(f'\n=== 賣方期望損益 (EV) 回測 ===')
    print(f'來源 {src} | 樣本 {n} 日 | {dates[0]} ~ {dates[-1]} | 持有 {HOLD} 交易日\n')

    ma20 = [None]*n; sd20 = [None]*n; bbU=[None]*n; bias=[None]*n; hv20=[None]*n
    for i in range(20, n):
        seg = C[i-19:i+1]
        m = sum(seg)/20; s = st.pstdev(seg)
        ma20[i]=m; sd20[i]=s; bbU[i]=m+2*s; bias[i]=(C[i]-m)/m*100
        rets = [math.log(C[k]/C[k-1]) for k in range(i-19, i+1)]
        hv20[i] = st.pstdev(rets)*math.sqrt(252)   # 小數
    hv_med = st.median([hv20[i] for i in range(20, n) if hv20[i]])
    print(f'HV20 中位 {hv_med*100:.1f}%（≥中位=高IV代理）\n')

    T = HOLD/252

    def sim(side, buf, cond):
        """side: 'call'/'put'。回傳 (n, 勝率, 均P&L點, 均權利金點, 均NT)。"""
        pnl=[]; prem=[]; wins=0
        for i in range(20, n-HOLD):
            if hv20[i] is None or not cond(i):
                continue
            S=C[i]; sig=hv20[i]; Sx=C[i+HOLD]
            if side=='call':
                K=S*(1+buf)
                p=bs_price(S,K,T,sig,is_put=False,r=RF)
                payoff=max(0.0, Sx-K)
            else:
                K=S*(1-buf)
                p=bs_price(S,K,T,sig,is_put=True,r=RF)
                payoff=max(0.0, K-Sx)
            pl=p-payoff
            pnl.append(pl); prem.append(p)
            if pl>0: wins+=1
        if len(pnl)<15: return None
        return (len(pnl), wins/len(pnl), st.mean(pnl), st.mean(prem), st.mean(pnl)*TXO_MULTIPLIER)

    conds = [
        ('全樣本',     lambda i: True),
        ('HV高',       lambda i: hv20[i]>=hv_med),
        ('HV低',       lambda i: hv20[i]<hv_med),
        ('過熱(站上軌)', lambda i: bbU[i] and C[i]>bbU[i]),
        ('過熱+HV高',  lambda i: bbU[i] and C[i]>bbU[i] and hv20[i]>=hv_med),
        ('過熱+HV低',  lambda i: bbU[i] and C[i]>bbU[i] and hv20[i]<hv_med),
    ]
    for side, slabel in (('call','賣 Call'),('put','賣 Put')):
        for buf in BUFS:
            print(f'── {slabel} 履約 {"+" if side=="call" else "-"}{buf*100:.0f}% ──')
            print(f'   {"條件":<10} {"n":>4} {"勝率":>6} {"均權利金":>8} {"均損益":>8} {"均NT":>9}')
            for name,c in conds:
                r=sim(side,buf,c)
                if not r:
                    print(f'   {name:<10} 樣本不足'); continue
                cnt,wr,mpl,mp,mnt=r
                print(f'   {name:<10} {cnt:>4} {wr*100:>5.0f}% {mp:>8.0f} {mpl:>+8.1f} {mnt:>+9.0f}')
            print()


if __name__ == '__main__':
    days = int(sys.argv[1]) if len(sys.argv)>1 else 1500
    src, rows = fetch_daily_ohlc(days)
    if len(rows)<60:
        print('資料太少', file=sys.stderr); sys.exit(1)
    analyze(src, rows)
