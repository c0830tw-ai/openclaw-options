"""
morning_report.py — 早晨向 Telegram 推送 portfolio 簡報

從 latest_collar.json 讀資料，組成中文簡報後送到 Telegram。

來源：
  - latest_collar.json（shioaji_collar.py 每次 refresh 寫入）
  - 環境變數 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID（同 alerts.py）

CLI：
  python3 morning_report.py            # 週末自動跳過
  python3 morning_report.py --force    # 強制送（測試用）
  python3 morning_report.py --print    # 只印不送
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

_HERE = Path(__file__).resolve().parent
LATEST_FILE = _HERE / 'latest_collar.json'


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


def _build_highlights(data: dict) -> List[str]:
    """挑出今日最該注意的 1-3 件事，按優先順序：
       事件迫近 > 換倉迫切 > IV 極端 > hedge 嚴重失衡 > 其他 alert"""
    out: List[str] = []

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

    # 3. IV 極端
    iv = data.get('iv_used') or 0
    iv_pct = iv * 100 if iv and iv < 1 else (iv or 0)
    if iv_pct >= 35 or (iv_pct > 0 and iv_pct < 18):
        label, view = _iv_label(iv)
        out.append(f'📉 近月 IV {iv_pct:.1f}%（{label}）— {view}')

    # 4. Hedge 對齊度（recommended_put_lots vs 實際 long puts）
    rec = ((data.get('portfolio') or {}).get('totals') or {}).get('recommended_put_lots') or 0
    pg_legs = ((data.get('portfolio_greeks') or {}).get('legs') or [])
    held = sum((L.get('qty_signed') or 0) for L in pg_legs
               if L.get('right') == 'put' and (L.get('qty_signed') or 0) > 0)
    if rec > 0 and held > 0:
        gap = held - rec
        if gap <= -2:
            out.append(f'🛡️ Hedge 不足：建議 {rec} 口 put，目前持有 {held} 口（缺 {-gap} 口）')
        elif gap >= 2:
            out.append(f'⚖️ Over-hedge：建議 {rec} 口 put，目前 {held} 口（多 {gap} 口，多付 theta）')

    # 5. 中等優先換倉（沒高優才提）
    if not any('🔴' in x for x in out):
        for r in (data.get('roll_suggestions') or []):
            if r.get('priority') == 'medium':
                reason = (r.get('reason') or '')[:60]
                out.append(f'🟡 {reason}')
                break

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
    iv_state, _ = _iv_label(iv)
    lines.append('')
    lines.append('📊 行情')
    if m.get('tx_futures'):
        lines.append(f'TX: {_fmt_n(m["tx_futures"])}  |  TAIEX: {_fmt_n(m.get("taiex"))}')
    lines.append(f'近月 IV: {iv_pct:.1f}% [{iv_state}]  |  DTE: {data.get("dte_trading", "—")}d')

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
            label = '月 hedge 成本' if theta_ntd < 0 else '月 theta 收入'
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


def main(force: bool = False, print_only: bool = False) -> int:
    _load_env()
    now = datetime.now()

    if not force and _is_weekend(now):
        print(f'[morning_report] {now.strftime("%a")} 週末跳過（用 --force 強制送）')
        return 0

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
    if _A.send_telegram(msg):
        print('\n[morning_report] Telegram 推送成功')
    else:
        print('\n[morning_report] Telegram 未設定或失敗（僅 console 輸出）')
    return 0


if __name__ == '__main__':
    force = '--force' in sys.argv
    print_only = '--print' in sys.argv
    sys.exit(main(force=force, print_only=print_only))
