"""
drawdown_tracker.py — 從 peak 追當前 portfolio drawdown

equity 定義：core_long.notional + core_long.unrealized + ledger.lifetime_realized
（持股當前市值 + 已實現 P&L）

回傳：
  current_dd_pct  — 當前回檔 %
  peak_date / peak_value — 歷史高點
  current_value   — 今日值
  days_in_dd      — 從 peak 已過幾天
  max_dd_history  — 全期 max DD（極限）
  series          — 過去 30 天 (date, equity, dd_pct) 序列
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

import snapshot as S


def _equity_of(snap: Dict[str, Any]) -> Optional[float]:
    """從 snapshot 算 equity proxy。需要 core_long 與 ledger。"""
    cl = snap.get('core_long') or {}
    lg = snap.get('ledger')    or {}
    notional = cl.get('notional') or 0
    unrealized = cl.get('unrealized') or 0
    lifetime_realized = lg.get('lifetime_realized') or 0
    return notional + unrealized + lifetime_realized if notional else None


def compute() -> Optional[Dict[str, Any]]:
    data = S.load()
    snaps = sorted(data.get('snapshots') or [], key=lambda s: s.get('date', ''))
    rows = []
    for s in snaps:
        eq = _equity_of(s)
        if eq is not None:
            rows.append({
                'date': s.get('date'),
                'equity': eq,
                'tx': (s.get('market') or {}).get('tx'),
            })
    if len(rows) < 2:
        return None

    # 累積 peak + 計算 DD
    peak = rows[0]['equity']
    peak_date = rows[0]['date']
    max_dd = 0.0
    max_dd_date = rows[0]['date']
    series = []
    days_in_dd = 0
    cur_peak_streak = 0   # 從 peak 已過天數

    for r in rows:
        eq = r['equity']
        if eq > peak:
            peak = eq
            peak_date = r['date']
            cur_peak_streak = 0
        else:
            cur_peak_streak += 1
        dd = (eq - peak) / peak * 100 if peak > 0 else 0
        if dd < max_dd:
            max_dd = dd
            max_dd_date = r['date']
        series.append({
            'date':   r['date'],
            'equity': round(eq),
            'dd_pct': round(dd, 2),
            'tx':     round(r.get('tx'), 1) if r.get('tx') else None,
        })

    last = series[-1]
    current_dd = last['dd_pct']

    # 警戒分級
    if current_dd <= -15:
        severity = 'critical'; sev_msg = '🔴 重大回檔，檢視結構或減碼'
    elif current_dd <= -10:
        severity = 'high';     sev_msg = '🟠 顯著回檔，留意 hedge 是否生效'
    elif current_dd <= -5:
        severity = 'medium';   sev_msg = '🟡 中度回檔'
    elif current_dd <= -2:
        severity = 'low';      sev_msg = '正常波動範圍'
    else:
        severity = 'ok';       sev_msg = '✓ 接近 peak 或新高'

    return {
        'current_value':   round(last['equity']),
        'current_dd_pct':  current_dd,
        'peak_value':      round(peak),
        'peak_date':       peak_date,
        'max_dd_pct':      round(max_dd, 2),
        'max_dd_date':     max_dd_date,
        'days_in_dd':      cur_peak_streak,
        'severity':        severity,
        'severity_msg':    sev_msg,
        'series':          series[-30:],   # 後 30 天時序
        'snapshot_count':  len(rows),
    }
