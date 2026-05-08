"""
trade_journal.py — 交易日記分析

從 trades_ledger.json 抽出帶 thesis / outcome / context 的紀錄，
聚合 monthly 統計：
  - thesis 命中率（thesis_correct=1 比例）
  - 平均 P&L（按 thesis 文字分桶 / 按 book 分桶）
  - 月度交易筆數
  - Recent journal entries（最近 8 筆 thesis/outcome 配對）
"""
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

import ledger as L


def _bucket_thesis(thesis: str) -> str:
    """依 thesis 關鍵字分桶（最多 3 桶 + other）。"""
    t = (thesis or '').lower()
    if not t:
        return '無 thesis'
    keys = [
        ('hedge',       ['hedge', '對沖', '保護', 'put', '避險']),
        ('iv 收割',     ['iv', 'theta', '賣方', 'sell', '收 premium', '收權利金']),
        ('趨勢',         ['趨勢', 'trend', '突破', 'breakout', '上漲', '下跌', '反彈']),
        ('事件',         ['fomc', 'cpi', '法說', '財報', '事件']),
    ]
    for label, words in keys:
        if any(w in t for w in words):
            return label
    return '其他'


def summary() -> Optional[Dict[str, Any]]:
    """產出 journal 統計。沒紀錄回 None。"""
    data = L.load() or {}
    trades = data.get('trades') or []
    if not trades:
        return None

    # 只看 open 那筆（thesis 在 open，realized_pnl 已被回填）
    opens = [t for t in trades if t.get('side', '').endswith('_to_open')]
    if not opens:
        return None

    by_thesis: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        'count': 0, 'closed': 0, 'wins': 0, 'pnl': 0.0, 'thesis_correct': 0, 'thesis_marked': 0,
    })
    by_month: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        'count': 0, 'closed': 0, 'wins': 0, 'pnl': 0.0,
    })
    journal_entries: List[Dict[str, Any]] = []

    for t in opens:
        bucket = _bucket_thesis(t.get('thesis', ''))
        b = by_thesis[bucket]
        b['count'] += 1

        ym = (t.get('datetime') or '')[:7] or 'unknown'
        m = by_month[ym]
        m['count'] += 1

        pnl = t.get('realized_pnl')
        if t.get('status') == 'closed' and pnl is not None:
            b['closed'] += 1; m['closed'] += 1
            b['pnl'] += pnl;  m['pnl']    += pnl
            if pnl > 0:
                b['wins'] += 1; m['wins'] += 1
            tc = t.get('thesis_correct')
            if tc is not None:
                b['thesis_marked']  += 1
                b['thesis_correct'] += int(bool(tc))

        # 收進 journal 列表（thesis 或 outcome 任一有值才算）
        if t.get('thesis') or t.get('outcome'):
            journal_entries.append({
                'id':        t['id'],
                'date':      (t.get('datetime') or '')[:10],
                'instrument': t.get('instrument'),
                'thesis':    t.get('thesis'),
                'outcome':   t.get('outcome'),
                'thesis_correct': t.get('thesis_correct'),
                'realized_pnl':   pnl,
                'context':   t.get('context'),
                'status':    t.get('status'),
            })

    journal_entries.sort(key=lambda e: e['date'], reverse=True)

    # 整理輸出
    def _stats(d):
        out = dict(d)
        out['win_rate']    = round(d['wins']   / d['closed'] * 100, 1) if d['closed'] else None
        out['pnl']         = round(d['pnl'])
        if 'thesis_marked' in d and d['thesis_marked']:
            out['thesis_hit_rate'] = round(d['thesis_correct'] / d['thesis_marked'] * 100, 1)
        return out

    return {
        'total_trades':   len(opens),
        'closed_trades':  sum(1 for t in opens if t.get('status') == 'closed'),
        'by_thesis':      {k: _stats(v) for k, v in by_thesis.items()},
        'by_month':       dict(sorted(({k: _stats(v) for k, v in by_month.items()}).items(), reverse=True)),
        'recent_entries': journal_entries[:8],
    }
