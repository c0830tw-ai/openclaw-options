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
# 內建假日備援清單（holidays 套件安裝失敗時使用）
_HOLIDAYS_BUILTIN: frozenset = frozenset({
    '2025-01-01', '2025-01-27', '2025-01-28', '2025-01-29', '2025-01-30', '2025-01-31',
    '2025-02-28', '2025-04-03', '2025-04-04', '2025-05-01', '2025-05-31',
    '2025-10-06', '2025-10-10',
    '2026-01-01', '2026-01-22', '2026-01-23', '2026-01-26', '2026-01-27',
    '2026-01-28', '2026-01-29', '2026-01-30',
    '2026-02-28', '2026-04-03', '2026-04-04', '2026-05-01',
    '2026-06-19', '2026-09-25', '2026-10-09', '2026-10-10',
})


_HOLIDAYS_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'taiwan_holidays_cache.json')


def _build_taiwan_holidays() -> frozenset:
    """每年自動抓一次台灣假日並快取到 taiwan_holidays_cache.json。
    快取年份不符（跨年）才重新抓，套件未安裝時退回內建清單。"""
    current_year = datetime.now().year

    # 讀快取：年份相符直接使用
    if os.path.exists(_HOLIDAYS_CACHE):
        try:
            with open(_HOLIDAYS_CACHE, encoding='utf-8') as f:
                cached = json.load(f)
            if cached.get('year') == current_year:
                dates = frozenset(cached['holidays'])
                log.info(f'台灣假日從快取載入，共 {len(dates)} 個（{current_year} 年）')
                return dates
        except Exception:
            pass  # 快取損壞，繼續重抓

    # 重新抓取
    years = range(current_year - 1, current_year + 2)
    try:
        import holidays as hd
        result: list = sorted({str(d) for year in years
                                for d in hd.country_holidays('TW', years=year).keys()})
        with open(_HOLIDAYS_CACHE, 'w', encoding='utf-8') as f:
            json.dump({'year': current_year, 'holidays': result}, f,
                      ensure_ascii=False, indent=2)
        log.info(f'台灣假日已更新快取，共 {len(result)} 個（{current_year} 年）')
        return frozenset(result)
    except ImportError:
        log.warning('holidays 套件未安裝，使用內建清單（pip install holidays）')
        return _HOLIDAYS_BUILTIN
    except Exception as e:
        log.warning(f'holidays 抓取失敗（{e}），使用內建清單')
        return _HOLIDAYS_BUILTIN


TAIWAN_HOLIDAYS: frozenset = _build_taiwan_holidays()


def adjust_settlement(d) -> datetime:
    """若結算日為假日或週末，順延至下一個交易日。支援 datetime 和 date。"""
    while d.weekday() >= 5 or d.strftime('%Y-%m-%d') in TAIWAN_HOLIDAYS:
        d += timedelta(days=1)
    return d


def is_market_hours() -> bool:
    """台股交易時間 09:00-13:30。"""
    now = datetime.now().time()
    return dtime(9, 0) <= now <= dtime(13, 30)


def _third_wed_of(dt: datetime) -> datetime:
    """某月第三個週三。"""
    first = dt.replace(day=1)
    days_to_wed = (2 - first.weekday()) % 7
    return first + timedelta(days=days_to_wed + 14)


def get_txo_settlement(today: Optional[datetime] = None) -> Tuple[str, datetime]:
    """返回近月 (月份 YYYYMM, 結算日)。結算日為當月第三個週三，遇假日順延。"""
    today = today or datetime.now()
    settlement = adjust_settlement(_third_wed_of(today))
    if (settlement.date() - today.date()).days < CFG.days_to_next_month:
        next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        settlement = adjust_settlement(_third_wed_of(next_month))
        return next_month.strftime('%Y%m'), settlement
    return today.strftime('%Y%m'), settlement


def get_far_month_settlement(near_settlement: datetime) -> Tuple[str, datetime]:
    """近月結算日後的下一個月份（遠月）。遇假日順延。"""
    next_month = (near_settlement.replace(day=28) + timedelta(days=4)).replace(day=1)
    far_settlement = adjust_settlement(_third_wed_of(next_month))
    return next_month.strftime('%Y%m'), far_settlement


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


