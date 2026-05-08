"""
evening_report.py — 收盤後 Telegram 回顧報告

Morning report 的 evening 對偶：
  早報：「今日要注意什麼、現在曝險」
  晚報：「今日做了什麼、明日該動手什麼」

從 latest_collar.json + daily_snapshots（trend）+ pnl_attribution +
trade_journal 聚合，組成 5 段式回顧：
  📊 今日結果
  🧮 P&L 拆解（Δ/θ/ν）
  📓 今日交易（若有）
  🎯 明日重點
  📈 健診走勢

CLI：
  python3 evening_report.py            # 週末/假日跳過
  python3 evening_report.py --force    # 強制送
  python3 evening_report.py --print    # 只印不送
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

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
    return d.weekday() >= 5


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


def _safe(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def build_report(data: Dict[str, Any], now: datetime = None) -> str:
    if now is None:
        now = datetime.now()
    lines = [f'🌆 收盤回顧（{now.strftime("%-m/%-d %H:%M")}）']

    # ━━━ 今日結果 ━━━
    m = data.get('market') or {}
    trend = data.get('trend') or {}
    vs_y = _safe(trend, 'changes', 'vs_yesterday', default={}) or {}

    lines.append('')
    lines.append('📊 今日結果')
    if m.get('tx_futures'):
        tx_pct = vs_y.get('tx_delta_pct')
        tx_str = f' ({tx_pct:+.2f}%)' if tx_pct is not None else ''
        lines.append(f'TX: {_fmt_n(m["tx_futures"])}{tx_str}')

    iv = data.get('iv_used') or 0
    iv_pp = vs_y.get('iv_delta_pct')   # 已是 pp 變動
    iv_pct = iv * 100 if iv and iv < 1 else (iv or 0)
    if iv_pp is not None and iv_pp != 0:
        lines.append(f'IV: {iv_pct:.1f}% ({iv_pp:+.1f}pp 對昨)')
    else:
        lines.append(f'IV: {iv_pct:.1f}%')

    # IV percentile
    ivp = data.get('iv_percentile') or {}
    if ivp.get('enough_data'):
        lines.append(f'  IV @ {ivp["percentile"]:.0f} pctile [{ivp["label"]}]')

    # 浮動 P&L
    unr_d = vs_y.get('unrealized_delta')
    if unr_d is not None and unr_d != 0:
        lines.append(f'浮動 P&L Δ vs 昨: {_fmt_s(unr_d)} NT')

    # ━━━ P&L 拆解（如有資料）━━━
    pa = data.get('pnl_attribution') or {}
    if pa and pa.get('rows'):
        latest = pa['rows'][-1] if pa['rows'] else None
        if latest:
            lines.append('')
            lines.append('🧮 P&L 拆解（昨→今）')
            lines.append(f'Δ {_fmt_s(latest["delta_pl_ntd"])} '
                         f'· θ {_fmt_s(latest["theta_pl_ntd"])} '
                         f'· ν {_fmt_s(latest["vega_pl_ntd"])}')
            lines.append(f'解釋 {_fmt_s(latest["explained_ntd"])} / '
                         f'實際 {_fmt_s(latest["actual_pl_ntd"])} / '
                         f'殘差 {_fmt_s(latest["residual_ntd"])}')

    # ━━━ 今日交易（若有今天的 trades） ━━━
    tj = data.get('trade_journal') or {}
    today_str = now.strftime('%Y-%m-%d')
    today_entries = [e for e in (tj.get('recent_entries') or [])
                     if e.get('date') == today_str]
    if today_entries:
        lines.append('')
        lines.append(f'📓 今日交易 ({len(today_entries)})')
        for e in today_entries[:3]:
            pnl = e.get('realized_pnl')
            pnl_str = f' [{_fmt_s(pnl)}]' if pnl is not None else ''
            lines.append(f'• {e.get("instrument", "?")}{pnl_str}')
            if e.get('thesis'):
                lines.append(f'  論點：{e["thesis"][:50]}')

    # ━━━ 策略推薦（regime advisor） ━━━
    ra = data.get('regime_advisor') or {}
    if ra and ra.get('recommendation'):
        r = ra['recommendation']
        cur = ra.get('current') or {}
        lines.append('')
        lines.append(f'🎯 {ra.get("regime_label", "?")}（月 {ra.get("monthly_pct", 0):+.1f}%）')
        lines.append(f'💡 主推 {r.get("strategy")}')
        if r.get('stats'):
            s = r['stats']; total = r.get('quarters_total', 0)
            line = f'📊 歷史 {s["wins"]}/{total} 冠 ({s["win_rate_pct"]}%)'
            if s.get('regime_total'):
                line += f' · 同情境 {s["regime_wins"]}/{s["regime_total"]}'
            lines.append(line)
        if r.get('fallback'):
            fb = f'🛡️ Fallback {r.get("fallback")}'
            if r.get('fallback_stats'):
                fb += f'（前 3 {r["fallback_stats"]["top3_rate_pct"]}%）'
            lines.append(fb)
        if cur.get('has_positions') and ra.get('deviations'):
            n = len(ra['deviations'])
            lines.append(f'⚠️ 當前 {cur.get("strategy")} 偏離 {n} 項')

    # ━━━ 明日重點 ━━━
    lines.append('')
    lines.append('🎯 明日重點')
    upcoming = data.get('upcoming_events') or []
    tomorrow = [e for e in upcoming if e.get('days_until') in (1, 0)]
    if tomorrow:
        for e in tomorrow:
            d = e.get('days_until')
            when = '明日' if d == 1 else '今晚'
            iv_risk = e.get('iv_risk')
            risk_tag = '🔴' if iv_risk == 'high' else '🟠'
            lines.append(f'{risk_tag} {when} {e.get("name", "")}')
    else:
        lines.append('（5 天內無高影響事件）')

    # 換倉建議
    rolls = data.get('roll_suggestions') or []
    if rolls:
        for r in rolls[:2]:
            pri = '🔴' if r.get('priority') == 'high' else '🟠'
            lines.append(f'{pri} {(r.get("reason") or "")[:60]}')

    # collar dashboard 觸發中的腳
    cd = data.get('collar_dashboard') or {}
    for leg_key, leg_label in [('put_leg', 'put'), ('call_leg', 'call')]:
        leg = cd.get(leg_key) or {}
        for tg in (leg.get('triggers') or []):
            if tg.get('level') == 'high':
                lines.append(f'⚠️ {leg_label}：{tg.get("msg", "")}')
                break

    # ━━━ 健診走勢 ━━━
    hc = data.get('health_check') or {}
    if hc:
        lines.append('')
        lines.append(f'🏥 健診: {hc["overall_score"]}/100 [{hc["grade"]}]')
        if hc.get('violations'):
            lines.append(f'  違規 {len(hc["violations"])} 條')
            for v in hc['violations'][:1]:
                lines.append(f'  ⚠ {v[:60]}')

    return '\n'.join(lines)


def main(force: bool = False, print_only: bool = False) -> int:
    _load_env()
    now = datetime.now()

    if not force and _is_weekend(now):
        print(f'[evening_report] {now.strftime("%a")} 週末跳過（用 --force 強制送）')
        return 0

    if not LATEST_FILE.exists():
        print(f'⚠ {LATEST_FILE.name} 不存在', file=sys.stderr)
        return 1

    try:
        data = json.loads(LATEST_FILE.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'[evening_report] 讀檔失敗：{e}', file=sys.stderr)
        return 1

    msg = build_report(data, now=now)
    print(msg)

    if print_only:
        return 0

    sys.path.insert(0, str(_HERE))
    import alerts as _A
    buttons = [
        [{'text': '📋 Last 5', 'data': '/last'},
         {'text': '📂 Positions', 'data': '/positions'}],
        [{'text': '📉 Drawdown', 'data': '/dd'},
         {'text': '⚠️ Risk', 'data': '/risk'}],
        [{'text': '📅 Events', 'data': '/events'},
         {'text': '🔧 Roll', 'data': '/roll'}],
    ]
    if _A.send_telegram(msg, buttons=buttons):
        print('\n[evening_report] Telegram 推送成功')
    else:
        print('\n[evening_report] Telegram 未設定或失敗')
    return 0


if __name__ == '__main__':
    sys.exit(main(force='--force' in sys.argv, print_only='--print' in sys.argv))
