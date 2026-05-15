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

    # Risk limits（abs 值；達 80% 警告、超過紅色 alert）
    'risk_max_delta_ntd_per_1pct_tx': 15000,
    'risk_max_theta_ntd_per_day':     4000,
    'risk_max_vega_ntd_per_pct_iv':   3000,
    'risk_max_put_lots':              8,
    'risk_max_short_call_lots':       4,
    'risk_max_drawdown_pct':         -10,

    # Delta target band（保持中性 / 微負方向）
    'risk_target_delta_ntd_per_1pct_tx':       0,      # 目標 0 = neutral
    'risk_target_delta_tolerance_ntd':         5000,   # 允許偏離 ±5000

    'cooldown_minutes':          60,    # 同一規則最少間隔分鐘
    'telegram_enabled':          True,
    'event_alerts_enabled':      False, # 事件提醒（CPI/FOMC 等），預設關閉避免每次 refresh 都發
    'risk_alerts_skip_metrics':  ['Theta 成本'],  # 略過這些風險指標的 Telegram 推播
    'trim_add_alerts_enabled':   True,  # 動態管理 SOP 訊號 — 必須開啟（用戶 5/15 要求不能遺漏）
    'trim_add_cooldown_minutes': 720,   # trim/add 訊號專用 cooldown 12h（避免每次 refresh 重推）
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

    # 12b. 風險限額使用率（read-only — risk_limits 模組已算好）
    rl = data.get('risk_limits') or {}
    skip_metrics = set(rules.get('risk_alerts_skip_metrics') or [])
    for m in (rl.get('metrics') or []):
        if m.get('label') in skip_metrics:
            continue   # 使用者要求略過此項指標的 Telegram 推播
        if m.get('status') == 'over':
            alerts.append({
                'key':   f"risk_over_{m['label']}",
                'level': '🔴',
                'msg':   f"{m['label']} 超限：{m['current']} {m.get('unit', '')} (限 {m['limit']}, 用 {m['usage_pct']}%)",
                'tip':   '達到風險上限；考慮減倉、roll 履約或調整結構',
            })
        elif m.get('status') == 'hot':
            alerts.append({
                'key':   f"risk_hot_{m['label']}",
                'level': '🟠',
                'msg':   f"{m['label']} 接近上限：{m['current']} {m.get('unit', '')} (用 {m['usage_pct']}%)",
                'tip':   '已用 80%+，新增部位需審慎',
            })

    # 13a. Drawdown 警戒（從 peak 追當前 DD）
    dd = data.get('drawdown') or {}
    if dd.get('current_dd_pct') is not None:
        cdd = dd['current_dd_pct']
        if cdd <= -10:
            alerts.append({
                'key':   'dd_high',
                'level': '🔴' if cdd <= -15 else '🟠',
                'msg':   f"Drawdown {cdd:+.1f}% (peak {dd.get('peak_date', '?')} → 已 {dd.get('days_in_dd', 0)} 天)",
                'tip':   dd.get('severity_msg') or '檢視 hedge 結構是否生效；嚴重時減碼或 roll 履約',
            })

    # 13. 高影響事件（events.json）— 事件前 5 天 IV spike 風險
    upcoming = data.get('upcoming_events') or [] if rules.get('event_alerts_enabled') else []
    for ev in upcoming:
        if ev.get('impact') != 'high':
            continue
        d = ev.get('days_until')
        if d is None or d > 5:
            continue
        # ≤ 2 天 = 高優先；3-5 天 = 中優先
        level = '🔴' if d <= 2 else '🟠'
        when = '今日' if d == 0 else '明日' if d == 1 else f'{d} 天後'
        iv_risk = ev.get('iv_risk', 'medium')
        risk_tip = (
            '事件前 IV 通常 spike，賣方建倉延後到事件後（避免被軋）；長 vega 部位有利' if iv_risk == 'high'
            else '事件後 IV 可能 crush，已賣方部位可受惠；長 vega 部位有風險'
        )
        alerts.append({
            'key':   f'event_{ev.get("type", "?")}_{ev.get("date")}',
            'level': level,
            'msg':   f"{when}（{ev.get('date')}）{ev.get('name')}",
            'tip':   f'{ev.get("note") or risk_tip}。賣方/長 vega 部位請評估是否調整',
        })

    # 14. 動態管理 SOP 訊號（trim & add-back）— 用戶 5/15 要求一定要推
    # 預設開啟、不在 dashboard dedup 範圍（行動類訊號）
    if rules.get('trim_add_alerts_enabled', True):
        # 14a. DD 里程碑警示（-5% / -10% / -15% from recent high）
        # 用戶 5/15 強調「創高後回檔 5%、10% 一定要通知」，獨立於 trim 動作
        DD_MILESTONES = [5, 10, 15]
        for key, label in [('trim_add_0050', '0050'),
                           ('trim_add_2330', '2330'),
                           ('trim_add_00679b', '00679B')]:
            s = data.get(key)
            if not s:
                continue
            dd  = s.get('dd_from_high_pct')
            rh  = s.get('recent_high')
            if dd is None or rh is None:
                continue
            ticker = s.get('ticker', label)
            # 找出當前 DD 已突破的最深里程碑（如 DD -6%，回報 -5%；DD -11% 回報 -10%）
            crossed = [m for m in DD_MILESTONES if dd <= -m]
            if crossed:
                deepest = max(crossed)
                level = '🔴' if deepest >= 10 else '🟠'
                alerts.append({
                    'key':   f'dd_warn_{ticker}_{deepest}',
                    'level': level,
                    'msg':   f"{ticker} 從 60d 高 {rh:.2f} 回檔 {dd:.2f}%（突破 -{deepest}% 里程碑）",
                    'tip':   f'規則：{s.get("rule", "")}。詳見 dashboard 動態管理 SOP 卡片',
                })

        # 14b. Trim & add-back 動作訊號
        for key, label in [('trim_add_0050', '0050'),
                           ('trim_add_2330', '2330'),
                           ('trim_add_00679b', '00679B')]:
            s = data.get(key)
            if not s:
                continue
            ticker = s.get('ticker', label)
            rule   = s.get('rule', '')
            price  = s.get('price')
            mode   = s.get('trim_mode', 'single')
            lots   = s.get('lots_held', 0)
            # Trim 觸發（依 mode 區分訊號）
            tier2 = s.get('trim_triggered_2') is True
            tier1 = s.get('trim_triggered') is True
            if mode == 'tiered':
                if tier2:
                    alerts.append({
                        'key':   f'trim_{ticker}_tier2',
                        'level': '🔴',
                        'msg':   f"{ticker} Trim 第 2 階觸發：現價 {price}，**全出 {lots} 口**",
                        'tip':   f'規則：{rule}。從高點跌 -10%+，T+1 09:00-09:30 限價賣出剩餘所有口數',
                    })
                elif tier1:
                    half = round(lots * 0.5)
                    alerts.append({
                        'key':   f'trim_{ticker}_tier1',
                        'level': '🟠',
                        'msg':   f"{ticker} Trim 第 1 階觸發：現價 {price}，砍 {half} 口（留 {lots - half} 口）",
                        'tip':   f'規則：{rule}。從高點跌 -5%+，T+1 09:00-09:30 限價賣 50%',
                    })
            elif mode == 'signal_full':
                if tier1:
                    sig = s.get('trim_signal', '訊號')
                    alerts.append({
                        'key':   f'trim_{ticker}_signal',
                        'level': '🔴',
                        'msg':   f"{ticker} Trim 訊號：{sig}，現價 {price}，**全出 {lots} 口**",
                        'tip':   f'規則：{rule}。T+1 09:00-09:30 限價全出',
                    })
            else:   # single_full / single
                if tier1:
                    size = s.get('trim_size_pct', 50)
                    act  = '全出' if size == 100 else f'砍 {round(lots*size/100)} 口'
                    alerts.append({
                        'key':   f'trim_{ticker}',
                        'level': '🔴',
                        'msg':   f"{ticker} Trim 觸發：現價 {price}，**{act}**",
                        'tip':   f'規則：{rule}。T+1 09:00-09:30 限價執行',
                    })
            # Add-back 訊號（cross_up = 收盤剛站上 add_level，今天才剛 cross）
            if s.get('cross_up'):
                lvl = s.get('add_level_label', 'MA')
                lvl_val = s.get('add_level')
                lvl_str = f'{lvl_val:.2f}' if lvl_val is not None else '—'
                lot_size = s.get('lot_size', 0)
                # 估算買回 notional（以滿倉 size 算）
                est_notional = (lots * lot_size * price / 10000) if (lots and lot_size and price) else 0
                futures_name = s.get('futures_name', '')
                alerts.append({
                    'key':   f'add_{ticker}',
                    'level': '🟢',
                    'msg':   f"{ticker} Add-back 訊號：收盤站上 {lvl} ({lvl_str})，**買回到 {lots} 口滿倉**",
                    'tip':   (f'規則：{rule}\n'
                              f'目標部位：{lots} 口 {futures_name}（依當前 trim 狀態買回對應口數）\n'
                              f'預估 notional：~{est_notional:.0f} 萬\n'
                              f'執行：T+1 09:00-09:30 限價買回；流動性好可一次掛、差就分 2-3 次掛'),
                })

    return alerts


