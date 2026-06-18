#!/usr/bin/env python3
"""backtest_redblack.py — 大盤連 N 根紅K 後，隔日黑K 機率回測

紅K = 收盤 > 開盤；黑K = 收盤 < 開盤（台股慣例）。
另外也用「上漲日/下跌日」(今收 vs 昨收) 做對照定義。
資料：Shioaji kbars 抓 TAIEX(TSE001) 日 OHLC，抓不到改用 TXFR1 連續期貨。
"""
import os
import sys
from collections import OrderedDict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest import _load_env   # 重用 .env 載入


def fetch_daily_ohlc(days: int = 1500):
    _load_env()
    import shioaji as sj
    print('[rb] Shioaji login...', file=sys.stderr)
    api = sj.Shioaji()
    api.login(api_key=os.environ['SHIOAJI_API_KEY'],
              secret_key=os.environ['SHIOAJI_SECRET_KEY'],
              contracts_timeout=60_000)

    # 優先大盤指數 TSE001，失敗改 TXFR1 連續期貨
    contract, src = None, None
    try:
        c = api.Contracts.Indexs.TSE['TSE001']
        if c is not None:
            contract, src = c, 'TAIEX(TSE001)'
    except Exception:
        pass
    if contract is None:
        contract = next((c for c in api.Contracts.Futures.TXF if c.symbol == 'TXFR1'), None)
        src = 'TXFR1(連續期貨)'
    if contract is None:
        api.logout(); raise RuntimeError('找不到合約')
    print(f'[rb] 資料來源: {src}', file=sys.stderr)

    daily = OrderedDict()  # date -> [o,h,l,c]
    end = datetime.now().date()
    remaining = days
    while remaining > 0:
        chunk = min(60, remaining)
        start = end - timedelta(days=chunk)
        try:
            kb = api.kbars(contract=contract,
                           start=start.strftime('%Y-%m-%d'),
                           end=end.strftime('%Y-%m-%d'))
            ts, O, H, L, C = list(kb.ts), list(kb.Open), list(kb.High), list(kb.Low), list(kb.Close)
            for i, t in enumerate(ts):
                d = datetime.fromtimestamp(t / 1e9).date()
                if d not in daily:
                    daily[d] = [O[i], H[i], L[i], C[i]]
                else:
                    daily[d][1] = max(daily[d][1], H[i])
                    daily[d][2] = min(daily[d][2], L[i])
                    daily[d][3] = C[i]
            print(f'[rb] {start}→{end}: 累計 {len(daily)} 日', file=sys.stderr)
        except Exception as e:
            print(f'[rb] chunk 失敗 {start}: {e}', file=sys.stderr)
            break
        end = start - timedelta(days=1)
        remaining -= chunk
    api.logout()
    rows = sorted(daily.items())
    return src, rows


def _streak_next(colors, target='down', run=3, prev='up'):
    """連續 run 根 prev 之後，下一根為 target 的次數/總數。"""
    hit = tot = 0
    for i in range(run, len(colors)):
        if all(colors[i - 1 - j] == prev for j in range(run)):
            tot += 1
            if colors[i] == target:
                hit += 1
    return hit, tot


def analyze(src, rows):
    import math
    dates = [d for d, _ in rows]
    O = [v[0] for _, v in rows]; C = [v[3] for _, v in rows]
    n = len(rows)
    print(f'\n=== 大盤連紅K後黑K 回測 ===')
    print(f'來源 {src} | 樣本 {n} 個交易日 | {dates[0]} ~ {dates[-1]}\n')

    # --- 定義1：紅/黑K（收 vs 開）---
    col1 = ['up' if C[i] > O[i] else 'down' if C[i] < O[i] else 'flat' for i in range(n)]
    up1 = col1.count('up'); dn1 = col1.count('down'); fl1 = col1.count('flat')
    print('[定義A] 紅K=收>開, 黑K=收<開')
    print(f'  紅K {up1} ({up1/n*100:.1f}%) | 黑K {dn1} ({dn1/n*100:.1f}%) | 平盤 {fl1}')
    base = dn1 / n
    for run in (2, 3, 4):
        hit, tot = _streak_next(col1, 'down', run, 'up')
        if tot:
            p = hit / tot
            se = math.sqrt(p * (1 - p) / tot)
            print(f'  連{run}紅後 → 隔日黑K: {hit}/{tot} = {p*100:.1f}%  (基準 {base*100:.1f}%, 差 {(p-base)*100:+.1f}pp, ±{se*100:.1f}pp)')
        else:
            print(f'  連{run}紅: 樣本不足')
    # 連3紅後繼續紅（動能 vs 反轉）
    h3, t3 = _streak_next(col1, 'up', 3, 'up')
    if t3:
        print(f'  連3紅後 → 隔日續紅: {h3}/{t3} = {h3/t3*100:.1f}%')

    # --- 定義2：上漲/下跌日（今收 vs 昨收）---
    col2 = ['up' if C[i] > C[i-1] else 'down' if C[i] < C[i-1] else 'flat' for i in range(1, n)]
    n2 = len(col2)
    up2 = col2.count('up'); dn2 = col2.count('down')
    print('\n[定義B] 上漲日=今收>昨收, 下跌日=今收<昨收')
    print(f'  上漲 {up2} ({up2/n2*100:.1f}%) | 下跌 {dn2} ({dn2/n2*100:.1f}%)')
    base2 = dn2 / n2
    for run in (2, 3, 4):
        hit, tot = _streak_next(col2, 'down', run, 'up')
        if tot:
            p = hit / tot
            se = math.sqrt(p * (1 - p) / tot)
            print(f'  連{run}漲後 → 隔日跌: {hit}/{tot} = {p*100:.1f}%  (基準 {base2*100:.1f}%, 差 {(p-base2)*100:+.1f}pp, ±{se*100:.1f}pp)')

    # --- 隔日報酬（連3紅後，收對收）---
    rets = []
    for i in range(3, n):
        if all(col1[i-1-j] == 'up' for j in range(3)) and i + 1 < n:
            rets.append((C[i] - C[i-1]) / C[i-1] * 100)  # 連3紅當日後一天... 用 i 當隔日
    # 連3紅之後那一天的報酬
    fwd = []
    for i in range(3, n):
        if all(col1[i-1-j] == 'up' for j in range(3)):
            fwd.append((C[i] - C[i-1]) / C[i-1] * 100)
    if fwd:
        fwd.sort()
        mean = sum(fwd) / len(fwd)
        med = fwd[len(fwd)//2]
        print(f'\n  連3紅後「隔日報酬」(收對收): 平均 {mean:+.2f}% | 中位 {med:+.2f}% | n={len(fwd)}')
        print(f'    最差 {fwd[0]:+.2f}% | 最好 {fwd[-1]:+.2f}%')


if __name__ == '__main__':
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    src, rows = fetch_daily_ohlc(days)
    if len(rows) < 30:
        print(f'資料太少（{len(rows)} 日），無法分析', file=sys.stderr); sys.exit(1)
    analyze(src, rows)
