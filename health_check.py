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

    # ── Rule 5: 比例式價差對齊（BP:SP = 1:2 的偵測）─────────
    # 只在用戶有 short puts 時才評估（否則跳過、視為 N/A）
    short_puts = [L for L in pg_legs
                   if L.get('right') == 'put' and (L.get('qty_signed') or 0) < 0]
    held_short_put_lots = sum(-(L.get('qty_signed') or 0) for L in short_puts)

    if held_short_put_lots > 0:
        # 有短 put → 檢查比例
        if held_put_lots > 0:
            actual_ratio = held_short_put_lots / held_put_lots
            if 1.8 <= actual_ratio <= 2.2:
                r5_score = 100
                r5_detail = f'BP:SP = 1:{actual_ratio:.2f}（接近 1:2，符合輝哥比例式）'
            elif 1.5 <= actual_ratio < 1.8 or 2.2 < actual_ratio <= 2.5:
                r5_score = 80
                r5_detail = f'BP:SP = 1:{actual_ratio:.2f}（略偏離 1:2）'
            elif actual_ratio > 2.5:
                r5_score = 50
                r5_detail = f'BP:SP = 1:{actual_ratio:.2f}（短 SP 比例過高，下檔風險放大）'
                violations.append(f'short put ({held_short_put_lots} 口) 對 long put ({held_put_lots} 口) '
                                  f'比例 1:{actual_ratio:.1f}，超過建議 1:2')
                suggestions.append('考慮減少 short put 口數或加買 long put 平衡')
            else:
                r5_score = 70
                r5_detail = f'BP:SP = 1:{actual_ratio:.2f}（短 SP 比例偏低，credit 收益少）'
        else:
            # 無 long put 但有 short put → 純裸賣，極危險
            r5_score = 30
            r5_detail = f'⚠️ 持有 {held_short_put_lots} 口裸賣 put（無 BP 保護！）'
            violations.append(f'裸賣 put {held_short_put_lots} 口無上方保護，極端跌幅 unlimited 虧損')
            suggestions.append('立即加買對應 long put 形成 spread，至少 1:2 比例')
        breakdown.append({'rule': '比例式對齊', 'score': r5_score, 'detail': r5_detail})

        # ── Rule 6: 保證金壓力（簡化：每口短 put × 60K NT 估算）──
        margin_per_lot = 60000     # 永豐 TXO 短 put 約略保證金 (NT$)
        used_margin = held_short_put_lots * margin_per_lot
        # 假設可用資金 = core_long_notional × 30%（保守，避免動到 hedge core）
        available = pf_notional * 0.30 if pf_notional > 0 else 0
        if available > 0:
            usage_pct = used_margin / available * 100
            if usage_pct < 50:
                r6_score = 100
                r6_detail = f'估保證金 {used_margin:,} NT ({usage_pct:.0f}% 預算，安全)'
            elif usage_pct < 75:
                r6_score = 75
                r6_detail = f'估保證金 {used_margin:,} NT ({usage_pct:.0f}% 預算，中度)'
            elif usage_pct < 100:
                r6_score = 50
                r6_detail = f'估保證金 {used_margin:,} NT ({usage_pct:.0f}% 預算，偏高)'
                violations.append(f'保證金佔可用資金 {usage_pct:.0f}%，下跌時可能追繳')
                suggestions.append('減少 short put 口數或預留更多現金')
            else:
                r6_score = 30
                r6_detail = f'估保證金 {used_margin:,} NT ({usage_pct:.0f}% 預算，超標)'
                violations.append(f'保證金已超出可用 30% 預算（{usage_pct:.0f}%），追繳風險高')
                suggestions.append('立即減 short put 部位')
            breakdown.append({'rule': '保證金壓力', 'score': r6_score, 'detail': r6_detail})

    # ── Rule 7: Delta 目標偏差 (target band) ─────────────
    # 從 alerts_config 讀 target + tolerance
    try:
        import alerts as _AL
        rules = _AL.load_rules()
        target_delta = rules.get('risk_target_delta_ntd_per_1pct_tx')
        tol = rules.get('risk_target_delta_tolerance_ntd', 5000)
        cur_delta = pg_totals.get('delta_ntd_per_1pct_tx')
        if target_delta is not None and cur_delta is not None and tol > 0:
            deviation = abs(cur_delta - target_delta)
            if deviation <= tol:
                r7_score = 100
                r7_detail = f'Δ {int(cur_delta):+,} 在 ±{int(tol):,} band 內 (target {target_delta:+})'
            elif deviation <= tol * 2:
                r7_score = 70
                r7_detail = f'Δ {int(cur_delta):+,} 偏離 target {target_delta:+} 達 {int(deviation):,}'
            else:
                r7_score = 40
                r7_detail = f'Δ {int(cur_delta):+,} 嚴重偏離 target {target_delta:+}（diff {int(deviation):,}）'
                violations.append(f'Delta 偏離目標：當前 {int(cur_delta):+,}、target {target_delta:+,}、tol ±{int(tol):,}')
                if cur_delta > target_delta + tol:
                    suggestions.append('Delta 偏多：考慮買 put 或減 call 短部位調整')
                else:
                    suggestions.append('Delta 偏空：考慮減 put 或加 long call 平衡')
            breakdown.append({'rule': 'Δ 目標 band', 'score': r7_score, 'detail': r7_detail})
    except Exception:
        pass

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