def _calc_adx(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """Wilder 平滑法 ADX（平均趨向指數）。"""
    n = len(closes)
    if n < period * 2 + 1:
        return 0.0

    plus_dms, minus_dms, trs = [], [], []
    for i in range(1, n):
        up   = highs[i]  - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dms.append(up   if up > down and up > 0   else 0.0)
        minus_dms.append(down if down > up and down > 0 else 0.0)
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i]  - closes[i - 1]),
                       abs(lows[i]   - closes[i - 1])))

    def wilder_smooth(data):
        s = sum(data[:period])
        result = [s]
        for x in data[period:]:
            s = s - s / period + x
            result.append(s)
        return result

    s_plus  = wilder_smooth(plus_dms)
    s_minus = wilder_smooth(minus_dms)
    s_tr    = wilder_smooth(trs)

    dxs = []
    for p, m, t in zip(s_plus, s_minus, s_tr):
        if t == 0:
            continue
        di_plus  = 100 * p / t
        di_minus = 100 * m / t
        denom = di_plus + di_minus
        dxs.append(100 * abs(di_plus - di_minus) / denom if denom else 0.0)

    if len(dxs) < period:
        return 0.0

    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


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


def bs_delta(S: float, K: float, T: float, sigma: float, is_put: bool,
             r: Optional[float] = None) -> float:
    """
    S: 標的現價（TAIEX 現貨或 TX 期貨）  K: 履約價  T: 到期年數
    sigma: 年化波動率  r: 無風險利率（傳 0.0 即 Black-76 期貨定價）
    returns: put delta（負值）或 call delta（正值）
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    if r is None:
        r = CFG.risk_free_rate
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return _norm_cdf(d1) - 1 if is_put else _norm_cdf(d1)
    except (ValueError, ZeroDivisionError):
        return 0.0


# ── 履約價選擇 ────────────────────────────────────────────────

def compute_target_strikes(
    price: float, taiex: float, indicators: Optional[dict], dte: int,
    beta: Optional[float] = None,
    fallback_protect: Optional[float] = None,
    fallback_cap: Optional[float] = None,
) -> Dict[str, Any]:
    """
    動態：BB 下/上軌 + ATR × 乘數（隨 DTE 縮放），轉換為 TAIEX 目標點。
    fallback：kbar 失敗時用靜態比例。
    beta / fallback_* 預設使用 2330 設定。
    """
    _beta    = beta            if beta            is not None else CFG.beta
    _protect = fallback_protect if fallback_protect is not None else CFG.protect_pct_2330
    _cap     = fallback_cap    if fallback_cap    is not None else CFG.cap_pct_2330

    if indicators:
        mult = CFG.atr_mult_base * (0.75 + 0.25 * min(dte, 20) / 20)
        atr  = indicators['atr']

        put_bb  = min(indicators['bb_lower'], price - 1)
        put_atr = price - mult * atr
        put_tgt = max(put_bb, put_atr)

        call_bb  = max(indicators['bb_upper'], price + 1)
        call_atr = price + mult * atr
        call_tgt = min(call_bb, call_atr)

        r_put  = (put_tgt  - price) / price
        r_call = (call_tgt - price) / price
        method = 'bb_atr'
    else:
        r_put  = _protect
        r_call = _cap
        mult   = CFG.atr_mult_base
        method = 'static_fallback'

    return {
        'put_strike':  round(taiex * (1 + r_put  / _beta) / 50) * 50,
        'call_strike': round(taiex * (1 + r_call / _beta) / 50) * 50,
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
    s_price: Optional[float] = None,
    r: Optional[float] = None,
) -> Any:
    """
    從合約鏈挑履約價：
      1. 以 preferred_strike 為起點
      2. 往 OTM 方向走，找第一個 abs(delta) <= delta_target 的合約
      3. 全部超標時退回最 OTM 選項
    s_price: B-S 用的標的價格（傳 TX 期貨即 Black-76；不傳則用 taiex）
    r:       無風險利率（傳 0.0 對應 Black-76；不傳則用 CFG.risk_free_rate）
    """
    S      = s_price if s_price is not None else taiex
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
        d = bs_delta(S, contract.strike_price, T, hv_taiex, is_put, r=r)
        if abs(d) <= CFG.delta_target:
            log.info(f'  {label} {contract.strike_price:.0f}: delta={d:+.3f} ✓')
            return contract
        log.debug(f'  {label} {contract.strike_price:.0f}: delta={d:+.3f} > {CFG.delta_target}，往 OTM 移')

    fallback = search[-1]
    d = bs_delta(S, fallback.strike_price, T, hv_taiex, is_put, r=r)
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

    half = max(1, n_contracts // 2)
    return [
        make('symmetric',       '對稱領式 NC/NP',                        n_contracts, n_contracts),
        make('skewed',          f'偏賣方 {n_contracts}C/{half}P',         n_contracts, half),
        make('covered_call',    '純 Covered Call',                        n_contracts, 0),
        make('defensive',       f'偏防守 {half}C/{n_contracts}P',         half,        n_contracts),
        make('protective_put',  '純保護 Put 0C/NP',                       0,           n_contracts),
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


def fetch_kbars(api, stock_code: str) -> Optional[dict]:
    """抓個股日K，計算 ATR / BB / HV / ADX。失敗回傳 None。"""
    try:
        contract = api.Contracts.Stocks.TSE[stock_code]
        end_dt   = datetime.now()
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
            log.warning(f'{stock_code} 日K不足（{n_days} 天），退回靜態備援')
            return None

        atr            = _calc_atr(highs, lows, closes, CFG.atr_period)
        ma, bb_u, bb_l = _calc_bollinger(closes, CFG.bb_period)
        hv             = _calc_hv(closes, CFG.bb_period)
        adx            = _calc_adx(highs, lows, closes, CFG.atr_period)

        log.info(
            f'{stock_code} 日K {n_days}根  ATR({CFG.atr_period})={atr:.2f}  '
            f'BB上={bb_u:.2f} 中={ma:.2f} 下={bb_l:.2f}  HV={hv:.1%}  ADX={adx:.1f}'
        )
        return {
            'atr': atr, 'bb_upper': bb_u, 'bb_lower': bb_l,
            'ma20': ma, 'hv': hv, 'adx': adx, 'days': n_days,
        }
    except Exception as e:
        log.warning(f'{stock_code} kbar 抓取失敗（{e}）')
        return None


def fetch_hv_tx(api, month: str) -> Optional[float]:
    """
    抓台指期近月 K 棒，直接計算 TAIEX 年化歷史波動率。
    比 hv_2330/beta 代理更準確：排除台積電個股波動的干擾。
    失敗時回傳 None，主流程退回 hv_2330/beta 代理。
    """
    try:
        all_tx = list(api.Contracts.Futures.TXF)
        candidates = sorted(
            [c for c in all_tx if c.delivery_month >= month],
            key=lambda c: c.delivery_month,
        )
        if not candidates:
            log.warning('找不到 TX 合約，HV 退回 2330/beta 代理')
            return None

        contract = candidates[0]
        log.info(f'TX HV 來源: {contract.symbol} ({contract.delivery_month})')

        end_dt   = datetime.now()
        start_dt = end_dt - timedelta(days=int(CFG.bb_period * 1.5 * 7 / 5) + 10)
        kbars = api.kbars(
            contract=contract,
            start=start_dt.strftime('%Y-%m-%d'),
            end=end_dt.strftime('%Y-%m-%d'),
        )
        daily  = _resample_daily(kbars)
        closes = daily['Close']
        n_days = daily['days']

        if n_days < CFG.bb_period // 2:
            log.warning(f'TX 日K不足（{n_days} 天），退回 2330/beta 代理')
            return None

        hv = _calc_hv(closes, CFG.bb_period)
        log.info(f'TX 日K {n_days} 根  HV_TAIEX={hv:.1%}（直接計算）')
        return hv
    except Exception as e:
        log.warning(f'TX kbar 失敗（{e}），退回 2330/beta 代理')
        return None


def fetch_market_snapshot(api):
    """批次抓現貨（2330/0050/TAIEX/OTC）與期貨（TX近遠、台積電股期近遠、0050股期近遠）。"""
    today_month = datetime.now().strftime('%Y%m')
    registry: Dict[str, Any] = {}

    # ── 現貨 ──────────────────────────────────────────────────
    registry['2330']  = api.Contracts.Stocks.TSE['2330']
    registry['taiex'] = api.Contracts.Indexs.TSE['TSE001']
    registry['0050']  = api.Contracts.Stocks.TSE['0050']
    try:
        registry['otc'] = api.Contracts.Indexs.OTC['OTC101']
    except Exception as e:
        log.warning(f'OTC 合約失敗：{e}')

    # ── TX 近/遠月 ─────────────────────────────────────────────
    try:
        tx_sorted = sorted(
            [c for c in api.Contracts.Futures.TXF
             if c.delivery_month >= today_month
             and c.symbol == f'TXF{c.delivery_month}'],   # 排除 TXFR1/TXFR2 滾動合約
            key=lambda c: c.delivery_month,
        )
        if len(tx_sorted) >= 1: registry['tx_near'] = tx_sorted[0]
        if len(tx_sorted) >= 2: registry['tx_far']  = tx_sorted[1]
        log.info(f"TX 近月={tx_sorted[0].delivery_month if tx_sorted else '—'}  遠月={tx_sorted[1].delivery_month if len(tx_sorted)>=2 else '—'}")
    except Exception as e:
        log.warning(f'TX 期貨合約：{e}')

    # ── 台積電股期 近/遠月（CDF，排除 CDFR1/CDFR2 滾動合約）──────
    try:
        tf_sorted = sorted(
            [c for c in api.Contracts.Futures.CDF
             if c.delivery_month >= today_month
             and c.symbol == f'CDF{c.delivery_month}'],
            key=lambda c: c.delivery_month,
        )
        if len(tf_sorted) >= 1: registry['tf_near'] = tf_sorted[0]
        if len(tf_sorted) >= 2: registry['tf_far']  = tf_sorted[1]
        log.info(f"台積電股期 近={tf_sorted[0].delivery_month if tf_sorted else '—'}  遠={tf_sorted[1].delivery_month if len(tf_sorted)>=2 else '—'}")
    except Exception as e:
        log.warning(f'台積電股期（CDF）：{e}')

    # ── 0050 ETF 股期 近/遠月（NYF，排除 NYFR1/NYFR2 滾動合約）──
    try:
        etf_sorted = sorted(
            [c for c in api.Contracts.Futures.NYF
             if c.delivery_month >= today_month
             and c.symbol == f'NYF{c.delivery_month}'],
            key=lambda c: c.delivery_month,
        )
        if len(etf_sorted) >= 1: registry['etf_near'] = etf_sorted[0]
        if len(etf_sorted) >= 2: registry['etf_far']  = etf_sorted[1]
        log.info(f"0050股期 近={etf_sorted[0].delivery_month if etf_sorted else '—'}  遠={etf_sorted[1].delivery_month if len(etf_sorted)>=2 else '—'}")
    except Exception as e:
        log.warning(f'0050 ETF 股期（NYF）：{e}')

    # ── 批次 snapshot ──────────────────────────────────────────
    keys      = list(registry.keys())
    snap_list = api.snapshots(list(registry.values()))
    smap      = dict(zip(keys, snap_list))

    def sv(key, field, cast=float):
        s = smap.get(key)
        if s is None: return None
        v = getattr(s, field, None)
        try:   return cast(v) if v is not None else None
        except (ValueError, TypeError): return None

    def mo(key):
        c = registry.get(key)
        return getattr(c, 'delivery_month', None) if c else None

    taiex    = sv('taiex', 'close') or 0.0
    tx_near  = sv('tx_near', 'close')
    basis    = round(tx_near - taiex, 1) if tx_near and taiex else None
    if tx_near:
        log.info(f"TX 近月 {mo('tx_near')}: {tx_near}  基差 {basis:+.1f}")

    return {
        # 現貨
        'price_2330':       sv('2330', 'close'),
        'price_2330_open':  sv('2330', 'open'),
        'price_2330_high':  sv('2330', 'high'),
        'price_2330_low':   sv('2330', 'low'),
        'chg_2330':         sv('2330', 'change_price'),
        'chgpct_2330':      sv('2330', 'change_rate'),
        'volume_2330':      sv('2330', 'volume', cast=int),
        'taiex':            taiex,
        'chg_taiex':        sv('taiex', 'change_price'),
        'chgpct_taiex':     sv('taiex', 'change_rate'),
        'price_0050':       sv('0050', 'close'),
        'chg_0050':         sv('0050', 'change_price'),
        'chgpct_0050':      sv('0050', 'change_rate'),
        'otc':              sv('otc',  'close'),
        'chg_otc':          sv('otc',  'change_price'),
        'chgpct_otc':       sv('otc',  'change_rate'),
        # 台指期
        'tx_futures':       tx_near,
        'chg_tx':           sv('tx_near', 'change_price'),
        'chgpct_tx':        sv('tx_near', 'change_rate'),
        'tx_near_month':    mo('tx_near'),
        'tx_far':           sv('tx_far', 'close'),
        'chg_tx_far':       sv('tx_far', 'change_price'),
        'chgpct_tx_far':    sv('tx_far', 'change_rate'),
        'tx_far_month':     mo('tx_far'),
        # 台積電股期
        'tf_near':          sv('tf_near', 'close'),
        'chg_tf_near':      sv('tf_near', 'change_price'),
        'chgpct_tf_near':   sv('tf_near', 'change_rate'),
        'tf_near_month':    mo('tf_near'),
        'tf_far':           sv('tf_far', 'close'),
        'chg_tf_far':       sv('tf_far', 'change_price'),
        'chgpct_tf_far':    sv('tf_far', 'change_rate'),
        'tf_far_month':     mo('tf_far'),
        # 0050 股期
        'etf_near':         sv('etf_near', 'close'),
        'chg_etf_near':     sv('etf_near', 'change_price'),
        'chgpct_etf_near':  sv('etf_near', 'change_rate'),
        'etf_near_month':   mo('etf_near'),
        'etf_far':          sv('etf_far', 'close'),
        'chg_etf_far':      sv('etf_far', 'change_price'),
        'chgpct_etf_far':   sv('etf_far', 'change_rate'),
        'etf_far_month':    mo('etf_far'),
    }


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int):
    """某月第 n 個指定星期幾（weekday: 0=Mon, 2=Wed, 4=Fri）。
    若該月沒有第 n 個（如某月只有 4 個週五），回傳 None。"""
    first = datetime(year, month, 1)
    days_to_wd = (weekday - first.weekday()) % 7
    result = first + timedelta(days=days_to_wd + 7 * (n - 1))
    return result.date() if result.month == month else None


def _build_weekly_candidates(series_map: dict, weekday: int,
                             month_offsets: int = 2) -> list:
    """產生指定星期幾的週選候選清單，按結算日排序。"""
    today = datetime.now().date()
    candidates = []
    for offset in range(month_offsets):
        base = datetime.now()
        if offset > 0:
            base = (base.replace(day=28) + timedelta(days=4)).replace(day=1)
        y, m = base.year, base.month
        month_str = f'{y}{m:02d}'
        for n, series in series_map.items():
            d = _nth_weekday_of_month(y, m, weekday, n)
            if not d:
                continue
            adj = adjust_settlement(d)
            if adj > today:
                candidates.append((adj, d, series, month_str))
    candidates.sort()
    return candidates


def _pick_weekly_chain(api, candidates: list, label: str) -> Optional[dict]:
    """從候選清單找第一個有合約的週選，回傳 chain info。"""
    today = datetime.now().date()
    log.info(f'{label} 候選: {[(str(a), str(o), s) for a, o, s, _ in candidates[:4]]}')
    for adj_date, orig_date, series, month_str in candidates[:6]:
        try:
            series_contracts = list(getattr(api.Contracts.Options, series))
            chain = [c for c in series_contracts if c.delivery_month == month_str]
            if chain:
                dte = (adj_date - today).days
                suffix = f'（假日順延自 {orig_date}）' if adj_date != orig_date else ''
                log.info(f'{label} 確認: {series} {adj_date}{suffix}  DTE: {dte}  合約數: {len(chain)}')
                return {
                    'delivery_month': orig_date.strftime('%Y%m%d'),
                    'settlement_date': adj_date.strftime('%Y%m%d'),
                    'series': series,
                    'dte': dte,
                    'chain': chain,
                }
            log.debug(f'{label} {series} {month_str} 無合約，繼續')
        except AttributeError:
            log.debug(f'api.Contracts.Options.{series} 不存在，跳過')
        except Exception as e:
            log.debug(f'{label} {series} 失敗: {e}')
    log.warning(f'{label}：所有候選均無合約或無法存取')
    return None


def get_nearest_weekly_wed(api, monthly_settlement_date) -> Optional[dict]:
    """找最近的週三週選 (TX1/TX2/TX4/TX5)，跳過月選結算日（第3週）。"""
    WED = {1: 'TX1', 2: 'TX2', 4: 'TX4', 5: 'TX5'}
    candidates = [c for c in _build_weekly_candidates(WED, weekday=2)
                  if c[1] != monthly_settlement_date]
    return _pick_weekly_chain(api, candidates, '週三')


def get_nearest_weekly_fri(api) -> Optional[dict]:
    """找最近的週五週選 (TXU/TXV/TXX/TXY/TXZ)。"""
    FRI = {1: 'TXU', 2: 'TXV', 3: 'TXX', 4: 'TXY', 5: 'TXZ'}
    candidates = _build_weekly_candidates(FRI, weekday=4)
    return _pick_weekly_chain(api, candidates, '週五')


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
        market      = fetch_market_snapshot(api)
        price_2330  = market['price_2330']
        taiex       = market['taiex']
        tx_futures  = market.get('tx_futures') or taiex   # Black-76 定價基準
        bs_s        = tx_futures
        bs_r        = 0.0 if market.get('tx_futures') else None
        price_0050  = market.get('price_0050') or 0.0
        chg_0050    = market.get('chg_0050')
        chgpct_0050 = market.get('chgpct_0050')
        log.info(f'2330: {price_2330} | TAIEX現: {taiex} | TX期: {tx_futures} '
                 f'| 0050: {price_0050} | 櫃買: {market.get("otc")}')

        # 2. K棒指標（ATR / BB / HV）
        indicators_raw  = fetch_kbars(api, '2330')
        if indicators_raw:
            hv_2330    = indicators_raw.pop('hv')
            indicators = {**indicators_raw, 'hv_2330': hv_2330, 'hv_taiex': hv_2330 / CFG.beta}
        else:
            indicators = None
        indicators_0050 = fetch_kbars(api, '0050')

        # 3. DTE（距結算天數）
        month, settlement_dt = get_txo_settlement()
        dte = max(0, (settlement_dt.date() - datetime.now().date()).days)
        log.info(f'TXO month: {month}  結算: {settlement_dt.date()}  DTE: {dte}')

        # 3b. 嘗試用 TX 期貨直接計算 TAIEX HV（比 2330/beta 代理更準）
        hv_tx = fetch_hv_tx(api, month)
        if indicators:
            if hv_tx is not None:
                indicators['hv_taiex'] = hv_tx
                indicators['hv_source'] = 'tx_direct'
            else:
                indicators['hv_source'] = 'proxy_2330/beta'

        # 4. 動態履約價目標
        targets = compute_target_strikes(price_2330, taiex, indicators, dte)
        targets_0050 = compute_target_strikes(
            price_0050, taiex, indicators_0050, dte,
            beta=CFG.beta_0050,
        )
        log.info(f"目標履約價 2330: Put @ {targets['put_strike']}  Call @ {targets['call_strike']}  [{targets['method']}]")
        log.info(f"目標履約價 0050: Put @ {targets_0050['put_strike']}  Call @ {targets_0050['call_strike']}  [{targets_0050['method']}]")
        if indicators_0050:
            indicators_0050.update({
                'target_put_strike':  targets_0050['put_strike'],
                'target_call_strike': targets_0050['call_strike'],
                'method':             targets_0050['method'],
            })

        # 5. 口數
        contracts = compute_contract_count(price_2330, taiex)
        log.info(f"口數: {contracts['recommended_contracts']} (beta_ratio {contracts['beta_adjusted_ratio']:.2f})")

        # 6. 抓 TXO 鏈
        chain = fetch_txo_chain(api, month)

        # 7. 選履約價（含 delta 過濾）
        hv_taiex = indicators['hv_taiex'] if indicators else 0.20
        T = dte / 365
        log.info(f'Delta 篩選: target ≤ {CFG.delta_target}  HV_TAIEX={hv_taiex:.1%}  T={T:.3f}y  S={bs_s}')
        put_c  = find_strike_with_delta(chain, taiex, T, hv_taiex, OptionRight.Put,  targets['put_strike'],  s_price=bs_s, r=bs_r)
        call_c = find_strike_with_delta(chain, taiex, T, hv_taiex, OptionRight.Call, targets['call_strike'], s_price=bs_s, r=bs_r)

        put_delta  = bs_delta(bs_s, put_c.strike_price,  T, hv_taiex, is_put=True,  r=bs_r)
        call_delta = bs_delta(bs_s, call_c.strike_price, T, hv_taiex, is_put=False, r=bs_r)
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

        # 10b. 遠月建議（結算日換倉目標）
        far_month_data = None
        try:
            far_month, far_settlement_dt = get_far_month_settlement(settlement_dt)
            far_dte = max(0, (far_settlement_dt.date() - datetime.now().date()).days)
            log.info(f'遠月 TXO: {far_month}  結算: {far_settlement_dt.date()}  DTE: {far_dte}')

            far_targets = compute_target_strikes(price_2330, taiex, indicators, far_dte)
            log.info(f"遠月目標: Put @ {far_targets['put_strike']}  Call @ {far_targets['call_strike']}")

            far_chain = fetch_txo_chain(api, far_month)
            far_T     = far_dte / 365
            far_put_c  = find_strike_with_delta(far_chain, taiex, far_T, hv_taiex, OptionRight.Put,  far_targets['put_strike'],  s_price=bs_s, r=bs_r)
            far_call_c = find_strike_with_delta(far_chain, taiex, far_T, hv_taiex, OptionRight.Call, far_targets['call_strike'], s_price=bs_s, r=bs_r)

            far_put_delta  = bs_delta(bs_s, far_put_c.strike_price,  far_T, hv_taiex, is_put=True,  r=bs_r)
            far_call_delta = bs_delta(bs_s, far_call_c.strike_price, far_T, hv_taiex, is_put=False, r=bs_r)
            log.info(f'遠月最終: Put {far_put_c.strike_price:.0f} (δ={far_put_delta:+.3f})  Call {far_call_c.strike_price:.0f} (δ={far_call_delta:+.3f})')

            far_quotes     = fetch_option_quotes(api, far_put_c, far_call_c)
            far_structures = build_structures(
                n_contracts=contracts['recommended_contracts'],
                call_strike=far_call_c.strike_price,
                put_strike=far_put_c.strike_price,
                call_bid=far_quotes['call_bid'],
                put_ask=far_quotes['put_ask'],
                beta_adj_ratio=contracts['beta_adjusted_ratio'],
            )
            far_month_data = {
                'txo_month': far_month,
                'dte':       far_dte,
                'targets': {
                    'target_put_strike':  far_targets['put_strike'],
                    'target_call_strike': far_targets['call_strike'],
                    'method':             far_targets['method'],
                },
                'selected_options': {
                    'put': {
                        'strike': far_put_c.strike_price,
                        'symbol': far_put_c.symbol,
                        'delta':  round(far_put_delta, 4),
                        'ask':    far_quotes['put_ask'],
                        'bid':    far_quotes['put_bid'],
                        'mid':    far_quotes['put_mid'],
                        'volume': far_quotes['put_volume'],
                    },
                    'call': {
                        'strike': far_call_c.strike_price,
                        'symbol': far_call_c.symbol,
                        'delta':  round(far_call_delta, 4),
                        'ask':    far_quotes['call_ask'],
                        'bid':    far_quotes['call_bid'],
                        'mid':    far_quotes['call_mid'],
                        'volume': far_quotes['call_volume'],
                    },
                },
                'structures': [asdict(s) for s in far_structures],
            }
        except Exception as e:
            log.warning(f'遠月計算失敗（{e}），略過')

        def _calc_weekly(w_info: dict, label: str) -> Optional[dict]:
            if not w_info:
                return None
            w_dte   = w_info['dte']
            w_chain = w_info['chain']
            w_T     = max(w_dte, 1) / 365
            w_tgt   = compute_target_strikes(price_2330, taiex, indicators, w_dte)
            w_put_c  = find_strike_with_delta(w_chain, taiex, w_T, hv_taiex, OptionRight.Put,  w_tgt['put_strike'],  s_price=bs_s, r=bs_r)
            w_call_c = find_strike_with_delta(w_chain, taiex, w_T, hv_taiex, OptionRight.Call, w_tgt['call_strike'], s_price=bs_s, r=bs_r)
            w_pd = bs_delta(bs_s, w_put_c.strike_price,  w_T, hv_taiex, is_put=True,  r=bs_r)
            w_cd = bs_delta(bs_s, w_call_c.strike_price, w_T, hv_taiex, is_put=False, r=bs_r)
            log.info(f'{label} 最終: Put {w_put_c.strike_price:.0f} (δ={w_pd:+.3f})  Call {w_call_c.strike_price:.0f} (δ={w_cd:+.3f})')
            w_q = fetch_option_quotes(api, w_put_c, w_call_c)
            w_structs = build_structures(
                n_contracts=contracts['recommended_contracts'],
                call_strike=w_call_c.strike_price,
                put_strike=w_put_c.strike_price,
                call_bid=w_q['call_bid'],
                put_ask=w_q['put_ask'],
                beta_adj_ratio=contracts['beta_adjusted_ratio'],
            )
            return {
                'delivery_month': w_info['delivery_month'],
                'settlement_date': w_info['settlement_date'],
                'series': w_info['series'],
                'dte': w_dte,
                'targets': {
                    'target_put_strike':  w_tgt['put_strike'],
                    'target_call_strike': w_tgt['call_strike'],
                    'method':             w_tgt['method'],
                },
                'selected_options': {
                    'put': {
                        'strike': w_put_c.strike_price,
                        'symbol': w_put_c.symbol,
                        'delta':  round(w_pd, 4),
                        'ask':    w_q['put_ask'],
                        'bid':    w_q['put_bid'],
                        'mid':    w_q['put_mid'],
                        'volume': w_q['put_volume'],
                    },
                    'call': {
                        'strike': w_call_c.strike_price,
                        'symbol': w_call_c.symbol,
                        'delta':  round(w_cd, 4),
                        'ask':    w_q['call_ask'],
                        'bid':    w_q['call_bid'],
                        'mid':    w_q['call_mid'],
                        'volume': w_q['call_volume'],
                    },
                },
                'structures': [asdict(s) for s in w_structs],
            }

        # 10c. 週三週選
        weekly_wed_data = None
        try:
            weekly_wed_data = _calc_weekly(
                get_nearest_weekly_wed(api, settlement_dt.date()), '週三')
        except Exception as e:
            log.warning(f'週三週選計算失敗（{e}），略過')

        # 10d. 週五週選
        weekly_fri_data = None
        try:
            weekly_fri_data = _calc_weekly(
                get_nearest_weekly_fri(api), '週五')
        except Exception as e:
            log.warning(f'週五週選計算失敗（{e}），略過')

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
            'indicators_0050': indicators_0050,
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
                'chg_0050':            chg_0050,
                'chgpct_0050':         chgpct_0050,
                'lots':                CFG.lots_0050,
                'lot_size':            CFG.lot_size_0050,
                'notional':            notional_0050,
                'beta':                CFG.beta_0050,
                'beta_adj_ratio':      beta_ratio_0050,
                'recommended_contracts': n_contracts_0050,
                'structures':          [asdict(s) for s in structures_0050],
            },
            'far_month':   far_month_data,
            'weekly_wed':  weekly_wed_data,
            'weekly_fri':  weekly_fri_data,
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
