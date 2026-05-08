"""
health_check.py — 持倉健診評分

對照用戶記憶裡的 SOP / 規則，給當前持倉打對齊度分數：
  Rule 1 — 200 萬法則：long puts 口數 vs recommended_put_lots
  Rule 2 — 動態履約緩衝：put 履約距 TX -6% ~ -8%
  Rule 3 — Theta 預算：月 hedge 成本 vs 名目
  Rule 4 — 趨勢牛市賣 call 風險：有 short call 且 TX 動能向上 → 警示

輸出 dict: {overall_score, grade, breakdown[], violations[], suggestions[]}
"""
from typing import Any, Dict, List, Optional


def _grade(score: int) -> str:
    if score >= 90: return 'A'
    if score >= 80: return 'B+'
    if score >= 70: return 'B'
    if score >= 60: return 'C+'
    if score >= 50: return 'C'
    return 'D'


def _safe_n(d, *keys, default=0):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def evaluate(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """從 latest_collar result 計算健診。回傳 None 若資料不完整。"""
    if not data:
        return None

    bs_s    = _safe_n(data, 'market', 'tx_futures', default=0)
    rec_lots = _safe_n(data, 'portfolio', 'totals', 'recommended_put_lots', default=0)
    pf_notional = _safe_n(data, 'portfolio', 'totals', 'core_long_notional', default=0)
    pg_legs = _safe_n(data, 'portfolio_greeks', 'legs', default=[]) or []
    pg_totals = _safe_n(data, 'portfolio_greeks', 'totals', default={}) or {}

    # 抽出 long puts 與 short calls
    long_puts  = [L for L in pg_legs if L.get('right') == 'put'  and (L.get('qty_signed') or 0) > 0]
    short_calls = [L for L in pg_legs if L.get('right') == 'call' and (L.get('qty_signed') or 0) < 0]
    held_put_lots = sum((L.get('qty_signed') or 0) for L in long_puts)
    held_short_call_lots = sum(-(L.get('qty_signed') or 0) for L in short_calls)

    breakdown: List[Dict[str, Any]] = []
    violations: List[str] = []
    suggestions: List[str] = []

    # ── Rule 1: 200 萬法則 ────────────────────────────────────
    if rec_lots > 0:
        gap = held_put_lots - rec_lots
        if abs(gap) <= 1:
            r1_score, r1_detail = 100, f'建議 {rec_lots} 口、實際 {held_put_lots} 口（差 {gap:+d}，合格）'
        elif abs(gap) <= 2:
            r1_score = 80
            r1_detail = f'建議 {rec_lots} 口、實際 {held_put_lots} 口（差 {gap:+d}）'
            if gap < 0:
                violations.append(f'put 不足：建議 {rec_lots} 口、實際 {held_put_lots} 口（缺 {-gap}）')
                suggestions.append(f'加買 {-gap} 口 OTM put 補足對沖比例')
            else:
                violations.append(f'put 過多：建議 {rec_lots} 口、實際 {held_put_lots} 口（多 {gap}）')
                suggestions.append(f'考慮減 {gap} 口 put 以降低 theta 拖累')
        else:
            r1_score = max(0, 60 - abs(gap) * 10)
            r1_detail = f'建議 {rec_lots} 口、實際 {held_put_lots} 口（差 {gap:+d}，嚴重失衡）'
            violations.append(r1_detail)
            if gap < 0:
                suggestions.append(f'立即加買 {-gap} 口 put 補足保護')
    else:
        r1_score, r1_detail = 0, 'recommended_put_lots 缺資料'
    breakdown.append({'rule': '200 萬法則', 'score': r1_score, 'detail': r1_detail})

    # ── Rule 2: 動態履約緩衝（put 履約距 TX -6% ~ -8%）──────
    if long_puts and bs_s > 0:
        scores = []
        details = []
        for L in long_puts:
            K = L.get('strike') or 0
            if K <= 0:
                continue
            buffer_pct = (bs_s - K) / bs_s * 100
            if 6 <= buffer_pct <= 8:
                scores.append(100); details.append(f'{int(K)}P 距 -{buffer_pct:.1f}%')
            elif 5 <= buffer_pct < 6 or 8 < buffer_pct <= 10:
                scores.append(80);  details.append(f'{int(K)}P 距 -{buffer_pct:.1f}%（略偏）')
            elif 3 <= buffer_pct < 5:
                scores.append(50);  details.append(f'{int(K)}P 距 -{buffer_pct:.1f}%（太近，要 roll）')
                violations.append(f'put 履約 {int(K)} 距現價 -{buffer_pct:.1f}%（建議下移）')
            elif buffer_pct > 10:
                scores.append(50);  details.append(f'{int(K)}P 距 -{buffer_pct:.1f}%（太遠，保護薄）')
                violations.append(f'put 履約 {int(K)} 距現價 -{buffer_pct:.1f}%（保護薄，可上移）')
            else:
                scores.append(30);  details.append(f'{int(K)}P 距 -{buffer_pct:.1f}%（極端）')
        r2_score = int(sum(scores) / len(scores)) if scores else 0
        r2_detail = ' / '.join(details)
    else:
        r2_score, r2_detail = 0, '無 long put 持倉'
    breakdown.append({'rule': '動態履約緩衝', 'score': r2_score, 'detail': r2_detail})

    # ── Rule 3: Theta 預算（月 hedge 成本 vs 名目）─────────
    theta_per_day = pg_totals.get('theta_ntd_per_day') or 0
    if pf_notional > 0 and theta_per_day:
        monthly_cost = abs(theta_per_day) * 30
        cost_ratio = monthly_cost / pf_notional * 100   # %
        if cost_ratio < 0.5:
            r3_score, r3_detail = 100, f'月成本 {monthly_cost:,.0f} NT ({cost_ratio:.2f}% 名目，合理)'
        elif cost_ratio < 1.0:
            r3_score, r3_detail = 75, f'月成本 {monthly_cost:,.0f} NT ({cost_ratio:.2f}% 名目，偏高)'
        elif cost_ratio < 1.5:
            r3_score, r3_detail = 50, f'月成本 {monthly_cost:,.0f} NT ({cost_ratio:.2f}% 名目，過高)'
            violations.append(f'theta 成本 {cost_ratio:.2f}% 名目 / 月，太高')
            suggestions.append('考慮改買更 OTM 的 put 或減少口數降低 theta drag')
        else:
            r3_score, r3_detail = 30, f'月成本 {monthly_cost:,.0f} NT ({cost_ratio:.2f}% 名目，極高)'
            violations.append(f'theta 成本 {cost_ratio:.2f}% 名目 / 月，極高')
            suggestions.append('立即檢視 hedge 結構，考慮降低 put 口數或下移履約')
    else:
        r3_score, r3_detail = 50, '資料不足無法評估'
    breakdown.append({'rule': 'Theta 預算', 'score': r3_score, 'detail': r3_detail})

    # ── Rule 4: 趨勢市賣 call 風險 ─────────────────────────
    if held_short_call_lots > 0:
        # 用 trend 區段 vs_week_ago 看上漲動能
        weekly_change = _safe_n(data, 'trend', 'changes', 'vs_week_ago', 'tx_delta_pct', default=0) or 0
        if weekly_change > 3:
            r4_score = 30
            r4_detail = f'持有 {held_short_call_lots} 口 short call，TX 週漲 {weekly_change:+.2f}%（動能向上）'
            violations.append(f'趨勢上漲中持有 {held_short_call_lots} 口 short call，被軋風險高')
            suggestions.append('考慮買回 short call 或 roll up 履約價')
        elif weekly_change > 1:
            r4_score = 70
            r4_detail = f'持有 {held_short_call_lots} 口 short call，TX 週漲 {weekly_change:+.2f}%（小漲）'
        else:
            r4_score = 100
            r4_detail = f'持有 {held_short_call_lots} 口 short call，TX 週 {weekly_change:+.2f}%（盤整或下跌，OK）'
    else:
        r4_score, r4_detail = 100, '無 short call 部位'
    breakdown.append({'rule': '賣 call 趨勢風險', 'score': r4_score, 'detail': r4_detail})

    # ── Overall ────────────────────────────────────────────
    overall = int(sum(b['score'] for b in breakdown) / len(breakdown))
    return {
        'overall_score': overall,
        'grade':         _grade(overall),
        'breakdown':     breakdown,
        'violations':    violations,
        'suggestions':   suggestions,
    }
