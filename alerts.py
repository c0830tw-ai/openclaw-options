#!/usr/bin/env python3
"""
alerts.py — 從 latest_collar.json 評估觸發規則並送通知

執行方式：
    python3 alerts.py             # 一次性檢查
    python3 alerts.py --dry-run   # 印出觸發但不送通知

整合：
    server.py 在 refresh 後自動呼叫 main()
    cron 排程也可獨立跑

設定：
    alerts_config.json — 規則閾值（不存在則用預設）
    .env — TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID（可選；不設則僅 console 輸出）

狀態：
    alerts_state.json — 紀錄每條規則上次觸發時間，防止 cooldown 內重複通知
"""
import os, sys, json, time, math, pathlib

ROOT = pathlib.Path(__file__).parent
DATA_FILE   = ROOT / 'latest_collar.json'
CONFIG_FILE = ROOT / 'alerts_config.json'
STATE_FILE  = ROOT / 'alerts_state.json'

DEFAULT_RULES = {
    'tx_drop_pct':              -1.5,   # 當日跌幅閾值
    'tx_rise_pct':               2.0,
    'short_call_distance_sigma': 1.0,   # 短 call 距現價 σ 數
    'weekly_put_roll_dte':       1,     # weekly put 剩 DTE
    'iv_spike':                  0.35,  # ATM IV 上限
    'iv_crush':                  0.20,  # ATM IV 下限

    # 6 口 portfolio 量身規則
    'tx_anchor_price':           0,     # TX 基準價（0 = 不啟用此規則）
    'tx_drawdown_alert_pct':    -10.0,  # 從 anchor 跌 X% 觸發平倉提醒
    'profit_lock_threshold':     0,     # 0050 ETF期 unrealized 浮盈超過此 NT 觸發（0 = 不啟用）

    # Phase 6 broker drift detection
    'drift_check_enabled':       True,  # positions.json vs broker 真實持倉一致性檢查

    'cooldown_minutes':          60,    # 同一規則最少間隔分鐘
    'telegram_enabled':          True,
}


def load_rules() -> dict:
    rules = dict(DEFAULT_RULES)
    if CONFIG_FILE.exists():
        try:
            user = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            for k, v in user.items():
                if k in rules and isinstance(v, type(rules[k])):
                    rules[k] = v
        except Exception as e:
            print(f'[alerts] 配置檔解析失敗，用預設：{e}', file=sys.stderr)
    return rules


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding='utf-8')


def in_cooldown(state: dict, key: str, cooldown_min: int) -> bool:
    last = state.get(key)
    if last is None:
        return False
    return (time.time() - last) < cooldown_min * 60


def mark_fired(state: dict, key: str):
    state[key] = time.time()


