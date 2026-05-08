"""
regime_advisor.py — 依當前市場 regime 推薦最佳 SOP 參數

根據 backtest_regime.py 在 2025-04 ~ 2026-05 真實 TX 資料的 4 quarter sweep
結果，針對牛/熊/盤整三種情境給出最佳 (DTE, delta, strategy) 推薦。

並對照用戶當前實際 SOP（從 portfolio_greeks.legs 推算 avg DTE / avg delta），
標示「應該調整哪些」。
"""
from typing import Any, Dict, List, Optional


# ── Regime → 最佳參數（來自 backtest_regime.py 跑出來的 insight）─
REGIME_RECOMMENDATIONS = {
    'bull_strong': {
        'label':    '🐂 強勢牛市',
        'criteria': '30 天 TX 漲幅 > 10%',
        'dte':      30,
        'delta':    0.05,
        'strategy': 'collar',
        'why':      '強牛遠 OTM call (Δ 0.05) 不易被軋，short DTE 30 與 month roll 對齊，'
                    'collar 既賺 call premium 又留下漲空間',
        'expected': 'Calmar 7.65（backtest Q2 +21% 那段最佳）',
    },
    'bull_mild': {
        'label':    '🐃 中等牛市',
        'criteria': '30 天 TX 漲幅 5-10%',
        'dte':      45,
        'delta':    0.10,
        'strategy': 'collar',
        'why':      '中牛長 DTE 累積 theta 收益，Δ 0.10 平衡保護與 premium',
        'expected': 'Calmar 3.91（backtest Q4 加速漲那段）',
    },
    'sideways': {
        'label':    '😴 盤整',
        'criteria': '30 天 TX 變動 -5% ~ +5%',
        'dte':      21,
        'delta':    0.15,
        'strategy': 'collar',
        'why':      '盤整時 collar 唯一勝過裸長部位（call premium 補 put 成本）；'
                    '短 DTE 21 + ATM-ish (Δ 0.15) 抓住小波動',
        'expected': 'Calmar 1.42 + 勝裸長 +5.7%（backtest Q1）',
    },
    'bear_mild': {
        'label':    '🐃↓ 中等熊市',
        'criteria': '30 天 TX 跌幅 5-10%',
        'dte':      21,
        'delta':    0.15,
        'strategy': 'put_only',
        'why':      '下跌中 put_only 為主，避免 sell call 鎖死反彈',
        'expected': '理論最佳（backtest 樣本不足，依合成情境推論）',
    },
    'bear_strong': {
        'label':    '🐻 強勢熊市',
        'criteria': '30 天 TX 跌幅 > 10%',
        'dte':      15,
        'delta':    0.20,
        'strategy': 'put_only',
        'why':      '崩跌中極短 DTE 高 delta put 緊跟現價提供最大保護；不要 sell call',
        'expected': '崩跌情境保命為主',
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
            'why':       rec['why'],
            'expected':  rec['expected'],
        },
        'current':        cur,
        'deviations':     deviations,
        'aligned':        len(deviations) == 0 and cur.get('has_positions'),
    }
