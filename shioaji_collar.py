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
import math
import logging
from datetime import datetime, timedelta, time as dtime
from typing import Optional, List, Dict, Any, Tuple
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

    # 策略參數 — 2330
    beta: float = 1.18                    # 2330 對加權的 beta
    large_futures_lots: int = 1           # 大台積電股期口數
    target_protection_min: float = 0.70   # 下檔保護下限
    target_protection_max: float = 0.80   # 下檔保護上限

    # 策略參數 — 0050
    beta_0050: float = 0.97               # 0050 對加權的 beta
    lots_0050: int = 2                    # 0050 股期口數
    lot_size_0050: int = 10000            # 每口受益憑證單位數

    # 動態履約價選擇
    delta_target: float = 0.10            # Put/Call delta 絕對值上限
    risk_free_rate: float = 0.0175        # 無風險利率（TWD 年化）
    atr_period: int = 14
    bb_period: int = 20
    atr_mult_base: float = 1.5            # ATR 乘數（DTE >= 20 天）

    # 靜態備援（kbar 失敗時使用）
    protect_pct_2330: float = -0.157
    cap_pct_2330: float = 0.10

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


def get_txo_settlement(today: Optional[datetime] = None) -> Tuple[str, datetime]:
    """返回 (月份 YYYYMM, 結算日)。結算日為當月第三個週三。"""
    today = today or datetime.now()

    def third_wed_of(dt: datetime) -> datetime:
        first = dt.replace(day=1)
        days_to_wed = (2 - first.weekday()) % 7
        return first + timedelta(days=days_to_wed + 14)

    settlement = third_wed_of(today)
    if (settlement.date() - today.date()).days < CFG.days_to_next_month:
        next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        settlement = third_wed_of(next_month)
        return next_month.strftime('%Y%m'), settlement
    return today.strftime('%Y%m'), settlement


def get_target_txo_month(today: Optional[datetime] = None) -> str:
    month, _ = get_txo_settlement(today)
    return month


# ── 技術指標計算 ──────────────────────────────────────────────

def _calc_atr(highs: list, lows: list, closes: list, period: int) -> float:
    trs = [max(highs[i] - lows[i],
               abs(highs[i] - closes[i - 1]),
               abs(lows[i] - closes[i - 1]))
           for i in range(1, len(closes))]
    n = min(period, len(trs))
    return sum(trs[-n:]) / n if n else 0.0


def _calc_bollinger(closes: list, period: int) -> Tuple[float, float, float]:
    n = min(period, len(closes))
    window = closes[-n:]
    ma = sum(window) / n
    std = math.sqrt(sum((x - ma) ** 2 for x in window) / n)
    return ma, ma + 2 * std, ma - 2 * std   # (ma, upper, lower)


def _calc_hv(closes: list, period: int) -> float:
    """年化歷史波動率（對數報酬）。"""
    n = min(period, len(closes) - 1)
    if n < 2:
        return 0.25
    rets = [math.log(closes[-n + i] / closes[-n + i - 1]) for i in range(n)]
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n
    return math.sqrt(var * 252)


# ── Black-Scholes Delta ───────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return (1 + math.erf(x / math.sqrt(2))) / 2


