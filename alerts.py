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
    'short_call_distance_sigma': 1.0,   # 賣出的 call 距現價幾個標準差以下觸發
    'weekly_put_roll_dte':       1,     # 週選 put 剩幾個交易日觸發
    'iv_spike':                  0.35,  # 市場波動率上限
    'iv_crush':                  0.20,  # 市場波動率下限

    # 6 口 portfolio 量身規則
    'tx_anchor_price':           0,     # TX 基準價（0 = 不啟用此規則）
    'tx_drawdown_alert_pct':    -10.0,  # 從 anchor 跌 X% 觸發平倉提醒
    'profit_lock_threshold':     0,     # 0050 ETF期 unrealized 浮盈超過此 NT 觸發（0 = 不啟用）

    # Phase 6 broker drift detection
    'drift_check_enabled':       True,  # positions.json vs broker 真實持倉一致性檢查

    # 多策略紅線（你做 short call、put spread、純短 put 等）
    'max_short_calls':            6,     # 總賣出 call 口數上限
    'short_put_distance_sigma':   1.0,   # 你賣的 put 距現價幾個標準差以下警告
    'trading_loss_mtd_cap':      -50000, # 短線交易（trading book）本月累計虧損上限

    # 近月期貨 5 分 K 布林軌道（盤中進場訊號）
    'intraday_bb_alert_enabled':  True,  # 啟用 5 分 K 開布林通知

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
            'msg':   f"TX 大跌 {chgpct:.2f}%",
            'tip':   '若你的 put 履約價已比現價低超過 8%（保護太遠等於沒用），加買履約價較高的新 put，或平倉 1-2 口期貨停損',
        })

    # 2. TX 急漲
    if chgpct is not None and chgpct >= rules['tx_rise_pct']:
        alerts.append({
            'key':   'tx_rise',
            'level': '🟡',
            'msg':   f"TX 大漲 +{chgpct:.2f}%",
            'tip':   '可平倉 1 口 0050 期把獲利落袋，不要在高點再加碼',
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
                'msg':   f"你賣出的 Call 履約 {call_strike:.0f}，距現價已剩 {distance_sigma:.2f} 個標準差",
                'tip':   '這個 call 越來越接近被軋，把它買回來換成更高履約價的新 call。早做比晚做便宜——等變成價內就賠定了',
            })

    # 4. Weekly put 即將失效
    for w_key, w_label in (('weekly_wed', '週三週選'), ('weekly_fri', '週五週選')):
        wk = data.get(w_key)
        if wk and wk.get('dte_trading') is not None and wk['dte_trading'] <= rules['weekly_put_roll_dte']:
            alerts.append({
                'key':   f'roll_{w_key}',
                'level': '🟡',
                'msg':   f"{w_label} {wk.get('settlement_date','')} 結算還剩 {wk['dte_trading']} 個交易日",
                'tip':   '保護快過期了。用「組合單」（賣舊+買新一次成交）換倉，避免一邊買到一邊沒買到。記得用 add_trade.py 紀錄這筆交易',
            })

    # 5. IV 飆
    if iv is not None and iv >= rules['iv_spike']:
        alerts.append({
            'key':   'iv_spike',
            'level': '🔴',
            'msg':   f"市場波動率衝到 {iv*100:.1f}%（平常 25-30% 算正常）",
            'tip':   '波動率高 = 保險變貴，暫停加買 put，等市場冷靜下來再動',
        })

    # 6. IV 崩
    if iv is not None and iv <= rules['iv_crush']:
        alerts.append({
            'key':   'iv_crush',
            'level': '🟢',
            'msg':   f"市場波動率掉到 {iv*100:.1f}%（保險打折中）",
            'tip':   '波動率低 = 保險便宜，可趁機加買 put，或賣 call 收一點權利金',
        })

    # 7. TX drawdown from anchor — 從基準價跌 X% 提醒平倉
    anchor = rules.get('tx_anchor_price', 0)
    if anchor and tx:
        drawdown_pct = (tx - anchor) / anchor * 100
        if drawdown_pct <= rules['tx_drawdown_alert_pct']:
            alerts.append({
                'key':   'tx_drawdown',
                'level': '🔴',
                'msg':   f"TX 從你進場時的 {anchor:.0f} 跌了 {drawdown_pct:.1f}%（現 {tx:.0f}）",
                'tip':   '已是大幅回檔。建議：平倉 1-2 口 0050 期鎖部分損失。已經大賺的 put（變成價內很深）可以平掉換成更低履約價的新 put，把利潤先入袋',
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
                'msg':   f"0050 ETF期未實現獲利已達 {unrealized:,.0f} NT",
                'tip':   '賺最多的時候反而最危險（一波 -10% 修正就吃掉一半）。建議：平倉 1-2 口落袋，並把保護 put 換到更高履約價鎖剩餘獲利',
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
                    'msg':   '系統紀錄與券商實際持倉不一致：' + '；'.join(drifts),
                    'tip':   '你下單後 positions.json 沒更新。對一下實際口數，編輯設定檔即可',
                })

    # 10. 紅線：總賣 call 過多（避免封頂太多 + 增加風險暴露）
    broker = data.get('broker') or {}
    broker_pos = broker.get('positions') or []
    short_call_qty = sum(
        p.get('quantity', 0) for p in broker_pos
        if p.get('direction') == 'Sell'
        and p.get('category') == 'index_option'
        and _option_type_from_code(p.get('code', '')) == 'call'
    )
    max_calls = rules.get('max_short_calls', 6)
    if short_call_qty > max_calls:
        alerts.append({
            'key':   'too_many_short_calls',
            'level': '🟠',
            'msg':   f"你賣出的 Call 已達 {short_call_qty} 口（上限 {max_calls} 口）",
            'tip':   '上漲被封頂的風險升高。下次新賣 call 前先平掉 1-2 口舊的，或暫停一週',
        })

    # 11. 紅線：你賣的 put 距現價過近（可能被指派）
    short_put_min_sigma = rules.get('short_put_distance_sigma', 1.0)
    if tx and dte_t and dte_t > 0:
        T = dte_t / 252
        sd = tx * (iv or 0.20) * (T ** 0.5)
        if sd > 0:
            for p in broker_pos:
                if (p.get('direction') == 'Sell'
                    and p.get('category') == 'index_option'
                    and _option_type_from_code(p.get('code', '')) == 'put'):
                    strike = _option_strike_from_code(p.get('code', ''))
                    if not strike:
                        continue
                    distance = (tx - strike) / sd
                    if distance < short_put_min_sigma:
                        alerts.append({
                            'key':   f'short_put_close_{p["code"]}',
                            'level': '🔴',
                            'msg':   f"你賣出的 Put {strike:.0f}（{p['code']}）距現價剩 {distance:.2f} 個標準差",
                            'tip':   '快變價內 = 快被指派買股。立刻平倉、或往下調履約價（roll down）',
                        })
                        break  # 同類別只報 1 條，避免 spam

    # 12. 紅線：trading book 本月累計虧損超過上限
    ledger = data.get('ledger') or {}
    by_book_mtd = ledger.get('by_book_mtd') or {}
    trading_mtd = by_book_mtd.get('trading', 0)
    loss_cap = rules.get('trading_loss_mtd_cap', -50000)
    if trading_mtd <= loss_cap:
        alerts.append({
            'key':   'trading_loss_cap',
            'level': '🔴',
            'msg':   f"短線交易本月累計虧損 {trading_mtd:,.0f} NT（上限 {loss_cap:,.0f}）",
            'tip':   '這個月已經輸太多。停止新建週選 / spread 部位，等下個月重來',
        })

    # 13. 近月期貨（台指期/股期）5 分 K 布林開口（盤中進場訊號）
    if rules.get('intraday_bb_alert_enabled', True):
        intraday = data.get('intraday_bb') or {}
        for fam_key, info in intraday.items():
            if info.get('bb_state') == 'expanding':
                label = info.get('label', fam_key.upper())
                width = info.get('bb_width', 0)
                ratio = info.get('bb_width_ratio', 0)
                alerts.append({
                    'key':   f'intraday_bb_{fam_key}',
                    'level': '🟠',
                    'msg':   f"{label} 5 分 K 開布林（軌寬 {width:.2f}%，是 20 根均的 {ratio:.2f} 倍）",
                    'tip':   '盤中波動率釋放，可能進入趨勢段。順勢方向加碼或等回測，逆勢方向避免接刀',
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


import re
from typing import Optional


_OPTION_PREFIXES = ('TXO', 'TX1', 'TX2', 'TX4', 'TX5',
                    'TXU', 'TXV', 'TXX', 'TXY', 'TXZ')


def _option_strip_prefix(code: str) -> Optional[str]:
    """剝掉已知 option 前綴後回剩餘字串；非 option code 回 None。"""
    if not code:
        return None
    for p in _OPTION_PREFIXES:
        if code.startswith(p):
            return code[len(p):]
    return None


def _option_type_from_code(code: str) -> Optional[str]:
    """從 TXO / TX2 / TXV 等 option code 抽 call 或 put。
    Letter 編碼：A-L = Call (Jan-Dec)；M-X = Put (Jan-Dec)。
    非 option 代碼（個股期、現股）回 None。"""
    rest = _option_strip_prefix(code)
    if rest is None or len(rest) < 2 or not rest[-1].isdigit():
        return None
    letter = rest[-2]
    if letter in 'ABCDEFGHIJKL':
        return 'call'
    if letter in 'MNOPQRSTUVWX':
        return 'put'
    return None


def _option_strike_from_code(code: str) -> Optional[float]:
    """從 option code 抽履約價（4-5 位數字 + month_letter + year_digit）。"""
    rest = _option_strip_prefix(code)
    if not rest:
        return None
    m = re.match(r'^(\d{4,5})[A-X]\d$', rest)
    return float(m.group(1)) if m else None


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
        cost_s = f'，約 NT$ {cost:,.0f}' if cost else ''
        return f"📍 買 {qty} 口月選 {strike} Put  限價試 {t1}→{t2}→{t3} 點{cost_s}"

    if rs['action'] == 'roll_up_call' and est.get('current_strike'):
        strike = int(est['current_strike'])
        return f"📍 先買回履約 {strike} 的 Call  限價試 {t1}→{t2}→{t3} 點，再賣更遠的新 Call"

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
