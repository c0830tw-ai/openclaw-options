"""
strength_rules.py
=================
價格強弱判斷模組。

用途：
- 給選擇權 app / dashboard / alert 使用。
- 把「位置 → 結構 → 動能 → 量能」轉成 0-9 分規則。
- 不直接下單，只輸出 bias、score、label、actions。

核心分數：
1. 價格在 MA20 上方
2. MA20 上彎
3. 近期高點墊高
4. 近期低點墊高
5. RSI > 50
6. MACD 在 0 軸上方
7. MACD hist 轉正或放大
8. OBV 突破前高 / 墊高
9. 突破時量能放大

分數解讀：
7-9：強勢
5-6：偏強
3-4：盤整
1-2：偏弱
0 以下：弱勢
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


Number = float | int


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _sma(values: List[Number], period: int) -> Optional[float]:
    if not values or len(values) < period:
        return None
    window = [_safe_float(v) for v in values[-period:]]
    if any(v is None for v in window):
        return None
    return sum(window) / period  # type: ignore[arg-type]


def _obv(closes: List[Number], volumes: List[Number]) -> List[float]:
    """計算 OBV；資料不足或量為 None 時回空陣列。"""
    if not closes or not volumes or len(closes) != len(volumes) or len(closes) < 2:
        return []
    out = [0.0]
    for i in range(1, len(closes)):
        c0 = _safe_float(closes[i - 1])
        c1 = _safe_float(closes[i])
        vol = _safe_float(volumes[i])
        if c0 is None or c1 is None or vol is None:
            out.append(out[-1])
        elif c1 > c0:
            out.append(out[-1] + vol)
        elif c1 < c0:
            out.append(out[-1] - vol)
        else:
            out.append(out[-1])
    return out


def _recent_structure(
    highs: List[Number],
    lows: List[Number],
    lookback: int = 10,
) -> Tuple[Optional[bool], Optional[bool]]:
    """回傳 (higher_high, higher_low)。用最近 lookback 根 vs 前 lookback 根。"""
    n = len(highs)
    if n < lookback * 2 or len(lows) < n:
        return None, None

    h_recent = max(float(v) for v in highs[-lookback:])
    h_prev = max(float(v) for v in highs[-lookback * 2:-lookback])
    l_recent = min(float(v) for v in lows[-lookback:])
    l_prev = min(float(v) for v in lows[-lookback * 2:-lookback])

    return h_recent > h_prev, l_recent > l_prev


def _macd_hist_improving(macd_hist_series: Optional[List[Number]], macd_hist: Optional[Number]) -> bool:
    """柱狀體轉正或比前一根放大。"""
    cur = _safe_float(macd_hist)
    if cur is None:
        return False
    if cur > 0:
        return True
    if macd_hist_series and len(macd_hist_series) >= 2:
        prev = _safe_float(macd_hist_series[-2])
        return prev is not None and cur > prev
    return False


def classify_strength(score: int) -> Dict[str, str]:
    """依 0-9 分回傳狀態與操作態度。"""
    if score >= 7:
        return {
            "label": "強勢",
            "bias": "bullish",
            "emoji": "🟢",
            "action": "順勢偏多；Pullback 優先找多點，不宜逆勢賣 Call 太近。",
        }
    if score >= 5:
        return {
            "label": "偏強",
            "bias": "mild_bullish",
            "emoji": "🟡",
            "action": "偏多但不追價；等 5 分K回測不破或 MACD/KD 轉強再執行。",
        }
    if score >= 3:
        return {
            "label": "盤整",
            "bias": "neutral",
            "emoji": "⚪",
            "action": "用箱型/KD/布林；避免單看 MACD 金叉死叉追單。",
        }
    if score >= 1:
        return {
            "label": "偏弱",
            "bias": "mild_bearish",
            "emoji": "🟠",
            "action": "多單降低槓桿或補保護；反彈先視為反彈，不急著追多。",
        }
    return {
        "label": "弱勢",
        "bias": "bearish",
        "emoji": "🔴",
        "action": "優先避險/降曝險；賣 Put 要拉遠或暫停，偏向買 Put / Put spread 防守。",
    }


def compute_strength_score(
    *,
    closes: List[Number],
    highs: Optional[List[Number]] = None,
    lows: Optional[List[Number]] = None,
    volumes: Optional[List[Number]] = None,
    rsi: Optional[Number] = None,
    macd: Optional[Number] = None,
    macd_hist: Optional[Number] = None,
    macd_hist_series: Optional[List[Number]] = None,
    ma20: Optional[Number] = None,
    lookback: int = 10,
) -> Dict[str, Any]:
    """
    計算強弱分數。

    參數說明：
    - closes/highs/lows/volumes：同一時間週期 K 線資料，例如日K、60分K、30分K、5分K。
    - rsi/macd/macd_hist：可直接餵既有指標，避免重複計算。
    - macd_hist_series：若有歷史柱狀體，會用來判斷柱狀體是否放大。
    - ma20：若外部已算好可直接傳；否則用 closes 算。
    """
    if not closes or len(closes) < 21:
        base = classify_strength(0)
        return {
            "score": 0,
            "max_score": 9,
            "label": "資料不足",
            "bias": "unknown",
            "emoji": "⚪",
            "action": "K 線不足，至少需要 21 根；完整評分建議 60 根以上。",
            "checks": {},
        }

    c = _safe_float(closes[-1])
    prev_ma20 = _sma(closes[:-1], 20)
    ma20_v = _safe_float(ma20) or _sma(closes, 20)

    higher_high, higher_low = (None, None)
    if highs and lows:
        higher_high, higher_low = _recent_structure(highs, lows, lookback=lookback)

    rsi_v = _safe_float(rsi)
    macd_v = _safe_float(macd)
    macd_hist_v = _safe_float(macd_hist)

    obv_line = _obv(closes, volumes or [])
    obv_break_high = None
    obv_rising = None
    if len(obv_line) >= lookback * 2:
        recent_obv_high = max(obv_line[-lookback:])
        prev_obv_high = max(obv_line[-lookback * 2:-lookback])
        obv_break_high = recent_obv_high > prev_obv_high
        obv_rising = obv_line[-1] > obv_line[-min(lookback, len(obv_line))]

    volume_breakout = None
    if volumes and len(volumes) >= 21:
        last_vol = _safe_float(volumes[-1])
        avg20_vol = _sma(volumes[:-1], 20)
        volume_breakout = bool(last_vol is not None and avg20_vol is not None and last_vol > avg20_vol * 1.2)

    checks: Dict[str, Dict[str, Any]] = {
        "price_above_ma20": {
            "pass": bool(c is not None and ma20_v is not None and c > ma20_v),
            "weight": 1,
            "label": "價格在 MA20 上方",
            "value": round(c, 2) if c is not None else None,
            "ref": round(ma20_v, 2) if ma20_v is not None else None,
        },
        "ma20_rising": {
            "pass": bool(ma20_v is not None and prev_ma20 is not None and ma20_v > prev_ma20),
            "weight": 1,
            "label": "MA20 上彎",
            "value": round(ma20_v, 2) if ma20_v is not None else None,
            "ref": round(prev_ma20, 2) if prev_ma20 is not None else None,
        },
        "higher_high": {
            "pass": bool(higher_high),
            "weight": 1,
            "label": "高點墊高",
            "value": higher_high,
        },
        "higher_low": {
            "pass": bool(higher_low),
            "weight": 1,
            "label": "低點墊高",
            "value": higher_low,
        },
        "rsi_above_50": {
            "pass": bool(rsi_v is not None and rsi_v > 50),
            "weight": 1,
            "label": "RSI > 50",
            "value": round(rsi_v, 1) if rsi_v is not None else None,
        },
        "macd_above_zero": {
            "pass": bool(macd_v is not None and macd_v > 0),
            "weight": 1,
            "label": "MACD 在 0 軸上方",
            "value": round(macd_v, 3) if macd_v is not None else None,
        },
        "macd_hist_improving": {
            "pass": _macd_hist_improving(macd_hist_series, macd_hist_v),
            "weight": 1,
            "label": "MACD 柱狀體轉正或放大",
            "value": round(macd_hist_v, 3) if macd_hist_v is not None else None,
        },
        "obv_confirming": {
            "pass": bool(obv_break_high or obv_rising),
            "weight": 1,
            "label": "OBV 突破前高或墊高",
            "value": {
                "break_high": obv_break_high,
                "rising": obv_rising,
            },
        },
        "volume_breakout": {
            "pass": bool(volume_breakout),
            "weight": 1,
            "label": "突破量能放大",
            "value": volume_breakout,
        },
    }

    score = sum(item["weight"] for item in checks.values() if item["pass"])
    state = classify_strength(score)

    return {
        "score": score,
        "max_score": 9,
        "label": state["label"],
        "bias": state["bias"],
        "emoji": state["emoji"],
        "action": state["action"],
        "checks": checks,
    }


def summarize_multi_timeframe_strength(
    *,
    strength_60m: Optional[Dict[str, Any]] = None,
    strength_30m: Optional[Dict[str, Any]] = None,
    strength_5m: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把 60/30/5 分K強弱整合成交易用結論。"""
    s60 = strength_60m or {}
    s30 = strength_30m or {}
    s5 = strength_5m or {}

    b60 = s60.get("bias")
    b30 = s30.get("bias")
    b5 = s5.get("bias")

    if b60 in ("bullish", "mild_bullish") and b30 in ("bullish", "mild_bullish") and b5 in ("bullish", "mild_bullish"):
        mode = "trend_long"
        conclusion = "60/30/5 分K同步偏強，可順勢做多；避險 Put 可縮小但不歸零。"
    elif b60 in ("bullish", "mild_bullish") and b5 in ("mild_bearish", "bearish"):
        mode = "bull_pullback"
        conclusion = "大週期偏多，5 分K轉弱先視為拉回；等 5 分K止跌再找多，不急著翻空。"
    elif b60 in ("bearish", "mild_bearish") and b5 in ("bullish", "mild_bullish"):
        mode = "bear_rebound"
        conclusion = "大週期偏弱，5 分K轉強先視為反彈；不追多，偏向逢高減碼或補避險。"
    elif b60 in ("bearish", "mild_bearish") and b30 in ("bearish", "mild_bearish") and b5 in ("bearish", "mild_bearish"):
        mode = "trend_short_or_hedge"
        conclusion = "60/30/5 分K同步偏弱，優先避險；賣 Put 應拉遠或暫停。"
    elif b60 == "neutral" or b30 == "neutral":
        mode = "range"
        conclusion = "盤整盤；用箱型、布林與 KD，不宜追 MACD 金叉死叉。"
    else:
        mode = "mixed"
        conclusion = "多空訊號混雜；降低槓桿，等 30 分K位置或 60 分K方向確認。"

    return {
        "mode": mode,
        "conclusion": conclusion,
        "timeframes": {
            "60m": s60,
            "30m": s30,
            "5m": s5,
        },
    }
