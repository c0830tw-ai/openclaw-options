"""
telegram_bot.py — Telegram /command 互動 bot

Long-poll Telegram getUpdates，根據 /command 從 latest_collar.json
回應對應資訊。常駐執行（launchd KeepAlive）。

支援命令：
  /status  — 行情快照（TX / IV / DTE）
  /health  — 健診評分 + 違規清單
  /greeks  — 當前 Δ/θ/ν
  /events  — 近 5 天事件
  /roll    — 換倉建議
  /dd      — drawdown 狀態
  /risk    — 風險限額使用率
  /iv      — IV 百分位
  /regime  — 情境推薦 + 對照當前
  /report  — 觸發 morning_report 內容
  /help    — 列出指令

執行：
  python3 telegram_bot.py
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
LATEST_FILE = _HERE / 'latest_collar.json'
STATE_FILE  = _HERE / 'telegram_bot_state.json'


def _load_env() -> None:
    env = _HERE / '.env'
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _api_get(token: str, method: str, params=None, timeout: int = 35):
    url = f'https://api.telegram.org/bot{token}/{method}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _api_post(token: str, method: str, data: dict):
    url = f'https://api.telegram.org/bot{token}/{method}'
    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=encoded, method='POST')
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode('utf-8'))


def _load_data() -> dict:
    try:
        return json.loads(LATEST_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


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


# ── 各 /command handler ──────────────────────────────────────
def cmd_help(data):
    return ('📚 查詢命令：\n'
            '/status — 行情快照\n'
            '/health — 健診評分\n'
            '/greeks — 曝險 Δ/θ/ν\n'
            '/events — 近 5 天事件\n'
            '/roll   — 換倉建議\n'
            '/dd     — drawdown\n'
            '/risk   — 風險限額\n'
            '/iv     — IV 百分位\n'
            '/regime — 情境推薦\n'
            '/report — 完整早報\n'
            '\n📝 交易紀錄命令：\n'
            '/buy <履約> <口> <價> [thesis]\n'
            '/sell <履約> <口> <價> [thesis]\n'
            '/close <id> <價> [outcome]\n'
            '/positions — 未平倉清單\n'
            '/last — 近 5 筆交易\n'
            '\n⚙️ 設定命令：\n'
            '/setrisk — 列出當前風險限額\n'
            '/setrisk <key> <value> — 動態調整\n'
            '   key: delta / theta / vega / puts / calls / dd\n'
            '\n例：/buy 39000P 3 50 hedge FOMC\n'
            '例：/setrisk delta 18000')


def cmd_status(data):
    m = data.get('market') or {}
    iv = data.get('iv_used') or 0
    iv_pct = iv * 100 if iv < 1 else iv
    ts = data.get('timestamp', '')[:16]
    return (f'📊 行情快照（{ts}）\n'
            f'TX: {_fmt_n(m.get("tx_futures"))}  TAIEX: {_fmt_n(m.get("taiex"))}\n'
            f'IV: {iv_pct:.1f}%  DTE: {data.get("dte_trading", "?")}d')


def cmd_health(data):
    hc = data.get('health_check') or {}
    if not hc:
        return '❌ 沒有健診資料'
    lines = [f'🏥 健診 {hc["overall_score"]}/100 [{hc["grade"]}]']
    for b in hc.get('breakdown', []):
        lines.append(f'  [{b["score"]:>3}] {b["rule"]}')
    if hc.get('violations'):
        lines.append('\n⚠️ 違規：')
        for v in hc['violations'][:3]:
            lines.append(f'  • {v}')
    return '\n'.join(lines)


def cmd_greeks(data):
    pgt = _safe(data, 'portfolio_greeks', 'totals') or {}
    if not pgt:
        return '❌ 沒有 Greeks 資料'
    lines = ['🧮 Greeks 曝險']
    lines.append(f'Δ {_fmt_s(pgt.get("delta_ntd_per_1pct_tx"))} NT / 1% TX')
    lines.append(f'θ {_fmt_s(pgt.get("theta_ntd_per_day"))} NT / 天')
    lines.append(f'ν {_fmt_s(pgt.get("vega_ntd_per_pct_iv"))} NT / 1% IV')
    th = pgt.get('theta_ntd_per_day') or 0
    if th:
        lines.append(f'\n月 hedge 估算 ≈ {_fmt_s(th * 30)} NT')
    return '\n'.join(lines)


def cmd_events(data):
    evs = data.get('upcoming_events') or []
    near = [e for e in evs if (e.get('days_until') or 99) <= 5]
    if not near:
        return '📅 5 天內無高影響事件'
    lines = ['📅 近 5 天事件']
    for e in near:
        d = e.get('days_until')
        when = '今日' if d == 0 else '明日' if d == 1 else f'{d}d'
        lines.append(f'  {when} ({e.get("date")}) {e.get("name")}')
    return '\n'.join(lines)


def cmd_roll(data):
    rolls = data.get('roll_suggestions') or []
    if not rolls:
        return '✓ 目前無換倉建議'
    lines = [f'🔧 換倉建議 ({len(rolls)})']
    for r in rolls[:3]:
        pri = '🔴' if r.get('priority') == 'high' else '🟠'
        lines.append(f'{pri} {(r.get("reason") or "")[:80]}')
    return '\n'.join(lines)


def cmd_dd(data):
    dd = data.get('drawdown') or {}
    if not dd:
        return '❌ drawdown 資料不足（需 ≥ 2 天 snapshots）'
    sev = dd.get('severity', 'ok')
    icon = {'critical':'🔴','high':'🟠','medium':'🟡','low':'·','ok':'✓'}.get(sev, '·')
    return (f'📉 Drawdown 追蹤\n'
            f'{icon} 當前 {dd.get("current_dd_pct", 0):+.2f}% ({dd.get("days_in_dd", 0)} 天從 peak)\n'
            f'歷史 max DD: {dd.get("max_dd_pct", 0):.2f}%\n'
            f'{dd.get("severity_msg", "")}')


def cmd_risk(data):
    rl = data.get('risk_limits') or {}
    if not rl:
        return '❌ 沒有風險限額資料'
    lines = [f'⚠️ 風險限額（{rl.get("overall", "?")}）']
    for m in rl.get('metrics', []):
        if m.get('status') == 'disabled':
            continue
        icon = {'over':'🔴','hot':'🟠','warn':'🟡','ok':'✓'}.get(m.get('status'), '·')
        lines.append(f'{icon} {m["label"]}: {m["usage_pct"]:.0f}% ({_fmt_n(m["current"])}/{_fmt_n(m["limit"])} {m.get("unit", "")})')
    return '\n'.join(lines)


def cmd_iv(data):
    ivp = data.get('iv_percentile') or {}
    if not ivp.get('enough_data'):
        return f'📊 IV 資料累積中（n={ivp.get("history_n", 0)}）'
    return (f'📊 IV 百分位\n'
            f'當前 {ivp["current_iv_pct"]:.1f}% @ {ivp["percentile"]:.0f} pctile\n'
            f'[{ivp["label"]}] {ivp["view"]}\n'
            f'歷史範圍 {ivp["min_pct"]:.1f}% ~ {ivp["max_pct"]:.1f}%（中位 {ivp["median_pct"]:.1f}%）')


def cmd_regime(data):
    ra = data.get('regime_advisor') or {}
    if not ra:
        return '❌ regime advisor 資料不足'
    r = ra.get('recommendation', {})
    cur = ra.get('current', {}) or {}
    lines = [f'🎯 {ra.get("regime_label", "?")}',
             f'月 {ra.get("monthly_pct", 0):+.2f}%  週 {ra.get("weekly_pct", 0):+.2f}%',
             '',
             f'💡 推薦：DTE {r.get("dte")} / Δ {r.get("delta")} / {r.get("strategy")}',
             f'   {r.get("why", "")[:80]}']
    if cur.get('has_positions'):
        lines.append('')
        lines.append(f'📍 當前：DTE {cur.get("avg_dte")} / Δ {cur.get("avg_put_delta", 0):.2f} / {cur.get("strategy")}')
        if ra.get('deviations'):
            lines.append(f'⚠ {len(ra["deviations"])} 條偏差')
    return '\n'.join(lines)


def cmd_report(data):
    """觸發 morning_report build_report"""
    try:
        sys.path.insert(0, str(_HERE))
        import morning_report as MR
        return MR.build_report(data)
    except Exception as e:
        return f'❌ 產生 report 失敗: {e}'


# ── 交易紀錄相關 ─────────────────────────────────────────
import re as _re
_INSTRUMENT_SHORT = _re.compile(r'^(\d{4,5})([PC])$', _re.I)


def _expand_instrument(short: str, data: dict) -> str:
    """39000P → 'TXO 202605 39000P'。若已是完整字串直接回傳。"""
    m = _INSTRUMENT_SHORT.match(short.strip())
    if not m:
        return short
    strike, right = m.group(1), m.group(2).upper()
    month = data.get('txo_month') or datetime.now().strftime('%Y%m')
    return f'TXO {month} {strike}{right}'


def _capture_context_from(data: dict) -> dict:
    m = data.get('market') or {}
    evs = data.get('upcoming_events') or []
    nearest = next((e for e in evs if (e.get('days_until') or 99) <= 5), None)
    return {
        'tx':       m.get('tx_futures'),
        'taiex':    m.get('taiex'),
        'iv_atm':   data.get('iv_used'),
        'dte':      data.get('dte_trading'),
        'session':  data.get('market_session'),
        'next_event': f"{nearest['days_until']}d {nearest['name']}" if nearest else None,
    }


def _gen_trade_id(ledger_data: dict) -> str:
    prefix = datetime.now().strftime('T%Y%m%d-%H%M')
    existing_max = 0
    for t in ledger_data.get('trades', []):
        tid = t.get('id', '')
        if tid.startswith(prefix):
            try:
                existing_max = max(existing_max, int(tid.rsplit('-', 1)[-1]))
            except (ValueError, IndexError):
                pass
    return f'{prefix}-{existing_max + 1:03d}'


def _open_trade(side: str, instrument: str, qty: int, price: float,
                book: str, thesis: str, fee: float = 25):
    sys.path.insert(0, str(_HERE))
    import ledger as L
    data_market = _load_data()
    instrument = _expand_instrument(instrument, data_market)
    ledger_data = L.load() or {'trades': []}
    trade = {
        'id':           _gen_trade_id(ledger_data),
        'datetime':     datetime.now().isoformat(timespec='seconds'),
        'side':         f'{side}_to_open',
        'instrument':   instrument,
        'qty':          qty,
        'price':        price,
        'fee':          fee,
        'book':         book,
        'status':       'open',
        'linked_id':    None,
        'realized_pnl': None,
        'note':         '',
        'thesis':       thesis or '',
        'context':      _capture_context_from(data_market),
    }
    ledger_data['trades'].append(trade)
    L.save(ledger_data)
    return trade


def _close_trade(trade_id: str, price: float, fee: float = 25, outcome: str = ''):
    sys.path.insert(0, str(_HERE))
    import ledger as L
    ledger_data = L.load() or {'trades': []}
    open_t = next((t for t in ledger_data['trades'] if t['id'] == trade_id), None)
    if not open_t:
        return None, f'找不到 ID: {trade_id}'
    if open_t.get('status') != 'open':
        return None, f'{trade_id} 已 closed'

    pnl = L.compute_realized_pnl(open_t, price, fee)
    close_side = 'buy_to_close' if open_t['side'].startswith('sell') else 'sell_to_close'
    close_t = {
        'id':           _gen_trade_id(ledger_data),
        'datetime':     datetime.now().isoformat(timespec='seconds'),
        'side':         close_side,
        'instrument':   open_t['instrument'],
        'qty':          open_t['qty'],
        'price':        price,
        'fee':          fee,
        'book':         open_t['book'],
        'status':       'closed',
        'linked_id':    open_t['id'],
        'realized_pnl': pnl['net_pnl'],
        'note':         '',
    }
    ledger_data['trades'].append(close_t)
    open_t['status']       = 'closed'
    open_t['linked_id']    = close_t['id']
    open_t['realized_pnl'] = pnl['net_pnl']
    if outcome:
        open_t['outcome'] = outcome
    L.save(ledger_data)
    return open_t, pnl


def cmd_buy(data, args_text):
    parts = args_text.split(maxsplit=3) if args_text else []
    if len(parts) < 3:
        return '用法：/buy <履約> <口數> <價格> [thesis]\n例：/buy 39000P 3 50 hedge FOMC'
    try:
        instrument, qty, price = parts[0], int(parts[1]), float(parts[2])
        thesis = parts[3] if len(parts) > 3 else ''
    except ValueError:
        return '❌ 參數格式錯誤'
    book = 'hedge' if 'P' in instrument.upper() else 'trading'
    try:
        trade = _open_trade('buy', instrument, qty, price, book, thesis)
        msg = (f'✓ 開倉：{trade["id"]}\n'
               f'  買 {qty} 口 {trade["instrument"]} @ {price}\n'
               f'  book={book}')
        if thesis: msg += f'\n  論點：{thesis}'
        return msg
    except Exception as e:
        return f'❌ 開倉失敗：{e}'


def cmd_sell(data, args_text):
    parts = args_text.split(maxsplit=3) if args_text else []
    if len(parts) < 3:
        return '用法：/sell <履約> <口數> <價格> [thesis]\n例：/sell 44900C 1 95 covered call'
    try:
        instrument, qty, price = parts[0], int(parts[1]), float(parts[2])
        thesis = parts[3] if len(parts) > 3 else ''
    except ValueError:
        return '❌ 參數格式錯誤'
    try:
        trade = _open_trade('sell', instrument, qty, price, 'trading', thesis)
        msg = (f'✓ 開倉：{trade["id"]}\n'
               f'  賣 {qty} 口 {trade["instrument"]} @ {price}')
        if thesis: msg += f'\n  論點：{thesis}'
        return msg
    except Exception as e:
        return f'❌ 開倉失敗：{e}'


def cmd_close(data, args_text):
    parts = args_text.split(maxsplit=2) if args_text else []
    if len(parts) < 2:
        return '用法：/close <id> <價格> [outcome]\n例：/close T20260508-1700-001 80 thesis 命中'
    try:
        trade_id, price = parts[0], float(parts[1])
        outcome = parts[2] if len(parts) > 2 else ''
    except ValueError:
        return '❌ 參數格式錯誤'
    try:
        result, pnl = _close_trade(trade_id, price, outcome=outcome)
        if result is None:
            return f'❌ {pnl}'
        sign = '+' if pnl['net_pnl'] >= 0 else ''
        msg = (f'✓ 平倉：{trade_id}\n'
               f'  {result["instrument"]}\n'
               f'  open {result["price"]} → close {price}\n'
               f'  P&L {sign}{pnl["net_pnl"]:,.0f} NT')
        if outcome: msg += f'\n  反思：{outcome}'
        return msg
    except Exception as e:
        return f'❌ 平倉失敗：{e}'


def cmd_positions(data, args_text=''):
    sys.path.insert(0, str(_HERE))
    import ledger as L
    ledger_data = L.load() or {'trades': []}
    opens = [t for t in ledger_data.get('trades', [])
             if t.get('status') == 'open' and t.get('side', '').endswith('_to_open')]
    if not opens:
        return '📭 沒有未平倉部位'
    lines = [f'📂 未平倉 ({len(opens)})']
    for t in opens[:10]:
        side_tag = '買' if t['side'].startswith('buy') else '賣'
        lines.append(f'  {t["id"][:18]} {side_tag} {t["qty"]} {t["instrument"][:24]} @ {t["price"]}')
    return '\n'.join(lines)


RISK_ALIASES = {
    'delta':  'risk_max_delta_ntd_per_1pct_tx',
    'theta':  'risk_max_theta_ntd_per_day',
    'vega':   'risk_max_vega_ntd_per_pct_iv',
    'puts':   'risk_max_put_lots',
    'put':    'risk_max_put_lots',
    'calls':  'risk_max_short_call_lots',
    'call':   'risk_max_short_call_lots',
    'dd':     'risk_max_drawdown_pct',
    'drawdown': 'risk_max_drawdown_pct',
}
RISK_LABELS = {
    'risk_max_delta_ntd_per_1pct_tx': 'Delta 曝險 (NT/1%TX)',
    'risk_max_theta_ntd_per_day':     'Theta 成本 (NT/天)',
    'risk_max_vega_ntd_per_pct_iv':   'Vega 曝險 (NT/1%IV)',
    'risk_max_put_lots':              'Long put 口數',
    'risk_max_short_call_lots':       'Short call 口數',
    'risk_max_drawdown_pct':          'Drawdown 警戒 (%)',
}


def _load_alerts_config():
    cfg_path = _HERE / 'alerts_config.json'
    if not cfg_path.exists():
        return {}, cfg_path
    try:
        return json.loads(cfg_path.read_text(encoding='utf-8')), cfg_path
    except Exception:
        return {}, cfg_path


def _save_alerts_config(cfg):
    (_HERE / 'alerts_config.json').write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')


def cmd_setrisk(data, args_text):
    """/setrisk <key> <value>  或  /setrisk 無參數列出當前。"""
    sys.path.insert(0, str(_HERE))
    import alerts as _A
    rules = _A.load_rules()

    if not args_text or args_text.strip() == '':
        # 列出
        lines = ['⚙️ 當前風險限額（編輯方式：/setrisk <key> <value>）']
        for alias, full in [('delta','risk_max_delta_ntd_per_1pct_tx'),
                             ('theta','risk_max_theta_ntd_per_day'),
                             ('vega','risk_max_vega_ntd_per_pct_iv'),
                             ('puts','risk_max_put_lots'),
                             ('calls','risk_max_short_call_lots'),
                             ('dd','risk_max_drawdown_pct')]:
            v = rules.get(full)
            lines.append(f'  /{alias} → {v}  ({RISK_LABELS[full]})')
        return '\n'.join(lines)

    parts = args_text.split(maxsplit=1)
    if len(parts) < 2:
        return '用法：/setrisk <key> <value>\n例：/setrisk delta 18000\nkey 可用 delta/theta/vega/puts/calls/dd'

    key_raw, val_raw = parts[0].lower(), parts[1].strip()
    full_key = RISK_ALIASES.get(key_raw)
    if not full_key:
        return f'❌ 未知 key：{key_raw}\n支援 delta/theta/vega/puts/calls/dd'

    # 解析值；類型需與 alerts.DEFAULT_RULES 對齊（load_rules 用 isinstance 過濾）
    try:
        val = float(val_raw)
        default_type = type(rules.get(full_key, 0))
        if default_type is int and val.is_integer():
            val = int(val)
    except ValueError:
        return f'❌ 數值格式錯誤：{val_raw}'

    # 寫回 config
    cfg, _ = _load_alerts_config()
    old = rules.get(full_key)
    cfg[full_key] = val
    try:
        _save_alerts_config(cfg)
    except Exception as e:
        return f'❌ 寫入 config 失敗：{e}'

    return (f'✓ 已更新 {RISK_LABELS[full_key]}\n'
            f'  {old} → {val}\n'
            f'下次 refresh 後生效')


def cmd_last(data, args_text=''):
    sys.path.insert(0, str(_HERE))
    import ledger as L
    ledger_data = L.load() or {'trades': []}
    trades = ledger_data.get('trades', [])
    if not trades:
        return '📭 沒有交易紀錄'
    recent = sorted(trades, key=lambda t: t.get('datetime', ''))[-5:]
    lines = ['📋 近 5 筆交易']
    for t in reversed(recent):
        d = (t.get('datetime') or '')[:10]
        side = t.get('side', '?')
        pnl = t.get('realized_pnl')
        pnl_str = f' P&L {_fmt_s(pnl)}' if pnl is not None else ''
        lines.append(f'  {d} {side} {t.get("qty")} {t.get("instrument", "")[:20]} @ {t.get("price")}{pnl_str}')
    return '\n'.join(lines)


HANDLERS = {
    '/help':    cmd_help,
    '/start':   cmd_help,
    '/status':  cmd_status,
    '/health':  cmd_health,
    '/greeks':  cmd_greeks,
    '/events':  cmd_events,
    '/roll':    cmd_roll,
    '/dd':      cmd_dd,
    '/risk':    cmd_risk,
    '/iv':      cmd_iv,
    '/regime':  cmd_regime,
    '/report':  cmd_report,
}
HANDLERS_WITH_ARGS = {
    '/buy':       cmd_buy,
    '/sell':      cmd_sell,
    '/close':     cmd_close,
    '/positions': cmd_positions,
    '/last':      cmd_last,
    '/setrisk':   cmd_setrisk,
}


def handle_message(token, chat_id, text):
    text = (text or '').strip()
    if not text.startswith('/'):
        return
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split('@')[0]
    args_text = parts[1] if len(parts) > 1 else ''

    data = _load_data()
    if cmd in HANDLERS_WITH_ARGS:
        fn = HANDLERS_WITH_ARGS[cmd]
        try:
            msg = fn(data, args_text)
        except Exception as e:
            msg = f'❌ 處理失敗: {e}'
    elif cmd in HANDLERS:
        fn = HANDLERS[cmd]
        try:
            msg = fn(data)
        except Exception as e:
            msg = f'❌ 處理失敗: {e}'
    else:
        msg = f'未知命令：{cmd}\n用 /help 看清單'
    try:
        _api_post(token, 'sendMessage', {'chat_id': chat_id, 'text': msg})
    except Exception as e:
        print(f'[telegram_bot] sendMessage 失敗: {e}', file=sys.stderr)


def main():
    _load_env()
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        print('[telegram_bot] TELEGRAM_BOT_TOKEN missing', file=sys.stderr)
        return 1

    offset = 0
    if STATE_FILE.exists():
        try:
            offset = int(json.loads(STATE_FILE.read_text()).get('offset', 0))
        except Exception:
            pass

    print(f'[telegram_bot] start, offset={offset}', file=sys.stderr)
    while True:
        try:
            r = _api_get(token, 'getUpdates', {'offset': offset, 'timeout': 30})
            updates = r.get('result', [])
            for upd in updates:
                offset = upd['update_id'] + 1
                # 1. 一般訊息
                msg = upd.get('message') or {}
                text = msg.get('text', '')
                chat_id = _safe(msg, 'chat', 'id')
                if chat_id and text:
                    print(f'[telegram_bot] {chat_id}: {text}', file=sys.stderr)
                    handle_message(token, chat_id, text)
                    continue
                # 2. inline keyboard 按鈕回呼
                cbq = upd.get('callback_query') or {}
                cb_data = cbq.get('data')
                cb_chat = _safe(cbq, 'message', 'chat', 'id')
                cb_id   = cbq.get('id')
                if cb_id and cb_data and cb_chat:
                    print(f'[telegram_bot] callback {cb_chat}: {cb_data}', file=sys.stderr)
                    # 必須回 answerCallbackQuery（即使空，讓 Telegram 停 spinner）
                    try:
                        _api_post(token, 'answerCallbackQuery', {'callback_query_id': cb_id})
                    except Exception:
                        pass
                    # 把 callback_data 當 /command 處理
                    handle_message(token, cb_chat, cb_data)
            if updates:
                STATE_FILE.write_text(json.dumps({'offset': offset, 'last_update': datetime.now().isoformat()}))
        except Exception as e:
            print(f'[telegram_bot] poll error: {e}', file=sys.stderr)
            time.sleep(10)


if __name__ == '__main__':
    sys.exit(main() or 0)
