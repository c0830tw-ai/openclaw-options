"""
stop_loss.py — TXO 賣方部位停損監控（Stage 1 純建議）

從 latest_collar.json 的 broker.positions 偵測 direction='Sell' 的 index_option，
若虧損達到開倉權利金的 multiplier 倍 → 觸發停損訊號 + 推薦 buy-to-close ladder。

範例：開倉賣 30 點，現價 90 點 → loss% = +200% = 收進 premium 的 2 倍虧損 → 觸發

整合：
  - alerts.py evaluate() 內呼叫 analyze()，每個觸發腳產生一條 alert
  - cooldown 由 alerts.py 管理（key 含 broker code，每口獨立）

CLI 自查：
  python3 stop_loss.py             # 印出當前所有 TXO 短腳的損益狀態
  python3 stop_loss.py --triggered # 只印觸發停損的
"""
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
LATEST_FILE = _HERE / 'latest_collar.json'

DEFAULT_MULTIPLIER = 2.0       # 預設：虧損 = 開倉權利金的 2x（即現價 = 開倉 × 3）
MIN_OPENING_PREMIUM = 5.0      # 開倉 < 5 點不觸發（避免雞肋部位）
TXO_POINT_VALUE = 50           # NT per point


# ─── option code parsing ─────────────────────────────────────────────
# Shioaji TXO code 結構：<prefix><strike><month-letter><year-digit>
#   prefix: TXO（月）, TX1/TX2/TX4（週三 series）, TXU-Z（週五）
#   month-letter: A-L = call 1-12 月，M-X = put 1-12 月
#   year-digit: 西元年最後一位
_CALL_LETTERS = 'ABCDEFGHIJKL'
_PUT_LETTERS  = 'MNOPQRSTUVWX'


def parse_option_code(code: str) -> Optional[Dict[str, Any]]:
    """解析 broker option code → {strike, right, prefix, expiry_month, expiry_year}。
    無法解析回 None。"""
    if not code or len(code) < 5:
        return None
    c = code.upper()

    if c.startswith('TXO'):
        prefix, rest = 'TXO', c[3:]
    elif c[:2] == 'TX' and len(c) > 3 and c[2] in '12345':
        prefix, rest = c[:3], c[3:]
    elif c[:3] in ('TXU', 'TXV', 'TXX', 'TXY', 'TXZ'):
        prefix, rest = c[:3], c[3:]
    else:
        return None

    if len(rest) < 3:
        return None
    year_digit = rest[-1]
    letter = rest[-2]
    strike_str = rest[:-2]

    try:
        strike = float(strike_str)
        year = 2020 + int(year_digit) + (10 if int(year_digit) < 5 else 0)    # 簡單滾動
    except ValueError:
        return None

    if letter in _CALL_LETTERS:
        right, month = 'call', _CALL_LETTERS.index(letter) + 1
    elif letter in _PUT_LETTERS:
        right, month = 'put', _PUT_LETTERS.index(letter) + 1
    else:
        return None

    return {
        'strike':       strike,
        'right':        right,
        'prefix':       prefix,
        'expiry_month': month,
        'expiry_year':  year,
    }


# ─── ladder 計算 ──────────────────────────────────────────────────────
def _approx_ladder(current: float) -> Dict[str, int]:
    """無 live bid/ask 時用 last_price ±幾點估算 buy-to-close ladder。
    保守 → 主動：bid 試探 / mid / ask 主動成交。
    spread 估算：current 低時用固定 1-2 點，高時用 3%。"""
    spread = max(1.0, current * 0.03)
    bid = max(current - spread, 1.0)
    ask = current + spread
    return {
        'try_1': int(round(bid)),    # 試探（保守）
        'try_2': int(round(current)),    # 中位
        'try_3': int(round(ask)),    # 主動成交
    }


# ─── 核心分析 ──────────────────────────────────────────────────────
def analyze(data: Dict[str, Any],
            multiplier: float = DEFAULT_MULTIPLIER,
            include_unfired: bool = False) -> List[Dict[str, Any]]:
    """掃 broker positions 找 TXO 短腳並計算停損狀態。

    Args:
        data:            latest_collar.json 內容
        multiplier:      虧損倍數（2.0 = 虧到開倉權利金的 2x 觸發）
        include_unfired: True 時也回傳未觸發的（CLI 自查用）

    Returns: list of leg dicts（依虧損 % 大→小排序）
    """
    broker = data.get('broker') or {}
    positions = broker.get('positions') or []

    out: List[Dict[str, Any]] = []
    for p in positions:
        if p.get('category') != 'index_option':
            continue
        if p.get('direction') != 'Sell':
            continue

        opening = float(p.get('price') or 0)
        current = float(p.get('last_price') or 0)
        qty = int(p.get('quantity') or 0)

        if opening < MIN_OPENING_PREMIUM or qty <= 0:
            continue

        loss_pct = (current - opening) / opening    # >0 = 虧損中
        loss_ntd = int((current - opening) * TXO_POINT_VALUE * qty)
        threshold_price = opening * (1 + multiplier)
        triggered = loss_pct >= multiplier

        if not triggered and not include_unfired:
            continue

        parsed = parse_option_code(p.get('code', '')) or {}
        ladder = _approx_ladder(current) if triggered else None

        out.append({
            'code':            p['code'],
            'name':            p.get('name'),
            'right':           parsed.get('right', '?'),
            'strike':          parsed.get('strike'),
            'family':          parsed.get('prefix'),
            'expiry_month':    parsed.get('expiry_month'),
            'qty':             qty,
            'opening_price':   opening,
            'current_price':   current,
            'loss_pct':        loss_pct,
            'loss_ntd':        loss_ntd,
            'threshold_price': threshold_price,
            'multiplier':      multiplier,
            'triggered':       triggered,
            'ladder':          ladder,
        })

    out.sort(key=lambda x: x['loss_pct'], reverse=True)
    return out