def evaluate(data: dict, rules: dict) -> list:
    """回傳已觸發的 alert dict 列表：[{'key':..., 'level':..., 'msg':...}, ...]"""
    alerts = []
    if not data:
        return alerts

    market = data.get('market', {}) or {}
    tx     = market.get('tx_futures')
    chgpct = market.get('chgpct_tx')
    iv     = data.get('iv_used')

    # 1. TX 急跌
    if chgpct is not None and chgpct <= rules['tx_drop_pct']:
        alerts.append({
            'key':   'tx_drop',
            'level': '🔴',
            'msg':   f"TX 急跌 {chgpct:.2f}%",
            'tip':   '若 put 距 TX > 8% 已失效，加 closer put 或減 1-2 口停損',
        })

    # 2. TX 急漲
    if chgpct is not None and chgpct >= rules['tx_rise_pct']:
        alerts.append({
            'key':   'tx_rise',
            'level': '🟡',
            'msg':   f"TX 急漲 +{chgpct:.2f}%",
            'tip':   '可減 1 口 0050 期落袋鎖獲利，別追長',
        })

    # 3. 短 call 距現價過近
    call = (data.get('selected_options') or {}).get('call', {})
    call_strike = call.get('strike')
    dte_t = data.get('dte_trading')
    if tx and call_strike and iv and dte_t and dte_t > 0:
        T = dte_t / 252
        sd_pts = tx * iv * math.sqrt(T)
        distance_sigma = (call_strike - tx) / sd_pts if sd_pts > 0 else 99
        if distance_sigma < rules['short_call_distance_sigma']:
            alerts.append({
                'key':   'call_close',
                'level': '🟠',
                'msg':   f"近月 Call {call_strike:.0f} 距現價剩 {distance_sigma:.2f}σ",
                'tip':   'Roll up 早做比晚做便宜，距 < 1σ 已是行動點',
            })

    # 4. Weekly put 即將失效
    for w_key, w_label in (('weekly_wed', '週三週選'), ('weekly_fri', '週五週選')):
        wk = data.get(w_key)
        if wk and wk.get('dte_trading') is not None and wk['dte_trading'] <= rules['weekly_put_roll_dte']:
            alerts.append({
                'key':   f'roll_{w_key}',
                'level': '🟡',
                'msg':   f"{w_label} {wk.get('settlement_date','')} 剩 {wk['dte_trading']} 交易日",
                'tip':   '組合單一次 roll，避免分腿；記得 add_trade.py 紀錄',
            })

    # 5. IV 飆
    if iv is not None and iv >= rules['iv_spike']:
        alerts.append({
            'key':   'iv_spike',
            'level': '🔴',
            'msg':   f"ATM IV {iv*100:.1f}% (≥ {rules['iv_spike']*100:.0f}%)",
            'tip':   '保險貴，暫緩加買 put，等 IV 回落',
        })

    # 6. IV 崩
    if iv is not None and iv <= rules['iv_crush']:
        alerts.append({
            'key':   'iv_crush',
            'level': '🟢',
            'msg':   f"ATM IV {iv*100:.1f}% (≤ {rules['iv_crush']*100:.0f}%)",
            'tip':   '保險便宜，可加買 put 或賣 call 收 premium',
        })

    # 7. TX drawdown from anchor — 從基準價跌 X% 提醒平倉
    anchor = rules.get('tx_anchor_price', 0)
    if anchor and tx:
        drawdown_pct = (tx - anchor) / anchor * 100
        if drawdown_pct <= rules['tx_drawdown_alert_pct']:
            alerts.append({
                'key':   'tx_drawdown',
                'level': '🔴',
                'msg':   f"TX 從基準 {anchor:.0f} 跌 {drawdown_pct:.1f}% (現 {tx:.0f})",
                'tip':   '減 1-2 口 0050 期鎖損失，深 ITM put 可 roll down 落袋',
            })

    # 8. Unrealized profit lock — 0050 ETF期浮盈超過閾值
    threshold = rules.get('profit_lock_threshold', 0)
    pos = data.get('positions') or {}
    cost_basis = pos.get('cost_basis_0050', 0) or 0
    lots = pos.get('lots_0050', 0) or 0
    lot_size = pos.get('lot_size_0050', 10000) or 10000
    cur_0050 = (market.get('price_0050') or 0)
    if threshold and cost_basis > 0 and lots > 0 and cur_0050 > 0:
        unrealized = (cur_0050 - cost_basis) * lot_size * lots
        if unrealized >= threshold:
            alerts.append({
                'key':   'profit_lock',
                'level': '🟡',
                'msg':   f"0050 ETF期浮盈 {unrealized:,.0f}",
                'tip':   '落袋 1-2 口 + roll up put，sequence risk 最大時',
            })

    # 9. Positions drift — positions.json vs broker 真實持倉不一致
    if rules.get('drift_check_enabled', True):
        broker = data.get('broker') or {}
        broker_pos = broker.get('positions') or []
        if broker_pos:
            drifts = []

            def _broker_qty(family: str) -> int:
                """sum buy positions of given family（賣方倉跳過）。"""
                return sum(p.get('quantity', 0) for p in broker_pos
                           if p.get('family') == family and p.get('direction') == 'Buy')

            # 大台積電期 (CDF)
            cfg_large = pos.get('large_futures_lots', 0) or 0
            br_cdf = _broker_qty('CDF')
            if (cfg_large > 0 or br_cdf > 0) and cfg_large != br_cdf:
                drifts.append(f'大台積電期 cfg={cfg_large} vs broker={br_cdf}')

            # 0050 ETF期 (NYF) — 只在 broker 看得到才比（避免 H 帳戶 inaccessible 假警報）
            cfg_0050 = pos.get('lots_0050', 0) or 0
            br_nyf = _broker_qty('NYF')
            if br_nyf > 0 and cfg_0050 != br_nyf:
                drifts.append(f'0050 ETF期 cfg={cfg_0050} vs broker={br_nyf}')

            if drifts:
                alerts.append({
                    'key':   'positions_drift',
                    'level': '🟡',
                    'msg':   'positions drift: ' + '；'.join(drifts),
                    'tip':   '下單後忘了更新 positions.json，對一下即可',
                })

    return alerts


