"""
performance_metrics.py — 績效指標計算

從 daily_snapshots 的 equity 序列算：
  - Total / Annualized return
  - Volatility (年化)
  - Sharpe (annual return / annual vol)
  - Sortino (annual return / annual downside vol)
  - Calmar (annual return / |max_drawdown|)
  - Win rate (正報酬日 / 總天數)
  - Avg daily win / Avg daily loss / Profit factor
  - 月度勝率（月 P&L 正的月份比例）

vs benchmark (TX buy-and-hold) 加 alpha
"""
import math
import statistics
from datetime import datetime
from typing import Any, Dict, List, Optional

import snapshot as S


def _equity_of(snap: Dict[str, Any]) -> Optional[float]:
    cl = snap.get('core_long') or {}
    lg = snap.get('ledger')    or {}
    notional = cl.get('notional') or 0
    unrealized = cl.get('unrealized') or 0
    lifetime_realized = lg.get('lifetime_realized') or 0
    return notional + unrealized + lifetime_realized if notional else None


def _annualize_ret(daily_ret: float, n: int) -> float:
    if n <= 0:
        return 0
    return (1 + daily_ret) ** (252 / n) - 1 if daily_ret > -1 else -1


def compute() -> Optional[Dict[str, Any]]:
    data = S.load()
    snaps = sorted(data.get('snapshots') or [], key=lambda s: s.get('date', ''))
    rows = []
    for s in snaps:
        eq = _equity_of(s)
        if eq is not None and eq > 0:
            rows.append({
                'date':   s.get('date'),
                'equity': eq,
                'tx':     (s.get('market') or {}).get('tx'),
            })
    if len(rows) < 5:
        return None

    # Daily returns
    daily_rets = []
    for i in range(1, len(rows)):
        prev_eq = rows[i - 1]['equity']
        if prev_eq > 0:
            daily_rets.append((rows[i]['equity'] - prev_eq) / prev_eq)
    if not daily_rets:
        return None

    n = len(daily_rets)
    mean_d = statistics.mean(daily_rets)
    std_d  = statistics.stdev(daily_rets) if n > 1 else 0
    neg_rets = [r for r in daily_rets if r < 0]
    downside_std = statistics.stdev(neg_rets) if len(neg_rets) > 1 else 0

    annual_ret = (1 + mean_d) ** 252 - 1
    annual_vol = std_d * math.sqrt(252)
    annual_dnv = downside_std * math.sqrt(252)

    sharpe  = annual_ret / annual_vol if annual_vol > 0 else 0
    sortino = annual_ret / annual_dnv if annual_dnv > 0 else 0

    # Max drawdown
    peak = rows[0]['equity']
    max_dd = 0.0
    for r in rows:
        peak = max(peak, r['equity'])
        if peak > 0:
            max_dd = min(max_dd, (r['equity'] - peak) / peak)
    calmar = annual_ret / abs(max_dd) if max_dd < 0 else 0

    # Win/loss stats
    wins = [r for r in daily_rets if r > 0]
    losses = [r for r in daily_rets if r < 0]
    win_rate = len(wins) / n * 100
    avg_win = statistics.mean(wins) * 100 if wins else 0
    avg_loss = statistics.mean(losses) * 100 if losses else 0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (float('inf') if wins else 0)

    # 月度勝率（按月 group）
    monthly: Dict[str, List[float]] = {}
    for i, r in enumerate(rows[1:], start=1):
        ym = (r['date'] or '')[:7]
        ret = daily_rets[i - 1]
        monthly.setdefault(ym, []).append(ret)
    monthly_rets = {}
    for ym, rets in monthly.items():
        monthly_rets[ym] = (1 + statistics.mean(rets)) ** len(rets) - 1
    monthly_wins = sum(1 for r in monthly_rets.values() if r > 0)
    monthly_total = len(monthly_rets)
    monthly_win_rate = monthly_wins / monthly_total * 100 if monthly_total else 0

    # vs TX benchmark
    tx_first = rows[0].get('tx')
    tx_last  = rows[-1].get('tx')
    bench_total_pct = ((tx_last - tx_first) / tx_first * 100) if (tx_first and tx_last) else None
    total_pct = (rows[-1]['equity'] - rows[0]['equity']) / rows[0]['equity'] * 100
    alpha = total_pct - bench_total_pct if bench_total_pct is not None else None

    return {
        'period_days':        n,
        'period_years':       round(n / 252, 2),
        'first_date':         rows[0]['date'],
        'last_date':          rows[-1]['date'],

        'total_return_pct':   round(total_pct, 2),
        'annual_return_pct':  round(annual_ret * 100, 2),
        'annual_vol_pct':     round(annual_vol * 100, 2),
        'max_drawdown_pct':   round(max_dd * 100, 2),

        'sharpe':             round(sharpe, 2),
        'sortino':            round(sortino, 2),
        'calmar':             round(calmar, 2),

        'win_rate_pct':       round(win_rate, 1),
        'avg_win_pct':        round(avg_win, 3),
        'avg_loss_pct':       round(avg_loss, 3),
        'profit_factor':      round(profit_factor, 2) if profit_factor != float('inf') else None,

        'monthly_win_rate_pct': round(monthly_win_rate, 1),
        'monthly_count':        monthly_total,

        'bench_total_pct':    round(bench_total_pct, 2) if bench_total_pct is not None else None,
        'alpha_pct':          round(alpha, 2) if alpha is not None else None,
    }
