"""
backtest_report.py — 月度多策略回測報告 + Telegram push

跑 backtest_all_regime（4 quarters × 8 strategies），
組成 Markdown 報告，推送到 Telegram。

CLI：
  python3 backtest_report.py                # 預設合成 400 天 + 推 Telegram
  python3 backtest_report.py --shioaji      # 真實 TX
  python3 backtest_report.py --no-push      # 只寫檔不推
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
OUT_DIR = _HERE / 'backtest_reports'

sys.path.insert(0, str(_HERE))
import backtest as B           # noqa: E402
import backtest_all as BA      # noqa: E402
import backtest_all_regime as BAR  # noqa: E402
import backtest_regime as BR   # noqa: E402


def _load_env():
    env = _HERE / '.env'
    if not env.exists(): return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def build_markdown(prices, lots=5, quarters=4):
    """跑全策略多情境分析，組 markdown。"""
    md = []
    md.append(f'# 多策略回測月報 — {datetime.now().strftime("%Y-%m-%d")}')
    md.append('')
    md.append(f'> 回測期間：{prices[0][0]} → {prices[-1][0]}（{len(prices)} 天）')
    md.append(f'> TX: {prices[0][1]:.0f} → {prices[-1][1]:.0f} '
              f'（{((prices[-1][1] / prices[0][1]) - 1) * 100:+.2f}%）')
    md.append('')

    # 全期 8 策略對比
    md.append('## 1. 全期 8 策略 ranking')
    md.append('')
    md.append('| 排名 | 策略 | 總報酬 | MaxDD | Calmar | Sharpe |')
    md.append('|---|---|---|---|---|---|')
    res = BAR.evaluate_window(prices, hedge_lots=lots)
    for i, r in enumerate(res, 1):
        bar = '🏆 ' if i == 1 else ('⭐ ' if i == 2 else '')
        md.append(f'| {i} | {bar}**{r["name"]}** | '
                  f'{r["total_ret"]:+.2f}% | {r["mdd"]:+.2f}% | '
                  f'{r["calmar"]:+.2f} | {r["sharpe"]:+.2f} |')
    md.append('')
    if res:
        md.append(f'**冠軍策略**：{res[0]["name"]}（Calmar {res[0]["calmar"]:+.2f}）')
        md.append('')

    # 多情境分段
    md.append(f'## 2. 多情境分段（{quarters} quarters）')
    md.append('')
    n = len(prices)
    chunk = n // quarters
    LABEL = {'bull': '🐂 牛市', 'bear': '🐻 熊市', 'side': '😴 盤整'}
    win_count, top3_count = {}, {}

    for i in range(quarters):
        s = i * chunk
        e = (i + 1) * chunk - 1 if i < quarters - 1 else n - 1
        ret = (prices[e][1] - prices[s][1]) / prices[s][1] * 100
        regime = 'bull' if ret > 5 else 'bear' if ret < -5 else 'side'
        slice_p = prices[s:e + 1]
        stats = BAR.evaluate_window(slice_p, hedge_lots=lots)
        if not stats:
            continue
        md.append(f'### Q{i+1}: {LABEL.get(regime, regime)}（{prices[s][0]} → {prices[e][0]}, TX {ret:+.2f}%）')
        md.append('')
        md.append('| 排名 | 策略 | 報酬 | DD | Calmar |')
        md.append('|---|---|---|---|---|')
        for j, r in enumerate(stats[:5], 1):  # top 5
            bar = '🏆' if j == 1 else ('⭐' if j == 2 else '')
            md.append(f'| {j} | {bar} {r["name"]} | {r["total_ret"]:+.2f}% | {r["mdd"]:+.2f}% | {r["calmar"]:+.2f} |')
        md.append('')
        for j, r in enumerate(stats[:3]):
            top3_count[r['name']] = top3_count.get(r['name'], 0) + 1
            if j == 0:
                win_count[r['name']] = win_count.get(r['name'], 0) + 1

    # 統計與建議
    md.append('## 3. 跨情境統計')
    md.append('')
    md.append('### 🏆 奪冠次數')
    for k, v in sorted(win_count.items(), key=lambda x: -x[1]):
        md.append(f'- **{k}**: {v} 次')
    md.append('')
    md.append('### 進前 3 次數')
    for k, v in sorted(top3_count.items(), key=lambda x: -x[1]):
        md.append(f'- {k}: {v}')
    md.append('')
    md.append('## 4. 建議')
    md.append('')
    if win_count:
        top_winner = max(win_count.items(), key=lambda x: x[1])
        md.append(f'- **最 robust 全能策略**：{top_winner[0]}（{top_winner[1]} 次奪冠）')
    if top3_count:
        most_consistent = max(top3_count.items(), key=lambda x: x[1])
        md.append(f'- **最穩定亞軍**：{most_consistent[0]}（{most_consistent[1]} 次進前 3）')
    md.append(f'- 配合 RegimeAdvisor 動態切換對應冠軍策略')
    md.append('')

    return '\n'.join(md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', help='TX CSV')
    ap.add_argument('--shioaji', action='store_true')
    ap.add_argument('--days', type=int, default=400)
    ap.add_argument('--lots', type=int, default=5)
    ap.add_argument('--quarters', type=int, default=4)
    ap.add_argument('--no-push', action='store_true')
    args = ap.parse_args()

    if args.csv:        prices = B.load_csv(args.csv)
    elif args.shioaji:  prices = B.fetch_tx_history_shioaji(days=args.days)
    else:               prices = B.synthetic_prices(days=args.days)

    if not prices or len(prices) < 50:
        print('資料不足', file=sys.stderr); return 1

    print(f'[backtest_report] {len(prices)} 天 · 跑 8 策略 × {args.quarters} 段', file=sys.stderr)
    md = build_markdown(prices, lots=args.lots, quarters=args.quarters)

    OUT_DIR.mkdir(exist_ok=True)
    fname = OUT_DIR / f'{datetime.now().strftime("%Y-%m")}.md'
    fname.write_text(md, encoding='utf-8')
    print(f'[backtest_report] wrote → {fname}', file=sys.stderr)

    if not args.no_push:
        _load_env()
        sys.path.insert(0, str(_HERE))
        try:
            import alerts as _A
        except Exception as e:
            print(f'load alerts failed: {e}', file=sys.stderr); return 0
        caption = f'📊 多策略回測月報 {datetime.now().strftime("%Y-%m")}'
        if _A.send_telegram_document(str(fname), caption=caption):
            print('[backtest_report] Telegram 推送成功', file=sys.stderr)
        else:
            print('[backtest_report] Telegram 未設或失敗', file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main() or 0)
