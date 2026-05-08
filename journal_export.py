"""
journal_export.py — 月度交易日記匯出（Markdown）

從 trades_ledger.json 撈指定月份的交易，配合 daily_snapshots、
event_history、health_check（最新值），組成一份月度回顧 Markdown。

CLI：
  python3 journal_export.py --month 2026-04          # 指定月份
  python3 journal_export.py --current                 # 本月（含至今）
  python3 journal_export.py --pdf                     # 同時輸出 PDF (需 pandoc)
"""
import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import ledger as L

_HERE = Path(__file__).resolve().parent
EXPORT_DIR = _HERE / 'journal_exports'


def _safe(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _fmt_n(n) -> str:
    if n is None:
        return '—'
    try:
        return f'{int(round(float(n))):,}'
    except (TypeError, ValueError):
        return str(n)


def _fmt_s(n) -> str:
    if n is None:
        return '—'
    try:
        v = int(round(float(n)))
        return ('+' if v > 0 else '') + f'{v:,}'
    except (TypeError, ValueError):
        return str(n)


def _filter_month_trades(trades: List[Dict[str, Any]], year: int, month: int) -> List[Dict[str, Any]]:
    prefix = f'{year:04d}-{month:02d}'
    return [t for t in trades if (t.get('datetime') or '')[:7] == prefix]


def _bucket_thesis(thesis: str) -> str:
    t = (thesis or '').lower()
    if not t: return '無 thesis'
    keys = [
        ('hedge',    ['hedge', '對沖', '保護', 'put', '避險']),
        ('iv 收割',  ['iv', 'theta', '賣方', 'sell', '收 premium', '收權利金']),
        ('趨勢',     ['趨勢', 'trend', '突破', 'breakout', '上漲', '下跌', '反彈']),
        ('事件',     ['fomc', 'cpi', '法說', '財報', '事件']),
    ]
    for label, words in keys:
        if any(w in t for w in words):
            return label
    return '其他'


def export_markdown(year: int, month: int, output_path: Optional[Path] = None) -> Path:
    data = L.load() or {}
    all_trades = data.get('trades') or []
    trades = _filter_month_trades(all_trades, year, month)
    opens = [t for t in trades if t.get('side', '').endswith('_to_open')]
    closes = [t for t in trades if t.get('side', '').endswith('_to_close')]

    # 統計
    closed_opens = [t for t in opens if t.get('status') == 'closed']
    pnls = [t.get('realized_pnl') for t in closed_opens if t.get('realized_pnl') is not None]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    total_pnl = sum(pnls) if pnls else 0
    win_rate = (wins / len(pnls) * 100) if pnls else 0

    # By thesis
    by_thesis: Dict[str, Dict[str, Any]] = {}
    for t in opens:
        b = _bucket_thesis(t.get('thesis', ''))
        by_thesis.setdefault(b, {'count': 0, 'closed': 0, 'wins': 0, 'pnl': 0,
                                  'thesis_correct': 0, 'thesis_marked': 0})
        by_thesis[b]['count'] += 1
        if t.get('status') == 'closed':
            by_thesis[b]['closed'] += 1
            pnl = t.get('realized_pnl') or 0
            by_thesis[b]['pnl'] += pnl
            if pnl > 0: by_thesis[b]['wins'] += 1
            tc = t.get('thesis_correct')
            if tc is not None:
                by_thesis[b]['thesis_marked'] += 1
                by_thesis[b]['thesis_correct'] += int(bool(tc))

    # 開頭
    lines = []
    lines.append(f'# 交易日記月報 — {year} 年 {month} 月')
    lines.append('')
    lines.append(f'> 匯出時間：{datetime.now().strftime("%Y-%m-%d %H:%M")}')
    lines.append('')
    lines.append('---')
    lines.append('')

    # 1. 月度總結
    lines.append('## 1. 月度總結')
    lines.append('')
    lines.append('| 指標 | 數值 |')
    lines.append('|---|---|')
    lines.append(f'| 開倉筆數 | {len(opens)} |')
    lines.append(f'| 平倉筆數 | {len(closes)} |')
    lines.append(f'| 月末未平倉 | {len(opens) - len(closed_opens)} |')
    lines.append(f'| 已實現 P&L | **{_fmt_s(total_pnl)} NT** |')
    lines.append(f'| 勝率 | {wins}/{len(pnls)} ({win_rate:.1f}%) |')
    lines.append(f'| 賠率 | {losses}/{len(pnls)} |')
    lines.append('')

    # 2. By thesis
    if by_thesis:
        lines.append('## 2. 按論點分類')
        lines.append('')
        lines.append('| 論點 | 筆數 | 已平 | 勝率 | 命中率 | P&L |')
        lines.append('|---|---|---|---|---|---|')
        for label, s in sorted(by_thesis.items(), key=lambda x: -x[1]['pnl']):
            wr = f'{(s["wins"] / s["closed"] * 100):.0f}%' if s['closed'] else '—'
            tc = (f'{(s["thesis_correct"] / s["thesis_marked"] * 100):.0f}%'
                  if s['thesis_marked'] else '—')
            lines.append(f'| {label} | {s["count"]} | {s["closed"]} | {wr} | {tc} | {_fmt_s(s["pnl"])} |')
        lines.append('')

    # 3. 個別交易
    if opens:
        lines.append('## 3. 交易明細')
        lines.append('')
        for t in sorted(opens, key=lambda x: x.get('datetime', '')):
            d_open = (t.get('datetime') or '')[:10]
            status = '✅ 平倉' if t.get('status') == 'closed' else '📌 仍持有'
            pnl = t.get('realized_pnl')
            pnl_str = f' / P&L **{_fmt_s(pnl)} NT**' if pnl is not None else ''
            tc = t.get('thesis_correct')
            tc_str = (' ✓' if tc == 1 else ' ✗') if tc is not None else ''

            lines.append(f'### {d_open} · {t.get("instrument", "?")} · {t["id"]}')
            lines.append('')
            lines.append(f'**{t.get("side")} {t.get("qty")} 口 @ {t.get("price")}**  '
                         f'(book: {t.get("book", "-")}) — {status}{pnl_str}{tc_str}')
            lines.append('')
            if t.get('thesis'):
                lines.append(f'> **論點**：{t["thesis"]}')
                lines.append('')
            ctx = t.get('context') or {}
            if ctx and any(ctx.values()):
                ctx_parts = []
                if ctx.get('tx'):       ctx_parts.append(f'TX {ctx["tx"]:.0f}')
                if ctx.get('iv_atm'):   ctx_parts.append(f'IV {ctx["iv_atm"]*100:.1f}%')
                if ctx.get('dte'):      ctx_parts.append(f'DTE {ctx["dte"]}d')
                if ctx.get('next_event'): ctx_parts.append(f'下個事件 {ctx["next_event"]}')
                if ctx.get('session'):  ctx_parts.append(f'時段 {ctx["session"]}')
                if ctx_parts:
                    lines.append(f'  Context: {" · ".join(ctx_parts)}')
                    lines.append('')
            if t.get('outcome'):
                lines.append(f'> **結果反思**：{t["outcome"]}')
                lines.append('')
            if t.get('note'):
                lines.append(f'  備註：{t["note"]}')
                lines.append('')
            lines.append('---')
            lines.append('')

    # 4. 月底反思（空白讓用戶手動填）
    lines.append('## 4. 月底反思')
    lines.append('')
    lines.append('> 自由文字 — 補完後存檔當作回顧紀錄')
    lines.append('')
    lines.append('### 哪些做對了？')
    lines.append('- ')
    lines.append('')
    lines.append('### 哪些做錯了？')
    lines.append('- ')
    lines.append('')
    lines.append('### 下個月要調整什麼？')
    lines.append('- ')
    lines.append('')

    # 寫檔
    EXPORT_DIR.mkdir(exist_ok=True)
    if output_path is None:
        output_path = EXPORT_DIR / f'{year:04d}-{month:02d}.md'
    output_path.write_text('\n'.join(lines), encoding='utf-8')
    return output_path


def export_pdf(md_path: Path) -> Optional[Path]:
    """用 pandoc 把 md 轉 pdf。沒安裝就回 None。"""
    if not shutil.which('pandoc'):
        print('[journal_export] pandoc 未安裝，跳過 PDF', file=sys.stderr)
        return None
    pdf_path = md_path.with_suffix('.pdf')
    try:
        subprocess.run(
            ['pandoc', str(md_path), '-o', str(pdf_path),
             '-V', 'CJKmainfont=PingFang TC',
             '--pdf-engine=xelatex'],
            check=True, capture_output=True,
        )
        return pdf_path
    except subprocess.CalledProcessError as e:
        print(f'[journal_export] pandoc 失敗: {e.stderr.decode()}', file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--month', help='YYYY-MM (預設上月)')
    ap.add_argument('--current', action='store_true', help='本月（含至今）')
    ap.add_argument('--pdf', action='store_true', help='也輸出 PDF (需 pandoc)')
    args = ap.parse_args()

    if args.current:
        now = datetime.now()
        year, month = now.year, now.month
    elif args.month:
        year, month = map(int, args.month.split('-'))
    else:
        # 預設上月
        now = datetime.now()
        if now.month == 1:
            year, month = now.year - 1, 12
        else:
            year, month = now.year, now.month - 1

    md_path = export_markdown(year, month)
    print(f'[journal_export] wrote → {md_path}')

    if args.pdf:
        pdf = export_pdf(md_path)
        if pdf:
            print(f'[journal_export] PDF → {pdf}')


if __name__ == '__main__':
    sys.exit(main() or 0)
