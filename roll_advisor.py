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


def _limit_ladder_buy(bid: float, mid: float, ask: float) -> Dict[str, Any]:
    """買進的 3 段階梯限價：bid+2（試）→ mid（中位）→ ask（吃 ask）"""
    bid = bid or 0
    ask = ask or 0
    mid = mid or ((bid + ask) / 2 if (bid and ask) else 0)
    return {
        'try_1': max(round(bid + 2), 1) if bid else round(mid * 0.97),
        'try_2': round(mid) if mid else None,
        'try_3': round(ask) if ask else None,
        'wait_minutes': 8,
        'side': 'buy',
    }


def _limit_ladder_sell(bid: float, mid: float, ask: float) -> Dict[str, Any]:
    """賣出的 3 段階梯限價：ask-2（試）→ mid（中位）→ bid（吃 bid）"""
    bid = bid or 0
    ask = ask or 0
    mid = mid or ((bid + ask) / 2 if (bid and ask) else 0)
    return {
        'try_1': max(round(ask - 2), 1) if ask else round(mid * 1.03),
        'try_2': round(mid) if mid else None,
        'try_3': round(bid) if bid else None,
        'wait_minutes': 8,
        'side': 'sell',
    }


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
            ladder = _limit_ladder_buy(near_put.get('bid'), near_put.get('mid'), near_put.get('ask'))
            suggestions.append({
                'priority':  'high' if (w_dte or 0) == 0 else 'medium',
                'trigger':   f'{label}_expiring',
                'reason':    f'{label} {settle} 剩 {w_dte} 個交易日，保護快過期需要換倉',
                'action':    'roll_to_monthly',
                'instructions': [
                    f'你的 {label} put 殘值 ~{w_put.get("mid", 0)} 點，可放任歸零或市價平掉',
                    f'買 {rec_hedge} 口 近月月選 Put 履約 {near_put["strike"]:.0f}',
                    f'限價試三段（每段等 {ladder["wait_minutes"]} 分）：',
                    f'  1️⃣ 限價 {ladder["try_1"]} 點（買方稍微讓利，等賣方送上門）',
                    f'  2️⃣ 限價 {ladder["try_2"]} 點（公平價）',
                    f'  3️⃣ 限價 {ladder["try_3"]} 點（直接吃對方掛價，立即成交）',
                ],
                'limit_ladder': ladder,
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

    # ── 1b. 近月 put 即將到期 → roll 到遠月（每月例行換倉）─────────
    far  = result.get('far_month') or {}
    far_put = _safe(far, 'selected_options', 'put', default={})
    if (
        rec_hedge > 0
        and near_dte_t and 0 < near_dte_t <= 5
        and near_put.get('strike') and near_put.get('bid') is not None
        and far_put.get('strike') and far_put.get('ask')
    ):
        # 平掉現有近月（賣出）+ 買進遠月
        close_ladder = _limit_ladder_sell(near_put.get('bid'), near_put.get('mid'), near_put.get('ask'))
        open_ladder  = _limit_ladder_buy(far_put.get('bid'),  far_put.get('mid'),  far_put.get('ask'))

        close_credit_per_lot = float(near_put.get('bid') or 0) * TXO_MULTIPLIER
        open_cost_per_lot    = float(far_put.get('ask') or 0)  * TXO_MULTIPLIER
        net_debit_per_lot    = open_cost_per_lot - close_credit_per_lot
        net_debit_total      = net_debit_per_lot * rec_hedge

        far_settle = _format_settlement(far.get('settlement_date', ''))
        far_iv     = far.get('iv_used') or 0.20
        far_dte_t  = far.get('dte_trading') or 0

        priority = 'high' if near_dte_t <= 2 else 'medium'
        suggestions.append({
            'priority':  priority,
            'trigger':   'roll_near_to_far_put',
            'reason':    f'近月 put 履約 {near_put["strike"]:.0f} 剩 {near_dte_t} 個交易日，'
                         f'到期前換到遠月（{far_settle} 結算）維持避險',
            'action':    'roll_to_far_month',
            'instructions': [
                f'步驟 1：平掉現有 {rec_hedge} 口 近月 Put 履約 {near_put["strike"]:.0f}（賣出階梯）',
                f'  1️⃣ 限價 {close_ladder["try_1"]} 點（賣方稍微讓利）',
                f'  2️⃣ 限價 {close_ladder["try_2"]} 點（中位價）',
                f'  3️⃣ 限價 {close_ladder["try_3"]} 點（吃 bid）',
                f'步驟 2：買 {rec_hedge} 口 遠月 Put 履約 {far_put["strike"]:.0f}（買進階梯，推薦下一個結算日 {far_settle}）',
                f'  1️⃣ 限價 {open_ladder["try_1"]} 點',
                f'  2️⃣ 限價 {open_ladder["try_2"]} 點',
                f'  3️⃣ 限價 {open_ladder["try_3"]} 點（吃 ask）',
                f'每段等 {open_ladder["wait_minutes"]} 分鐘；建議結算翌日 10:00–11:30 流動性最佳',
            ],
            'limit_ladder':       open_ladder,    # 主腳：買新月 put（向後兼容）
            'close_ladder':       close_ladder,   # 平倉腳
            'open_ladder':        open_ladder,    # 開倉腳
            'estimates': {
                'qty':                rec_hedge,
                'close_strike':       near_put['strike'],
                'close_bid':          near_put.get('bid'),
                'close_credit_per_lot': round(close_credit_per_lot),
                'open_strike':        far_put['strike'],
                'open_ask':           far_put.get('ask'),
                'open_cost_per_lot':  round(open_cost_per_lot),
                'net_debit_per_lot':  round(net_debit_per_lot),
                'total_cost':         round(net_debit_total),     # 與舊 schema 對齊
                'net_debit':          round(net_debit_total),
                'replacement_dte_trading': far_dte_t,
                'replacement_iv':          far_iv,
                'far_settlement':          far.get('settlement_date'),
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
                # 買回 (close short) 用買進階梯
                buyback_ladder = _limit_ladder_buy(
                    near_call.get('bid'), near_call.get('mid'), near_call.get('ask'))
                suggestions.append({
                    'priority':  'high' if distance < 0.5 else 'medium',
                    'trigger':   'short_call_too_close',
                    'reason':    f'你賣出的 Call 履約 {near_call["strike"]:.0f}，'
                                 f'距現價剩 {distance:.2f} 個標準差（風險升高）',
                    'action':    'roll_up_call',
                    'instructions': [
                        f'步驟 1：買回現有的 Call（履約 {near_call["strike"]:.0f}）',
                        f'  限價試三段（每段等 {buyback_ladder["wait_minutes"]} 分）：',
                        f'    1️⃣ 限價 {buyback_ladder["try_1"]} 點',
                        f'    2️⃣ 限價 {buyback_ladder["try_2"]} 點',
                        f'    3️⃣ 限價 {buyback_ladder["try_3"]} 點（直接吃對方掛價）',
                        '步驟 2：賣新一張 Call，履約價更高（更遠價外，敏感度約 0.05）',
                        '  下次 refresh 系統會自動推薦新履約價',
                    ],
                    'limit_ladder': buyback_ladder,
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
                    'reason':    f'你的 put 履約 {near_put["strike"]:.0f}，'
                                 f'距現價剩 {distance:.2f} 個標準差（市場下跌中）',
                    'action':    'reinforce_or_roll_down',
                    'instructions': [
                        '選項 A：再加買履約價更高、貼近現價的新 put',
                        '選項 B：把現有 put 平掉換成履約價更低的新 put——把已賺到的利潤先入袋',
                    ],
                    'estimates': {
                        'distance_sigma': round(distance, 2),
                        'current_strike': near_put['strike'],
                    },
                })

    return suggestions
