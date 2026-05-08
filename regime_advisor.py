"""
regime_advisor.py — 依當前市場 regime 推薦最佳 SOP 參數

根據 backtest_regime.py 在 2025-04 ~ 2026-05 真實 TX 資料的 4 quarter sweep
結果，針對牛/熊/盤整三種情境給出最佳 (DTE, delta, strategy) 推薦。

並對照用戶當前實際 SOP（從 portfolio_greeks.legs 推算 avg DTE / avg delta），
標示「應該調整哪些」。
"""
from typing import Any, Dict, List, Optional


# ── Regime → 最佳策略（來自 backtest_all_regime.py 8 策略 sweep）─
# 推薦順序：primary（積極派）/ fallback（保守派 = 你目前 SOP）
REGIME_RECOMMENDATIONS = {
    'bull_strong': {
        'label':    '🐂 強勢牛市',
        'criteria': '30 天 TX 漲幅 > 10%',
        'dte':      30,
        'delta':    0.10,
        'strategy': 'cal-reverse',
        'fallback': 'put_only',
        'why':      '【全策略 sweep】cal-reverse 牛市連 2 次奪冠（Calmar +4 ~ +5）。'
                    '不熟 calendar 操作可 fallback put-only（穩定亞軍、DD 最小）。'
                    '避免 collar/hui-full（call 被軋排第 7-8 名）',
        'expected': 'cal-reverse +14 ~ +18%（小勝 naked 1-2%）',
    },
    'bull_mild': {
        'label':    '🐃 中等牛市',
        'criteria': '30 天 TX 漲幅 5-10%',
        'dte':      30,
        'delta':    0.10,
        'strategy': 'cal-reverse',
        'fallback': 'put_only',
        'why':      '【sweep 驗證】中牛 cal-reverse 仍領先（Calmar +4.13）。'
                    'put-only 跟在後面（Calmar +3.63）',
        'expected': 'cal-reverse +14% / put-only +12%（vs naked +13%）',
    },
    'sideways': {
        'label':    '😴 盤整',
        'criteria': '30 天 TX 變動 -5% ~ +5%',
        'dte':      30,
        'delta':    0.10,
        'strategy': 'iron-condor',
        'fallback': 'put_only',
        'why':      '【sweep 驗證】盤整 iron-condor (寶典 4 腳) 冠軍 Calmar +1.54，'
                    '小贏 put-only。雙向收 credit 對中性市場最有效',
        'expected': 'iron-condor +4.7%（小勝 naked +4.4%）',
    },
    'bear_mild': {
        'label':    '🐃↓ 中等熊市',
        'criteria': '30 天 TX 跌幅 5-10%',
        'dte':      21,
        'delta':    0.15,
        'strategy': 'put_only',
        'fallback': 'cal-reverse',
        'why':      'put-only 是熊市保護導向首選（DD 最小）。'
                    'cal-reverse 在跌市能收 long near put 的 gamma 增值，可作 alternative',
        'expected': 'put-only DD 最小（vs naked 多保 5-7%）',
    },
    'bear_strong': {
        'label':    '🐻 強勢熊市',
        'criteria': '30 天 TX 跌幅 > 10%',
        'dte':      15,
        'delta':    0.20,
        'strategy': 'put_only',
        'fallback': 'cal-reverse',
        'why':      '崩跌期保命為主：put-only 緊跟現價（Δ 0.20）+ short DTE 快速 gamma'
                    '。避免 hui-full（DD -28%）；cal-reverse 絕對虧損也少',
        'expected': '崩跌情境保命，預期 -10 ~ -15% vs naked -20%+',
    },
}


def detect_regime(data: Dict[str, Any]) -> Dict[str, Any]:
    """從 trend.changes.vs_month_start 推當前 regime。"""
    monthly = ((data.get('trend') or {}).get('changes') or {}).get('vs_month_start') or {}
    weekly  = ((data.get('trend') or {}).get('changes') or {}).get('vs_week_ago')    or {}
    m_pct = (monthly.get('tx_delta_pct') if isinstance(monthly, dict) else 0) or 0
    w_pct = (weekly.get('tx_delta_pct')  if isinstance(weekly,  dict) else 0) or 0

    if m_pct > 10:
        regime = 'bull_strong'
    elif m_pct > 5:
        regime = 'bull_mild'
    elif m_pct < -10:
        regime = 'bear_strong'
    elif m_pct < -5:
        regime = 'bear_mild'
    else:
        regime = 'sideways'

    return {
        'regime':       regime,
        'monthly_pct':  round(m_pct, 2),
        'weekly_pct':   round(w_pct, 2),
    }


