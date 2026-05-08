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
from pathlib import Path
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
    cost_basis_0050: float = 0.0          # 進場均價（NT/單位）；0 = 未提供，用於計算 unrealized P&L

    # 動態履約價選擇
    delta_target: float = 0.10            # Put/Call delta 絕對值上限
    risk_free_rate: float = 0.0175        # 無風險利率（TWD 年化）
    atr_period: int = 14
    bb_period: int = 20
    atr_mult_base: float = 1.5            # ATR 乘數（DTE >= 20 天）

    # ADX 趨勢/盤整調整：趨勢市放寬、盤整市收緊
    adx_trend_threshold: float = 25.0     # ADX > 此值 = 趨勢
    adx_range_threshold: float = 20.0     # ADX < 此值 = 盤整
    adx_trend_mult: float = 1.30          # 趨勢市 ATR 乘數倍率
    adx_range_mult: float = 0.85          # 盤整市 ATR 乘數倍率

    # 靜態備援（kbar 失敗時使用）
    protect_pct_2330: float = -0.157
    cap_pct_2330: float = 0.10

    # 選月策略：距結算 < N 天就用次月
    days_to_next_month: int = 5

    # 垂直價差設定
    spread_width: float = 100.0            # 價差寬度（點數，TXO 通常 50–200）

    # 輸出
    local_output: str = './latest_collar.json'


CFG = Config()


# ============ Positions Override ============
_POSITIONS_FILE = Path(__file__).parent / 'positions.json'
_POSITIONS_OVERRIDABLE = {'large_futures_lots', 'lots_0050', 'lot_size_0050', 'cost_basis_0050'}
POSITIONS_SOURCE = 'config_default'   # 'positions_file' 表示來自檔案
POSITIONS_INSTRUMENTS: List[Dict[str, Any]] = []   # 多商品 schema (Phase 1+)


