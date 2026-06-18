"""
morning_report.py — 早晨向 Telegram 推送 portfolio 簡報

從 latest_collar.json 讀資料，組成中文簡報後送到 Telegram。
發報前會先呼叫 shioaji_collar.py 抓一次新資料（夜盤後通常已 11+ 小時無更新）。

來源：
  - latest_collar.json（shioaji_collar.py 每次 refresh 寫入）
  - 環境變數 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID（同 alerts.py）

CLI：
  python3 morning_report.py               # 週末自動跳過；發報前先 refresh
  python3 morning_report.py --force       # 強制送（測試用）
  python3 morning_report.py --print       # 只印不送
  python3 morning_report.py --no-refresh  # 跳過 refresh，直接讀現有 latest_collar.json
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from strategy_terms import gloss, annotate

_HERE = Path(__file__).resolve().parent
LATEST_FILE = _HERE / 'latest_collar.json'
COLLAR_SCRIPT = _HERE / 'shioaji_collar.py'
REFRESH_TIMEOUT_SEC = 120


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


def _is_weekend(d: datetime) -> bool:
    return d.weekday() >= 5    # Sat=5, Sun=6


def _refresh_data() -> None:
    """同步呼叫 shioaji_collar.py 抓最新資料；失敗不致命，會 fallback 到舊 snapshot。"""
    if not COLLAR_SCRIPT.exists():
        print(f'[morning_report] 找不到 {COLLAR_SCRIPT.name}，跳過 refresh', file=sys.stderr)
        return
    print(f'[morning_report] refresh 中（timeout {REFRESH_TIMEOUT_SEC}s）...', file=sys.stderr)
    try:
        r = subprocess.run(
            ['/usr/bin/python3', str(COLLAR_SCRIPT)],
            cwd=str(_HERE),
            timeout=REFRESH_TIMEOUT_SEC,
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            print('[morning_report] refresh 完成', file=sys.stderr)
        else:
            print(f'[morning_report] refresh 失敗 (rc={r.returncode})；改用既有 snapshot', file=sys.stderr)
            tail = (r.stderr or r.stdout or '').strip().splitlines()[-5:]
            for ln in tail:
                print(f'  | {ln}', file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f'[morning_report] refresh 超過 {REFRESH_TIMEOUT_SEC}s 逾時；改用既有 snapshot', file=sys.stderr)
    except Exception as e:
        print(f'[morning_report] refresh 例外：{e}；改用既有 snapshot', file=sys.stderr)


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


def _iv_label(iv_frac: float) -> Tuple[str, str]:
    """回傳 (狀態標籤, 操作觀點)。iv 是小數（0.31 = 31%）。"""
    pct = iv_frac * 100 if iv_frac and iv_frac < 1 else (iv_frac or 0)
    if pct >= 35:
        return '極高', '事件前 IV spike，賣方注意被軋；長 vega 部位有利'
    if pct >= 28:
        return '偏高', '賣方有利、買方避建倉'
    if pct >= 22:
        return '中性', '無明顯方向偏好'
    if pct >= 18:
        return '偏低', '買方有利、賣方需確認 catalyst'
    return '極低', 'IV crush 後低點，賣方收益薄但風險小'


def _iv_sizing_hint(iv_pct: float) -> str:
    """根據用戶 SOP：IV<20% 加倉、>35% 減半、中間照建議"""
    if iv_pct >= 35:
        return '建議分批且減半（IV 極高，避免被軋）'
    if iv_pct >= 28:
        return '建議分批入場（IV 偏高，權利金貴）'
    if iv_pct < 20:
        return '可一次到位（IV 偏低，權利金便宜）'
    return '照建議口數入場'


def _build_strategy_analysis(data: dict) -> List[str]:
    """3-5 行專業敘述：市場狀態 → 持倉對齊度 → 主要風險。"""
    out: List[str] = []

    ra = data.get('regime_advisor') or {}
    dd = data.get('drawdown') or {}
    ivp = data.get('iv_percentile') or {}
    iv = data.get('iv_used') or 0
    iv_pct = iv * 100 if iv and iv < 1 else (iv or 0)
    pgt = (data.get('portfolio_greeks') or {}).get('totals') or {}

    # 1) 市場狀態合成
    regime_label = ra.get('regime_label') or '—'
    monthly_pct = ra.get('monthly_pct')
    dd_pct = dd.get('current_dd_pct')
    days_in_dd = dd.get('days_in_dd')
    severity = dd.get('severity')

    state_bits = [regime_label]
    if monthly_pct is not None:
        state_bits.append(f'月 {monthly_pct:+.1f}%')
    if dd_pct is not None and dd_pct < -3:
        sev_emoji = {'critical': '🔴', 'high': '🟠', 'medium': '🟡'}.get(severity, '·')
        state_bits.append(f'DD {dd_pct:.1f}% {sev_emoji}（{days_in_dd}d）')
    if ivp.get('enough_data'):
        state_bits.append(f'IV {iv_pct:.1f}% @ {ivp["percentile"]:.0f}pct')

    out.append(' · '.join(state_bits))

    # 2) 市場詮釋
    interp = []
    if dd_pct is not None and dd_pct <= -10:
        interp.append('熊段中段以上，下檔保護是核心')
    elif dd_pct is not None and dd_pct <= -5:
        interp.append('短期修正中，留意是否擴大')
    if ivp.get('enough_data') and ivp['percentile'] >= 75:
        interp.append('賣方溢價豐厚（IV 偏貴）')
    elif ivp.get('enough_data') and ivp['percentile'] <= 25:
        interp.append('買方便宜（IV 偏便宜，適合建保護）')
    if interp:
        out.append('→ ' + '；'.join(interp))

    # 3) 持倉與 regime 對齊度
    delta_ntd = pgt.get('delta_ntd_per_1pct_tx') or 0
    vega_ntd = pgt.get('vega_ntd_per_pct_iv') or 0
    align_bits = []
    if delta_ntd < -500:
        align_bits.append(f'Δ {_fmt_s(delta_ntd)} 偏空（避險偏向保護）')
    elif delta_ntd > 500:
        align_bits.append(f'Δ {_fmt_s(delta_ntd)} 偏多（避險偏弱）')
    if vega_ntd > 200 and ivp.get('enough_data') and ivp['percentile'] >= 75:
        align_bits.append(f'vega 多頭 +{vega_ntd:,} → 若 IV 收斂吐回 ~{vega_ntd*5:,} NT (IV-5pp)')
    elif vega_ntd < -200 and ivp.get('enough_data') and ivp['percentile'] <= 25:
        align_bits.append(f'vega 空頭 {vega_ntd:,} → 若 IV 反彈賠 ~{abs(vega_ntd)*5:,} NT (IV+5pp)')
    if align_bits:
        out.append('持倉：' + '；'.join(align_bits))

    # 4) 主要風險（單句）
    risk_msg = None
    if severity == 'critical' and (days_in_dd or 0) >= 7:
        risk_msg = f'⚠️ 主要風險：回檔已 {days_in_dd} 天未恢復，檢視是否需要減核心部位而非僅加避險'
    elif ivp.get('enough_data') and ivp['percentile'] >= 85 and vega_ntd > 200:
        risk_msg = '⚠️ 主要風險：IV 極高且 vega 多頭，事件後 IV crush 會吐回獲利'
    elif ra.get('current', {}).get('avg_dte', 99) <= 7:
        risk_msg = f'⚠️ 主要風險：平均 DTE {ra["current"]["avg_dte"]}d，接近結算：價格敏感度高（gamma）、時間價值衰減快（theta）'
    if risk_msg:
        out.append(risk_msg)

    return out


def _build_action_plan(data: dict) -> List[str]:
    """產生今日執行建議：高優/本週/監控/暫緩 4 類，最多 6 條。"""
    out: List[str] = []
    high: List[str] = []
    week: List[str] = []
    watch: List[str] = []
    avoid: List[str] = []

    iv = data.get('iv_used') or 0
    iv_pct = iv * 100 if iv and iv < 1 else (iv or 0)
    ivp = data.get('iv_percentile') or {}

    # 1) Hedge gap → 補/減 put（用 IV 調整節奏）
    rec_puts = ((data.get('portfolio') or {}).get('totals') or {}).get('recommended_put_lots') or 0
    pg_legs = ((data.get('portfolio_greeks') or {}).get('legs') or [])
    held_puts = sum((L.get('qty_signed') or 0) for L in pg_legs
                    if L.get('right') == 'put' and (L.get('qty_signed') or 0) > 0)
    if rec_puts > 0 and held_puts >= 0:
        gap = held_puts - rec_puts
        if gap <= -2:
            sizing = _iv_sizing_hint(iv_pct)
            high.append(f'補 {-gap} 口 put（現 {held_puts} → 目標 {rec_puts}）— {sizing}')
        elif gap >= 2:
            high.append(f'減 {gap} 口 put（現 {held_puts} → 目標 {rec_puts}，避免多付 theta）')

    # 2) Roll suggestions（高/中優先）— 若 reason 已含「換倉/換月」不再加 prefix
    for r in (data.get('roll_suggestions') or []):
        reason = (r.get('reason') or '')[:55]
        prefix = '' if any(k in reason for k in ('換倉', '換月', '滾倉')) else '換倉：'
        if r.get('priority') == 'high':
            high.append(f'{prefix}{reason}')
        elif r.get('priority') == 'medium':
            week.append(f'{prefix}{reason}')

    # 3) Regime 策略偏離 → 結構調整建議（本週）
    ra = data.get('regime_advisor') or {}
    cur = ra.get('current') or {}
    rec = ra.get('recommendation') or {}
    if cur.get('has_positions') and rec.get('strategy') and cur.get('strategy') and \
       cur['strategy'] != rec['strategy'] and len(ra.get('deviations') or []) >= 2:
        week.append(f'結構：{gloss(cur["strategy"])} → 推薦 {gloss(rec["strategy"])}（{ra.get("regime_label", "")}）')

    # 4) Add-back 監控（距離 ≤ 3% 才提）
    for key, label in [('trim_add_0050', '0050'),
                        ('trim_add_2330', '2330'),
                        ('trim_add_00679b', '00679B')]:
        ta = data.get(key) or {}
        price = ta.get('price')
        add_level = ta.get('add_level')
        add_label = ta.get('add_level_label', '')
        if not (price and add_level):
            continue
        if ta.get('cross_up'):
            high.append(f'{label} 觸發 {ta.get("add_signal", "")} → 啟動加碼')
            continue
        # MA20 cross-up 需從下往上，BB↓ touch 需從上往下
        if add_label == 'MA20':
            dist_pct = (add_level - price) / price * 100
            if 0 < dist_pct <= 3:
                watch.append(f'{label} 距 {add_label} 還 +{dist_pct:.2f}% 即 cross-up 加碼')
        else:    # BB↓
            dist_pct = (price - add_level) / price * 100
            if 0 < dist_pct <= 3:
                watch.append(f'{label} 距 {add_label} 還 -{dist_pct:.2f}% 即 touch 加碼')

    # 5) 暫緩條件
    pgt = (data.get('portfolio_greeks') or {}).get('totals') or {}
    vega_ntd = pgt.get('vega_ntd_per_pct_iv') or 0
    if ivp.get('enough_data') and ivp['percentile'] >= 80:
        avoid.append('追買 long put（IV 偏貴；除非新事件 catalyst，否則改 spread 降 vega）')
    if ivp.get('enough_data') and ivp['percentile'] <= 15 and vega_ntd < -200:
        avoid.append('裸賣 put / 賣方加碼（IV 太薄，權利金不夠覆蓋風險）')

    # 6) Drawdown critical 額外提示
    dd = data.get('drawdown') or {}
    if dd.get('severity') == 'critical' and (dd.get('days_in_dd') or 0) >= 10:
        high.append(f'檢視 core 部位是否需減碼（DD {dd.get("current_dd_pct"):.1f}% 已 {dd["days_in_dd"]} 天）')

    # 組裝（保持精簡）
    for x in high[:3]:
        out.append(f'✅ 高優：{x}')
    for x in week[:2]:
        out.append(f'🔄 本週：{x}')
    for x in watch[:3]:
        out.append(f'👀 監控：{x}')
    for x in avoid[:2]:
        out.append(f'🚫 暫緩：{x}')
    return out


def _build_addback_countdown(data: dict) -> List[str]:
    """加碼觸發倒數：各標的離 add_level 還多遠。"""
    out: List[str] = []
    for key, label in [('trim_add_0050', '0050'),
                        ('trim_add_2330', '2330'),
                        ('trim_add_00679b', '00679B')]:
        ta = data.get(key) or {}
        price = ta.get('price')
        add_level = ta.get('add_level')
        add_label = ta.get('add_level_label', '?')
        if not (price and add_level):
            continue
        if add_label == 'MA20':
            dist_pct = (add_level - price) / price * 100
            arrow = '需' + (f'+{dist_pct:.2f}% cross-up' if dist_pct > 0 else f'已上穿 +{-dist_pct:.2f}%')
        else:
            dist_pct = (price - add_level) / price * 100
            arrow = (f'距 -{dist_pct:.2f}%' if dist_pct > 0 else f'已觸發 +{-dist_pct:.2f}%')
        marker = '🟢' if dist_pct <= 0 else ('🟡' if abs(dist_pct) <= 3 else '·')
        out.append(f'{marker} {label}: {price:g} → {add_label} {add_level:.2f}（{arrow}）')
    return out


def _build_highlights(data: dict) -> List[str]:
    """挑出今日最該注意的 1-3 件事，按優先順序：
       事件迫近 > 換倉迫切 > 健診低分 > IV 極端 > hedge 失衡 > 其他 alert"""
    out: List[str] = []

    # 0. 健診評分過低
    hc = data.get('health_check') or {}
    score = hc.get('overall_score')
    if score is not None and score < 70:
        # 列前 2 條 violations
        viols = (hc.get('violations') or [])[:2]
        for v in viols:
            out.append(f'🏥 [健診 {score}/100] {v}')

    # 1. 高優事件 ≤ 2 天
    for ev in (data.get('upcoming_events') or []):
        if ev.get('impact') == 'high' and (ev.get('days_until') or 99) <= 2:
            d = ev['days_until']
            when = '今日' if d == 0 else '明日'
            risk = ev.get('iv_risk', 'medium')
            tip = ('IV 風險高，賣方延後建倉' if risk == 'high'
                   else 'IV 事後可能 crush，賣方受惠')
            out.append(f'⚠️ {when}（{ev["date"]}）{ev["name"]} — {tip}')

    # 2. 高優先換倉
    for r in (data.get('roll_suggestions') or []):
        if r.get('priority') == 'high':
            reason = (r.get('reason') or '')[:60]
            out.append(f'🔴 {reason}')

    # 3. IV 極端（優先用百分位判斷）
    iv = data.get('iv_used') or 0
    iv_pct = iv * 100 if iv and iv < 1 else (iv or 0)
    ivp = data.get('iv_percentile') or {}
    if ivp.get('enough_data'):
        if ivp['percentile'] >= 80:
            out.append(f'📈 IV {iv_pct:.1f}% @ {ivp["percentile"]:.0f} pctile — {ivp["view"]}')
        elif ivp['percentile'] <= 15:
            out.append(f'📉 IV {iv_pct:.1f}% @ {ivp["percentile"]:.0f} pctile — {ivp["view"]}')
    elif iv_pct >= 35 or (iv_pct > 0 and iv_pct < 18):
        label, view = _iv_label(iv)
        out.append(f'📉 近月 IV {iv_pct:.1f}%（{label}）— {view}')

    # 4. Hedge 對齊度（recommended_put_lots vs 實際 long puts）
    # 2026-05-26 用戶要求：put 不足不提醒；over-hedge 仍提（多付 theta）
    rec = ((data.get('portfolio') or {}).get('totals') or {}).get('recommended_put_lots') or 0
    pg_legs = ((data.get('portfolio_greeks') or {}).get('legs') or [])
    held = sum((L.get('qty_signed') or 0) for L in pg_legs
               if L.get('right') == 'put' and (L.get('qty_signed') or 0) > 0)
    if rec > 0 and held > 0:
        gap = held - rec
        if gap >= 2:
            out.append(f'⚖️ 避險過頭：建議 {rec} 口 put，目前 {held} 口（多 {gap} 口，多付時間價值成本 theta）')

    # 5. 中等優先換倉（沒高優才提）
    if not any('🔴' in x for x in out):
        for r in (data.get('roll_suggestions') or []):
            if r.get('priority') == 'medium':
                reason = (r.get('reason') or '')[:60]
                out.append(f'🟡 {reason}')
                break

    # 6. 策略 regime 偏離（≥2 項偏離才進重點）
    ra = data.get('regime_advisor') or {}
    cur = ra.get('current') or {}
    deviations = ra.get('deviations') or []
    rec = ra.get('recommendation') or {}
    if len(out) < 3 and cur.get('has_positions') and len(deviations) >= 2 and rec.get('strategy'):
        out.append(f'🎯 策略偏離：當前 {gloss(cur.get("strategy"))} → 推薦 {gloss(rec.get("strategy"))} ({ra.get("regime_label", "")})')

    return out[:3]   # 最多 3 條，避免訊息過長


def build_report(data: dict, now: datetime = None) -> str:
    if now is None:
        now = datetime.now()
    lines = [f'☀️ 早安！portfolio 簡報（{now.strftime("%-m/%-d %H:%M")}）']

    # 資料新鮮度
    ts_s = data.get('timestamp')
    if ts_s:
        try:
            ts = datetime.fromisoformat(ts_s)
            age_h = (now - ts).total_seconds() / 3600
            if age_h > 6:
                lines.append(f'⏰ 注意：資料已 {age_h:.1f} 小時未更新')
        except Exception:
            pass

    # ━━━ 今日重點 ━━━
    highlights = _build_highlights(data)
    if highlights:
        lines.append('')
        lines.append('📌 今日重點')
        for h in highlights:
            lines.append(f'• {h}')

    # ━━━ 行情 ━━━
    m = data.get('market') or {}
    iv = data.get('iv_used') or 0
    iv_pct = iv * 100 if iv and iv < 1 else (iv or 0)
    ivp = data.get('iv_percentile') or {}
    lines.append('')
    lines.append('📊 行情')
    if m.get('tx_futures'):
        lines.append(f'TX: {_fmt_n(m["tx_futures"])}  |  TAIEX: {_fmt_n(m.get("taiex"))}')
    if ivp.get('enough_data'):
        # 用百分位來描述比絕對 IV 更有意義
        lines.append(f'近月 IV: {iv_pct:.1f}% @ {ivp["percentile"]:.0f} pctile '
                     f'[{ivp["label"]}]')
        lines.append(f'  → {ivp["view"]}（過去 1 年中位 {ivp.get("median_pct", 0):.1f}%）')
    else:
        iv_state, _ = _iv_label(iv)
        lines.append(f'近月 IV: {iv_pct:.1f}% [{iv_state}]')
    lines.append(f'近月 DTE: {data.get("dte_trading", "—")}d')

    # ━━━ Greeks 解讀 ━━━
    pg = data.get('portfolio_greeks') or {}
    pgt = pg.get('totals') or {}
    if pgt:
        lines.append('')
        lines.append('🧮 Greeks（曝險解讀）')
        delta_ntd = pgt.get('delta_ntd_per_1pct_tx') or 0
        theta_ntd = pgt.get('theta_ntd_per_day') or 0
        vega_ntd  = pgt.get('vega_ntd_per_pct_iv') or 0
        ref_tx    = pgt.get('reference_tx') or 0

        # Delta：TX 跌 1% → 賺/賠多少
        if delta_ntd:
            pts = int(round(ref_tx * 0.01)) if ref_tx else 0
            verb = '賺' if delta_ntd < 0 else '賠'   # delta<0 即跌時賺
            lines.append(f'Δ {_fmt_s(delta_ntd)} NT / 1% TX')
            lines.append(f'   → TX 跌 1%（≈{pts}點）你{verb} {_fmt_n(abs(delta_ntd))}')

        # Theta：每日成本/收入 → 月估算
        if theta_ntd:
            monthly = theta_ntd * 30
            label = '月避險成本' if theta_ntd < 0 else '月時間價值收入'
            lines.append(f'θ {_fmt_s(theta_ntd)} NT/天')
            lines.append(f'   → {label}約 {_fmt_n(abs(monthly))} NT')

        # Vega：IV 漲 5pp 估算
        if vega_ntd:
            verb = '賺' if vega_ntd > 0 else '賠'
            lines.append(f'ν {_fmt_s(vega_ntd)} NT / 1% IV')
            lines.append(f'   → IV 漲 5pp 你{verb} {_fmt_n(abs(vega_ntd * 5))}')

        # 累積 theta（如有歷史）
        gh = data.get('greeks_history') or {}
        cum = gh.get('cumulative') or {}
        if cum and (cum.get('lifetime') or 0):
            lines.append(f'累積 θ：30d {_fmt_s(cum.get("last_30d"))}  |  lifetime {_fmt_s(cum.get("lifetime"))}')

    # ━━━ 今日策略解讀（合成敘述） ━━━
    analysis = _build_strategy_analysis(data)
    if analysis:
        lines.append('')
        lines.append('🧭 今日策略解讀')
        for ln in analysis:
            lines.append(ln)

    # ━━━ 今日執行建議（具體 action） ━━━
    actions = _build_action_plan(data)
    if actions:
        lines.append('')
        lines.append('📋 今日執行建議')
        for ln in actions:
            lines.append(ln)

    # ━━━ 加碼觸發倒數 ━━━
    addback = _build_addback_countdown(data)
    if addback:
        lines.append('')
        lines.append('⏳ 加碼觸發倒數')
        for ln in addback:
            lines.append(ln)

    # ━━━ 策略推薦（依當前 regime） ━━━
    ra = data.get('regime_advisor') or {}
    if ra and ra.get('recommendation'):
        r = ra['recommendation']
        cur = ra.get('current') or {}
        lines.append('')
        lines.append(f'🎯 {ra.get("regime_label", "?")}（月 {ra.get("monthly_pct", 0):+.1f}%）')
        lines.append(f'💡 主推 {gloss(r.get("strategy"))}')
        if r.get('stats'):
            s = r['stats']; total = r.get('quarters_total', 0)
            line = f'📊 歷史: 奪冠 {s["wins"]}/{total} ({s["win_rate_pct"]}%) · 前 3 ({s["top3_rate_pct"]}%)'
            if s.get('regime_total'):
                line += f' · 同情境 {s["regime_wins"]}/{s["regime_total"]}'
            lines.append(line)
        if r.get('fallback'):
            fb_line = f'🛡️ 後備 {gloss(r.get("fallback"))}'
            if r.get('fallback_stats'):
                fb_line += f'（前 3 率 {r["fallback_stats"]["top3_rate_pct"]}%）'
            lines.append(fb_line)
        if r.get('expected'):
            lines.append(f'   {annotate(r.get("expected")[:60])}')
        if cur.get('has_positions') and ra.get('deviations'):
            n = len(ra['deviations'])
            lines.append(f'⚠️ 當前 {gloss(cur.get("strategy"))} 與推薦不一致 ({n} 項偏離)')

    # ━━━ 近 5 天事件（重點以外） ━━━
    evs = data.get('upcoming_events') or []
    near = [e for e in evs if (e.get('days_until') or 99) <= 5]
    # 過濾掉已在 highlights 顯示的（≤2 天高優）
    near = [e for e in near if not (e.get('impact') == 'high' and (e.get('days_until') or 99) <= 2)]
    if near:
        lines.append('')
        lines.append('📅 3-5 天事件')
        for e in near[:5]:
            d = e.get('days_until')
            when = f'{d}d' if d > 1 else '明日' if d == 1 else '今日'
            lines.append(f'• {when} ({e.get("date")}) {e.get("name", "")}')

    # ━━━ Live alerts（重點以外） ━━━
    try:
        sys.path.insert(0, str(_HERE))
        import alerts as _A
        rules = _A.load_rules()
        rules['cooldown_minutes'] = 0
        rules['telegram_enabled'] = False
        active = _A.evaluate(data, rules)
        # 排除 event_ alert（已在 highlights/事件區）
        active = [a for a in active if not str(a.get('key', '')).startswith('event_')]
        if active:
            lines.append('')
            lines.append(f'⚠ 其他 alert ({len(active)})')
            for a in active[:5]:
                msg = (a.get('msg') or '')[:80]
                lines.append(f'{a.get("level", "·")} {msg}')
    except Exception as e:
        print(f'[morning_report] alerts compute failed: {e}', file=sys.stderr)

    return '\n'.join(lines)


def main(force: bool = False, print_only: bool = False, no_refresh: bool = False) -> int:
    _load_env()
    now = datetime.now()

    if not force and _is_weekend(now):
        print(f'[morning_report] {now.strftime("%a")} 週末跳過（用 --force 強制送）')
        return 0

    if not no_refresh:
        _refresh_data()
        now = datetime.now()    # refresh 可能花數十秒，重抓現在時間以正確計算 age

    if not LATEST_FILE.exists():
        msg = f'⚠ morning_report：{LATEST_FILE.name} 不存在，請先跑一次 server.py 或 shioaji_collar.py'
        print(msg, file=sys.stderr)
        return 1

    try:
        data = json.loads(LATEST_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'[morning_report] 讀檔失敗：{e}', file=sys.stderr)
        return 1

    msg = build_report(data, now=now)
    print(msg)

    if print_only:
        return 0

    sys.path.insert(0, str(_HERE))
    import alerts as _A
    buttons = [
        [{'text': '🏥 健診', 'data': '/health'},
         {'text': '🧮 Greeks', 'data': '/greeks'}],
        [{'text': '📊 IV', 'data': '/iv'},
         {'text': '🎯 Regime', 'data': '/regime'}],
        [{'text': '📂 Positions', 'data': '/positions'},
         {'text': '📅 Events', 'data': '/events'}],
    ]
    if _A.send_telegram(msg, buttons=buttons):
        print('\n[morning_report] Telegram 推送成功')
    else:
        print('\n[morning_report] Telegram 未設定或失敗（僅 console 輸出）')
    return 0


if __name__ == '__main__':
    force = '--force' in sys.argv
    print_only = '--print' in sys.argv
    no_refresh = '--no-refresh' in sys.argv
    sys.exit(main(force=force, print_only=print_only, no_refresh=no_refresh))
