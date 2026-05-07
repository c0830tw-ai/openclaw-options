"""
roll_advisor.py — 從 latest_collar 推導具體 roll 動作建議

主要觸發條件：
  1. weekly put（週三/週五）剩餘 DTE_trading ≤ 1：建議 roll 到下一週或近月月選
  2. 近月 short call 距現價 < 1σ：建議 roll up 履約
  3. 近月 long put 履約已被市場往下追到 < 1σ：建議考慮加碼或下移

唯讀模組，純從 result 計算。
"""
from typing import Dict, Any, List, Optional


TXO_MULTIPLIER = 50


def _safe(d: Optional[dict], *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def _format_settlement(d: str) -> str:
    """20260520 → 5/20"""
    if not d or len(d) < 8:
        return d or '?'
    return f'{int(d[4:6])}/{int(d[6:8])}'


def compute_roll_suggestions(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """從 result 推導 roll 建議；回傳 list (空時 = 沒事)。"""
    suggestions: List[Dict[str, Any]] = []
    if not result:
        return suggestions

    bs_s         = _safe(result, 'market', 'tx_futures', default=0)
    near_dte_t   = _safe(result, 'dte_trading', default=0)
    near_iv      = _safe(result, 'iv_used', default=0.20) or 0.20
    near_put     = _safe(result, 'selected_options', 'put',  default={})
    near_call    = _safe(result, 'selected_options', 'call', default={})
    weekly_fri   = result.get('weekly_fri') or {}
    weekly_wed   = result.get('weekly_wed') or {}

    # 對沖需求（從 portfolio 算出）
    rec_hedge = _safe(result, 'portfolio', 'totals', 'recommended_put_lots', default=0)

    # ── 1. 週選即將到期 → roll 到月選 ─────────────────────────────
    for w_info, label in [(weekly_fri, '週五週選'), (weekly_wed, '週三週選')]:
        if not w_info:
            continue
        w_dte = w_info.get('dte_trading')
        if w_dte is None or w_dte > 1:
            continue
        w_put  = _safe(w_info, 'selected_options', 'put', default={})
        w_call = _safe(w_info, 'selected_options', 'call', default={})
        settle = _format_settlement(w_info.get('settlement_date', ''))

        # 若有持週選，提示 roll
        if rec_hedge > 0 and near_put.get('strike') and near_put.get('ask'):
            cost_per_lot   = float(near_put['ask']) * TXO_MULTIPLIER
            total_cost     = cost_per_lot * rec_hedge
            suggestions.append({
                'priority':  'high' if (w_dte or 0) == 0 else 'medium',
                'trigger':   f'{label}_expiring',
                'reason':    f'{label} {settle} 剩交易日 {w_dte} 天，需處理 hedge',
                'action':    'roll_to_monthly',
                'instructions': [
                    f'若有 {label} put：殘值 ~{w_put.get("mid", 0)} 點，可放任歸零或市價平倉',
                    f'買 {rec_hedge} 口 近月月選 Put K={near_put["strike"]:.0f} '
                    f'@ ~{near_put["ask"]:.0f} 點',
                ],
                'estimates': {
                    'replace_qty':    rec_hedge,
                    'replace_strike': near_put['strike'],
                    'replace_ask':    near_put['ask'],
                    'cost_per_lot':   cost_per_lot,
                    'total_cost':     total_cost,
                    'replacement_dte_trading': near_dte_t,
                    'replacement_iv':          near_iv,
                },
            })

    # ── 2. 短 call 距現價過近 → roll up ────────────────────────────
    if bs_s and near_call.get('strike') and near_dte_t and near_dte_t > 0:
        T = near_dte_t / 252
        sigma_T = near_iv * (T ** 0.5)
        sd = bs_s * sigma_T if sigma_T > 0 else 0
        if sd > 0:
            distance = (near_call['strike'] - bs_s) / sd
            if distance < 1.0:
                suggestions.append({
                    'priority':  'high' if distance < 0.5 else 'medium',
                    'trigger':   'short_call_too_close',
                    'reason':    f'近月 short call K={near_call["strike"]:.0f} '
                                 f'距現價剩 {distance:.2f}σ（< 1σ）',
                    'action':    'roll_up_call',
                    'instructions': [
                        f'買回 short call K={near_call["strike"]:.0f} '
                        f'@ ~{near_call.get("ask", 0):.0f} 點',
                        '賣新 call delta ≈ 0.05 (較遠 OTM)，下次 refresh 系統會選新履約',
                    ],
                    'estimates': {
                        'distance_sigma': round(distance, 2),
                        'current_strike': near_call['strike'],
                        'buyback_cost':   near_call.get('ask', 0),
                    },
                })

    # ── 3. Long put 履約被追到 → 提示加碼或下移 ────────────────────
    if bs_s and near_put.get('strike') and near_dte_t and near_dte_t > 0:
        T = near_dte_t / 252
        sigma_T = near_iv * (T ** 0.5)
        sd = bs_s * sigma_T if sigma_T > 0 else 0
        if sd > 0:
            distance = (bs_s - near_put['strike']) / sd
            if distance < 1.0 and distance > 0:
                suggestions.append({
                    'priority':  'high' if distance < 0.5 else 'medium',
                    'trigger':   'long_put_close_to_money',
                    'reason':    f'近月 long put K={near_put["strike"]:.0f} '
                                 f'距現價剩 {distance:.2f}σ（市場下跌中）',
                    'action':    'reinforce_or_roll_down',
                    'instructions': [
                        '選項 A：加買更高履約的 put（緊跟現價）',
                        '選項 B：roll down（賣現有 put + 買更低履約）— 鎖部分獲利',
                    ],
                    'estimates': {
                        'distance_sigma': round(distance, 2),
                        'current_strike': near_put['strike'],
                    },
                })

    return suggestions
