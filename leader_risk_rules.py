"""
leader_risk_rules.py
====================

主流股 / 千金股轉弱風險判斷模組。

用途：
- 給選擇權 app / dashboard / alert 使用。
- 把「千金股跌破月線、月線下彎」轉成可顯示的市場風險狀態。
- 不直接下單，只輸出 risk_level、bias、leverage_cap、hedge_action。

核心觀念：
- 多檔千金股跌破 MA20，代表主流高價股籌碼鬆動。
- 多檔千金股 MA20 下彎，代表反彈容易遇壓，行情由進攻轉防守。
- 這不是立刻判定空頭，但應降低 0050 股期槓桿，避免在主流退潮時加碼。

建議配合 strength_rules.py：
- strength_rules.py 判斷單一標的 / 多週期強弱。
- leader_risk_rules.py 判斷主流股群體風險，作為 0050 股期與 TXO 避險濾網。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


Number = float | int


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _ratio(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return count / total


def classify_leader_risk(score: int) -> Dict[str, Any]:
    """依 0-100 分回傳主流股風險狀態。"""
    if score >= 75:
        return {
            "risk_level": "高風險",
            "bias": "risk_off",
            "emoji": "🔴",
            "leverage_cap": "0050 股期實際槓桿建議壓到 2 倍以下；先降口數，不只靠 Put。",
            "hedge_action": "固定 Put Spread；Covered Call 可做半套；停止追多與停止賣近價 Put。",
        }
    if score >= 55:
        return {
            "risk_level": "偏高風險",
            "bias": "defensive",
            "emoji": "🟠",
            "leverage_cap": "0050 股期實際槓桿建議 2～3 倍以內；不加碼。",
            "hedge_action": "槓桿超過 3 倍就加 Put Spread；反彈靠近月線可賣部分 Covered Call。",
        }
    if score >= 35:
        return {
            "risk_level": "中性偏防守",
            "bias": "neutral_defensive",
            "emoji": "🟡",
            "leverage_cap": "0050 股期可維持核心倉，但不重壓；等待主流股站回 MA20。",
            "hedge_action": "保留小 Put Spread 或用 Covered Call 降成本；不把避險歸零。",
        }
    return {
        "risk_level": "正常",
        "bias": "normal",
        "emoji": "🟢",
        "leverage_cap": "可依原本趨勢策略操作，但仍以總名目本金 / 權益數控槓桿。",
        "hedge_action": "避險可縮小，但不建議完全取消。",
    }


def compute_leader_breakdown_risk(
    *,
    leaders: List[Dict[str, Any]],
    index_state: Optional[Dict[str, Any]] = None,
    core_etf_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    計算主流股轉弱風險。

    leaders 每個元素建議格式：
    {
        "symbol": "6669",
        "name": "緯穎",
        "close": 3000,
        "ma20": 3100,
        "ma20_slope": -1,     # >0 上彎，=0 走平，<0 下彎
        "sector": "AI",
        "role": "leader",     # leader / weight / watch
    }

    index_state / core_etf_state 可傳入加權指數、0050、台積電等核心狀態：
    {
        "symbol": "0050",
        "close": 100,
        "ma20": 102,
        "ma20_slope": -1,
        "ma60": 95,
    }
    """
    total = len(leaders)
    below_ma20 = 0
    ma20_down = 0
    both_weak = 0
    leader_names: List[str] = []

    for item in leaders:
        close = _safe_float(item.get("close"))
        ma20 = _safe_float(item.get("ma20"))
        slope = _safe_float(item.get("ma20_slope"))

        is_below = close is not None and ma20 is not None and close < ma20
        is_down = slope is not None and slope < 0

        if is_below:
            below_ma20 += 1
        if is_down:
            ma20_down += 1
        if is_below and is_down:
            both_weak += 1
            leader_names.append(str(item.get("name") or item.get("symbol") or "unknown"))

    below_ratio = _ratio(below_ma20, total)
    down_ratio = _ratio(ma20_down, total)
    both_ratio = _ratio(both_weak, total)

    score = 0

    # 群體跌破月線：主流股退潮
    if below_ratio >= 0.70:
        score += 35
    elif below_ratio >= 0.50:
        score += 25
    elif below_ratio >= 0.30:
        score += 15

    # 群體月線下彎：反彈壓力增加
    if down_ratio >= 0.60:
        score += 30
    elif down_ratio >= 0.40:
        score += 20
    elif down_ratio >= 0.25:
        score += 10

    # 同時跌破 MA20 + MA20 下彎：比單純跌破更危險
    if both_ratio >= 0.50:
        score += 20
    elif both_ratio >= 0.30:
        score += 12
    elif both_ratio >= 0.20:
        score += 6

    def _state_penalty(state: Optional[Dict[str, Any]], label: str) -> Dict[str, Any]:
        if not state:
            return {"penalty": 0, "below_ma20": None, "ma20_down": None, "below_ma60": None, "label": label}
        close = _safe_float(state.get("close"))
        ma20 = _safe_float(state.get("ma20"))
        ma60 = _safe_float(state.get("ma60"))
        slope = _safe_float(state.get("ma20_slope"))
        below20 = close is not None and ma20 is not None and close < ma20
        down20 = slope is not None and slope < 0
        below60 = close is not None and ma60 is not None and close < ma60
        penalty = 0
        if below20:
            penalty += 6
        if down20:
            penalty += 6
        if below60:
            penalty += 8
        return {
            "penalty": penalty,
            "below_ma20": below20,
            "ma20_down": down20,
            "below_ma60": below60,
            "label": label,
        }

    index_check = _state_penalty(index_state, "index")
    core_check = _state_penalty(core_etf_state, "core_etf")
    score += int(index_check["penalty"])
    score += int(core_check["penalty"])
    score = min(score, 100)

    state = classify_leader_risk(score)

    if state["bias"] in ("risk_off", "defensive"):
        app_message = "千金股 / 主流股群體轉弱：0050 股期不加碼，槓桿降到防守區，優先 Put Spread 與部分 Covered Call。"
    elif state["bias"] == "neutral_defensive":
        app_message = "主流股動能降溫：保留核心倉，但等待重新站回月線再提高槓桿。"
    else:
        app_message = "主流股尚未明顯退潮：依既有趨勢與強弱評分執行。"

    return {
        "score": score,
        "max_score": 100,
        "risk_level": state["risk_level"],
        "bias": state["bias"],
        "emoji": state["emoji"],
        "app_message": app_message,
        "leverage_cap": state["leverage_cap"],
        "hedge_action": state["hedge_action"],
        "checks": {
            "leader_count": total,
            "below_ma20_count": below_ma20,
            "ma20_down_count": ma20_down,
            "below_ma20_and_ma20_down_count": both_weak,
            "below_ma20_ratio": round(below_ratio, 3),
            "ma20_down_ratio": round(down_ratio, 3),
            "both_weak_ratio": round(both_ratio, 3),
            "weak_leaders": leader_names[:20],
            "index_check": index_check,
            "core_etf_check": core_check,
        },
    }
