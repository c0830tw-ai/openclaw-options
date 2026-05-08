"""
risk_limits.py — 風險限額即時監控

對照 alerts_config.json 的 risk_max_* 閾值，計算當前使用率：
  - delta exposure
  - theta cost / day
  - vega exposure
  - long put lots / short call lots
  - drawdown %

每項回傳 {limit, current, usage_pct, status}：
  status: ok (<60%) / warn (60-80%) / hot (80-100%) / over (>100%)
"""
from typing import Any, Dict, Optional


def _safe(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _classify(usage: float) -> str:
    if usage > 100: return 'over'
    if usage >= 80: return 'hot'
    if usage >= 60: return 'warn'
    return 'ok'


def _metric(label: str, current: float, limit: float, unit: str = '') -> Dict[str, Any]:
    if not limit:
        return {'label': label, 'limit': 0, 'current': round(current, 2),
                'usage_pct': 0, 'status': 'disabled', 'unit': unit}
    usage = abs(current) / abs(limit) * 100
    return {
        'label':      label,
        'limit':      limit,
        'current':    round(current, 2),
        'usage_pct':  round(usage, 1),
        'status':     _classify(usage),
        'unit':       unit,
    }


def evaluate(data: Dict[str, Any], rules: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not data or not rules:
        return None

    pgt = _safe(data, 'portfolio_greeks', 'totals', default={}) or {}
    pg_legs = _safe(data, 'portfolio_greeks', 'legs', default=[]) or []
    held_puts  = sum((L.get('qty_signed') or 0) for L in pg_legs
                      if L.get('right') == 'put'  and (L.get('qty_signed') or 0) > 0)
    held_calls = sum(-(L.get('qty_signed') or 0) for L in pg_legs
                      if L.get('right') == 'call' and (L.get('qty_signed') or 0) < 0)
    dd_pct = _safe(data, 'drawdown', 'current_dd_pct', default=0) or 0

    metrics = [
        _metric('Delta 曝險',  pgt.get('delta_ntd_per_1pct_tx') or 0,
                rules.get('risk_max_delta_ntd_per_1pct_tx'), 'NT/1%TX'),
        _metric('Theta 成本',   pgt.get('theta_ntd_per_day') or 0,
                rules.get('risk_max_theta_ntd_per_day'), 'NT/天'),
        _metric('Vega 曝險',    pgt.get('vega_ntd_per_pct_iv') or 0,
                rules.get('risk_max_vega_ntd_per_pct_iv'), 'NT/1%IV'),
        _metric('Long put 口', held_puts,
                rules.get('risk_max_put_lots'), '口'),
        _metric('Short call 口', held_calls,
                rules.get('risk_max_short_call_lots'), '口'),
        _metric('Drawdown',    dd_pct,
                rules.get('risk_max_drawdown_pct'), '%'),
    ]

    n_over = sum(1 for m in metrics if m['status'] == 'over')
    n_hot  = sum(1 for m in metrics if m['status'] == 'hot')

    return {
        'metrics':  metrics,
        'n_over':   n_over,
        'n_hot':    n_hot,
        'overall':  'over' if n_over else 'hot' if n_hot else 'warn'
                    if any(m['status'] == 'warn' for m in metrics) else 'ok',
    }
