"""
shioaji_collar.py
=================
自動抓取 2330 + TXO 選擇權鏈，計算最佳領式結構，推送到 Firebase。

Designed for OpenClaw daily/intraday automation.

執行方式:
    python shioaji_collar.py

環境變數需求:
    SHIOAJI_API_KEY      - 永豐 API Key
    SHIOAJI_SECRET_KEY   - 永豐 Secret Key
    FIREBASE_CRED        - Firebase service account JSON 路徑 (預設 ./firebase-key.json)
    FIREBASE_URL         - Firebase RTDB URL

cron 範例 (台灣時間每小時 5 分執行，僅交易時段):
    5 9-13 * * 1-5  cd /path/to/openclaw && /usr/bin/python3 shioaji_collar.py >> logs/collar.log 2>&1
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta, time as dtime
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict

import shioaji as sj
from shioaji.constant import OptionRight

import firebase_admin
from firebase_admin import credentials, db


# ============ Logging ============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('collar')


# ============ Config ============
@dataclass
class Config:
    # Shioaji
    api_key: str = os.environ.get('SHIOAJI_API_KEY', '')
    secret_key: str = os.environ.get('SHIOAJI_SECRET_KEY', '')

    # Firebase
    firebase_cred: str = os.environ.get('FIREBASE_CRED', './firebase-key.json')
    firebase_url: str = os.environ.get('FIREBASE_URL', '')
    firebase_path: str = '/trading/2330/collar/latest'
    firebase_history_path: str = '/trading/2330/collar/history'

    # 策略參數
    beta: float = 1.18                    # 2330 對加權的 beta
    large_futures_lots: int = 1           # 大台積電股期口數
    protect_pct_2330: float = -0.157      # 下檔保護目標 (2330 跌 15.7%)
    cap_pct_2330: float = 0.10            # 上檔讓出目標 (2330 漲 10%)
    target_protection_min: float = 0.70   # 下檔保護下限
    target_protection_max: float = 0.80   # 下檔保護上限

    # 選月策略：距結算 < N 天就用次月
    days_to_next_month: int = 5

    # 輸出
    local_output: str = './latest_collar.json'


CFG = Config()


# ============ Data Classes ============
@dataclass
class CollarStructure:
    name: str
    desc: str
    calls: int
    puts: int
    call_strike: float
    put_strike: float
    call_premium: float       # bid (we receive when selling)
    put_premium: float        # ask (we pay when buying)
    monthly_net: float        # NT$
    protection_pct: float     # 0-100
    is_net_credit: bool


# ============ Helpers ============
def is_market_hours() -> bool:
    """台股交易時間 09:00-13:30。"""
    now = datetime.now().time()
    return dtime(9, 0) <= now <= dtime(13, 30)


def get_target_txo_month(today: Optional[datetime] = None) -> str:
    """
    決定要抓哪個月的 TXO。
    距本月結算日 < days_to_next_month 天 → 用次月。
    結算日為當月第三個週三。
    """
    today = today or datetime.now()
    first = today.replace(day=1)
    days_to_first_wed = (2 - first.weekday()) % 7
    third_wed = first + timedelta(days=days_to_first_wed + 14)

    if (third_wed.date() - today.date()).days < CFG.days_to_next_month:
        # 進入結算前一週，使用次月合約
        next_month_first = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        return next_month_first.strftime('%Y%m')
    return today.strftime('%Y%m')


def find_nearest_strike(contracts: list, target_strike: float, opt_right) -> Any:
    """從合約清單中找最接近目標履約價的合約。"""
    candidates = [c for c in contracts if c.option_right == opt_right]
    if not candidates:
        raise ValueError(f'No contracts found for {opt_right}')
    return min(candidates, key=lambda c: abs(c.strike_price - target_strike))


def compute_target_strikes(price_2330: float, taiex: float) -> Dict[str, float]:
    """根據 2330 目標保護/讓出比例，反推 TAIEX 目標履約價。"""
    target_put_taiex = taiex * (1 + CFG.protect_pct_2330 / CFG.beta)
    target_call_taiex = taiex * (1 + CFG.cap_pct_2330 / CFG.beta)

    return {
        'put_strike': round(target_put_taiex / 50) * 50,
        'call_strike': round(target_call_taiex / 50) * 50,
    }


def compute_contract_count(price_2330: float, taiex: float) -> Dict[str, Any]:
    """計算對應 1 口大期的 TXO 口數需求 (Beta 調整)。"""
    notional = CFG.large_futures_lots * 2000 * price_2330
    txo_notional = taiex * 50
    raw_ratio = notional / txo_notional
    beta_adj_ratio = raw_ratio * CFG.beta

    # 找符合 70-80% 保護的整數口數
    floor_n = int(beta_adj_ratio)
    ceil_n = floor_n + 1
    floor_coverage = floor_n / beta_adj_ratio
    ceil_coverage = ceil_n / beta_adj_ratio

    if CFG.target_protection_min <= floor_coverage <= CFG.target_protection_max:
        recommended = floor_n
    elif CFG.target_protection_min <= ceil_coverage <= CFG.target_protection_max:
        recommended = ceil_n
    else:
        # 都不在範圍內，選最接近區間中位
        target = (CFG.target_protection_min + CFG.target_protection_max) / 2
        recommended = floor_n if abs(floor_coverage - target) < abs(ceil_coverage - target) else ceil_n

    return {
        'notional': notional,
        'beta_adjusted_ratio': beta_adj_ratio,
        'recommended_contracts': max(1, recommended),
    }


def build_structures(
    n_contracts: int,
    call_strike: float,
    put_strike: float,
    call_bid: float,
    put_ask: float,
    beta_adj_ratio: float,
) -> List[CollarStructure]:
    """產生三種結構的詳細資料。"""

    def make(name: str, desc: str, calls: int, puts: int) -> CollarStructure:
        call_income = call_bid * 50 * calls
        put_cost = put_ask * 50 * puts
        net = call_income - put_cost
        protection = (puts / beta_adj_ratio * 100) if puts > 0 else 0.0
        return CollarStructure(
            name=name,
            desc=desc,
            calls=calls,
            puts=puts,
            call_strike=call_strike,
            put_strike=put_strike,
            call_premium=call_bid,
            put_premium=put_ask,
            monthly_net=net,
            protection_pct=min(100.0, protection),
            is_net_credit=(net > 0),
        )

    return [
        make('symmetric', '對稱領式 NC/NP', n_contracts, n_contracts),
        make('skewed', '偏賣方 2C/1P', n_contracts, max(1, n_contracts // 2)),
        make('covered_call', '純 Covered Call', n_contracts, 0),
    ]


# ============ Shioaji Operations ============
def fetch_market_snapshot(api):
    """抓 2330 和加權指數。"""
    c_2330 = api.Contracts.Stocks.TSE['2330']
    c_taiex = api.Contracts.Indexs.TSE['TSE001']  # 加權指數

    snaps = api.snapshots([c_2330, c_taiex])
    snap_2330, snap_taiex = snaps[0], snaps[1]

    return {
        'price_2330': float(snap_2330.close),
        'price_2330_open': float(snap_2330.open) if snap_2330.open else None,
        'price_2330_high': float(snap_2330.high) if snap_2330.high else None,
        'price_2330_low': float(snap_2330.low) if snap_2330.low else None,
        'taiex': float(snap_taiex.close),
        'volume_2330': int(snap_2330.volume) if snap_2330.volume else 0,
    }


def fetch_txo_chain(api, month: str) -> list:
    """抓指定月份的 TXO 合約清單。

    Contracts.Options.TXO 包含所有月份，用 delivery_month 欄位篩選。
    month 格式為 YYYYMM（例如 '202506'）。
    """
    all_contracts = list(api.Contracts.Options.TXO)
    chain = [c for c in all_contracts if c.delivery_month == month]
    if not chain:
        available = sorted({c.delivery_month for c in all_contracts})
        log.warning(f'TXO {month} 無合約，可用月份: {available}')
        raise ValueError(f'TXO month {month} not available. Available: {available}')
    log.info(f'TXO {month} chain has {len(chain)} contracts')
    return chain


def fetch_option_quotes(api, put_contract, call_contract) -> Dict[str, float]:
    """抓 put/call 的 bid/ask。"""
    snaps = api.snapshots([put_contract, call_contract])
    put_snap, call_snap = snaps[0], snaps[1]

    # 我們買 put → 付 ask；賣 call → 收 bid
    # Shioaji snapshot 欄位: sell_price = ask, buy_price = bid
    put_ask = float(put_snap.sell_price) if put_snap.sell_price else float(put_snap.close)
    call_bid = float(call_snap.buy_price) if call_snap.buy_price else float(call_snap.close)
    put_mid = float(put_snap.close)
    call_mid = float(call_snap.close)

    return {
        'put_ask': put_ask,
        'put_mid': put_mid,
        'put_bid': float(put_snap.buy_price) if put_snap.buy_price else put_mid,
        'call_bid': call_bid,
        'call_mid': call_mid,
        'call_ask': float(call_snap.sell_price) if call_snap.sell_price else call_mid,
        'put_volume': int(put_snap.volume) if put_snap.volume else 0,
        'call_volume': int(call_snap.volume) if call_snap.volume else 0,
    }


# ============ Firebase ============
def init_firebase() -> bool:
    if firebase_admin._apps:
        return True
    if not CFG.firebase_url or not os.path.exists(CFG.firebase_cred):
        log.warning('Firebase config missing, skipping Firebase push')
        return False
    cred = credentials.Certificate(CFG.firebase_cred)
    firebase_admin.initialize_app(cred, {'databaseURL': CFG.firebase_url})
    return True


def push_to_firebase(data: dict):
    if not init_firebase():
        return
    # 即時最新
    db.reference(CFG.firebase_path).set(data)
    # 歷史記錄 (key = timestamp)
    history_key = data['timestamp'].replace(':', '-').replace('.', '-')
    db.reference(f'{CFG.firebase_history_path}/{history_key}').set({
        'price_2330': data['market']['price_2330'],
        'taiex': data['market']['taiex'],
        'recommended_monthly_net': data['structures'][0]['monthly_net'],
    })
    log.info(f'Pushed to {CFG.firebase_path}')


# ============ Main ============
def main():
    if not CFG.api_key or not CFG.secret_key:
        log.error('SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY not set')
        sys.exit(1)

    if not is_market_hours():
        log.warning('Outside market hours, prices will be last close. Continuing anyway...')

    log.info('=' * 60)
    log.info('Shioaji Collar Fetcher')
    log.info('=' * 60)

    api = sj.Shioaji(simulation=False)
    api.login(CFG.api_key, CFG.secret_key, contracts_timeout=30000)
    log.info('Shioaji logged in')

    try:
        # 1. 抓現價
        market = fetch_market_snapshot(api)
        log.info(f"2330: {market['price_2330']} | TAIEX: {market['taiex']}")

        # 2. 計算目標
        targets = compute_target_strikes(market['price_2330'], market['taiex'])
        contracts = compute_contract_count(market['price_2330'], market['taiex'])
        log.info(f"Target strikes: Put @ {targets['put_strike']}, Call @ {targets['call_strike']}")
        log.info(f"Contract count: {contracts['recommended_contracts']} (ratio {contracts['beta_adjusted_ratio']:.2f})")

        # 3. 抓 TXO 鏈
        month = get_target_txo_month()
        log.info(f'Using TXO month: {month}')
        chain = fetch_txo_chain(api, month)

        # 4. 找實際履約價
        put_c = find_nearest_strike(chain, targets['put_strike'], OptionRight.Put)
        call_c = find_nearest_strike(chain, targets['call_strike'], OptionRight.Call)
        log.info(f'Selected strikes: Put @ {put_c.strike_price}, Call @ {call_c.strike_price}')

        # 5. 抓選擇權報價
        quotes = fetch_option_quotes(api, put_c, call_c)
        log.info(f"Put ask: {quotes['put_ask']}, Call bid: {quotes['call_bid']}")

        # 6. 計算結構
        structures = build_structures(
            n_contracts=contracts['recommended_contracts'],
            call_strike=call_c.strike_price,
            put_strike=put_c.strike_price,
            call_bid=quotes['call_bid'],
            put_ask=quotes['put_ask'],
            beta_adj_ratio=contracts['beta_adjusted_ratio'],
        )

        # 7. 組裝結果
        result = {
            'timestamp': datetime.now().isoformat(),
            'config': {
                'beta': CFG.beta,
                'large_futures_lots': CFG.large_futures_lots,
                'protect_pct_2330': CFG.protect_pct_2330,
                'cap_pct_2330': CFG.cap_pct_2330,
            },
            'market': market,
            'txo_month': month,
            'targets': {
                'target_put_strike': targets['put_strike'],
                'target_call_strike': targets['call_strike'],
                'beta_adjusted_ratio': contracts['beta_adjusted_ratio'],
            },
            'selected_options': {
                'put': {
                    'strike': put_c.strike_price,
                    'symbol': put_c.symbol,
                    'ask': quotes['put_ask'],
                    'bid': quotes['put_bid'],
                    'mid': quotes['put_mid'],
                    'volume': quotes['put_volume'],
                },
                'call': {
                    'strike': call_c.strike_price,
                    'symbol': call_c.symbol,
                    'ask': quotes['call_ask'],
                    'bid': quotes['call_bid'],
                    'mid': quotes['call_mid'],
                    'volume': quotes['call_volume'],
                },
            },
            'structures': [asdict(s) for s in structures],
        }

        # 8. 輸出
        log.info('-' * 60)
        log.info('Result:')
        for s in structures:
            net_man = s.monthly_net / 10000
            log.info(f'  {s.name:20s} {s.calls}C/{s.puts}P  '
                     f'淨收 {net_man:+.1f}萬  保護 {s.protection_pct:.0f}%  '
                     f'{"✓ credit" if s.is_net_credit else "✗ debit"}')

        # 9. 寫本地檔
        with open(CFG.local_output, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        log.info(f'Local: {CFG.local_output}')

        # 10. 推 Firebase
        push_to_firebase(result)

    except Exception as e:
        log.exception(f'Error: {e}')
        raise
    finally:
        api.logout()
        log.info('Logged out')


if __name__ == '__main__':
    main()
