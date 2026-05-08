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

    # 行情
    m = data.get('market') or {}
    iv = data.get('iv_used') or 0
    lines.append('')
    lines.append('📊 行情')
    if m.get('tx_futures'):
        lines.append(f'TX: {_fmt_n(m["tx_futures"])} | TAIEX: {_fmt_n(m.get("taiex"))}')
    iv_pct = iv * 100 if iv and iv < 1 else (iv or 0)
    lines.append(f'近月 ATM IV: {iv_pct:.1f}% | DTE: {data.get("dte_trading", "—")}d')

    # Greeks
    pg = data.get('portfolio_greeks') or {}
    pgt = pg.get('totals') or {}
    if pgt:
        lines.append('')
        lines.append('🧮 Greeks（整體曝險）')
        lines.append(f'Δ: {_fmt_s(pgt.get("delta_ntd_per_1pct_tx"))} NT / 1% TX')
        lines.append(f'θ: {_fmt_s(pgt.get("theta_ntd_per_day"))} NT / 天')
        lines.append(f'ν: {_fmt_s(pgt.get("vega_ntd_per_pct_iv"))} NT / 1% IV')
        gh = data.get('greeks_history') or {}
        cum = gh.get('cumulative') or {}
        if cum:
            lines.append(f'累積 θ: 30d {_fmt_s(cum.get("last_30d"))} | lifetime {_fmt_s(cum.get("lifetime"))}')

    # 換倉建議
    rolls = data.get('roll_suggestions') or []
    if rolls:
        lines.append('')
        lines.append(f'🔧 換倉建議 ({len(rolls)})')
        for r in rolls[:3]:
            pri = '🔴' if r.get('priority') == 'high' else '🟠'
            reason = (r.get('reason') or '')[:80]
            lines.append(f'{pri} {reason}')

    # 近 5 天事件
    evs = data.get('upcoming_events') or []
    near = [e for e in evs if (e.get('days_until') or 99) <= 5]
    if near:
        lines.append('')
        lines.append('📅 近 5 天事件')
        for e in near[:5]:
            d = e.get('days_until')
            when = '今日' if d == 0 else '明日' if d == 1 else f'{d}d'
            lines.append(f'• {when} ({e.get("date")}) {e.get("name", "")}')

    # Alert（live 計算）
    try:
        sys.path.insert(0, str(_HERE))
        import alerts as _A
        rules = _A.load_rules()
        rules['cooldown_minutes'] = 0           # 忽略 cooldown 給每日 digest
        rules['telegram_enabled'] = False       # 別讓 evaluate 自己送一次
        active = _A.evaluate(data, rules)
        if active:
            lines.append('')
            lines.append(f'⚠ Alert ({len(active)})')
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
