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
        'strategy': 'collar',
        'fallback': 'hui-full',
        'why':      '【4 季 sweep】牛市 Q3 +20% collar 奪冠（短 call 變廢紙、淨權利金正向）。'
                    'hui-full 同季進前 3，2:1 put 在末段拉回時還能補一點',
        'expected': 'collar 跟得上 +15~+18%；hui-full +12~+15% 但下跌保護更厚',
    },
    'bull_mild': {
        'label':    '🐃 中等牛市',
        'criteria': '30 天 TX 漲幅 5-10%',
        'dte':      30,
        'delta':    0.10,
        'strategy': 'collar',
        'fallback': 'hui-full',
        'why':      '中牛 collar 收 call 權利金壓低成本。怕被軋換 hui-full（賣 1 / 買 2 比例式，'
                    '上漲被 short call 蓋住 1 點，但下跌有 2 口 put 翻倍保護）',
        'expected': 'collar +10~+13% / hui-full +8~+11%',
    },
    'sideways': {
        'label':    '😴 盤整',
        'criteria': '30 天 TX 變動 -5% ~ +5%',
        'dte':      30,
        'delta':    0.10,
        'strategy': 'hui-full',
        'fallback': 'cal-std',
        'why':      '【4 季 sweep】hui-full 是 4 季中唯一在盤整 + 中熊都進前 3 的策略 → 最穩。'
                    'cal-std (calendar 標準) 盤整奪冠但波動大，準備好換倉再用',
        'expected': 'hui-full +3~+5%（穩定 theta 收）；cal-std +5~+8% 但震盪大',
    },
    'bear_mild': {
        'label':    '🐃↓ 中等熊市',
        'criteria': '30 天 TX 跌幅 5-10%',
        'dte':      21,
        'delta':    0.15,
        'strategy': 'hui-full',
        'fallback': 'put_only',
        'why':      '【4 季 sweep】Q2 中熊 -12% hui-full 奪冠：賣 1 口 OTM call 補貼 + 買 2 口更遠 put '
                    '加倍保護，比單純 put-only 多吃 short call 收的 theta',
        'expected': 'hui-full DD 比 naked 少 4-6%；put-only DD 略小但 theta 成本較高',
    },
    'bear_strong': {
        'label':    '🐻 強勢熊市',
        'criteria': '30 天 TX 跌幅 > 10%',
        'dte':      15,
        'delta':    0.20,
        'strategy': 'put_only',
        'fallback': 'hui-full',
        'why':      '崩跌期保命為主：put-only 緊跟現價（Δ 0.20）+ short DTE 快速 gamma 反應。'
                    'hui-full 的 short call 在崩盤中變廢紙也算 OK，可備援',
        'expected': '崩跌情境保命，put-only 預期 -10 ~ -15% vs naked -20%+',
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


def _load_strategy_stats() -> Dict[str, Any]:
    """讀 strategy_stats.json (由 backtest_report 月跑寫入)。"""
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parent / 'strategy_stats.json'
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return {}


def evaluate(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not data:
        return None
    det = detect_regime(data)
    rec = REGIME_RECOMMENDATIONS.get(det['regime'])
    if not rec:
        return None

    cur = _avg_held_params(data)
    stats = _load_strategy_stats()

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

    # 從 stats 取主推 / fallback 的歷史命中率
    rec_strategy = rec['strategy']
    rec_fallback = rec.get('fallback', '')
    win_count    = (stats.get('win_count') or {})
    top3_count   = (stats.get('top3_count') or {})
    quarters_total = stats.get('quarters_total', 0) or 0
    per_regime_winner = stats.get('per_regime_winner') or {}

    def _stat_for(name):
        if not name or quarters_total == 0:
            return None
        wins  = win_count.get(name, 0)
        top3  = top3_count.get(name, 0)
        regime_wins = sum(1 for w in per_regime_winner.get(det['regime'], []) if w == name)
        regime_total = len(per_regime_winner.get(det['regime'], []))
        return {
            'wins':           wins,
            'win_rate_pct':   round(wins  / quarters_total * 100, 1),
            'top3':           top3,
            'top3_rate_pct':  round(top3  / quarters_total * 100, 1),
            'regime_wins':    regime_wins,
            'regime_total':   regime_total,
            'regime_win_rate_pct': round(regime_wins / regime_total * 100, 1) if regime_total else None,
        }

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
            'fallback':  rec_fallback,
            'why':       rec['why'],
            'expected':  rec['expected'],
            'stats':          _stat_for(rec_strategy),
            'fallback_stats': _stat_for(rec_fallback),
            'period':         f"{stats.get('first_date', '')}→{stats.get('last_date', '')}" if stats else None,
            'quarters_total': quarters_total,
        },
        'current':        cur,
        'deviations':     deviations,
        'aligned':        len(deviations) == 0 and cur.get('has_positions'),
    }
