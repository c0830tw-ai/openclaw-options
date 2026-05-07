#!/usr/bin/env python3
"""
一次性回填：用 Shioaji K 棒計算過去 252 個交易日的 TX HV，
推到 Firebase /trading/2330/hv_history/{YYYY-MM-DD}。

執行一次即可，之後 shioaji_collar.py 每天自動累積。

用法：
  export $(grep -v '^#' .env | xargs) && python3 backfill_hv.py
"""
import os, sys, math, json
from datetime import datetime, timedelta

import shioaji as sj
import firebase_admin
from firebase_admin import credentials, db

FIREBASE_CRED = os.environ.get('FIREBASE_CRED', './firebase-key.json')
FIREBASE_URL  = os.environ.get('FIREBASE_URL', '')
API_KEY       = os.environ.get('SHIOAJI_API_KEY', '')
API_SECRET    = os.environ.get('SHIOAJI_SECRET_KEY', '')
HV_PATH       = '/trading/2330/hv_history'
HV_PERIOD     = 20   # 計算 HV 用的 K 棒數（20日）

def init_firebase():
    if firebase_admin._apps:
        return True
    if not FIREBASE_URL or not os.path.exists(FIREBASE_CRED):
        print('Firebase config missing'); return False
    cred = credentials.Certificate(FIREBASE_CRED)
    firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_URL})
    return True

def calc_hv(closes: list, period: int = HV_PERIOD) -> float:
    n = min(period, len(closes) - 1)
    if n < 2:
        return 0.0
    rets = [math.log(closes[-n + i] / closes[-n + i - 1]) for i in range(n)]
    mean = sum(rets) / n
    var  = sum((r - mean) ** 2 for r in rets) / n
    return math.sqrt(var * 252)

def main():
    if not API_KEY or not API_SECRET:
        sys.exit('Missing SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY')
    if not init_firebase():
        sys.exit('Firebase init failed')

    # 已有的 HV 歷史
    existing = db.reference(HV_PATH).get() or {}
    print(f'Firebase 已有 {len(existing)} 筆 HV 記錄')

    api = sj.Shioaji()
    api.login(API_KEY, API_SECRET, simulation=False)
    api.fetch_contracts(contract_download=True)

    contract = api.Contracts.Futures.TXF
    # 找滾動合約 TXFR1
    roll = next((c for c in contract if c.symbol == 'TXFR1'), None)
    if roll is None:
        # fallback: 取第一個正式合約
        roll = next((c for c in contract
                     if c.symbol.startswith('TXF') and len(c.symbol) == 9), None)
    if roll is None:
        api.logout(); sys.exit('Cannot find TXF contract')

    today = datetime.now().date()
    start = (today - timedelta(days=400)).strftime('%Y-%m-%d')
    end   = today.strftime('%Y-%m-%d')

    print(f'抓取 TX K棒 {start} → {end} …')
    kbars = api.kbars(roll, start=start, end=end)
    if not kbars or not kbars.Close:
        api.logout(); sys.exit('No kbar data')

    closes = list(kbars.Close)
    ts_raw = list(kbars.ts)
    print(f'共 {len(closes)} 根 K 棒')

    # 每根 K 棒對應的日期
    dates = [datetime.fromtimestamp(t / 1e9).strftime('%Y-%m-%d') for t in ts_raw]

    pushed = 0
    for i in range(HV_PERIOD + 1, len(closes)):
        date_str = dates[i]
        if date_str in existing:
            continue
        hv = calc_hv(closes[:i + 1], HV_PERIOD)
        if hv > 0:
            db.reference(f'{HV_PATH}/{date_str}').set({'hv': round(hv, 4)})
            pushed += 1

    print(f'新增 {pushed} 筆歷史 HV')

    # 驗證
    updated = db.reference(HV_PATH).get() or {}
    print(f'Firebase 現有 {len(updated)} 筆 HV 記錄')
    api.logout()

if __name__ == '__main__':
    main()