def _avg_held_params(data: Dict[str, Any]) -> Dict[str, Any]:
    """從 portfolio_greeks.legs 推算當前實際持倉的 avg DTE / avg delta / strategy。"""
    legs = (data.get('portfolio_greeks') or {}).get('legs') or []
    if not legs:
        return {'has_positions': False}

    long_puts   = [L for L in legs if L.get('right') == 'put'  and (L.get('qty_signed') or 0) > 0]
    short_calls = [L for L in legs if L.get('right') == 'call' and (L.get('qty_signed') or 0) < 0]

    def _wavg(items, key):
        weights = [abs(L.get('qty_signed') or 0) for L in items]
        vals    = [L.get(key) for L in items]
        valid   = [(v, w) for v, w in zip(vals, weights) if v is not None]
        if not valid: return None
        total_w = sum(w for _, w in valid) or 1
        return sum(v * w for v, w in valid) / total_w

    avg_dte = _wavg(long_puts + short_calls, 'dte')
    # 取絕對 delta 做加權
    abs_deltas = [(abs(L.get('delta') or 0), abs(L.get('qty_signed') or 0)) for L in long_puts]
    if abs_deltas:
        total_w = sum(w for _, w in abs_deltas) or 1
        avg_put_delta = sum(d * w for d, w in abs_deltas) / total_w
    else:
        avg_put_delta = None

    if long_puts and short_calls:
        strategy = 'collar'
    elif long_puts:
        strategy = 'put_only'
    elif short_calls:
        strategy = 'short_calls_only'
    else:
        strategy = 'no_hedge'

    return {
        'has_positions':  True,
        'avg_dte':        round(avg_dte) if avg_dte else None,
        'avg_put_delta':  round(avg_put_delta, 3) if avg_put_delta else None,
        'strategy':       strategy,
        'put_lots':       sum((L.get('qty_signed') or 0) for L in long_puts),
        'call_lots':      sum(-(L.get('qty_signed') or 0) for L in short_calls),
    }


def evaluate(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not data:
        return None
    det = detect_regime(data)
    rec = REGIME_RECOMMENDATIONS.get(det['regime'])
    if not rec:
        return None

    cur = _avg_held_params(data)

    # 偏差 — 比較用戶當前 vs 推薦
    deviations: List[Dict[str, Any]] = []
    if cur.get('has_positions'):
        if cur.get('avg_dte') and abs(cur['avg_dte'] - rec['dte']) > 7:
            deviations.append({
                'param': 'DTE',
                'current': cur['avg_dte'],
                'recommended': rec['dte'],
                'note': f'平均 DTE 差 {abs(cur["avg_dte"] - rec["dte"])} 天',
            })
        if cur.get('avg_put_delta') and abs(cur['avg_put_delta'] - rec['delta']) > 0.04:
            deviations.append({
                'param': 'Δ',
                'current': cur['avg_put_delta'],
                'recommended': rec['delta'],
                'note': f'put delta 與推薦差 {abs(cur["avg_put_delta"] - rec["delta"]):.2f}',
            })
        if cur.get('strategy') and cur['strategy'] != rec['strategy']:
            deviations.append({
                'param': '策略',
                'current': cur['strategy'],
                'recommended': rec['strategy'],
                'note': f'當前 {cur["strategy"]} 與推薦 {rec["strategy"]} 不同',
            })

    return {
        'regime':         det['regime'],
        'regime_label':   rec['label'],
        'monthly_pct':    det['monthly_pct'],
        'weekly_pct':     det['weekly_pct'],
        'criteria':       rec['criteria'],
        'recommendation': {
            'dte':       rec['dte'],
            'delta':     rec['delta'],
            'strategy':  rec['strategy'],
            'fallback':  rec.get('fallback', ''),
            'why':       rec['why'],
            'expected':  rec['expected'],
        },
        'current':        cur,
        'deviations':     deviations,
        'aligned':        len(deviations) == 0 and cur.get('has_positions'),
    }
