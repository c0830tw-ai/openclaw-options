"""
order_helper.py — 限價階梯 + add_trade.py 命令產生器

針對 collar dashboard 推薦的結構或 weekly opportunities，產出：
  - 各腳的 3 段階梯限價（試探 / 中位 / 主動成交）
  - ready-to-paste add_trade.py 命令字串

純後端工具（也可被 UI 引用），不打 API。
"""
from typing import Any, Dict, List, Optional


def ladder_buy(bid: float, mid: float, ask: float, fmt_int: bool = True) -> Dict[str, Any]:
    """買進 3 段：bid+2（試）→ mid → ask（吃）。"""
    bid = bid or 0; ask = ask or 0
    mid = mid or ((bid + ask) / 2 if (bid and ask) else 0)
    fn = round if fmt_int else lambda x: round(x, 1)
    return {
        'side':   'buy',
        'try_1':  max(fn(bid + 2), 1) if bid > 0 else fn(mid * 0.97),
        'try_2':  fn(mid) if mid else None,
        'try_3':  fn(ask) if ask else None,
        'wait':   '8 分鐘 / 段',
    }


def ladder_sell(bid: float, mid: float, ask: float, fmt_int: bool = True) -> Dict[str, Any]:
    """賣出 3 段：ask-2（試）→ mid → bid（吃）。"""
    bid = bid or 0; ask = ask or 0
    mid = mid or ((bid + ask) / 2 if (bid and ask) else 0)
    fn = round if fmt_int else lambda x: round(x, 1)
    return {
        'side':   'sell',
        'try_1':  max(fn(ask - 2), 1) if ask > 0 else fn(mid * 1.03),
        'try_2':  fn(mid) if mid else None,
        'try_3':  fn(bid) if bid else None,
        'wait':   '8 分鐘 / 段',
    }


def _format_instrument(month: str, strike: float, right: str) -> str:
    """組合 add_trade.py 的 instrument 字串。
    e.g. 'TXO 202605F2 39000P' （給對應月份結算的格式）"""
    rl = 'P' if right.lower() == 'put' else 'C'
    return f'TXO {month} {int(strike)}{rl}'


def add_trade_cmd(side_open: str, instrument: str, qty: int, price: float,
                  book: str = 'hedge', thesis: Optional[str] = None) -> str:
    """生成 add_trade.py add 命令字串（用於 UI copy 按鈕）。"""
    side = 'sell_to_open' if side_open == 'sell' else 'buy_to_open'
    cmd = (
        f'python3 add_trade.py add {side} '
        f'"{instrument}" {qty} {int(price) if float(price).is_integer() else price} '
        f'--book {book}'
    )
    if thesis:
        thesis_safe = thesis.replace('"', '\\"')
        cmd += f' --thesis "{thesis_safe}"'
    return cmd


def helper_for_put_buy(strike: float, qty: int, bid: float, mid: float, ask: float,
                       month: str = '202605', thesis: str = '') -> Dict[str, Any]:
    """單腳：買 put（hedge）。"""
    instrument = _format_instrument(month, strike, 'put')
    ladder = ladder_buy(bid, mid, ask)
    return {
        'leg':         'buy_put',
        'instrument':  instrument,
        'qty':         qty,
        'ladder':      ladder,
        'commands':    [
            add_trade_cmd('buy', instrument, qty, ladder['try_1'], 'hedge', thesis or 'Hedge buy put'),
            add_trade_cmd('buy', instrument, qty, ladder['try_2'], 'hedge', thesis or 'Hedge buy put'),
            add_trade_cmd('buy', instrument, qty, ladder['try_3'], 'hedge', thesis or 'Hedge buy put'),
        ],
    }


def helper_for_call_sell(strike: float, qty: int, bid: float, mid: float, ask: float,
                          month: str = '202605', thesis: str = '') -> Dict[str, Any]:
    """單腳：賣 call（trading book = covered call）。"""
    instrument = _format_instrument(month, strike, 'call')
    ladder = ladder_sell(bid, mid, ask)
    return {
        'leg':         'sell_call',
        'instrument':  instrument,
        'qty':         qty,
        'ladder':      ladder,
        'commands':    [
            add_trade_cmd('sell', instrument, qty, ladder['try_1'], 'trading', thesis or 'Sell call premium'),
            add_trade_cmd('sell', instrument, qty, ladder['try_2'], 'trading', thesis or 'Sell call premium'),
            add_trade_cmd('sell', instrument, qty, ladder['try_3'], 'trading', thesis or 'Sell call premium'),
        ],
    }


def helper_from_collar_recommendation(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """根據 collar_dashboard.recommended_structure 產出對應的下單建議。
    讀 result 內已 enrich 的 selected_options + structures。"""
    cd = data.get('collar_dashboard') or {}
    rs = cd.get('recommended_structure') or {}
    if not rs:
        return None

    so = data.get('selected_options') or {}
    put = so.get('put')  or {}
    call = so.get('call') or {}
    month = data.get('txo_month', '')

    # 從 structures list 找符合推薦結構的口數設定
    structs = data.get('structures') or []
    target = next((s for s in structs if s.get('name') == rs.get('structure')), None)
    qty_call = (target.get('calls') if target else 0) or 0
    qty_put  = (target.get('puts')  if target else 0) or 0

    legs = []
    if qty_put > 0 and put.get('strike'):
        legs.append(helper_for_put_buy(
            strike=put['strike'], qty=qty_put,
            bid=put.get('bid'), mid=put.get('mid'), ask=put.get('ask'),
            month=month,
            thesis=f'結構 {rs["label"]}：{rs["reason"]}',
        ))
    if qty_call > 0 and call.get('strike'):
        legs.append(helper_for_call_sell(
            strike=call['strike'], qty=qty_call,
            bid=call.get('bid'), mid=call.get('mid'), ask=call.get('ask'),
            month=month,
            thesis=f'結構 {rs["label"]}：{rs["reason"]}',
        ))

    if not legs:
        return None
    return {
        'structure':  rs['structure'],
        'label':      rs['label'],
        'reason':     rs['reason'],
        'legs':       legs,
    }