def send_telegram_document(file_path: str, caption: str = '') -> bool:
    """送檔案附件給 Telegram。回傳 True/False。"""
    import os as _os
    from pathlib import Path as _P
    token = _os.environ.get('TELEGRAM_BOT_TOKEN')
    chat  = _os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat:
        return False
    fp = _P(file_path)
    if not fp.exists():
        print(f'[alerts] sendDocument: 檔案不存在 {file_path}', file=sys.stderr)
        return False
    try:
        import urllib.request
        import uuid
        boundary = uuid.uuid4().hex
        body = b''
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat}\r\n'.encode()
        if caption:
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'.encode()
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{fp.name}"\r\n'.encode()
        body += b'Content-Type: application/octet-stream\r\n\r\n'
        body += fp.read_bytes()
        body += f'\r\n--{boundary}--\r\n'.encode()

        req = urllib.request.Request(
            f'https://api.telegram.org/bot{token}/sendDocument',
            data=body, method='POST',
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except Exception as e:
        print(f'[alerts] sendDocument 失敗：{e}', file=sys.stderr)
        return False


def send_telegram(msg: str, buttons=None) -> bool:
    """送 Telegram；token/chat_id 沒設或失敗時回 False。
    buttons 可選：[[{'text':..., 'data':...}, ...], ...] 二維 inline keyboard。"""
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat  = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat:
        return False
    try:
        import json as _json
        import urllib.request, urllib.parse
        payload = {'chat_id': chat, 'text': msg}
        if buttons:
            keyboard = {
                'inline_keyboard': [
                    [{'text': b['text'], 'callback_data': b['data']} for b in row]
                    for row in buttons
                ],
            }
            payload['reply_markup'] = _json.dumps(keyboard, ensure_ascii=False)
        data = urllib.parse.urlencode(payload).encode()
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
    trim_cooldown = rules.get('trim_add_cooldown_minutes', cooldown)
    # trim/add 訊號用較長 cooldown（避免每次 refresh 重推），其他用通用 cooldown
    def _cd(key: str) -> int:
        if key.startswith('trim_') or key.startswith('add_') or key.startswith('dd_warn_'):
            return trim_cooldown
        return cooldown
    new_fired = [a for a in fired if not in_cooldown(state, a['key'], _cd(a['key']))]

    for a in fired:
        prefix = '[FIRED]' if a in new_fired else '[COOLDOWN]'
        print(f"{prefix} {a['level']} {a['msg']}")
        if a.get('tip'):
            print(f"        📌 {a['tip']}")

    if new_fired and not dry_run:
        msg = format_message(new_fired, data)
        # 依 alert key 動態決定按鈕：dd_high → /dd /risk；roll → /roll /positions；其他通用
        keys = {a['key'] for a in new_fired}
        rows = []
        if 'dd_high' in keys or any(k.startswith('risk_') for k in keys):
            rows.append([{'text': '📉 DD', 'data': '/dd'},
                         {'text': '⚠️ Risk', 'data': '/risk'}])
        if any(k.startswith('roll_') or k == 'call_close' for k in keys):
            rows.append([{'text': '🔧 Roll', 'data': '/roll'},
                         {'text': '📂 Positions', 'data': '/positions'}])
        if any(k.startswith('event_') for k in keys):
            rows.append([{'text': '📅 Events', 'data': '/events'},
                         {'text': '🎯 Regime', 'data': '/regime'}])
        if any(k.startswith('iv_') for k in keys):
            rows.append([{'text': '📊 IV', 'data': '/iv'},
                         {'text': '🧮 Greeks', 'data': '/greeks'}])
        # 沒任何 specific 規則 → 通用 row
        if not rows:
            rows = [[{'text': '🏥 健診', 'data': '/health'},
                     {'text': '🧮 Greeks', 'data': '/greeks'}]]
        ok = rules.get('telegram_enabled', True) and send_telegram(msg, buttons=rows)
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
