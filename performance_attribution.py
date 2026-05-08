"""
performance_attribution.py — 每日 P&L 分解

從 daily_snapshots 取昨日 → 今日，把 P&L 變化拆解成：
  Δ — TX 變動 × 昨日 net_delta（方向曝險貢獻）
  ν — IV 變動 × 昨日 net_vega（波動率貢獻）
  θ — 1 天 theta cost（時間貢獻）
  殘差 — 上面三項解釋不了的部分（開倉/平倉/結構變動/gamma 二階）

回傳 dict 含每項 NT$ 貢獻 + 殘差。
"""
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import snapshot as S


def _safe(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def attribute(today: Dict[str, Any], yesterday: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """從兩日 snapshot 拆解 P&L。需要 snapshot 含 greeks + market 區塊。"""
    if not today or not yesterday:
        return None

    tx_t  = _safe(today,     'market', 'tx')
    tx_y  = _safe(yesterday, 'market', 'tx')
    iv_t  = _safe(today,     'market', 'iv_atm')
    iv_y  = _safe(yesterday, 'market', 'iv_atm')

    # 用昨日的 greeks（曝險生效在昨夜→今早）
    delta_y = _safe(yesterday, 'greeks', 'delta_ntd_per_1pct_tx')
    vega_y  = _safe(yesterday, 'greeks', 'vega_ntd_per_pct_iv')
    theta_y = _safe(yesterday, 'greeks', 'theta_ntd_per_day')

    if tx_t is None or tx_y is None or tx_y == 0:
        return None

    tx_pct = (tx_t - tx_y) / tx_y * 100
    iv_pp  = ((iv_t - iv_y) * 100) if (iv_t is not None and iv_y is not None) else 0

    delta_pl = (delta_y or 0) * tx_pct
    vega_pl  = (vega_y  or 0) * iv_pp
    theta_pl = (theta_y or 0)            # 1 day worth (snapshot interval)

    # 觀測 P&L = unrealized 變動 + ledger mtd 變動
    unr_t = _safe(today,     'core_long', 'unrealized') or 0
    unr_y = _safe(yesterday, 'core_long', 'unrealized') or 0
    actual_pl = unr_t - unr_y

    explained = delta_pl + vega_pl + theta_pl
    residual  = actual_pl - explained

    return {
        'date_today':     today.get('date'),
        'date_yesterday': yesterday.get('date'),
        'tx_change_pct':  round(tx_pct, 2),
        'iv_change_pp':   round(iv_pp, 1) if iv_pp else 0,
        'delta_pl_ntd':   round(delta_pl),
        'vega_pl_ntd':    round(vega_pl),
        'theta_pl_ntd':   round(theta_pl),
        'explained_ntd':  round(explained),
        'actual_pl_ntd':  round(actual_pl),
        'residual_ntd':   round(residual),
    }


def summary() -> Optional[Dict[str, Any]]:
    """從所有 snapshots 計算最近 N 日 attribution chain。"""
    data = S.load()
    snaps = sorted(data.get('snapshots', []), key=lambda s: s.get('date', ''))
    rows = [s for s in snaps if s.get('greeks') and s.get('market', {}).get('tx')]
    if len(rows) < 2:
        return None

    attribs = []
    for i in range(1, len(rows)):
        a = attribute(rows[i], rows[i - 1])
        if a:
            attribs.append(a)
    if not attribs:
        return None

    # 累積彙整（最近 30 天）
    recent = attribs[-30:]
    sums = {
        'delta':  sum(a['delta_pl_ntd']  for a in recent),
        'vega':   sum(a['vega_pl_ntd']   for a in recent),
        'theta':  sum(a['theta_pl_ntd']  for a in recent),
        'actual': sum(a['actual_pl_ntd'] for a in recent),
    }
    sums['explained'] = sums['delta'] + sums['vega'] + sums['theta']
    sums['residual']  = sums['actual'] - sums['explained']

    return {
        'rows':       attribs,
        'recent':     recent,
        'cumulative': {k: round(v) for k, v in sums.items()},
        'days':       len(recent),
    }
