"""
snapshot.py — 每日 portfolio 快照與趨勢分析

被 shioaji_collar.py 在 main() 結尾呼叫一次，自動寫入 daily_snapshots.json。
同一交易日多次 refresh 只保留「最新」一筆（overwrite 當日）。

daily_snapshots.json schema:
{
  "snapshots": [
    {
      "date":      "2026-05-08",
      "timestamp": "2026-05-08T13:45:00",
      "market":    {"tx": ..., "taiex": ..., "price_0050": ..., "price_2330": ...,
                    "iv_atm": ..., "market_session": ...},
      "core_long": {"notional": ..., "unrealized": ..., "n_items": ...},
      "ledger":    {"open_count": ..., "mtd_realized": ..., "lifetime_realized": ...},
      "betas":     {"beta_2330": ..., "beta_0050": ...}
    },
    ...
  ]
}
"""
import json
import pathlib
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

SNAPSHOT_FILE = pathlib.Path(__file__).parent / 'daily_snapshots.json'


def load() -> Dict[str, Any]:
    if not SNAPSHOT_FILE.exists():
        return {'snapshots': []}
    try:
        return json.loads(SNAPSHOT_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {'snapshots': []}


def save(data: Dict[str, Any]) -> None:
    SNAPSHOT_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def capture(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """從 shioaji_collar result dict 抽取相關欄位寫入 snapshot。
    回傳剛寫入的 snapshot dict。"""
    if not result:
        return None

    market = result.get('market') or {}
    portfolio = result.get('portfolio') or {}
    pf_totals = (portfolio.get('totals') or {})
    pf_items  = (portfolio.get('items')  or [])
    ledger    = result.get('ledger') or {}
    betas     = result.get('betas')  or {}
    pg        = result.get('portfolio_greeks') or {}
    pg_totals = (pg.get('totals') or {})
    pg_legs   = pg.get('legs') or []

    now = datetime.now()
    snap = {
        'date':      now.strftime('%Y-%m-%d'),
        'timestamp': now.isoformat(timespec='seconds'),
        'market': {
            'tx':              market.get('tx_futures'),
            'taiex':           market.get('taiex'),
            'price_0050':      market.get('price_0050'),
            'price_2330':      market.get('price_2330'),
            'iv_atm':          result.get('iv_used'),
            'market_session':  result.get('market_session'),
        },
        'core_long': {
            'notional':   pf_totals.get('core_long_notional'),
            'beta_adj':   pf_totals.get('core_long_beta_adj'),
            'unrealized': pf_totals.get('core_long_unrealized'),
            'n_items':    len(pf_items),
        } if pf_totals else None,
        'ledger': {
            'open_count':        ledger.get('open_count'),
            'mtd_realized':      ledger.get('mtd_realized'),
            'lifetime_realized': ledger.get('lifetime_realized'),
        } if ledger else None,
        'betas': {
            'beta_2330': betas.get('beta_2330'),
            'beta_0050': betas.get('beta_0050'),
        } if betas else None,
        'greeks': {
            'net_delta':              pg_totals.get('net_delta'),
            'delta_ntd_per_1pct_tx':  pg_totals.get('delta_ntd_per_1pct_tx'),
            'theta_ntd_per_day':      pg_totals.get('theta_ntd_per_day'),
            'vega_ntd_per_pct_iv':    pg_totals.get('vega_ntd_per_pct_iv'),
            'leg_count':              len(pg_legs),
        } if pg_totals else None,
    }

    data = load()
    snaps = data.get('snapshots', [])
    today_str = snap['date']

    # Overwrite 同日；其他保留
    snaps = [s for s in snaps if s.get('date') != today_str]
    snaps.append(snap)
    snaps.sort(key=lambda s: s.get('date', ''))

    save({'snapshots': snaps})
    return snap


def _delta(today: dict, base: Optional[dict]) -> Optional[Dict[str, Any]]:
    """計算 today 相對 base 的關鍵差異。base 為 None 時回 None。"""
    if not base:
        return None

    def _get(d, *keys):
        cur = d
        for k in keys:
            if cur is None:
                return None
            cur = cur.get(k) if isinstance(cur, dict) else None
        return cur

    def _diff(a, b):
        if a is None or b is None:
            return None
        return a - b

    def _pct(a, b):
        if a is None or b is None or not b:
            return None
        return (a - b) / abs(b) * 100

    return {
        'base_date':            base.get('date'),
        'tx_delta_pct':         round(_pct(_get(today, 'market', 'tx'),
                                           _get(base,  'market', 'tx')) or 0, 2),
        'taiex_delta_pct':      round(_pct(_get(today, 'market', 'taiex'),
                                           _get(base,  'market', 'taiex')) or 0, 2),
        'unrealized_delta':     _diff(_get(today, 'core_long', 'unrealized'),
                                      _get(base,  'core_long', 'unrealized')),
        'mtd_realized_delta':   _diff(_get(today, 'ledger', 'mtd_realized'),
                                      _get(base,  'ledger', 'mtd_realized')),
        'iv_delta_pct':         round((_diff(_get(today, 'market', 'iv_atm'),
                                             _get(base,  'market', 'iv_atm')) or 0) * 100, 1),
    }


def trend_summary(window_days: int = 60) -> Optional[Dict[str, Any]]:
    """回傳近期 snapshot + 各時段 delta。"""
    data = load()
    snaps = sorted(data.get('snapshots', []), key=lambda s: s.get('date', ''))
    if not snaps:
        return None

    today = snaps[-1]
    today_date = today.get('date', '')

    yesterday      = snaps[-2] if len(snaps) >= 2 else None

    # 一週前：找日期 ≤ today - 7d 的最近一筆
    try:
        target_date = (datetime.strptime(today_date, '%Y-%m-%d')
                       - timedelta(days=7)).strftime('%Y-%m-%d')
        week_ago = next((s for s in reversed(snaps)
                         if s.get('date', '') <= target_date), None)
    except Exception:
        week_ago = None

    # 月初：本月第一筆
    month_prefix = today_date[:7]
    month_first = next((s for s in snaps if s.get('date', '').startswith(month_prefix)), None)

    return {
        'today':           today,
        'snapshot_count':  len(snaps),
        'first_date':      snaps[0].get('date'),
        'recent_snapshots': snaps[-window_days:],
        'changes': {
            'vs_yesterday':   _delta(today, yesterday),
            'vs_week_ago':    _delta(today, week_ago),
            'vs_month_start': _delta(today, month_first),
        },
    }


def greeks_trend(window_days: int = 30) -> Optional[Dict[str, Any]]:
    """從歷次 daily snapshot 抽 greeks，回傳：
      - history: 過去 window_days 內每日 greeks 值（時間序列）
      - cumulative: 各時段累積 theta cost 估算
        7d / 30d / lifetime（snapshot 起算）— 把每日 theta_ntd_per_day 加總
    沒有 greeks 資料的天會被略過。
    """
    data = load()
    snaps = sorted(data.get('snapshots', []), key=lambda s: s.get('date', ''))
    rows = [s for s in snaps if s.get('greeks')]
    if not rows:
        return None

    # 時間序列（取後 window_days 筆）
    history = [{
        'date':                  s['date'],
        'net_delta':             s['greeks'].get('net_delta'),
        'delta_ntd_per_1pct_tx': s['greeks'].get('delta_ntd_per_1pct_tx'),
        'theta_ntd_per_day':     s['greeks'].get('theta_ntd_per_day'),
        'vega_ntd_per_pct_iv':   s['greeks'].get('vega_ntd_per_pct_iv'),
        'leg_count':             s['greeks'].get('leg_count'),
    } for s in rows[-window_days:]]

    # 累積 theta：簡化假設「每筆 snapshot 代表持倉 1 日」
    today_str = rows[-1]['date']

    def _sum_window(days: int) -> int:
        try:
            cutoff = (datetime.strptime(today_str, '%Y-%m-%d')
                      - timedelta(days=days - 1)).strftime('%Y-%m-%d')
        except Exception:
            return 0
        return int(sum((s['greeks'].get('theta_ntd_per_day') or 0)
                       for s in rows if s['date'] >= cutoff))

    return {
        'history':         history,
        'snapshot_count':  len(rows),
        'first_date':      rows[0]['date'],
        'cumulative': {
            'last_7d':      _sum_window(7),
            'last_30d':     _sum_window(30),
            'lifetime':     int(sum((s['greeks'].get('theta_ntd_per_day') or 0) for s in rows)),
        },
    }