def _apply_positions_overrides() -> None:
    """從 ./positions.json 讀取持倉資料覆蓋 CFG。
    支援兩種 schema：
      v0: 平面欄位（large_futures_lots / lots_0050 / lot_size_0050 / cost_basis_0050）
      v1: instruments[] 陣列（多 instrument 詳細紀錄）
    兩者可同時存在（v1 不影響 v0 的舊行為，給新功能用）。
    只覆蓋白名單內欄位，避免亂改其他配置。"""
    global POSITIONS_SOURCE, POSITIONS_INSTRUMENTS
    if not _POSITIONS_FILE.exists():
        return
    try:
        data = json.loads(_POSITIONS_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        log.warning(f'positions.json 解析失敗: {e}，使用 CFG 預設值')
        return
    if not isinstance(data, dict):
        log.warning('positions.json 格式錯誤（必須是 object），使用 CFG 預設值')
        return

    # v0：平面欄位覆蓋
    applied = []
    for k, v in data.items():
        if k in _POSITIONS_OVERRIDABLE and isinstance(v, (int, float)):
            setattr(CFG, k, v)
            applied.append(f'{k}={v}')

    # v1：instruments 陣列
    insts = data.get('instruments')
    if isinstance(insts, list):
        valid = []
        for it in insts:
            if isinstance(it, dict) and it.get('type'):
                valid.append(it)
        POSITIONS_INSTRUMENTS = valid
        if valid:
            applied.append(f'instruments[{len(valid)}]')

    if applied:
        POSITIONS_SOURCE = 'positions_file'
        log.info(f'positions.json 覆蓋: {", ".join(applied)}')


_apply_positions_overrides()


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


@dataclass
class SpreadStructure:
    name: str
    desc: str
    option_type: str          # 'put' or 'call'
    n_contracts: int
    sell_strike: float        # the leg we sell
    buy_strike: float         # the leg we buy
    sell_premium: float       # points received (sell leg)
    buy_premium: float        # points paid (buy leg)
    net_per_point: float      # sell - buy; positive = credit, negative = debit
    spread_width: float       # abs(sell_strike - buy_strike) in points
    max_profit_ntd: float     # NT$
    max_loss_ntd: float       # NT$
    breakeven: float          # TAIEX level at expiry
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


def trading_days_between(start_date, end_date) -> int:
    """計算 start_date（不含）到 end_date（含）之間的交易日數。
    交易日 = 工作日 - 國定假日。"""
    if end_date <= start_date:
        return 0
    days = 0
    d = start_date
    while d < end_date:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.strftime('%Y-%m-%d') not in TAIWAN_HOLIDAYS:
            days += 1
    return days


def trading_T(settlement_date) -> float:
    """從今天到 settlement_date 的 BS T（年化）= 交易日 / 252。
    最少回 1/252（避免 T=0 在結算日當天造成 BS 公式失敗）。"""
    today = datetime.now().date()
    sd = settlement_date.date() if hasattr(settlement_date, 'date') else settlement_date
    return max(trading_days_between(today, sd), 1) / 252


def is_market_hours() -> bool:
    """台股日盤交易時間 09:00-13:30，且必須是工作日（非週末、非國定假日）。"""
    now = datetime.now()
    if now.weekday() >= 5:                                  # 週六/日
        return False
    if now.strftime('%Y-%m-%d') in TAIWAN_HOLIDAYS:         # 國定假日
        return False
    t = now.time()
    return dtime(9, 0) <= t <= dtime(13, 30)


def is_night_session() -> bool:
    """TX/TXO 期貨夜盤：15:00 開盤 → 隔日 05:00 收盤。
    必須前一個日盤是工作日（非週末、非國定假日）才會有對應的夜盤。
    所以週一凌晨、週日凌晨、假日後一天凌晨都不算夜盤。"""
    now = datetime.now()
    h = now.hour

    # 15:00–23:59：今晚的夜盤必須今天有日盤（工作日且非假日）
    if h >= 15:
        if now.weekday() >= 5:
            return False
        if now.strftime('%Y-%m-%d') in TAIWAN_HOLIDAYS:
            return False
        return True

    # 00:00–04:59：昨晚夜盤的延續，必須昨天有日盤
    if h < 5:
        yesterday = now - timedelta(days=1)
        if yesterday.weekday() >= 5:
            return False
        if yesterday.strftime('%Y-%m-%d') in TAIWAN_HOLIDAYS:
            return False
        return True

    return False


def market_session_label() -> str:
    """目前市場時段：'day' / 'night' / 'closed'"""
    if is_market_hours():
        return 'day'
    if is_night_session():
        return 'night'
    return 'closed'


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


def compute_beta(stock_closes: list, market_closes: list,
                 period: int = 60,
                 stock_dates: Optional[list] = None,
                 market_dates: Optional[list] = None) -> Optional[float]:
    """
    用最近 period 日對數報酬迴歸計算 beta = Cov(r_stock, r_market) / Var(r_market)。
    若提供 stock_dates / market_dates 會做日期對齊（取共同日期）；
    否則退回從尾端取等量（適合來源已對齊的情況）。
    資料 < 30 日或 Var(market) = 0 時回 None。
    """
    if stock_dates is not None and market_dates is not None:
        # 日期對齊：取兩邊都有的日期，按時間順序排序
        sd = dict(zip(stock_dates, stock_closes))
        md = dict(zip(market_dates, market_closes))
        common = sorted(set(stock_dates) & set(market_dates))
        if len(common) < 30:
            return None
        common = common[-(period + 1):]
        s = [sd[d] for d in common]
        m = [md[d] for d in common]
    else:
        n = min(len(stock_closes), len(market_closes), period + 1)
        if n < 30:
            return None
        s = stock_closes[-n:]
        m = market_closes[-n:]

    n = min(len(s), len(m))
    rs, rm = [], []
    for i in range(n - 1):
        if s[i] > 0 and s[i + 1] > 0 and m[i] > 0 and m[i + 1] > 0:
            rs.append(math.log(s[i + 1] / s[i]))
            rm.append(math.log(m[i + 1] / m[i]))
    if len(rs) < 20:
        return None

    mean_s = sum(rs) / len(rs)
    mean_m = sum(rm) / len(rm)
    cov   = sum((rs[i] - mean_s) * (rm[i] - mean_m) for i in range(len(rs))) / len(rs)
    var_m = sum((r - mean_m) ** 2 for r in rm) / len(rm)
    if var_m <= 0:
        return None
    return cov / var_m


# ── Black-Scholes Delta ───────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return (1 + math.erf(x / math.sqrt(2))) / 2


def bs_price(S: float, K: float, T: float, sigma: float, is_put: bool,
             r: Optional[float] = None) -> float:
    """Black-76 期權理論價（r=0 for futures）。"""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    if r is None:
        r = CFG.risk_free_rate
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        disc = math.exp(-r * T)
        if is_put:
            return disc * (K * _norm_cdf(-d2) - S * _norm_cdf(-d1))
        else:
            return disc * (S * _norm_cdf(d1) - K * _norm_cdf(d2))
    except (ValueError, ZeroDivisionError):
        return 0.0


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


def bs_greeks(S: float, K: float, T: float, sigma: float, is_put: bool,
              r: Optional[float] = None) -> Dict[str, float]:
    """Black-76 Greeks（r=0 for futures）。
    回傳 {delta, theta_per_day, vega_per_pct}：
      delta            : 標準 dimensionless delta（put 為負）
      theta_per_day    : 點/天（長部位通常為負 = 每天耗損點數）
      vega_per_pct     : 1% IV 變動下的點數變動
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {'delta': 0.0, 'theta_per_day': 0.0, 'vega_per_pct': 0.0}
    if r is None:
        r = CFG.risk_free_rate
    try:
        sqrt_T = math.sqrt(T)
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
        phi_d1 = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)
        delta = _norm_cdf(d1) - 1 if is_put else _norm_cdf(d1)
        theta_year = -S * phi_d1 * sigma / (2 * sqrt_T)
        return {
            'delta':         delta,
            'theta_per_day': theta_year / 365,
            'vega_per_pct':  S * phi_d1 * sqrt_T / 100,
        }
    except (ValueError, ZeroDivisionError, OverflowError):
        return {'delta': 0.0, 'theta_per_day': 0.0, 'vega_per_pct': 0.0}


def implied_vol_newton(S: float, K: float, T: float, market_price: float,
                       is_put: bool, r: float = 0.0,
                       initial_guess: float = 0.25) -> Optional[float]:
    """Newton-Raphson 反推 IV。無解／不收斂時回 None。回傳值範圍 [0.05, 3.0]。"""
    if T <= 0 or S <= 0 or K <= 0 or market_price <= 0:
        return None

    disc = math.exp(-r * T)
    intrinsic = max(0.0, (K * disc - S) if is_put else (S - K * disc))
    if market_price < intrinsic - 0.01:
        return None  # 套利機會或報價錯誤

    sigma = initial_guess
    for _ in range(50):
        try:
            sqrt_T = math.sqrt(T)
            d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
            d2 = d1 - sigma * sqrt_T
            if is_put:
                price = disc * (K * _norm_cdf(-d2) - S * _norm_cdf(-d1))
            else:
                price = disc * (S * _norm_cdf(d1) - K * _norm_cdf(d2))
            vega = S * disc * math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi) * sqrt_T
            diff = market_price - price
            if abs(diff) < 1e-3:
                return max(0.05, min(sigma, 3.0))
            if vega < 1e-6:
                return None
            sigma += diff / vega
            if sigma < 0.01 or sigma > 5.0:
                return None
        except (ValueError, ZeroDivisionError, OverflowError):
            return None
    return None


def interp_iv(curve: List[Tuple[float, float]], K: float,
              fallback_iv: float) -> float:
    """從 (strike, iv) skew 曲線線性內插指定履約的 IV；超出範圍時用最近端點（flat extrapolation）。
    曲線需先按 strike 升序排列。空曲線回 fallback_iv。"""
    if not curve:
        return fallback_iv
    if K <= curve[0][0]:
        return curve[0][1]
    if K >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        K0, iv0 = curve[i]
        K1, iv1 = curve[i + 1]
        if K0 <= K <= K1 and K1 > K0:
            t = (K - K0) / (K1 - K0)
            return iv0 + t * (iv1 - iv0)
    return fallback_iv


def build_skew_from_snaps(items: List[Tuple[float, float, bool]],
                          S: float, T: float, r: float = 0.0
                          ) -> List[Tuple[float, float]]:
    """從一組 (strike, mid_price, is_put) 反推 IV，建 (strike, iv) 曲線。
    多筆同 strike 取平均；無效點略過；按 strike 升序排序。"""
    bucket: Dict[float, List[float]] = {}
    for K, mid, is_put in items:
        if K <= 0 or mid <= 0:
            continue
        iv = implied_vol_newton(S, K, T, mid, is_put=is_put, r=r)
        if iv is None:
            continue
        bucket.setdefault(K, []).append(iv)
    return sorted(((K, sum(vs) / len(vs)) for K, vs in bucket.items()),
                  key=lambda x: x[0])


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

        # ADX 調整：趨勢市放寬（BB 容易被突破，走更 OTM）；盤整市收緊（BB 邊界靠譜，省 premium）
        adx = indicators.get('adx', 0) or 0
        if adx >= CFG.adx_trend_threshold:
            mult *= CFG.adx_trend_mult
            adx_regime = 'trend'
        elif adx > 0 and adx < CFG.adx_range_threshold:
            mult *= CFG.adx_range_mult
            adx_regime = 'range'
        else:
            adx_regime = 'neutral'

        atr  = indicators['atr']

        put_bb  = min(indicators['bb_lower'], price - 1)
        put_atr = price - mult * atr
        put_tgt = max(put_bb, put_atr)

        call_bb  = max(indicators['bb_upper'], price + 1)
        call_atr = price + mult * atr
        call_tgt = min(call_bb, call_atr)

        r_put  = (put_tgt  - price) / price
        r_call = (call_tgt - price) / price
        method = f'bb_atr_adx_{adx_regime}'
    else:
        r_put  = _protect
        r_call = _cap
        mult   = CFG.atr_mult_base
        adx_regime = 'unknown'
        method = 'static_fallback'

    return {
        'put_strike':  round(taiex * (1 + r_put  / _beta) / 50) * 50,
        'call_strike': round(taiex * (1 + r_call / _beta) / 50) * 50,
        'atr_mult': mult,
        'adx_regime': adx_regime,
        'method': method,
    }


def find_strike_with_delta(
    contracts: list,
    taiex: float,
    T: float,
    sigma: float,
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
    sigma: 該 expiry 的波動率（優先傳 ATM 反推 IV，退回 HV）
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
        d = bs_delta(S, contract.strike_price, T, sigma, is_put, r=r)
        if abs(d) <= CFG.delta_target:
            log.info(f'  {label} {contract.strike_price:.0f}: delta={d:+.3f} ✓')
            return contract
        log.debug(f'  {label} {contract.strike_price:.0f}: delta={d:+.3f} > {CFG.delta_target}，往 OTM 移')

    fallback = search[-1]
    d = bs_delta(S, fallback.strike_price, T, sigma, is_put, r=r)
    log.warning(f'  {label} delta 全超標，取最 OTM {fallback.strike_price:.0f}: delta={d:+.3f}')
    return fallback


def _find_spread_leg(chain: list, ref_strike: float, opt_right, is_put: bool, width: float = 100.0):
    """找垂直價差的另一腳：Put 往下、Call 往上，找最接近 ref ± width 的合約。"""
    candidates = [c for c in chain if c.option_right == opt_right]
    if is_put:
        target = ref_strike - width
        candidates = [c for c in candidates if c.strike_price < ref_strike]
    else:
        target = ref_strike + width
        candidates = [c for c in candidates if c.strike_price > ref_strike]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(c.strike_price - target))


def _broker_pos_to_item(p: dict, beta_0050: float, beta_2330: float) -> Optional[Dict[str, Any]]:
    """把 broker_sync 的 buy 方向 long 部位轉成 portfolio item 格式。"""
    if p.get('direction') != 'Buy' or p.get('quantity', 0) == 0:
        return None
    cat    = p.get('category')
    code   = p.get('code', '')
    family = p.get('family', '')
    qty    = p['quantity']
    last   = p.get('last_price') or 0
    cost   = p.get('price') or 0

    # Lot size by category/family
    if cat == 'cash_stock':
        lot_size = 1000   # 1 張 = 1000 股
    elif cat == 'stock_futures':
        lot_size = (
            10000 if family == 'NYF' else
            2000  if family == 'CDF' else
            100   # 小型股期預設 100 股/口（QFF/QNF/RGF/GXF/MKF...）
        )
    else:
        return None  # 期權、指數期不納入 long-side 名目

    notional = qty * lot_size * last
    cb_total = qty * lot_size * cost
    if notional <= 0:
        return None

    # Beta 對應
    if family == 'NYF' or code in ('0050', '0056'):
        beta = beta_0050
    elif family in ('CDF', 'QFF') or code == '2330':
        beta = beta_2330
    else:
        beta = 1.0

    return {
        'name':              f'[Broker] {p.get("name", code)} × {qty}',
        'role':              'core_long',
        'qty':               qty,
        'price':             round(last, 2),
        'notional':          round(notional, 0),
        'beta':              round(beta, 3),
        'beta_adj_notional': round(notional * beta, 0),
        'cost_basis':        cb_total if cb_total else None,
        'unrealized':        round(notional - cb_total, 0) if cb_total else None,
    }


def compute_portfolio_greeks(
    api, broker_positions: Optional[List[Dict[str, Any]]],
    bs_s: float, default_iv: float = 0.20,
) -> Optional[Dict[str, Any]]:
    """掃 broker_positions 的 TXO/週選 部位，計算每筆 + 整體 Greeks。
    回傳 {legs:[...], totals:{net_delta, delta_ntd_per_1pct_tx, theta_ntd_per_day, vega_ntd_per_pct_iv}}
    """
    if not broker_positions or not bs_s or bs_s <= 0:
        return None

    opt_pos = [p for p in broker_positions
               if p.get('category') == 'index_option' and (p.get('quantity') or 0) > 0]
    if not opt_pos:
        return None

    # Build code → contract index 一次（避免 N 次 list 掃描）
    code_map: Dict[str, Any] = {}
    for series in ('TXO', 'TX1', 'TX2', 'TX4', 'TX5',
                   'TXU', 'TXV', 'TXX', 'TXY', 'TXZ'):
        try:
            for c in getattr(api.Contracts.Options, series):
                code_map[c.code] = c
        except (AttributeError, TypeError):
            pass

    pairs = [(p, code_map[p['code']]) for p in opt_pos if p['code'] in code_map]
    if not pairs:
        log.debug(f'compute_portfolio_greeks: 0 of {len(opt_pos)} broker option codes matched')
        return None

    # 一次 snapshot 全部 leg
    try:
        snaps = api.snapshots([c for _, c in pairs])
    except Exception as e:
        log.warning(f'portfolio greeks snapshots 失敗: {e}')
        snaps = [None] * len(pairs)

    today = datetime.now().date()
    legs: List[Dict[str, Any]] = []
    for (p, c), snap in zip(pairs, snaps):
        K = float(c.strike_price)
        is_put = (c.option_right == OptionRight.Put)

        # T from delivery_date（'YYYY/MM/DD' 或 'YYYYMMDD'）
        del_str = getattr(c, 'delivery_date', '') or ''
        del_dt = None
        for fmt in ('%Y/%m/%d', '%Y-%m-%d', '%Y%m%d'):
            try:
                del_dt = datetime.strptime(del_str, fmt).date()
                break
            except ValueError:
                continue
        if not del_dt or del_dt <= today:
            continue
        dte = (del_dt - today).days
        T = dte / 365

        # IV：先試從 snapshot mid 反推；失敗用 default
        iv = default_iv
        mid = 0.0
        if snap is not None:
            close = float(getattr(snap, 'close', 0) or 0)
            bid   = float(getattr(snap, 'buy_price', 0) or 0)
            ask   = float(getattr(snap, 'sell_price', 0) or 0)
            mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else close
            if mid > 0:
                inv = implied_vol_newton(bs_s, K, T, mid, is_put=is_put)
                if inv is not None:
                    iv = inv

        sign = 1 if str(p.get('direction', '')).lower().startswith('buy') else -1
        qty  = int(p.get('quantity') or 0)
        qty_signed = sign * qty
        if qty_signed == 0:
            continue

        g = bs_greeks(bs_s, K, T, iv, is_put=is_put)
        legs.append({
            'code':             p['code'],
            'name':             p.get('name', ''),
            'right':            'put' if is_put else 'call',
            'strike':           K,
            'qty_signed':       qty_signed,
            'dte':              dte,
            'iv':               round(iv, 4),
            'mid':              round(mid, 1),
            'delta':            round(g['delta'], 4),
            'theta_per_day':    round(g['theta_per_day'], 3),
            'vega_per_pct':     round(g['vega_per_pct'], 3),
            'pos_delta':        round(g['delta'] * qty_signed, 3),
            'pos_theta_per_day': round(g['theta_per_day'] * qty_signed, 2),
            'pos_vega_per_pct':  round(g['vega_per_pct'] * qty_signed, 2),
        })

    if not legs:
        return None

    net_delta = sum(L['pos_delta']         for L in legs)   # 點/TX 1 點變動
    net_theta = sum(L['pos_theta_per_day'] for L in legs)   # 點/天
    net_vega  = sum(L['pos_vega_per_pct']  for L in legs)   # 點/1% IV

    return {
        'legs':   legs,
        'totals': {
            'net_delta':                  round(net_delta, 3),
            'delta_ntd_per_1pct_tx':      round(net_delta * 50 * bs_s * 0.01),
            'theta_ntd_per_day':          round(net_theta * 50),
            'vega_ntd_per_pct_iv':        round(net_vega * 50),
            'reference_tx':               round(bs_s, 1),
        },
    }


def compute_weekly_opportunities(
    weekly_data: Optional[Dict[str, Any]],
    bs_s: float, spread_width: float = 500.0,
) -> Optional[Dict[str, Any]]:
    """從 weekly section 推算本週可做的 3 種機會：
      - 賣 Call（covered call 收 premium）
      - Bull Put Spread（看不跌的信用價差）
      - Bear Call Spread（看不漲的信用價差）

    Spread 外腳用 BS 估算（不額外打 API），週選 ATM IV 已經在 weekly_data['iv_used']。
    """
    if not weekly_data or not bs_s:
        return None

    iv    = weekly_data.get('iv_used') or 0.20
    dte_t = weekly_data.get('dte_trading') or 0
    if dte_t <= 0:
        return None
    T = dte_t / 252

    so   = weekly_data.get('selected_options') or {}
    put  = so.get('put')  or {}
    call = so.get('call') or {}
    if not put.get('strike') or not call.get('strike'):
        return None

    put_strike   = put['strike']
    call_strike  = call['strike']
    put_bid      = put.get('bid')  or 0
    put_mid      = put.get('mid')  or put_bid
    call_bid     = call.get('bid') or 0
    call_mid     = call.get('mid') or call_bid

    # 取得真實 spread legs 報價（_calc_weekly 已 snapshot）；缺則 fallback 到 BS 估算
    spread_legs = weekly_data.get('spread_legs') or {}
    put_leg     = spread_legs.get('put_buy')  or {}
    call_leg    = spread_legs.get('call_buy') or {}

    # Vol skew curve（_calc_weekly 已從 spread_legs snapshots + ATM 反推）
    skew_curve: List[Tuple[float, float]] = [
        (s['strike'], s['iv']) for s in (weekly_data.get('skew') or [])
    ]

    def _leg_prices(leg, fallback_strike, is_put):
        """回傳 (worst_buy, mid_buy, strike, source)：
        worst_buy = 真實 ask（吃對方掛價）
        mid_buy   = 真實 (bid+ask)/2 或 mid（組合單預期 fill）
        缺則退回 BS 估算（用 skew curve 內插的 IV，而非 ATM IV）。"""
        bid = (leg.get('bid') or 0) if leg else 0
        ask = (leg.get('ask') or 0) if leg else 0
        mid = (leg.get('mid') or 0) if leg else 0
        if ask > 0 and bid > 0:
            return ask, (bid + ask) / 2, leg['strike'], 'real_mid'
        if ask > 0:
            return ask, mid if mid > 0 else ask, leg['strike'], 'real_ask'
        if mid > 0:
            return mid, mid, leg['strike'], 'real_mid'
        # BS fallback：優先用 skew 內插；無 skew 才退回 ATM IV
        local_iv = interp_iv(skew_curve, fallback_strike, iv) if skew_curve else iv
        bs_v = bs_price(bs_s, fallback_strike, T, local_iv, is_put=is_put, r=0.0)
        src = 'bs_estimate_skew' if skew_curve else 'bs_estimate'
        return bs_v, bs_v, fallback_strike, src

    # Bull put spread: 賣高履約 put（收 bid 或 mid）+ 買低履約 put（付 ask 或 mid）
    put_worst_buy, put_mid_buy, put_buy_strike, put_src = _leg_prices(put_leg, put_strike - spread_width, True)
    actual_put_width = put_strike - put_buy_strike

    # 組合單預期 (mid - mid) vs 保守底 (bid - ask)
    bull_put_mid_credit   = round(put_mid - put_mid_buy, 1)
    bull_put_worst_credit = round(put_bid - put_worst_buy, 1)
    bull_put_max_p  = round(bull_put_mid_credit * 50)
    bull_put_max_l  = round(max(0, (actual_put_width - bull_put_mid_credit)) * 50)
    bull_put_be     = round(put_strike - bull_put_mid_credit)

    # Bear call spread: 賣低履約 call（收 bid 或 mid）+ 買高履約 call（付 ask 或 mid）
    call_worst_buy, call_mid_buy, call_buy_strike, call_src = _leg_prices(call_leg, call_strike + spread_width, False)
    actual_call_width = call_buy_strike - call_strike

    bear_call_mid_credit   = round(call_mid - call_mid_buy, 1)
    bear_call_worst_credit = round(call_bid - call_worst_buy, 1)
    bear_call_max_p  = round(bear_call_mid_credit * 50)
    bear_call_max_l  = round(max(0, (actual_call_width - bear_call_mid_credit)) * 50)
    bear_call_be     = round(call_strike + bear_call_mid_credit)

    series = weekly_data.get('series') or ''
    weekday_label = (
        '週三' if series in ('TX1', 'TX2', 'TX4', 'TX5') else
        '週五' if series in ('TXU', 'TXV', 'TXX', 'TXY', 'TXZ') else
        '週選'
    )

    return {
        'series':          series,
        'weekday_label':   weekday_label,
        'settlement_date': weekly_data.get('settlement_date'),
        'dte_trading':     dte_t,
        'iv':              round(iv, 4),
        'spread_width':    spread_width,

        'sell_call': {
            'strike':       call_strike,
            'premium':      call_bid,
            'income_ntd':   round(call_bid * 50),
            'distance_pct': round((call_strike - bs_s) / bs_s * 100, 2),
            'viable':       call_bid > 5,   # 至少要收 5 點才值得做
        },
        'bull_put_spread': {
            'sell_strike':   put_strike,
            'sell_premium':  put_bid,                           # bid (you receive if take bid)
            'sell_mid':      put_mid,
            'buy_strike':    put_buy_strike,
            'buy_premium':   round(put_worst_buy, 1),           # ask (worst case)
            'buy_mid':       round(put_mid_buy, 1),
            'buy_premium_source': put_src,
            'net_credit':    bull_put_mid_credit,                # primary: combo-order expectation
            'worst_credit':  bull_put_worst_credit,              # bid-ask 保守底
            'max_profit':    bull_put_max_p,
            'max_loss':      bull_put_max_l,
            'breakeven':     bull_put_be,
            'distance_pct':  round((bs_s - put_strike) / bs_s * 100, 2),
            'viable':        bull_put_mid_credit > 5,
        },
        'bear_call_spread': {
            'sell_strike':   call_strike,
            'sell_premium':  call_bid,
            'sell_mid':      call_mid,
            'buy_strike':    call_buy_strike,
            'buy_premium':   round(call_worst_buy, 1),
            'buy_mid':       round(call_mid_buy, 1),
            'buy_premium_source': call_src,
            'net_credit':    bear_call_mid_credit,
            'worst_credit':  bear_call_worst_credit,
            'max_profit':    bear_call_max_p,
            'max_loss':      bear_call_max_l,
            'breakeven':     bear_call_be,
            'distance_pct':  round((call_strike - bs_s) / bs_s * 100, 2),
            'viable':        bear_call_mid_credit > 5,
        },
    }


def compute_portfolio_breakdown(
    instruments: List[Dict[str, Any]],
    price_0050: float, price_2330: float, taiex: float,
    beta_0050: float, beta_2330: float,
    broker_positions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    從 positions.json 的 instruments[] 計算逐商品名目、未實現 P&L、整體 hedge ratio。
    支援的 instrument types：
      0050_etf_futures      lots × lot_size (10000) × price_0050        beta=beta_0050
      0050_etf_cash         shares × price_0050                          beta=beta_0050
      tsmc_mini_futures     lots × lot_size (100) × price_2330           beta=beta_2330
      tsmc_large_futures    lots × lot_size (2000) × price_2330          beta=beta_2330
      generic_taiex_proxy   notional 直接給（其他台股 ETF/個股）           beta=1.0
    回傳：
      {
        'items': [...]                # 逐 instrument 結果
        'totals': {
          'core_long_notional': 總長期曝險,
          'core_long_beta_adj': beta-adj 曝險（折成 TAIEX 等值）,
          'core_long_unrealized': 已知 cost_basis 的 unrealized 加總,
          'recommended_put_lots': ceil(beta_adj / TXO_unit_notional),
        }
      }
    """
    items = []
    sum_notional = 0.0
    sum_beta_adj = 0.0
    sum_unrealized = 0.0
    txo_unit = (taiex or 0) * 50

    def _push(name, role, lots_or_shares, price, notional, unit_beta,
              cost_basis, current_value):
        nonlocal sum_notional, sum_beta_adj, sum_unrealized
        beta_adj = notional * unit_beta
        if role == 'core_long':
            sum_notional   += notional
            sum_beta_adj   += beta_adj
            if cost_basis and current_value is not None:
                sum_unrealized += (current_value - cost_basis)
        items.append({
            'name': name,
            'role': role,
            'qty': lots_or_shares,
            'price': round(price, 4) if price else None,
            'notional': round(notional, 0),
            'beta': round(unit_beta, 3),
            'beta_adj_notional': round(beta_adj, 0),
            'cost_basis': cost_basis,
            'unrealized': round(current_value - cost_basis, 0) if (cost_basis and current_value is not None) else None,
        })

    for it in instruments:
        t    = it.get('type')
        role = it.get('role', 'core_long')
        if t == '0050_etf_futures':
            lots = it.get('lots', 0) or 0
            ls   = it.get('lot_size', 10000) or 10000
            cb   = it.get('cost_basis', 0) or 0
            notional = lots * ls * (price_0050 or 0)
            cur_val  = lots * ls * (price_0050 or 0)
            cb_val   = lots * ls * cb if cb else 0
            _push(f'0050 ETF期 × {lots}口', role, lots, price_0050, notional,
                  beta_0050, cb_val if cb else None, cur_val if cb else None)
        elif t == '0050_etf_cash':
            shares = it.get('shares', 0) or 0
            cb     = it.get('cost_basis', 0) or 0
            notional = shares * (price_0050 or 0)
            cur_val  = shares * (price_0050 or 0)
            cb_val   = shares * cb if cb else 0
            _push(f'0050 現股 × {shares}股', role, shares, price_0050, notional,
                  beta_0050, cb_val if cb else None, cur_val if cb else None)
        elif t == 'tsmc_mini_futures':
            lots = it.get('lots', 0) or 0
            ls   = it.get('lot_size', 100) or 100
            cb   = it.get('cost_basis', 0) or 0
            notional = lots * ls * (price_2330 or 0)
            cur_val  = lots * ls * (price_2330 or 0)
            cb_val   = lots * ls * cb if cb else 0
            _push(f'小台積電期 × {lots}口', role, lots, price_2330, notional,
                  beta_2330, cb_val if cb else None, cur_val if cb else None)
        elif t == 'tsmc_large_futures':
            lots = it.get('lots', 0) or 0
            ls   = it.get('lot_size', 2000) or 2000
            cb   = it.get('cost_basis', 0) or 0
            notional = lots * ls * (price_2330 or 0)
            cur_val  = lots * ls * (price_2330 or 0)
            cb_val   = lots * ls * cb if cb else 0
            _push(f'大台積電期 × {lots}口', role, lots, price_2330, notional,
                  beta_2330, cb_val if cb else None, cur_val if cb else None)
        elif t == 'generic_taiex_proxy':
            name     = it.get('name', '其他台股')
            notional = it.get('notional', 0) or 0
            cb_total = it.get('cost_basis_total', 0) or 0
            _push(name, role, 1, None, notional, 1.0,
                  cb_total if cb_total else None,
                  notional if cb_total else None)
        else:
            log.debug(f'未知 instrument type: {t}，略過')

    # 自動把 broker 的 long-side 部位也納入（Phase 7 broker auto-merge）
    if broker_positions:
        for p in broker_positions:
            it = _broker_pos_to_item(p, beta_0050, beta_2330)
            if not it:
                continue
            items.append(it)
            sum_notional += it['notional']
            sum_beta_adj += it['beta_adj_notional']
            if it['unrealized'] is not None:
                sum_unrealized += it['unrealized']

    rec_lots = math.ceil(sum_beta_adj / txo_unit) if txo_unit > 0 else 0

    return {
        'items':  items,
        'totals': {
            'core_long_notional':   round(sum_notional, 0),
            'core_long_beta_adj':   round(sum_beta_adj, 0),
            'core_long_unrealized': round(sum_unrealized, 0),
            'txo_unit_notional':    round(txo_unit, 0),
            'recommended_put_lots': rec_lots,
        },
    }


def compute_contract_count(price_2330: float, taiex: float,
                           beta: Optional[float] = None) -> Dict[str, Any]:
    """計算對應 1 口大期的 TXO 口數需求 (Beta 調整)。"""
    price_2330 = price_2330 or 2260.0   # 夜盤快取失效時用保守預設
    taiex      = taiex      or 41000.0  # 同上
    _beta      = beta if beta is not None else CFG.beta
    notional = CFG.large_futures_lots * 2000 * price_2330
    txo_notional = taiex * 50
    raw_ratio = notional / txo_notional
    beta_adj_ratio = raw_ratio * _beta

    # Guard：沒有大台部位（large_futures_lots=0）→ 回最低 1 口讓下游 structures 還能算
    if beta_adj_ratio <= 0:
        return {
            'notional': notional,
            'beta_adjusted_ratio': beta_adj_ratio,
            'recommended_contracts': 1,
        }

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
        protection = (puts / beta_adj_ratio * 100) if (puts > 0 and beta_adj_ratio > 0) else 0.0
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


def build_spreads(
    n_contracts: int,
    put_short_strike: float, put_short_bid: float, put_short_ask: float,
    put_long_strike: float,  put_long_bid: float,  put_long_ask: float,
    call_short_strike: float, call_short_bid: float, call_short_ask: float,
    call_long_strike: float,  call_long_bid: float,  call_long_ask: float,
) -> List[SpreadStructure]:
    """產生 4 種垂直價差策略（月選，以主力履約價為基準）。

    call_short / put_short = 主力合約（和 collar 同一組履約價）
    call_long  / put_long  = 更 OTM 的腳（spread_width 點外）
    """
    M = 50 * n_contracts

    # 1. Bear Call Spread（空頭買權價差 — 信用）
    #    賣 call_short（收 bid），買 call_long（付 ask）
    bcall_net   = round(call_short_bid - call_long_ask, 1)
    bcall_width = call_long_strike - call_short_strike

    # 2. Bull Put Spread（多頭賣權價差 — 信用）
    #    賣 put_short（收 bid），買 put_long（付 ask）
    bput_net   = round(put_short_bid - put_long_ask, 1)
    bput_width = put_short_strike - put_long_strike

    # 3. Bear Put Spread（空頭賣權價差 — 借方，降低買 put 成本）
    #    買 put_short（付 ask），賣 put_long（收 bid）
    bear_put_net   = round(put_long_bid - put_short_ask, 1)   # negative = debit
    bear_put_width = put_short_strike - put_long_strike

    # 4. Bull Call Spread（多頭買權價差 — 借方，看多方向性）
    #    買 call_short（付 ask），賣 call_long（收 bid）
    bull_call_net   = round(call_long_bid - call_short_ask, 1)  # negative = debit
    bull_call_width = call_long_strike - call_short_strike

    return [
        SpreadStructure(
            name='bear_call_spread',
            desc=f'空頭買權價差 {int(call_short_strike)}/{int(call_long_strike)}C',
            option_type='call', n_contracts=n_contracts,
            sell_strike=call_short_strike, buy_strike=call_long_strike,
            sell_premium=call_short_bid,   buy_premium=call_long_ask,
            net_per_point=bcall_net,
            spread_width=bcall_width,
            max_profit_ntd=round(bcall_net * M),
            max_loss_ntd=round(max(0.0, (bcall_width - bcall_net) * M)),
            breakeven=call_short_strike + bcall_net,
            is_net_credit=True,
        ),
        SpreadStructure(
            name='bull_put_spread',
            desc=f'多頭賣權價差 {int(put_short_strike)}/{int(put_long_strike)}P',
            option_type='put', n_contracts=n_contracts,
            sell_strike=put_short_strike, buy_strike=put_long_strike,
            sell_premium=put_short_bid,   buy_premium=put_long_ask,
            net_per_point=bput_net,
            spread_width=bput_width,
            max_profit_ntd=round(bput_net * M),
            max_loss_ntd=round(max(0.0, (bput_width - bput_net) * M)),
            breakeven=put_short_strike - bput_net,
            is_net_credit=True,
        ),
        SpreadStructure(
            name='bear_put_spread',
            desc=f'空頭賣權價差（買保護降成本）{int(put_short_strike)}/{int(put_long_strike)}P',
            option_type='put', n_contracts=n_contracts,
            sell_strike=put_long_strike, buy_strike=put_short_strike,
            sell_premium=put_long_bid,   buy_premium=put_short_ask,
            net_per_point=bear_put_net,
            spread_width=bear_put_width,
            max_profit_ntd=round(max(0.0, (bear_put_width + bear_put_net) * M)),
            max_loss_ntd=round(abs(bear_put_net) * M),
            breakeven=put_short_strike + bear_put_net,
            is_net_credit=False,
        ),
        SpreadStructure(
            name='bull_call_spread',
            desc=f'多頭買權價差（看多方向性）{int(call_short_strike)}/{int(call_long_strike)}C',
            option_type='call', n_contracts=n_contracts,
            sell_strike=call_long_strike, buy_strike=call_short_strike,
            sell_premium=call_long_bid,   buy_premium=call_short_ask,
            net_per_point=bull_call_net,
            spread_width=bull_call_width,
            max_profit_ntd=round(max(0.0, (bull_call_width + bull_call_net) * M)),
            max_loss_ntd=round(abs(bull_call_net) * M),
            breakeven=call_short_strike - bull_call_net,
            is_net_credit=False,
        ),
    ]


# ============ Shioaji Operations ============
def _resample_daily(kbars) -> dict:
    """
    Shioaji kbars 固定回傳 1 分鐘 K，重採樣成日 K。
    ts 欄位為 nanosecond epoch。
    回傳含 'dates' 列表（與 closes 對齊），方便跨合約做日期對齊計算。
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
        'dates': days,
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
            'closes': closes,
            'dates':  daily['dates'],
        }
    except Exception as e:
        log.warning(f'{stock_code} kbar 抓取失敗（{e}）')
        return None


def fetch_hv_tx(api, month: str) -> Optional[dict]:
    """
    抓台指期近月 K 棒，回傳 {'hv': ..., 'closes': [...], 'days': N}。
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
        return {'hv': hv, 'closes': closes, 'dates': daily['dates'], 'days': n_days}
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
        'taiex_high':       sv('taiex', 'high'),
        'taiex_low':        sv('taiex', 'low'),
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


def fetch_atm_iv(api, chain: list, bs_s: float, T: float,
                 fallback_hv: float, r: float = 0.0,
                 label: str = '') -> Tuple[float, str]:
    """
    從合約鏈找 ATM call+put，反推 IV（取兩者平均）。
    回傳 (iv, source)：
      source = 'atm_market'（成功）/ 'hv_fallback'（失敗，用 HV 代替）
    """
    if T <= 0 or not bs_s or not chain:
        return fallback_hv, 'hv_fallback'

    calls = [c for c in chain if c.option_right == OptionRight.Call]
    puts  = [c for c in chain if c.option_right == OptionRight.Put]
    if not calls or not puts:
        return fallback_hv, 'hv_fallback'

    atm_call = min(calls, key=lambda c: abs(c.strike_price - bs_s))
    atm_put  = min(puts,  key=lambda c: abs(c.strike_price - bs_s))

    try:
        snaps = api.snapshots([atm_call, atm_put])
        if len(snaps) < 2:
            return fallback_hv, 'hv_fallback'

        ivs = []
        for snap, contract, is_put in [
            (snaps[0], atm_call, False),
            (snaps[1], atm_put,  True),
        ]:
            bid = float(snap.buy_price)  if snap.buy_price  else 0.0
            ask = float(snap.sell_price) if snap.sell_price else 0.0
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2
            else:
                mid = float(snap.close) if snap.close else 0.0
            if mid <= 0:
                continue
            iv = implied_vol_newton(bs_s, contract.strike_price, T, mid, is_put, r=r)
            if iv is not None:
                ivs.append(iv)

        if not ivs:
            return fallback_hv, 'hv_fallback'

        avg_iv = sum(ivs) / len(ivs)
        log.info(f'{label} ATM IV: {avg_iv:.1%} (call_K={atm_call.strike_price:.0f}, '
                 f'put_K={atm_put.strike_price:.0f}, n={len(ivs)})  HV_fallback={fallback_hv:.1%}')
        return avg_iv, 'atm_market'
    except Exception as e:
        log.debug(f'{label} fetch_atm_iv 失敗: {e}')
        return fallback_hv, 'hv_fallback'


def fetch_option_quotes(api, put_contract, call_contract,
                        bs_s: float = 0.0, T: float = 0.0,
                        sigma: float = 0.20, r: float = 0.0) -> Dict[str, float]:
    """抓 put/call 的 bid/ask。snapshot 失敗時退回 Black-Scholes 估算。"""
    snaps = api.snapshots([put_contract, call_contract])
    if len(snaps) < 2:
        # 夜盤 snapshot 回空 → BS 理論價
        log.warning('fetch_option_quotes: snapshot 不足，改用 BS 估算')
        _slippage = 0.05
        p_mid = bs_price(bs_s, put_contract.strike_price,  T, sigma, is_put=True,  r=r) if (bs_s and T) else 0.0
        c_mid = bs_price(bs_s, call_contract.strike_price, T, sigma, is_put=False, r=r) if (bs_s and T) else 0.0
        return {
            'put_ask':    round(p_mid * (1 + _slippage)),
            'put_mid':    round(p_mid),
            'put_bid':    round(p_mid * (1 - _slippage)),
            'call_bid':   round(c_mid * (1 - _slippage)),
            'call_mid':   round(c_mid),
            'call_ask':   round(c_mid * (1 + _slippage)),
            'put_volume': 0,
            'call_volume': 0,
        }
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


def compute_hv_percentile(current_hv: float) -> Optional[float]:
    """
    讀取 Firebase 的歷史每日 HV，回傳 current_hv 在其中的百分位（0–100）。
    資料不足 20 筆時回傳 None。
    """
    try:
        raw = db.reference('/trading/2330/hv_history').get()
        if not raw or len(raw) < 20:
            return None
        values = sorted(v['hv'] for v in raw.values()
                        if isinstance(v, dict) and 'hv' in v)
        if len(values) < 20:
            return None
        below = sum(1 for v in values if v <= current_hv)
        return round(below / len(values) * 100, 1)
    except Exception as e:
        log.warning(f'HV percentile calc failed: {e}')
        return None


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
    # 每日 HV 歷史（用於百分位計算）
    hv_val = (data.get('indicators') or {}).get('hv_taiex')
    if hv_val is not None:
        today = datetime.now().strftime('%Y-%m-%d')
        db.reference(f'/trading/2330/hv_history/{today}').set({'hv': round(hv_val, 4)})
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

    # 0b. 嘗試 activate CA + 抓 broker 真實持倉（read-only sync）
    _broker_positions = None
    try:
        import broker_sync as _BS
        _BS.activate_ca_if_configured(api)
        _broker_positions = _BS.fetch_all_positions(api)
    except Exception as _e:
        log.debug(f'broker_sync 失敗（可忽略）: {_e}')

    try:
        # 1. 抓現價
        market      = fetch_market_snapshot(api)

        # 夜盤/收盤時 close 欄位可能為 0 或 None，從快取補值
        _cached_full: Dict[str, Any] = {}
        _cache: Dict[str, Any] = {}
        if Path(CFG.local_output).exists():
            try:
                with open(CFG.local_output, 'r', encoding='utf-8') as _f:
                    _cached_full = json.load(_f)
                    _cache = _cached_full.get('market', {})
            except Exception:
                pass

        def _fallback(val, cache_key):
            if val:
                return val
            cached_val = _cache.get(cache_key)
            if cached_val:
                log.info(f'{cache_key}: live={val} → 使用快取 {cached_val}')
            return cached_val or val

        price_2330  = _fallback(market['price_2330'],          'price_2330')
        taiex_live  = market['taiex']
        tx_live     = market.get('tx_futures')
        taiex       = _fallback(taiex_live, 'taiex') or _fallback(tx_live, 'tx_futures') or 41000.0
        tx_futures  = _fallback(tx_live, 'tx_futures') or taiex
        bs_s        = tx_futures
        bs_r        = 0.0 if tx_live else None
        price_0050  = _fallback(market.get('price_0050'), 'price_0050') or 95.0
        chg_0050    = market.get('chg_0050')
        chgpct_0050 = market.get('chgpct_0050')
        log.info(f'2330: {price_2330} | TAIEX現: {taiex} | TX期: {tx_futures} '
                 f'| 0050: {price_0050} | 櫃買: {market.get("otc")}')

        # 2. K棒指標（ATR / BB / HV）
        indicators_raw  = fetch_kbars(api, '2330')
        closes_2330: list = []
        dates_2330:  list = []
        if indicators_raw:
            hv_2330    = indicators_raw.pop('hv')
            closes_2330 = indicators_raw.pop('closes', [])
            dates_2330  = indicators_raw.pop('dates',  [])
            # hv_taiex 之後會在 3b/3b-2 用 TX kbar 或 dynamic beta 設定，這邊先留空
            indicators = {**indicators_raw, 'hv_2330': hv_2330, 'hv_taiex': None}
        else:
            indicators = None
        indicators_0050 = fetch_kbars(api, '0050')
        closes_0050: list = []
        dates_0050:  list = []
        if indicators_0050:
            closes_0050 = indicators_0050.pop('closes', [])
            dates_0050  = indicators_0050.pop('dates',  [])

        # 把補正後的價格寫回 market，確保快取有效
        market['price_2330'] = price_2330
        market['taiex']      = taiex
        market['tx_futures'] = tx_futures
        market['price_0050'] = price_0050

        # 3. DTE（距結算天數）
        month, settlement_dt = get_txo_settlement()
        dte = max(0, (settlement_dt.date() - datetime.now().date()).days)
        log.info(f'TXO month: {month}  結算: {settlement_dt.date()}  DTE: {dte}')

        # 3b. 嘗試用 TX 期貨直接計算 TAIEX HV（比 2330/beta 代理更準）
        hv_tx_data = fetch_hv_tx(api, month)
        closes_tx: list = []
        dates_tx:  list = []
        if hv_tx_data is not None:
            closes_tx = hv_tx_data.get('closes', [])
            dates_tx  = hv_tx_data.get('dates',  [])
            if indicators:
                indicators['hv_taiex']  = hv_tx_data['hv']
                indicators['hv_source'] = 'tx_direct'
        elif indicators:
            indicators['hv_source'] = 'proxy_2330/beta'

        # 3b-2. 動態 beta 校準（60 日對數報酬迴歸，含日期對齊）
        beta_2330_raw = (compute_beta(closes_2330, closes_tx, period=60,
                                      stock_dates=dates_2330, market_dates=dates_tx)
                         if closes_2330 and closes_tx else None)
        beta_0050_raw = (compute_beta(closes_0050, closes_tx, period=60,
                                      stock_dates=dates_0050, market_dates=dates_tx)
                         if closes_0050 and closes_tx else None)

        # Sanity bound：[0.3, 3.0] 之外回退預設值（防 K 棒採樣異常導致負 beta 等）
        def _sane_beta(b: Optional[float]) -> bool:
            return b is not None and 0.3 <= b <= 3.0

        if _sane_beta(beta_2330_raw):
            beta_2330_used, beta_2330_source = beta_2330_raw, 'computed_60d'
        else:
            beta_2330_used, beta_2330_source = CFG.beta, (
                'config_default' if beta_2330_raw is None
                else f'config_default_oor({beta_2330_raw:.2f})')

        if _sane_beta(beta_0050_raw):
            beta_0050_used, beta_0050_source = beta_0050_raw, 'computed_60d'
        else:
            beta_0050_used, beta_0050_source = CFG.beta_0050, (
                'config_default' if beta_0050_raw is None
                else f'config_default_oor({beta_0050_raw:.2f})')
        log.info(f'Beta 2330: {beta_2330_used:.3f} ({beta_2330_source}, default {CFG.beta})')
        log.info(f'Beta 0050: {beta_0050_used:.3f} ({beta_0050_source}, default {CFG.beta_0050})')

        # 套用動態 beta 修正 hv_taiex（HV 是用 hv_2330/beta 推算 TAIEX 時的代理）
        if indicators and indicators.get('hv_source') != 'tx_direct':
            indicators['hv_taiex'] = indicators['hv_2330'] / beta_2330_used

        # 3c. HV 歷史百分位（需 Firebase 已有 ≥20 筆歷史）
        if indicators and init_firebase():
            hv_pct = compute_hv_percentile(indicators['hv_taiex'])
            if hv_pct is not None:
                indicators['hv_pct'] = hv_pct
                log.info(f"HV 百分位: {hv_pct:.1f}th%ile")

        # 4. 動態履約價目標
        targets = compute_target_strikes(price_2330, taiex, indicators, dte,
                                         beta=beta_2330_used)
        targets_0050 = compute_target_strikes(
            price_0050, taiex, indicators_0050, dte,
            beta=beta_0050_used,
        )
        log.info(f"目標履約價 2330: Put @ {targets['put_strike']}  Call @ {targets['call_strike']}  [{targets['method']}]")
        log.info(f"目標履約價 0050: Put @ {targets_0050['put_strike']}  Call @ {targets_0050['call_strike']}  [{targets_0050['method']}]")
        if indicators_0050:
            indicators_0050.update({
                'target_put_strike':  targets_0050['put_strike'],
                'target_call_strike': targets_0050['call_strike'],
                'method':             targets_0050['method'],
                'adx_regime':         targets_0050.get('adx_regime'),
            })

        # 5. 口數（用動態 beta）
        contracts = compute_contract_count(price_2330, taiex, beta=beta_2330_used)
        log.info(f"口數: {contracts['recommended_contracts']} (beta_ratio {contracts['beta_adjusted_ratio']:.2f})")

        # 5b. 多商品 portfolio 拆解（manual instruments[] + broker auto-merged）
        portfolio_breakdown = None
        if POSITIONS_INSTRUMENTS or _broker_positions:
            portfolio_breakdown = compute_portfolio_breakdown(
                POSITIONS_INSTRUMENTS or [],
                price_0050=price_0050, price_2330=price_2330, taiex=taiex,
                beta_0050=beta_0050_used, beta_2330=beta_2330_used,
                broker_positions=_broker_positions,
            )
            t = portfolio_breakdown['totals']
            log.info(f"Portfolio core_long: 名目 {t['core_long_notional']:,.0f}  "
                     f"beta-adj {t['core_long_beta_adj']:,.0f}  "
                     f"建議 {t['recommended_put_lots']} 口 put hedge  "
                     f"未實現 {t['core_long_unrealized']:+,.0f}")

        # 6. 抓 TXO 鏈
        chain = fetch_txo_chain(api, month)

        # 7. 選履約價（含 delta 過濾）
        hv_taiex = indicators['hv_taiex'] if indicators else 0.20
        # 交易日 / 252 比日曆日 / 365 準（短 DTE 差更大）
        T = trading_T(settlement_dt)
        dte_trading = trading_days_between(datetime.now().date(), settlement_dt.date())
        # 用近月自己的 ATM IV（比 HV 準很多，特別是短 DTE）
        near_iv, near_iv_src = fetch_atm_iv(api, chain, bs_s, T, hv_taiex, r=bs_r or 0.0, label='近月')
        log.info(f'Delta 篩選: target ≤ {CFG.delta_target}  IV={near_iv:.1%} ({near_iv_src})  '
                 f'DTE={dte}日(交易日={dte_trading})  T={T:.4f}y  S={bs_s}')
        put_c  = find_strike_with_delta(chain, taiex, T, near_iv, OptionRight.Put,  targets['put_strike'],  s_price=bs_s, r=bs_r)
        call_c = find_strike_with_delta(chain, taiex, T, near_iv, OptionRight.Call, targets['call_strike'], s_price=bs_s, r=bs_r)

        # 夜盤 delta 選出極 OTM 合約時，改用快取的合約（報價較有效）
        _cached_opts = _cached_full.get('selected_options')
        if _cached_opts and T > 0:
            cached_put_sym  = _cached_opts.get('put',  {}).get('symbol', '')
            cached_call_sym = _cached_opts.get('call', {}).get('symbol', '')
            cached_put_k    = _cached_opts.get('put',  {}).get('strike', put_c.strike_price)
            cached_call_k   = _cached_opts.get('call', {}).get('strike', call_c.strike_price)
            # 若 delta 選出的合約比快取更 OTM，改用快取
            put_delta_new  = abs(bs_delta(bs_s, put_c.strike_price,  T, near_iv, is_put=True,  r=bs_r))
            call_delta_new = abs(bs_delta(bs_s, call_c.strike_price, T, near_iv, is_put=False, r=bs_r))
            if put_delta_new < 0.01 or call_delta_new < 0.01:
                log.warning(f'夜盤 delta 太低 (put={put_delta_new:.4f} call={call_delta_new:.4f})，使用快取合約')
                put_c_fallback  = next((c for c in chain if c.symbol == cached_put_sym),  None)
                call_c_fallback = next((c for c in chain if c.symbol == cached_call_sym), None)
                if put_c_fallback and call_c_fallback:
                    put_c  = put_c_fallback
                    call_c = call_c_fallback

        put_delta  = bs_delta(bs_s, put_c.strike_price,  T, near_iv, is_put=True,  r=bs_r)
        call_delta = bs_delta(bs_s, call_c.strike_price, T, near_iv, is_put=False, r=bs_r)
        log.info(f'最終: Put {put_c.strike_price:.0f} (δ={put_delta:+.3f})  Call {call_c.strike_price:.0f} (δ={call_delta:+.3f})')

        # 7b. 找垂直價差的另一腳（spread_width 點外）
        put_spread_c  = _find_spread_leg(chain, put_c.strike_price,  OptionRight.Put,  is_put=True,  width=CFG.spread_width)
        call_spread_c = _find_spread_leg(chain, call_c.strike_price, OptionRight.Call, is_put=False, width=CFG.spread_width)
        if put_spread_c and call_spread_c:
            log.info(f'Spread legs: Put {put_c.strike_price:.0f}/{put_spread_c.strike_price:.0f}  '
                     f'Call {call_c.strike_price:.0f}/{call_spread_c.strike_price:.0f}')
        else:
            log.warning('無法找到足夠的 spread 合約')

        # 8. 抓選擇權報價（一次批次抓主力 + spread 腳，共 4 口）
        put_spread_c_saved  = put_spread_c   # 保留合約以供 BS 估算
        call_spread_c_saved = call_spread_c
        if put_spread_c and call_spread_c:
            all_snaps = api.snapshots([put_c, put_spread_c, call_c, call_spread_c])
            if len(all_snaps) < 4:
                log.warning(f'批次 snapshot 只回傳 {len(all_snaps)} 筆，退回單一抓取')
                put_spread_c = None
                call_spread_c = None

        def _q(snap):
            bid = float(snap.buy_price)  if snap.buy_price  else float(snap.close)
            ask = float(snap.sell_price) if snap.sell_price else float(snap.close)
            return {'bid': bid, 'ask': ask}

        if put_spread_c and call_spread_c:
            ps = _q(all_snaps[0])
            pl = _q(all_snaps[1])
            cs = _q(all_snaps[2])
            cl = _q(all_snaps[3])
            quotes = {
                'put_ask':    ps['ask'], 'put_bid': ps['bid'],
                'put_mid':    float(all_snaps[0].close),
                'call_bid':   cs['bid'], 'call_ask': cs['ask'],
                'call_mid':   float(all_snaps[2].close),
                'put_volume': int(all_snaps[0].volume) if all_snaps[0].volume else 0,
                'call_volume': int(all_snaps[2].volume) if all_snaps[2].volume else 0,
            }
            spread_quotes = {
                'put_short':  ps, 'put_long':  pl,
                'call_short': cs, 'call_long': cl,
            }
        else:
            try:
                quotes = fetch_option_quotes(api, put_c, call_c, bs_s=bs_s, T=T, sigma=near_iv, r=bs_r or 0.0)
            except (IndexError, Exception) as _e:
                log.warning(f'fetch_option_quotes 失敗（{_e}），使用快取報價')
                _co = _cached_full.get('selected_options', {})
                quotes = {
                    'put_ask':    _co.get('put',  {}).get('ask',  0.0),
                    'put_bid':    _co.get('put',  {}).get('bid',  0.0),
                    'put_mid':    _co.get('put',  {}).get('mid',  0.0),
                    'call_bid':   _co.get('call', {}).get('bid',  0.0),
                    'call_ask':   _co.get('call', {}).get('ask',  0.0),
                    'call_mid':   _co.get('call', {}).get('mid',  0.0),
                    'put_volume': _co.get('put',  {}).get('volume', 0),
                    'call_volume':_co.get('call', {}).get('volume', 0),
                }
            # 夜盤無法抓 spread leg 報價 → 用 BS 理論價估算
            if put_spread_c_saved and call_spread_c_saved:
                _pl_mid = bs_price(bs_s, put_spread_c_saved.strike_price,  T, near_iv, is_put=True,  r=bs_r)
                _cl_mid = bs_price(bs_s, call_spread_c_saved.strike_price, T, near_iv, is_put=False, r=bs_r)
                _slippage = 0.05  # bid/ask ±5% 估算
                spread_quotes = {
                    'put_short':  {'bid': quotes['put_bid'],  'ask': quotes['put_ask']},
                    'put_long':   {'bid': round(_pl_mid * (1 - _slippage)), 'ask': round(_pl_mid * (1 + _slippage))},
                    'call_short': {'bid': quotes['call_bid'], 'ask': quotes['call_ask']},
                    'call_long':  {'bid': round(_cl_mid * (1 - _slippage)), 'ask': round(_cl_mid * (1 + _slippage))},
                }
                log.info(f'BS 估算外腳: Put長腳 mid={_pl_mid:.1f}  Call長腳 mid={_cl_mid:.1f}')
            else:
                spread_quotes = None
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

        # 10. 0050 股期 2口 對應 TXO 領式（用動態 beta_0050_used）
        notional_0050    = CFG.lots_0050 * CFG.lot_size_0050 * price_0050
        beta_ratio_0050  = (notional_0050 * beta_0050_used) / (taiex * 50)
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

        # 9b. 垂直價差策略
        _put_spread_leg  = put_spread_c  or put_spread_c_saved
        _call_spread_leg = call_spread_c or call_spread_c_saved
        spreads = []
        if spread_quotes and _put_spread_leg and _call_spread_leg:
            try:
                spreads = build_spreads(
                    n_contracts=contracts['recommended_contracts'],
                    put_short_strike=put_c.strike_price,
                    put_short_bid=spread_quotes['put_short']['bid'],
                    put_short_ask=spread_quotes['put_short']['ask'],
                    put_long_strike=_put_spread_leg.strike_price,
                    put_long_bid=spread_quotes['put_long']['bid'],
                    put_long_ask=spread_quotes['put_long']['ask'],
                    call_short_strike=call_c.strike_price,
                    call_short_bid=spread_quotes['call_short']['bid'],
                    call_short_ask=spread_quotes['call_short']['ask'],
                    call_long_strike=_call_spread_leg.strike_price,
                    call_long_bid=spread_quotes['call_long']['bid'],
                    call_long_ask=spread_quotes['call_long']['ask'],
                )
                for sp in spreads:
                    log.info(f'  Spread {sp.name}: net={sp.net_per_point:+.1f}  '
                             f'max_profit={sp.max_profit_ntd/10000:.1f}萬  be={sp.breakeven:.0f}')
            except Exception as e:
                log.warning(f'build_spreads 失敗: {e}')

        # 10b. 遠月建議（結算日換倉目標）
        far_month_data = None
        try:
            far_month, far_settlement_dt = get_far_month_settlement(settlement_dt)
            far_dte = max(0, (far_settlement_dt.date() - datetime.now().date()).days)
            log.info(f'遠月 TXO: {far_month}  結算: {far_settlement_dt.date()}  DTE: {far_dte}')

            far_targets = compute_target_strikes(price_2330, taiex, indicators, far_dte)
            log.info(f"遠月目標: Put @ {far_targets['put_strike']}  Call @ {far_targets['call_strike']}")

            far_chain = fetch_txo_chain(api, far_month)
            far_T     = trading_T(far_settlement_dt)
            far_iv, far_iv_src = fetch_atm_iv(api, far_chain, bs_s, far_T, hv_taiex, r=bs_r or 0.0, label='遠月')
            far_put_c  = find_strike_with_delta(far_chain, taiex, far_T, far_iv, OptionRight.Put,  far_targets['put_strike'],  s_price=bs_s, r=bs_r)
            far_call_c = find_strike_with_delta(far_chain, taiex, far_T, far_iv, OptionRight.Call, far_targets['call_strike'], s_price=bs_s, r=bs_r)

            far_put_delta  = bs_delta(bs_s, far_put_c.strike_price,  far_T, far_iv, is_put=True,  r=bs_r)
            far_call_delta = bs_delta(bs_s, far_call_c.strike_price, far_T, far_iv, is_put=False, r=bs_r)
            if abs(far_put_delta) < 0.05 or abs(far_call_delta) < 0.05:
                # 以近月履約價為參考，找遠月鏈最近合約
                _fp = min((c for c in far_chain if c.option_right == OptionRight.Put),
                          key=lambda c: abs(c.strike_price - put_c.strike_price), default=None)
                _fc = min((c for c in far_chain if c.option_right == OptionRight.Call),
                          key=lambda c: abs(c.strike_price - call_c.strike_price), default=None)
                if _fp and _fc:
                    log.warning(f'遠月 delta 太低，改用近月參考 Put {_fp.strike_price} Call {_fc.strike_price}')
                    far_put_c, far_call_c = _fp, _fc
                    far_put_delta  = bs_delta(bs_s, far_put_c.strike_price,  far_T, far_iv, is_put=True,  r=bs_r)
                    far_call_delta = bs_delta(bs_s, far_call_c.strike_price, far_T, far_iv, is_put=False, r=bs_r)
            log.info(f'遠月最終: Put {far_put_c.strike_price:.0f} (δ={far_put_delta:+.3f})  Call {far_call_c.strike_price:.0f} (δ={far_call_delta:+.3f})')

            far_quotes     = fetch_option_quotes(api, far_put_c, far_call_c, bs_s=bs_s, T=far_T, sigma=far_iv, r=bs_r or 0.0)
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
                'settlement_date': far_settlement_dt.strftime('%Y%m%d'),
                'dte':       far_dte,
                'dte_trading': trading_days_between(datetime.now().date(), far_settlement_dt.date()),
                'iv_used':   round(far_iv, 4),
                'iv_source': far_iv_src,
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
            # 週選結算日字串 YYYYMMDD → 交易日 / 252
            try:
                w_settle = datetime.strptime(w_info['settlement_date'], '%Y%m%d').date()
                w_T = trading_T(w_settle)
                w_dte_trading = trading_days_between(datetime.now().date(), w_settle)
            except Exception:
                w_T = max(w_dte, 1) / 365
                w_dte_trading = w_dte
            w_tgt   = compute_target_strikes(price_2330, taiex, indicators, w_dte)
            # 用週選自己的 ATM IV — 短 DTE 通常 IV 比月選高 30-100%
            w_iv, w_iv_src = fetch_atm_iv(api, w_chain, bs_s, w_T, hv_taiex, r=bs_r or 0.0, label=label)
            w_put_c  = find_strike_with_delta(w_chain, taiex, w_T, w_iv, OptionRight.Put,  w_tgt['put_strike'],  s_price=bs_s, r=bs_r)
            w_call_c = find_strike_with_delta(w_chain, taiex, w_T, w_iv, OptionRight.Call, w_tgt['call_strike'], s_price=bs_s, r=bs_r)
            w_pd = bs_delta(bs_s, w_put_c.strike_price,  w_T, w_iv, is_put=True,  r=bs_r)
            w_cd = bs_delta(bs_s, w_call_c.strike_price, w_T, w_iv, is_put=False, r=bs_r)
            if abs(w_pd) < 0.01 or abs(w_cd) < 0.01:
                # 以近月履約價為參考，找週選鏈最近合約
                _wp = min((c for c in w_chain if c.option_right == OptionRight.Put),
                          key=lambda c: abs(c.strike_price - put_c.strike_price), default=None)
                _wc2 = min((c for c in w_chain if c.option_right == OptionRight.Call),
                           key=lambda c: abs(c.strike_price - call_c.strike_price), default=None)
                if _wp and _wc2:
                    log.warning(f'{label} delta 太低，改用近月參考 Put {_wp.strike_price} Call {_wc2.strike_price}')
                    w_put_c, w_call_c = _wp, _wc2
                    w_pd = bs_delta(bs_s, w_put_c.strike_price,  w_T, w_iv, is_put=True,  r=bs_r)
                    w_cd = bs_delta(bs_s, w_call_c.strike_price, w_T, w_iv, is_put=False, r=bs_r)
            log.info(f'{label} 最終: Put {w_put_c.strike_price:.0f} (δ={w_pd:+.3f})  Call {w_call_c.strike_price:.0f} (δ={w_cd:+.3f})')
            w_q = fetch_option_quotes(api, w_put_c, w_call_c, bs_s=bs_s, T=w_T, sigma=w_iv, r=bs_r or 0.0)

            # 找價差另一腳並抓真實 bid/ask（取代 BS 估算，避免 vol skew 失真）
            # 各腳 snapshot 最近 5 個 candidate，挑第一個有真實 ask 的；
            # 順便累積 (strike, mid, is_put) 做為 skew 曲線輸入
            w_spread_legs: Dict[str, Any] = {}
            skew_input: List[Tuple[float, float, bool]] = []
            for ref_c, opt_right, leg_key in [
                (w_put_c,  OptionRight.Put,  'put_buy'),
                (w_call_c, OptionRight.Call, 'call_buy'),
            ]:
                is_put = (opt_right == OptionRight.Put)
                ref_strike = ref_c.strike_price
                target = ref_strike - CFG.spread_width if is_put else ref_strike + CFG.spread_width
                cands = sorted(
                    [c for c in w_chain
                     if c.option_right == opt_right
                     and (c.strike_price < ref_strike if is_put else c.strike_price > ref_strike)],
                    key=lambda c: abs(c.strike_price - target),
                )[:5]
                if not cands:
                    continue
                try:
                    snaps = api.snapshots(cands)
                except Exception as _le:
                    log.debug(f'{label} {leg_key} snapshot 失敗: {_le}')
                    continue
                # 累積 skew 點：每個 candidate 的 mid → IV
                for cc, ss in zip(cands, snaps):
                    bb = float(ss.buy_price)  if ss.buy_price  else 0.0
                    aa = float(ss.sell_price) if ss.sell_price else 0.0
                    mm = (bb + aa) / 2 if (bb > 0 and aa > 0) else (float(ss.close) if ss.close else 0.0)
                    if mm > 0:
                        skew_input.append((float(cc.strike_price), mm, is_put))
                # 找第一個有真實 ask 的
                chosen = None
                for c, s in zip(cands, snaps):
                    if s.sell_price and float(s.sell_price) > 0:
                        chosen = (c, s)
                        break
                # 都沒 ask → 用最近的 fallback（之後 BS 估算）
                if not chosen:
                    chosen = (cands[0], snaps[0])
                c, s = chosen
                bid = float(s.buy_price)  if s.buy_price  else 0.0
                ask = float(s.sell_price) if s.sell_price else 0.0
                mid = float(s.close)      if s.close      else (bid + ask) / 2 if (bid and ask) else 0.0
                w_spread_legs[leg_key] = {
                    'strike': c.strike_price,
                    'symbol': c.symbol,
                    'bid':    bid,
                    'ask':    ask,
                    'mid':    mid,
                }

            # 加入 ATM put + ATM call 的 mid 做曲線錨點
            for sel_c, sel_q_mid, sel_is_put in [
                (w_put_c,  w_q.get('put_mid'),  True),
                (w_call_c, w_q.get('call_mid'), False),
            ]:
                if sel_q_mid and sel_q_mid > 0:
                    skew_input.append((float(sel_c.strike_price), float(sel_q_mid), sel_is_put))

            w_skew = build_skew_from_snaps(skew_input, bs_s, w_T, r=bs_r or 0.0)
            if w_skew:
                log.info(f'{label} skew curve: {len(w_skew)} 點 '
                         f'({w_skew[0][0]:.0f}→{w_skew[0][1]:.1%} ... '
                         f'{w_skew[-1][0]:.0f}→{w_skew[-1][1]:.1%})')
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
                'dte_trading': w_dte_trading,
                'iv_used':    round(w_iv, 4),
                'iv_source':  w_iv_src,
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
                'spread_legs': w_spread_legs,
                'skew': [{'strike': K, 'iv': round(iv, 4)} for K, iv in w_skew],
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
        # 報價來源以「實際 bid/ask 是否非 0」判斷，而非時段：夜盤 TXO 有真實報價
        _has_live_quote = bool(
            (quotes.get('put_bid', 0) or 0) > 0 or
            (quotes.get('call_bid', 0) or 0) > 0 or
            (quotes.get('put_ask', 0) or 0) > 0 or
            (quotes.get('call_ask', 0) or 0) > 0
        )
        _quotes_source = 'live' if _has_live_quote else 'bs_estimate'
        _market_session = market_session_label()
        result = {
            'timestamp': datetime.now().isoformat(),
            'quotes_source': _quotes_source,
            'market_session': _market_session,
            'config': {
                'beta': CFG.beta,
                'large_futures_lots': CFG.large_futures_lots,
                'delta_target': CFG.delta_target,
            },
            'betas': {
                'beta_2330':        round(beta_2330_used, 3),
                'beta_2330_source': beta_2330_source,
                'beta_0050':        round(beta_0050_used, 3),
                'beta_0050_source': beta_0050_source,
                'beta_2330_default': CFG.beta,
                'beta_0050_default': CFG.beta_0050,
            },
            'positions': {
                'large_futures_lots': CFG.large_futures_lots,
                'lots_0050':          CFG.lots_0050,
                'lot_size_0050':      CFG.lot_size_0050,
                'cost_basis_0050':    CFG.cost_basis_0050,
                'source':             POSITIONS_SOURCE,
            },
            'portfolio': portfolio_breakdown,   # None if no instruments[] config
            'ledger':    None,                  # filled below
            'trend':     None,                  # filled below
            'roll_suggestions': [],             # filled below
            'broker':    None,                  # filled below if CA active
            'weekly_opportunities': None,       # filled below
            'portfolio_greeks': None,           # broker 選擇權部位 Greeks 聚合
            'greeks_history':   None,           # 過去 30 天 Greeks 趨勢 + 累積 theta
            'health_check':     None,           # 持倉健診評分（對齊 SOP）
            'collar_dashboard': None,           # collar 整合 dashboard（per-leg + 結構推薦）
            'event_history':    None,           # 歷史事件 P&L 解析（events analysis CLI 寫入）
            'upcoming_events': [],              # 未來 14 天高影響事件
            'market': market,
            'txo_month': month,
            'dte': dte,
            'dte_trading': dte_trading,
            'indicators': indicators,
            'indicators_0050': indicators_0050,
            'iv_used':    round(near_iv, 4),
            'iv_source':  near_iv_src,
            'targets': {
                'target_put_strike':  targets['put_strike'],
                'target_call_strike': targets['call_strike'],
                'method': targets['method'],
                'adx_regime': targets.get('adx_regime'),
                'atr_mult':   round(targets.get('atr_mult', 0), 3),
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
            'spreads':    [asdict(s) for s in spreads],
            'collar_0050': {
                'price_0050':          price_0050,
                'chg_0050':            chg_0050,
                'chgpct_0050':         chgpct_0050,
                'lots':                CFG.lots_0050,
                'lot_size':            CFG.lot_size_0050,
                'notional':            notional_0050,
                'beta':                round(beta_0050_used, 3),
                'beta_source':         beta_0050_source,
                'beta_adj_ratio':      beta_ratio_0050,
                'recommended_contracts': n_contracts_0050,
                'structures':          [asdict(s) for s in structures_0050],
            },
            'far_month':   far_month_data,
            'weekly_wed':  weekly_wed_data,
            'weekly_fri':  weekly_fri_data,
        }

        # 14b. Trading ledger summary（若 trades_ledger.json 存在）
        try:
            import ledger as _L
            result['ledger'] = _L.summary()
            if result['ledger']:
                _ls = result['ledger']
                log.info(f"Ledger: {_ls['open_count']} open / {_ls['closed_count']} closed  "
                         f"MTD {_ls['mtd_realized']:+,.0f} / Lifetime {_ls['lifetime_realized']:+,.0f}")
        except Exception as _e:
            log.debug(f'ledger summary 失敗（可忽略）: {_e}')

        # 14c. Daily snapshot trend（先讀歷史；當日 capture 延後到所有 enrichment 後）
        try:
            import snapshot as _S
            result['trend'] = _S.trend_summary(window_days=60)
            if result.get('trend'):
                _ch = result['trend']['changes']
                _y = _ch.get('vs_yesterday')
                if _y:
                    log.info(f"Trend vs 昨日: TX {_y['tx_delta_pct']:+.2f}%  "
                             f"unrealized Δ {_y['unrealized_delta'] or 0:+,.0f}")
        except Exception as _e:
            log.debug(f'snapshot trend 失敗（可忽略）: {_e}')

        # 14d. Roll 操作建議
        try:
            import roll_advisor as _RA
            result['roll_suggestions'] = _RA.compute_roll_suggestions(result)
            for _sug in result['roll_suggestions']:
                log.info(f"Roll [{_sug['priority']}] {_sug['reason']}")
        except Exception as _e:
            log.debug(f'roll_advisor 失敗（可忽略）: {_e}')

        # 14e-bis. 本週機會（週三 + 週五各算一份，按 DTE 升序）
        try:
            opps = []
            for wd in (weekly_wed_data, weekly_fri_data):
                if not wd:
                    continue
                o = compute_weekly_opportunities(wd, bs_s, spread_width=CFG.spread_width)
                if o:
                    opps.append(o)
            opps.sort(key=lambda x: x.get('dte_trading', 999))
            if opps:
                result['weekly_opportunities'] = opps
                for o in opps:
                    log.info(f"本週機會 {o['weekday_label']}{o['series']}: "
                             f"賣call@{o['sell_call']['strike']:.0f} 收{o['sell_call']['income_ntd']:,} | "
                             f"bull put {o['bull_put_spread']['sell_strike']:.0f}/{o['bull_put_spread']['buy_strike']:.0f} "
                             f"收{o['bull_put_spread']['max_profit']:,}")
        except Exception as _e:
            log.debug(f'weekly_opportunities 失敗: {_e}')

        # 14e. Broker 真實持倉同步（在 login 階段已抓，這裡寫進 result）
        if _broker_positions is not None:
            try:
                import broker_sync as _BS2
                result['broker'] = _BS2.summary(_broker_positions)
                if result['broker']:
                    log.info(f"Broker: {result['broker']['total_positions']} 筆持倉  "
                             f"總 P&L {result['broker']['total_pnl']:+,.0f}")
            except Exception as _e:
                log.debug(f'broker summary 失敗: {_e}')

        # 14f. Portfolio Greeks（broker 選擇權持倉聚合）
        try:
            _default_iv = locals().get('near_iv') or 0.20
            pg = compute_portfolio_greeks(api, _broker_positions, bs_s, default_iv=_default_iv)
            if pg:
                result['portfolio_greeks'] = pg
                t = pg['totals']
                log.info(f"Portfolio Greeks: Δ={t['net_delta']:+.2f} ({t['delta_ntd_per_1pct_tx']:+,} NT/1%TX)  "
                         f"θ={t['theta_ntd_per_day']:+,} NT/day  ν={t['vega_ntd_per_pct_iv']:+,} NT/1%IV  "
                         f"({len(pg['legs'])} legs)")
        except Exception as _e:
            log.debug(f'portfolio greeks 失敗: {_e}')

        # 14f-bis. 持倉健診評分（依賴 portfolio_greeks 已算完）
        try:
            import health_check as _HC
            hc = _HC.evaluate(result)
            if hc:
                result['health_check'] = hc
                log.info(f"健診評分: {hc['overall_score']}/100 ({hc['grade']})  "
                         f"violations={len(hc['violations'])}  suggestions={len(hc['suggestions'])}")
        except Exception as _e:
            log.debug(f'health_check 失敗: {_e}')

        # 14f-ter. Collar dashboard（per-leg triggers + 結構推薦）
        try:
            import collar_dashboard as _CD
            cd = _CD.evaluate(result)
            if cd:
                result['collar_dashboard'] = cd
                rs = cd.get('recommended_structure', {})
                log.info(f"Collar 推薦結構: {rs.get('label', '?')}  ({rs.get('reason', '')})")
        except Exception as _e:
            log.debug(f'collar_dashboard 失敗: {_e}')

        # 14h-pre. 歷史事件 P&L 解析（從 event_history.json 載入；event_analysis.py CLI 產生）
        try:
            from pathlib import Path as _PP
            _eh_path = _PP(__file__).parent / 'event_history.json'
            if _eh_path.exists():
                result['event_history'] = json.loads(_eh_path.read_text(encoding='utf-8'))
                _eh = result['event_history']
                log.info(f"歷史事件解析載入：{_eh.get('event_count', 0)} 筆事件 "
                         f"({len(_eh.get('aggregates', {}))} 類)")
        except Exception as _e:
            log.debug(f'event_history 載入失敗: {_e}')

        # 14h. 高影響事件（events.json）— 未來 14 天清單
        try:
            import events as _EV
            result['upcoming_events'] = _EV.upcoming(window_days=14)
            if result['upcoming_events']:
                log.info(f"upcoming events: {len(result['upcoming_events'])} 筆 "
                         f"(最近：{result['upcoming_events'][0]['name']} +{result['upcoming_events'][0]['days_until']}d)")
        except Exception as _e:
            log.debug(f'events 失敗: {_e}')

        # 14i. Daily snapshot capture（在所有 enrichment 後，含 Greeks）+ greeks history
        try:
            import snapshot as _S2
            _S2.capture(result)
            result['greeks_history'] = _S2.greeks_trend(window_days=30)
            if result.get('greeks_history'):
                _gh = result['greeks_history']
                log.info(f"Greeks history: {_gh['snapshot_count']} 天紀錄  "
                         f"累積 theta（30d/lifetime）: "
                         f"{_gh['cumulative']['last_30d']:+,} / {_gh['cumulative']['lifetime']:+,} NT")
        except Exception as _e:
            log.debug(f'snapshot.capture / greeks_trend 失敗: {_e}')

        # 報價回填保護：當「這次抓到 BS 估算」且「快取有真實報價」時才回填
        # （之前條件是 not is_market_hours，現在夜盤 TXO 也有真實報價，改用 quotes_source 直接判斷）
        if _quotes_source == 'bs_estimate' and _cached_full.get('quotes_source') == 'live':
            today = datetime.now().date()

            # 快取時效檢查（>7 天直接放棄）
            cache_age_hr = float('inf')
            try:
                _ts = datetime.fromisoformat(_cached_full.get('timestamp', ''))
                cache_age_hr = (datetime.now() - _ts).total_seconds() / 3600
            except Exception:
                pass

            def _section_still_valid(key: str) -> bool:
                if cache_age_hr > 168:        # 超過 1 週的快取整批棄用
                    return False
                if key in ('selected_options', 'structures', 'spreads', 'collar_0050'):
                    # 必須是當前 TXO 月份（避免使用上個月已結算的合約）
                    return _cached_full.get('txo_month') == month
                if key in ('far_month', 'weekly_wed', 'weekly_fri'):
                    # 結算日必須仍在未來（避免使用已結算的週選/遠月）
                    sec = _cached_full.get(key) or {}
                    try:
                        settle = datetime.strptime(
                            str(sec.get('settlement_date', '')), '%Y%m%d').date()
                        return settle > today
                    except Exception:
                        return False
                return True

            def _is_bs_estimated(section):
                opts = (section or {}).get('selected_options', {})
                return (opts.get('put',  {}).get('volume') == 0 and
                        opts.get('call', {}).get('volume') == 0)

            for key in ('selected_options', 'structures', 'spreads',
                        'far_month', 'weekly_wed', 'weekly_fri', 'collar_0050'):
                cached_val = _cached_full.get(key)
                if cached_val is None:
                    continue
                if not _section_still_valid(key):
                    log.info(f'夜盤：{key} 快取已過期或合約過時，棄用')
                    continue
                new_val = result.get(key)
                # 如果新算的是 BS 估算（量=0）或 None，用快取取代
                if key in ('selected_options', 'far_month', 'weekly_wed', 'weekly_fri'):
                    if new_val is None or _is_bs_estimated(
                            {'selected_options': new_val} if key == 'selected_options' else new_val):
                        result[key] = cached_val
                        log.info(f'夜盤：{key} 使用快取資料')
                elif key in ('structures', 'spreads'):
                    if not new_val:
                        result[key] = cached_val
                        log.info(f'夜盤：{key} 使用快取資料')
                elif key == 'collar_0050':
                    c0050_opts = (cached_val.get('structures') or [])
                    if c0050_opts and not (result.get('collar_0050') or {}).get('structures'):
                        result['collar_0050']['structures'] = c0050_opts

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

        # 14. 觸發 alerts（不阻塞主流程；失敗不影響策略更新）
        try:
            import alerts
            alerts.main()
        except Exception as _e:
            log.warning(f'alerts 模組執行失敗（不影響策略結果）: {_e}')

    except Exception as e:
        log.exception(f'Error: {e}')
        raise
    finally:
        api.logout()
        log.info('Logged out')


if __name__ == '__main__':
    main()
