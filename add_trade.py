#!/usr/bin/env python3
"""
add_trade.py — 期權交易紀錄 CLI

用法:
  # 開倉
  python3 add_trade.py add sell_to_open "TXO 202605W2 44100C" 1 81 --fee 25 --book trading
  python3 add_trade.py add buy_to_open  "TXO 202605F2 39500P"  1 50 --book hedge

  # 平倉（自動算 P&L）
  python3 add_trade.py close T20260507-2200-001 50 --fee 25
  python3 add_trade.py close T20260507-2200-001 50 --note "停損"

  # 列表
  python3 add_trade.py list                # 全部
  python3 add_trade.py list --open         # 只看 open
  python3 add_trade.py list --book hedge   # 只看 hedge book

  # 彙總
  python3 add_trade.py summary
"""
import argparse, sys
from datetime import datetime

import ledger as L


# ─── ID 產生 ──────────────────────────────────────────────────────
def gen_id() -> str:
    """T{YYYYMMDD-HHMM}-{NNN}；NNN 為當分鐘內 max+1（避免單次 CLI 多筆撞號）。"""
    data = L.load() or {'trades': []}
    prefix = datetime.now().strftime('T%Y%m%d-%H%M')
    existing_max = 0
    for t in data.get('trades', []):
        if t.get('id', '').startswith(prefix):
            try:
                existing_max = max(existing_max, int(t['id'].rsplit('-', 1)[-1]))
            except (ValueError, IndexError):
                pass
    return f'{prefix}-{existing_max + 1:03d}'


# ─── add ──────────────────────────────────────────────────────────
def cmd_add(args):
    data = L.load() or {'trades': []}
    trade = {
        'id':          gen_id(),
        'datetime':    datetime.now().isoformat(timespec='seconds'),
        'side':        args.side,
        'instrument':  args.instrument,
        'qty':         args.qty,
        'price':       args.price,
        'fee':         args.fee,
        'book':        args.book,
        'status':      'open',
        'linked_id':   None,
        'realized_pnl': None,
        'note':        args.note,
    }
    data['trades'].append(trade)
    L.save(data)
    print(f"✓ 新增: {trade['id']}")
    print(f"  {trade['side']} {trade['instrument']}  qty={trade['qty']}  @ {trade['price']}  fee={trade['fee']}")
    print(f"  book={trade['book']}")


# ─── close ────────────────────────────────────────────────────────
def cmd_close(args):
    data = L.load() or {'trades': []}
    open_t = next((t for t in data['trades'] if t['id'] == args.trade_id), None)
    if not open_t:
        sys.exit(f"❌ 找不到 ID: {args.trade_id}")
    if open_t.get('status') != 'open':
        sys.exit(f"❌ {args.trade_id} 已 closed")

    pnl = L.compute_realized_pnl(open_t, args.price, args.fee)

    close_side = 'buy_to_close' if open_t['side'].startswith('sell') else 'sell_to_close'
    close_t = {
        'id':          gen_id(),
        'datetime':    datetime.now().isoformat(timespec='seconds'),
        'side':        close_side,
        'instrument':  open_t['instrument'],
        'qty':         open_t['qty'],
        'price':       args.price,
        'fee':         args.fee,
        'book':        open_t['book'],
        'status':      'closed',
        'linked_id':   open_t['id'],
        'realized_pnl': pnl['net_pnl'],
        'note':        args.note,
    }
    data['trades'].append(close_t)

    open_t['status']        = 'closed'
    open_t['linked_id']     = close_t['id']
    open_t['realized_pnl']  = pnl['net_pnl']

    L.save(data)
    sign = '+' if pnl['net_pnl'] >= 0 else ''
    print(f"✓ 平倉: {open_t['id']} → {close_t['id']}")
    print(f"  {open_t['instrument']}  open {open_t['price']} → close {args.price}")
    print(f"  Gross: {pnl['gross_pnl']:+,.0f}  Fees: {pnl['fees']:,.0f}  Net: {sign}{pnl['net_pnl']:,.0f} NT")


# ─── list ─────────────────────────────────────────────────────────
def cmd_list(args):
    data = L.load() or {'trades': []}
    trades = data['trades']
    if args.open_only:
        trades = [t for t in trades if t['status'] == 'open']
    if args.book:
        trades = [t for t in trades if t.get('book') == args.book]
    if args.recent:
        trades = trades[-args.recent:]
    if not trades:
        print('(無紀錄)')
        return

    print(f"{'ID':<22}{'Side':<16}{'Instrument':<26}{'Qty':>4}{'Price':>8}{'P&L':>10}  {'Book':<8}")
    print('─' * 96)
    for t in trades:
        pnl = t.get('realized_pnl')
        pnl_s = f"{pnl:+,.0f}" if pnl is not None else '—'
        ins   = t['instrument'][:25]
        print(f"{t['id']:<22}{t['side']:<16}{ins:<26}{t['qty']:>4}{t['price']:>8.1f}{pnl_s:>10}  {t.get('book',''):<8}")


# ─── summary ──────────────────────────────────────────────────────
def cmd_summary(args):
    s = L.summary()
    if not s:
        print('(無資料)')
        return
    print('═══ Trading Ledger Summary ═══')
    print(f"  Open positions:    {s['open_count']} 筆")
    print(f"  Closed:            {s['closed_count']} 筆")
    print(f"  本月已實現 P&L:    {s['mtd_realized']:+,.0f} NT")
    print(f"  Lifetime 已實現:   {s['lifetime_realized']:+,.0f} NT")
    print()
    print('  By book:')
    for book, pnl in (s.get('by_book') or {}).items():
        cnt = (s.get('by_book_count') or {}).get(book, 0)
        print(f"    {book:<10} {pnl:+,.0f} NT  ({cnt} 筆)")
    if s['open_positions']:
        print()
        print('  Open positions:')
        for op in s['open_positions']:
            print(f"    {op['id']}  {op['side']:<16} {op['instrument']:<26} qty={op['qty']} @ {op['open_price']}  ({op['book']})")


# ─── main ─────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description='期權交易紀錄管理')
    sub = p.add_subparsers(dest='cmd', required=True)

    p_add = sub.add_parser('add', help='新增 open trade')
    p_add.add_argument('side', choices=['sell_to_open', 'buy_to_open'])
    p_add.add_argument('instrument', help='e.g. "TXO 202605W2 44100C"')
    p_add.add_argument('qty', type=int)
    p_add.add_argument('price', type=float)
    p_add.add_argument('--fee', type=float, default=25)
    p_add.add_argument('--book', choices=['hedge', 'trading', 'core'], default='trading')
    p_add.add_argument('--note', default='')
    p_add.set_defaults(func=cmd_add)

    p_close = sub.add_parser('close', help='平倉 + 算 P&L')
    p_close.add_argument('trade_id')
    p_close.add_argument('price', type=float)
    p_close.add_argument('--fee', type=float, default=25)
    p_close.add_argument('--note', default='')
    p_close.set_defaults(func=cmd_close)

    p_list = sub.add_parser('list', help='列表')
    p_list.add_argument('--open', dest='open_only', action='store_true')
    p_list.add_argument('--book')
    p_list.add_argument('--recent', type=int, help='只列最近 N 筆')
    p_list.set_defaults(func=cmd_list)

    p_sum = sub.add_parser('summary', help='彙總')
    p_sum.set_defaults(func=cmd_summary)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