def bs_delta(S: float, K: float, T: float, sigma: float, is_put: bool) -> float:
    """
    S: 標的現價（TAIEX）  K: 履約價  T: 到期年數
    sigma: 年化波動率     returns: put delta（負值）或 call delta（正值）
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (CFG.risk_free_rate + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return _norm_cdf(d1) - 1 if is_put else _norm_cdf(d1)
    except (ValueError, ZeroDivisionError):
        return 0.0


# ── 履約價選擇 ────────────────────────────────────────────────

def compute_target_strikes(
    price_2330: float, taiex: float, indicators: Optional[dict], dte: int
) -> Dict[str, Any]:
    """
    動態：BB 下/上軌 + ATR × 乘數（隨 DTE 縮放），轉換為 TAIEX 目標點。
    fallback：kbar 失敗時用 Config 靜態比例。
    """
    if indicators:
        mult = CFG.atr_mult_base * (0.75 + 0.25 * min(dte, 20) / 20)
        atr  = indicators['atr']

        # Put：BB 下軌 vs ATR 下界，取較低者（更保守），但必須 < 現價
        put_2330_bb  = min(indicators['bb_lower'], price_2330 - 1)
        put_2330_atr = price_2330 - mult * atr
        put_2330     = min(put_2330_bb, put_2330_atr)

        # Call：BB 上軌 vs ATR 上界，取較高者（更保守），但必須 > 現價
        call_2330_bb  = max(indicators['bb_upper'], price_2330 + 1)
        call_2330_atr = price_2330 + mult * atr
        call_2330     = max(call_2330_bb, call_2330_atr)

        r_put  = (put_2330  - price_2330) / price_2330
        r_call = (call_2330 - price_2330) / price_2330
        method = 'bb_atr'
    else:
        r_put  = CFG.protect_pct_2330
        r_call = CFG.cap_pct_2330
        mult   = CFG.atr_mult_base
        method = 'static_fallback'

    return {
        'put_strike':  round(taiex * (1 + r_put  / CFG.beta) / 50) * 50,
        'call_strike': round(taiex * (1 + r_call / CFG.beta) / 50) * 50,
        'atr_mult': mult,
        'method': method,
    }


def find_strike_with_delta(
    contracts: list,
    taiex: float,
    T: float,
    hv_taiex: float,
    opt_right,
    preferred_strike: float,
) -> Any:
    """
    從合約鏈挑履約價：
      1. 以 preferred_strike 為起點
      2. 往 OTM 方向走，找第一個 abs(delta) <= delta_target 的合約
      3. 全部超標時退回最 OTM 選項
    """
    is_put = (opt_right == OptionRight.Put)
    candidates = [c for c in contracts if c.option_right == opt_right]
    if not candidates:
        raise ValueError(f'No contracts for {opt_right}')

    nearest = min(candidates, key=lambda c: abs(c.strike_price - preferred_strike))

    if is_put:
        search = sorted(
            [c for c in candidates if c.strike_price <= nearest.strike_price],
            key=lambda c: c.strike_price, reverse=True,
        )
    else:
        search = sorted(
            [c for c in candidates if c.strike_price >= nearest.strike_price],
            key=lambda c: c.strike_price,
        )

    if not search:
        search = [nearest]

    label = 'Put' if is_put else 'Call'
    for contract in search:
        d = bs_delta(taiex, contract.strike_price, T, hv_taiex, is_put)
        if abs(d) <= CFG.delta_target:
            log.info(f'  {label} {contract.strike_price:.0f}: delta={d:+.3f} ✓')
            return contract
        log.debug(f'  {label} {contract.strike_price:.0f}: delta={d:+.3f} > {CFG.delta_target}，往 OTM 移')

    fallback = search[-1]
    d = bs_delta(taiex, fallback.strike_price, T, hv_taiex, is_put)
    log.warning(f'  {label} delta 全超標，取最 OTM {fallback.strike_price:.0f}: delta={d:+.3f}')
    return fallback


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
def _resample_daily(kbars) -> dict:
    """
    Shioaji kbars 固定回傳 1 分鐘 K，重採樣成日 K。
    ts 欄位為 nanosecond epoch。
    """
    daily: dict = {}
    for i, ts_ns in enumerate(kbars.ts):
        d = datetime.fromtimestamp(ts_ns / 1e9).date()
        o, h, l, c = kbars.Open[i], kbars.High[i], kbars.Low[i], kbars.Close[i]
        if d not in daily:
            daily[d] = [o, h, l, c]
        else:
            daily[d][1] = max(daily[d][1], h)
            daily[d][2] = min(daily[d][2], l)
            daily[d][3] = c  # 最後一根的收盤

    days = sorted(daily)
    return {
        'Open':  [daily[d][0] for d in days],
        'High':  [daily[d][1] for d in days],
        'Low':   [daily[d][2] for d in days],
        'Close': [daily[d][3] for d in days],
        'days':  len(days),
    }


def fetch_kbars_2330(api) -> Optional[dict]:
    """
    抓 2330 分鐘 K 棒後重採樣為日K，計算 ATR / BB / HV。
    失敗時回傳 None（主流程退回靜態備援）。
    """
    try:
        contract = api.Contracts.Stocks.TSE['2330']
        end_dt   = datetime.now()
        # bb_period 個交易日 ≈ 1.5 倍日曆天
        start_dt = end_dt - timedelta(days=int(CFG.bb_period * 1.5 * 7 / 5) + 10)
        kbars = api.kbars(
            contract=contract,
            start=start_dt.strftime('%Y-%m-%d'),
            end=end_dt.strftime('%Y-%m-%d'),
        )
        daily  = _resample_daily(kbars)
        closes = daily['Close']
        highs  = daily['High']
        lows   = daily['Low']
        n_days = daily['days']

        if n_days < CFG.bb_period + 1:
            log.warning(f'日K不足（{n_days} 天），退回靜態備援')
            return None

        atr            = _calc_atr(highs, lows, closes, CFG.atr_period)
        ma, bb_u, bb_l = _calc_bollinger(closes, CFG.bb_period)
        hv_2330        = _calc_hv(closes, CFG.bb_period)
        hv_taiex       = hv_2330 / CFG.beta

        log.info(
            f'日K {n_days}根  ATR({CFG.atr_period})={atr:.1f}  '
            f'BB上={bb_u:.1f} 中={ma:.1f} 下={bb_l:.1f}  '
            f'HV={hv_2330:.1%}→TAIEX≈{hv_taiex:.1%}'
        )
        return {
            'atr': atr, 'bb_upper': bb_u, 'bb_lower': bb_l,
            'ma20': ma, 'hv_2330': hv_2330, 'hv_taiex': hv_taiex,
            'days': n_days,
        }
    except Exception as e:
        log.warning(f'kbar 抓取失敗（{e}），退回靜態備援')
        return None


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
        price_2330 = market['price_2330']
        taiex = market['taiex']
        snap_0050 = api.snapshots([api.Contracts.Stocks.TSE['0050']])[0]
        price_0050 = float(snap_0050.close)
        log.info(f'2330: {price_2330} | TAIEX: {taiex} | 0050: {price_0050}')

        # 2. K棒指標（ATR / BB / HV）
        indicators = fetch_kbars_2330(api)

        # 3. DTE（距結算天數）
        month, settlement_dt = get_txo_settlement()
        dte = max(0, (settlement_dt.date() - datetime.now().date()).days)
        log.info(f'TXO month: {month}  結算: {settlement_dt.date()}  DTE: {dte}')

        # 4. 動態履約價目標
        targets = compute_target_strikes(price_2330, taiex, indicators, dte)
        log.info(f"目標履約價: Put @ {targets['put_strike']}  Call @ {targets['call_strike']}  [{targets['method']}]")

        # 5. 口數
        contracts = compute_contract_count(price_2330, taiex)
        log.info(f"口數: {contracts['recommended_contracts']} (beta_ratio {contracts['beta_adjusted_ratio']:.2f})")

        # 6. 抓 TXO 鏈
        chain = fetch_txo_chain(api, month)

        # 7. 選履約價（含 delta 過濾）
        hv_taiex = indicators['hv_taiex'] if indicators else 0.20
        T = dte / 365
        log.info(f'Delta 篩選: target ≤ {CFG.delta_target}  HV_TAIEX={hv_taiex:.1%}  T={T:.3f}y')
        put_c  = find_strike_with_delta(chain, taiex, T, hv_taiex, OptionRight.Put,  targets['put_strike'])
        call_c = find_strike_with_delta(chain, taiex, T, hv_taiex, OptionRight.Call, targets['call_strike'])

        put_delta  = bs_delta(taiex, put_c.strike_price,  T, hv_taiex, is_put=True)
        call_delta = bs_delta(taiex, call_c.strike_price, T, hv_taiex, is_put=False)
        log.info(f'最終: Put {put_c.strike_price:.0f} (δ={put_delta:+.3f})  Call {call_c.strike_price:.0f} (δ={call_delta:+.3f})')

        # 8. 抓選擇權報價
        quotes = fetch_option_quotes(api, put_c, call_c)
        log.info(f"Put ask: {quotes['put_ask']}  Call bid: {quotes['call_bid']}")

        # 9. 計算結構
        structures = build_structures(
            n_contracts=contracts['recommended_contracts'],
            call_strike=call_c.strike_price,
            put_strike=put_c.strike_price,
            call_bid=quotes['call_bid'],
            put_ask=quotes['put_ask'],
            beta_adj_ratio=contracts['beta_adjusted_ratio'],
        )

        # 10. 0050 股期 2口 對應 TXO 領式
        notional_0050    = CFG.lots_0050 * CFG.lot_size_0050 * price_0050
        beta_ratio_0050  = (notional_0050 * CFG.beta_0050) / (taiex * 50)
        n_contracts_0050 = max(1, round(beta_ratio_0050))
        structures_0050  = build_structures(
            n_contracts=n_contracts_0050,
            call_strike=call_c.strike_price,
            put_strike=put_c.strike_price,
            call_bid=quotes['call_bid'],
            put_ask=quotes['put_ask'],
            beta_adj_ratio=beta_ratio_0050,
        )
        log.info(f'0050: {price_0050}  名目={notional_0050:,.0f}  beta_ratio={beta_ratio_0050:.2f}  建議{n_contracts_0050}口TXO')

        # 11. 組裝結果
        result = {
            'timestamp': datetime.now().isoformat(),
            'config': {
                'beta': CFG.beta,
                'large_futures_lots': CFG.large_futures_lots,
                'delta_target': CFG.delta_target,
            },
            'market': market,
            'txo_month': month,
            'dte': dte,
            'indicators': indicators,
            'targets': {
                'target_put_strike':  targets['put_strike'],
                'target_call_strike': targets['call_strike'],
                'method': targets['method'],
                'beta_adjusted_ratio': contracts['beta_adjusted_ratio'],
            },
            'selected_options': {
                'put': {
                    'strike': put_c.strike_price,
                    'symbol': put_c.symbol,
                    'delta':  round(put_delta, 4),
                    'ask':    quotes['put_ask'],
                    'bid':    quotes['put_bid'],
                    'mid':    quotes['put_mid'],
                    'volume': quotes['put_volume'],
                },
                'call': {
                    'strike': call_c.strike_price,
                    'symbol': call_c.symbol,
                    'delta':  round(call_delta, 4),
                    'ask':    quotes['call_ask'],
                    'bid':    quotes['call_bid'],
                    'mid':    quotes['call_mid'],
                    'volume': quotes['call_volume'],
                },
            },
            'structures': [asdict(s) for s in structures],
            'collar_0050': {
                'price_0050':          price_0050,
                'lots':                CFG.lots_0050,
                'lot_size':            CFG.lot_size_0050,
                'notional':            notional_0050,
                'beta':                CFG.beta_0050,
                'beta_adj_ratio':      beta_ratio_0050,
                'recommended_contracts': n_contracts_0050,
                'structures':          [asdict(s) for s in structures_0050],
            },
        }

        # 12. 輸出
        log.info('-' * 60)
        log.info('Result:')
        for s in structures:
            net_man = s.monthly_net / 10000
            log.info(f'  {s.name:20s} {s.calls}C/{s.puts}P  '
                     f'淨收 {net_man:+.1f}萬  保護 {s.protection_pct:.0f}%  '
                     f'{"✓ credit" if s.is_net_credit else "✗ debit"}')

        # 12. 寫本地檔
        with open(CFG.local_output, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        log.info(f'Local: {CFG.local_output}')

        # 13. 推 Firebase
        push_to_firebase(result)

    except Exception as e:
        log.exception(f'Error: {e}')
        raise
    finally:
        api.logout()
        log.info('Logged out')


if __name__ == '__main__':
    main()