def send_telegram(msg: str) -> bool:
    """送 Telegram；token/chat_id 沒設或失敗時回 False。"""
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat  = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat:
        return False
    try:
        import urllib.request, urllib.parse
        # 不用 Markdown parse_mode：訊息含中文括號、數字逗號等，
        # 容易被 legacy Markdown 誤判為連結語法觸發 400 Bad Request
        data = urllib.parse.urlencode({
            'chat_id': chat, 'text': msg,
        }).encode()
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{token}/sendMessage',
            data=data, method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f'[alerts] Telegram 失敗：{e}', file=sys.stderr)
        return False


from typing import Optional


def _find_matching_roll(alert_key: str, rolls: list) -> Optional[dict]:
    """根據 alert key 找對應的 roll suggestion（call_close、roll_weekly_*）。"""
    if alert_key == 'call_close':
        for rs in rolls:
            if rs.get('trigger') == 'short_call_too_close':
                return rs
    elif alert_key == 'roll_weekly_fri':
        for rs in rolls:
            if '週五週選' in rs.get('trigger', ''):
                return rs
    elif alert_key == 'roll_weekly_wed':
        for rs in rolls:
            if '週三週選' in rs.get('trigger', ''):
                return rs
    return None


def _format_price_line(rs: dict) -> Optional[str]:
    """把 roll suggestion 壓成一行：價: 動作 + 限價 a→b→c (~Xk)"""
    if not rs:
        return None
    ladder = rs.get('limit_ladder') or {}
    est    = rs.get('estimates')    or {}
    t1, t2, t3 = ladder.get('try_1'), ladder.get('try_2'), ladder.get('try_3')
    if not (t1 and t2 and t3):
        return None

    if rs['action'] == 'roll_to_monthly' and est.get('replace_strike'):
        strike = int(est['replace_strike'])
        qty    = est.get('replace_qty', 0)
        cost   = est.get('total_cost', 0)
        cost_s = f' (~{cost/1000:.0f}k)' if cost else ''
        return f"📍 買 {qty}口月選 {strike}P  限 {t1}→{t2}→{t3}{cost_s}"

    if rs['action'] == 'roll_up_call' and est.get('current_strike'):
        strike = int(est['current_strike'])
        return f"📍 買回 {strike}C  限 {t1}→{t2}→{t3}（再賣更遠 OTM）"

    return None


def format_message(alerts: list, data: dict) -> str:
    """簡潔三行格式：色標·觸發 / 📍點位 / 💡看法"""
    market = data.get('market', {}) or {}
    tx     = market.get('tx_futures', '?')
    sess   = data.get('market_session', '?')
    iv     = data.get('iv_used')
    iv_str = f"{iv*100:.1f}%" if iv else '?'
    header = f"📊 TX {tx} · IV {iv_str} · {sess}"

    rolls = data.get('roll_suggestions') or []

    blocks = []
    for a in alerts:
        block = [f"{a['level']} {a['msg']}"]
        rs = _find_matching_roll(a['key'], rolls)
        price_line = _format_price_line(rs) if rs else None
        if price_line:
            block.append(price_line)
        if a.get('tip'):
            block.append(f"💡 {a['tip']}")
        blocks.append('\n'.join(block))

    return f"{header}\n\n" + '\n\n'.join(blocks)


def main(dry_run: bool = False):
    if not DATA_FILE.exists():
        print('[alerts] latest_collar.json 不存在，略過', file=sys.stderr)
        return
    data  = json.loads(DATA_FILE.read_text(encoding='utf-8'))
    rules = load_rules()
    state = load_state()

    fired = evaluate(data, rules)
    cooldown = rules['cooldown_minutes']
    new_fired = [a for a in fired if not in_cooldown(state, a['key'], cooldown)]

    for a in fired:
        prefix = '[FIRED]' if a in new_fired else '[COOLDOWN]'
        print(f"{prefix} {a['level']} {a['msg']}")
        if a.get('tip'):
            print(f"        📌 {a['tip']}")

    if new_fired and not dry_run:
        msg = format_message(new_fired, data)
        ok = rules.get('telegram_enabled', True) and send_telegram(msg)
        for a in new_fired:
            mark_fired(state, a['key'])
        save_state(state)
        if not ok:
            print('[alerts] Telegram 未配置或失敗，僅 console 輸出')

    if not fired:
        print('[alerts] 無觸發')


if __name__ == '__main__':
    dry = '--dry-run' in sys.argv
    main(dry_run=dry)
