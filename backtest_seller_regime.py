#!/usr/bin/env python3
"""backtest_seller_regime.py — 分市況(多/空/盤整) 賣方 EV，檢驗方向性偏誤

市況用 MA20 vs MA60 分：
  多頭 = MA20>MA60 且 收>MA60；空頭 = MA20<MA60 且 收<MA60；其餘=盤整。
每日模擬賣 1 口 call(+buf)/put(-buf)，持有 5 交易日，BS 依 HV20 定價。
"""
import math
import statistics as st
import sys
from backtest import bs_price, RF, TXO_MULTIPLIER
from backtest_redblack import fetch_daily_ohlc

HOLD = 5


def analyze(src, rows):
    C = [v[3] for _, v in rows]
    dates = [d for d, _ in rows]
    n = len(rows)
    print(f'\n=== 分市況 賣方 EV 回測 ===')
    print(f'來源 {src} | {n} 日 | {dates[0]} ~ {dates[-1]} | 持有 {HOLD} 交易日\n')

    ma20=[None]*n; ma60=[None]*n; hv=[None]*n; reg=[None]*n
    for i in range(60, n):
        ma20[i]=sum(C[i-19:i+1])/20
        ma60[i]=sum(C[i-59:i+1])/60
        rets=[math.log(C[k]/C[k-1]) for k in range(i-19,i+1)]
        hv[i]=st.pstdev(rets)*math.sqrt(252)
        if ma20[i]>ma60[i] and C[i]>ma60[i]:   reg[i]='多頭'
        elif ma20[i]<ma60[i] and C[i]<ma60[i]: reg[i]='空頭'
        else:                                   reg[i]='盤整'
    hv_med=st.median([hv[i] for i in range(60,n) if hv[i]])
    cnt={r:sum(1 for i in range(60,n-HOLD) if reg[i]==r) for r in ('多頭','空頭','盤整')}
    print(f'HV20 中位 {hv_med*100:.1f}% | 市況分布: ' + ' '.join(f'{r} {cnt[r]}' for r in cnt) + '\n')

    T=HOLD/252
    def sim(side, buf, cond):
        pnl=[]; wins=0
        for i in range(60,n-HOLD):
            if hv[i] is None or not cond(i): continue
            S=C[i]; sig=hv[i]; Sx=C[i+HOLD]
            if side=='call':
                K=S*(1+buf); p=bs_price(S,K,T,sig,is_put=False,r=RF); payoff=max(0.0,Sx-K)
            else:
                K=S*(1-buf); p=bs_price(S,K,T,sig,is_put=True,r=RF); payoff=max(0.0,K-Sx)
            pl=p-payoff; pnl.append(pl)
            if pl>0: wins+=1
        if len(pnl)<12: return None
        return len(pnl), wins/len(pnl), st.mean(pnl), st.mean(pnl)*TXO_MULTIPLIER

    for side,buf,slabel in (('put',0.03,'賣 Put -3%'),('call',0.03,'賣 Call +3%')):
        print(f'── {slabel}（持有 {HOLD} 日 EV）──')
        print(f'   {"市況":<6} {"n":>4} {"勝率":>6} {"均損益點":>8} {"均NT":>9}')
        for r in ('多頭','空頭','盤整'):
            res=sim(side,buf,lambda i,r=r: reg[i]==r)
            if not res: print(f'   {r:<6} 樣本不足'); continue
            c,wr,mpl,mnt=res
            print(f'   {r:<6} {c:>4} {wr*100:>5.0f}% {mpl:>+8.1f} {mnt:>+9.0f}')
        print()

    # 賣 put 再加 HV 高/低（檢驗「高IV才有edge」是否跨市況成立）
    print('── 賣 Put -3%：市況 × HV ──')
    print(f'   {"市況/HV":<10} {"n":>4} {"勝率":>6} {"均損益點":>8} {"均NT":>9}')
    for r in ('多頭','空頭','盤整'):
        for hl,hcond in (('HV高',lambda i:hv[i]>=hv_med),('HV低',lambda i:hv[i]<hv_med)):
            res=sim('put',0.03,lambda i,r=r,h=hcond: reg[i]==r and h(i))
            label=f'{r}/{hl}'
            if not res: print(f'   {label:<10} 樣本不足'); continue
            c,wr,mpl,mnt=res
            print(f'   {label:<10} {c:>4} {wr*100:>5.0f}% {mpl:>+8.1f} {mnt:>+9.0f}')
    print()


if __name__ == '__main__':
    days=int(sys.argv[1]) if len(sys.argv)>1 else 1500
    src,rows=fetch_daily_ohlc(days)
    if len(rows)<120: print('資料太少',file=sys.stderr); sys.exit(1)
    analyze(src,rows)
