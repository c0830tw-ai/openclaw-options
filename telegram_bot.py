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
    return ('📚 可用命令：\n'
            '/status — 行情快照\n'
            '/health — 健診評分\n'
            '/greeks — 曝險 Δ/θ/ν\n'
            '/events — 近 5 天事件\n'
            '/roll   — 換倉建議\n'
            '/dd     — drawdown\n'
            '/risk   — 風險限額\n'
            '/iv     — IV 百分位\n'
            '/regime — 情境推薦\n'
            '/report — 完整早報')


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


def handle_message(token, chat_id, text):
    text = (text or '').strip()
    if not text.startswith('/'):
        return
    cmd = text.split()[0].lower().split('@')[0]   # 去掉 @bot_username
    fn = HANDLERS.get(cmd)
    if not fn:
        msg = f'未知命令：{cmd}\n用 /help 看清單'
    else:
        try:
            data = _load_data()
            msg = fn(data)
        except Exception as e:
            msg = f'❌ 處理失敗: {e}'
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
                msg = upd.get('message') or {}
                text = msg.get('text', '')
                chat_id = _safe(msg, 'chat', 'id')
                if chat_id and text:
                    print(f'[telegram_bot] {chat_id}: {text}', file=sys.stderr)
                    handle_message(token, chat_id, text)
            if updates:
                STATE_FILE.write_text(json.dumps({'offset': offset, 'last_update': datetime.now().isoformat()}))
        except Exception as e:
            print(f'[telegram_bot] poll error: {e}', file=sys.stderr)
            time.sleep(10)


if __name__ == '__main__':
    sys.exit(main() or 0)