# ─── 訊息格式化 ──────────────────────────────────────────────────────
def format_leg_line(L: Dict[str, Any]) -> str:
    right_zh = '賣 put' if L['right'] == 'put' else '賣 call' if L['right'] == 'call' else '短腳'
    strike = int(L['strike']) if L.get('strike') else '?'
    return (f"{right_zh} {strike} × {L['qty']}口 ({L['code']})\n"
            f"  開倉 {L['opening_price']:g} → 現價 {L['current_price']:g}"
            f"（{L['loss_pct']*100:+.0f}%）  損失 {L['loss_ntd']:+,} NT")


def format_alert_message(triggered_legs: List[Dict[str, Any]]) -> str:
    """組裝 Telegram 推送內容（所有觸發腳合成一則）。"""
    if not triggered_legs:
        return ''
    total_loss = sum(L['loss_ntd'] for L in triggered_legs)
    head = f'🛑 TXO 賣方停損觸發（{len(triggered_legs)} 口，累計 {total_loss:+,} NT）'
    parts = [head]
    for L in triggered_legs:
        parts.append('')
        parts.append(format_leg_line(L))
        ld = L.get('ladder') or {}
        if ld:
            parts.append(f"  平倉 ladder（buy-to-close，8 分鐘/段）：")
            parts.append(f"    試探 {ld['try_1']} → 中位 {ld['try_2']} → 主動 {ld['try_3']}")
    return '\n'.join(parts)


def format_single_alert(L: Dict[str, Any]) -> Dict[str, str]:
    """單一觸發腳的 alert dict（給 alerts.py 用）。
    回傳 {'msg': ..., 'tip': ...} 格式。"""
    ld = L.get('ladder') or {}
    right_zh = '賣 put' if L['right'] == 'put' else '賣 call' if L['right'] == 'call' else '短'
    strike = int(L['strike']) if L.get('strike') else '?'
    msg = (f"停損: {right_zh} {strike} × {L['qty']}口 ({L['code']}) "
           f"開 {L['opening_price']:g}→現 {L['current_price']:g} "
           f"({L['loss_pct']*100:+.0f}%) 損失 {L['loss_ntd']:+,} NT")
    tip = (f"buy-to-close ladder: {ld.get('try_1')}→{ld.get('try_2')}→{ld.get('try_3')}"
           if ld else '')
    return {'msg': msg, 'tip': tip}


# ─── CLI ───────────────────────────────────────────────────────────
def _cli() -> int:
    if not LATEST_FILE.exists():
        print(f'❌ {LATEST_FILE.name} 不存在，請先跑 server.py 或 shioaji_collar.py', file=sys.stderr)
        return 1
    data = json.loads(LATEST_FILE.read_text(encoding='utf-8'))
    only_triggered = '--triggered' in sys.argv

    # 解析 multiplier override
    mult = DEFAULT_MULTIPLIER
    for arg in sys.argv[1:]:
        if arg.startswith('--mult='):
            try:
                mult = float(arg.split('=', 1)[1])
            except ValueError:
                pass

    legs = analyze(data, multiplier=mult, include_unfired=not only_triggered)
    if not legs:
        print(f'✅ 沒有 TXO 賣方部位達到 {mult:g}× 停損門檻')
        return 0

    triggered = [L for L in legs if L['triggered']]
    print(f'TXO 短腳停損掃描（門檻 {mult:g}× = 現價 ≥ 開倉 × {1 + mult:g}）')
    print(f'觸發: {len(triggered)}  | 全部 {"賣方部位 " if not only_triggered else ""}: {len(legs)}')
    print()

    for L in legs:
        mark = '🛑' if L['triggered'] else ('🟡' if L['loss_pct'] > 0.5 else '·')
        print(f'{mark} {format_leg_line(L)}')
        if L['triggered'] and L.get('ladder'):
            ld = L['ladder']
            print(f'   buy-to-close ladder: {ld["try_1"]} → {ld["try_2"]} → {ld["try_3"]}')
        print()
    return 0


if __name__ == '__main__':
    sys.exit(_cli())
