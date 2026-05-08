"""
ledger.py — 交易紀錄讀取與彙總（唯讀）

資料源: ./trades_ledger.json（由 add_trade.py 寫入）
被 shioaji_collar.py 引入做 result JSON 的 summary，UI 也讀同份資料。

trades_ledger.json schema:
{
  "trades": [
    {
      "id": "T20260507-2200-001",
      "datetime": "2026-05-07T22:00:00",
      "side": "sell_to_open" | "buy_to_open" | "buy_to_close" | "sell_to_close",
      "instrument": "TXO 202605W2 44100C",
      "qty": 1,
      "price": 81.0,            # 點數
      "fee": 25,                # NT
      "book": "hedge" | "trading" | "core",
      "status": "open" | "closed",
      "linked_id": "T...",      # 對應的 close（在 open 上）或 open（在 close 上）
      "realized_pnl": null | 1500.0,
      "note": ""
    }
  ]
}
"""
import json
import pathlib
from datetime import datetime
from typing import Optional, Dict, Any

LEDGER_FILE = pathlib.Path(__file__).parent / 'trades_ledger.json'

# TXO 契約乘數
TXO_MULTIPLIER = 50


def load() -> Optional[Dict[str, Any]]:
    if not LEDGER_FILE.exists():
        return None
    try:
        return json.loads(LEDGER_FILE.read_text(encoding='utf-8'))
    except Exception:
        return None


def save(data: Dict[str, Any]) -> None:
    LEDGER_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def summary() -> Optional[Dict[str, Any]]:
    """彙總目前 ledger，回傳 dict（無資料時回 None）。"""
    data = load()
    if not data:
        return None
    trades = data.get('trades', []) or []
    if not trades:
        return None

    # 找所有 open（尚未平倉）
    open_trades = [t for t in trades if t.get('status') == 'open']

    # 已實現 P&L：在 close-side 紀錄上
    closes = [t for t in trades if t.get('realized_pnl') is not None
              and t.get('side', '').endswith('_close')]

    # MTD 統計
    today = datetime.now()
    mtd_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    mtd_pnl = 0.0
    for t in closes:
        try:
            ts = datetime.fromisoformat(t.get('datetime', ''))
            if ts >= mtd_start:
                mtd_pnl += float(t['realized_pnl'])
        except Exception:
            pass

    # 各 book 累計（lifetime）
    by_book: Dict[str, float] = {}
    by_book_count: Dict[str, int] = {}
    by_book_mtd: Dict[str, float] = {}      # 本月各 book 已實現
    for t in closes:
        book = t.get('book', 'trading')
        by_book[book]       = by_book.get(book, 0)       + float(t['realized_pnl'])
        by_book_count[book] = by_book_count.get(book, 0) + 1
        try:
            ts = datetime.fromisoformat(t.get('datetime', ''))
            if ts >= mtd_start:
                by_book_mtd[book] = by_book_mtd.get(book, 0) + float(t['realized_pnl'])
        except Exception:
            pass

    lifetime_pnl = sum(float(t['realized_pnl']) for t in closes)

    return {
        'open_count':         len(open_trades),
        'closed_count':       len(closes),
        'mtd_realized':       round(mtd_pnl, 0),
        'lifetime_realized':  round(lifetime_pnl, 0),
        'by_book':            {k: round(v, 0) for k, v in by_book.items()},
        'by_book_count':      by_book_count,
        'by_book_mtd':        {k: round(v, 0) for k, v in by_book_mtd.items()},
        'open_positions':     [
            {
                'id':         t['id'],
                'datetime':   t.get('datetime'),
                'side':       t.get('side'),
                'instrument': t.get('instrument'),
                'qty':        t.get('qty'),
                'open_price': t.get('price'),
                'book':       t.get('book', 'trading'),
            } for t in open_trades
        ],
    }


def compute_realized_pnl(open_trade: dict, close_price: float, close_fee: float) -> Dict[str, float]:
    """計算平倉的 gross / net P&L。short → 收高賣低賺；long → 買低賣高賺。"""
    qty       = open_trade['qty']
    open_p    = open_trade['price']
    open_fee  = open_trade.get('fee', 0) or 0
    side      = open_trade['side']

    if side.startswith('sell'):  # 賣方開倉，買回平倉
        gross = (open_p - close_price) * TXO_MULTIPLIER * qty
    else:                        # 買方開倉，賣出平倉
        gross = (close_price - open_p) * TXO_MULTIPLIER * qty

    fees = open_fee + close_fee
    return {
        'gross_pnl': round(gross, 0),
        'fees':      round(fees, 0),
        'net_pnl':   round(gross - fees, 0),
    }
