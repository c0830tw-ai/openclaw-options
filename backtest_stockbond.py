"""股債組合回測：0050 vs 60/40、50/50、80/20（含 00679B）。

用法：
    python3 backtest_stockbond.py --days 365
    python3 backtest_stockbond.py --days 365 --rebalance monthly
    python3 backtest_stockbond.py --days 365 --save reports/stockbond.csv

資料源：Shioaji api.kbars
- 0050: api.Contracts.Stocks.TSE['0050']
- 00679B: api.Contracts.Stocks.OTC['00679B']

策略：
- pure_stock        100% 0050
- combo_60_40       60% 0050 / 40% 00679B
- combo_50_50       50/50
- combo_80_20       80/20
- pure_bond         100% 00679B

Rebalance：依設定（monthly / quarterly / never）回到目標權重。
"""
from __future__ import annotations
import argparse
import csv
import math
import os
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, List, Tuple


def _load_env() -> None:
    env_file = Path(__file__).parent / '.env'
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def fetch_daily(api, contract, days: int) -> List[Tuple[date, float]]:
    """分塊抓 1-min K 重採成日 K 收盤（Shioaji；raw 未還原權息）。"""
    daily: OrderedDict = OrderedDict()
    end = datetime.now().date()
    remaining = days
    while remaining > 0:
        chunk = min(60, remaining)
        start = end - timedelta(days=chunk)
        try:
            kb = api.kbars(contract=contract,
                           start=start.strftime('%Y-%m-%d'),
                           end=end.strftime('%Y-%m-%d'))
            for ts_ns, cl in zip(kb.ts, kb.Close):
                d = datetime.fromtimestamp(ts_ns / 1e9).date()
                daily[d] = float(cl)
        except Exception as e:
            print(f'  chunk {start}→{end} 失敗: {e}', file=sys.stderr)
            break
        end = start - timedelta(days=1)
        remaining -= chunk
    return sorted(daily.items())


def fetch_yahoo_adj(ticker: str, days: int) -> List[Tuple[date, float]]:
    """從 Yahoo Finance 抓還原權息（auto-adjusted）日收盤。
    0050 → '0050.TW'；00679B → '00679B.TWO'。"""
    import yfinance as yf
    # period 字串：對應 days
    if days <= 31:    period = '1mo'
    elif days <= 92:  period = '3mo'
    elif days <= 183: period = '6mo'
    elif days <= 366: period = '1y'
    elif days <= 732: period = '2y'
    elif days <= 1830: period = '5y'
    else:             period = 'max'
    df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    out: List[Tuple[date, float]] = []
    for idx, row in df.iterrows():
        c = row.get('Close')
        if c is None or c != c:   # NaN 過濾
            continue
        out.append((idx.date(), float(c)))
    out.sort(key=lambda x: x[0])
    return out


def simulate_portfolio(prices_a: List[float], prices_b: List[float],
                       dates: List[date], weight_a: float,
                       rebalance: str = 'monthly',
                       starting_capital: float = 1_000_000) -> List[float]:
    """模擬 A/B 兩資產的固定比例組合，依 rebalance 頻率歸位。
    回傳逐日總市值序列。"""
    weight_b = 1.0 - weight_a
    # 起始：把資金按 weight 配到兩資產
    units_a = (starting_capital * weight_a) / prices_a[0] if prices_a[0] else 0
    units_b = (starting_capital * weight_b) / prices_b[0] if prices_b[0] else 0
    equity: List[float] = []
    last_rebal = dates[0]
    for i, d in enumerate(dates):
        v = units_a * prices_a[i] + units_b * prices_b[i]
        equity.append(v)
        # rebalance 判定
        should_rebal = False
        if rebalance == 'monthly' and d.month != last_rebal.month:
            should_rebal = True
        elif rebalance == 'quarterly' and (d.month - 1) // 3 != (last_rebal.month - 1) // 3:
            should_rebal = True
        if should_rebal and prices_a[i] and prices_b[i]:
            units_a = (v * weight_a) / prices_a[i]
            units_b = (v * weight_b) / prices_b[i]
            last_rebal = d
    return equity


def _stats(eq: List[float]) -> Dict[str, float]:
    if len(eq) < 2:
        return {'total_ret': 0, 'annual': 0, 'mdd': 0, 'calmar': 0, 'sharpe': 0, 'vol': 0}
    base = eq[0]
    total = eq[-1] / base - 1
    n = len(eq)
    annual = (eq[-1] / base) ** (252 / max(n - 1, 1)) - 1
    peak = eq[0]
    mdd = 0.0
    for v in eq:
        peak = max(peak, v)
        dd = v / peak - 1
        mdd = min(mdd, dd)
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, n)]
    mean_r = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / len(rets)) if rets else 0
    sharpe = (mean_r * 252) / (sd * math.sqrt(252)) if sd else 0
    vol = sd * math.sqrt(252)
    calmar = (annual / abs(mdd)) if mdd else 0
    return {'total_ret': total * 100, 'annual': annual * 100, 'mdd': mdd * 100,
            'calmar': calmar, 'sharpe': sharpe, 'vol': vol * 100}


def _label_regime(ret_pct: float) -> str:
    if ret_pct > 5:  return '🐂 牛市'
    if ret_pct < -5: return '🐻 熊市'
    return '😴 盤整'


def run_segment(label_q: str, dates: List[date], p_a: List[float], p_b: List[float],
                portfolios: List[Tuple[str, float]], rebalance: str) -> List[dict]:
    """單一時段跑 5 個組合，回傳排序後的統計列表。"""
    results = []
    for name, w in portfolios:
        eq = simulate_portfolio(p_a, p_b, dates, w, rebalance=rebalance)
        s = _stats(eq)
        results.append({'name': name, **s})
    results.sort(key=lambda r: r['calmar'], reverse=True)
    return results


def build_stockbond_markdown(days: int = 1095, segments: int = 6,
                             rebalance: str = 'monthly') -> str:
    """回傳「股債組合」回測 markdown 段落（嵌進 backtest_report 用）。
    資料源固定用 Yahoo auto_adjust（還原權息）。"""
    try:
        d_0050 = dict(fetch_yahoo_adj('0050.TW',   days))
        d_bond = dict(fetch_yahoo_adj('00679B.TWO', days))
    except Exception as e:
        return f'## 5. 股債組合對照\n\n> ⚠️ Yahoo 抓取失敗：{e}\n'

    common = sorted(set(d_0050) & set(d_bond))
    if len(common) < 60:
        return f'## 5. 股債組合對照\n\n> ⚠️ 共同交易日太少（{len(common)}），略過\n'

    dates = common
    p_a = [d_0050[d] for d in dates]
    p_b = [d_bond[d] for d in dates]

    portfolios = [
        ('pure_stock_100', 1.00),
        ('combo_80_20',    0.80),
        ('combo_60_40',    0.60),
        ('combo_50_50',    0.50),
        ('pure_bond_0',    0.00),
    ]

    md: List[str] = []
    md.append('## 5. 股債組合對照（0050 vs 00679B，Yahoo 還原權息）')
    md.append('')
    md.append(f'> 期間：{dates[0]} → {dates[-1]}（{len(dates)} 天，rebalance={rebalance}）')
    md.append(f'> 0050: {p_a[0]:.2f} → {p_a[-1]:.2f} '
              f'（{(p_a[-1]/p_a[0]-1)*100:+.2f}%）')
    md.append(f'> 00679B: {p_b[0]:.2f} → {p_b[-1]:.2f} '
              f'（{(p_b[-1]/p_b[0]-1)*100:+.2f}%）')
    md.append('')

    # 全期
    full = []
    for name, w in portfolios:
        eq = simulate_portfolio(p_a, p_b, dates, w, rebalance=rebalance)
        s = _stats(eq)
        full.append({'name': name, **s})
    full.sort(key=lambda r: r['calmar'], reverse=True)
    md.append('### 全期排名')
    md.append('')
    md.append('| 排名 | 組合 | 總報酬 | 年化 | MaxDD | Calmar | Sharpe | 年化波動 |')
    md.append('|---|---|---|---|---|---|---|---|')
    for i, r in enumerate(full, 1):
        bar = '🏆' if i == 1 else ('⭐' if i == 2 else '')
        md.append(f'| {i} | {bar} **{r["name"]}** | {r["total_ret"]:+.2f}% | '
                  f'{r["annual"]:+.2f}% | {r["mdd"]:+.2f}% | '
                  f'{r["calmar"]:+.2f} | {r["sharpe"]:+.2f} | {r["vol"]:.2f}% |')
    md.append('')

    # 分段
    if segments and segments > 1:
        n = len(dates)
        chunk = n // segments
        win, top3 = {}, {}
        md.append(f'### 多情境分段（{segments} segments）')
        md.append('')
        for q in range(segments):
            s_i = q * chunk
            e_i = (q + 1) * chunk - 1 if q < segments - 1 else n - 1
            sl_d  = dates[s_i:e_i + 1]
            sl_a  = p_a[s_i:e_i + 1]
            sl_b  = p_b[s_i:e_i + 1]
            ret_a = (sl_a[-1]/sl_a[0]-1)*100 if sl_a[0] else 0
            ret_b = (sl_b[-1]/sl_b[0]-1)*100 if sl_b[0] else 0
            regime = _label_regime(ret_a)
            seg = run_segment(f'Q{q+1}', sl_d, sl_a, sl_b, portfolios, rebalance)
            md.append(f'#### Q{q+1}: {regime}（{sl_d[0]} → {sl_d[-1]}, '
                      f'0050 {ret_a:+.2f}%, 00679B {ret_b:+.2f}%）')
            md.append('')
            md.append('| 排名 | 組合 | 總報酬 | MaxDD | Calmar | Sharpe |')
            md.append('|---|---|---|---|---|---|')
            for i, r in enumerate(seg, 1):
                bar = '🏆' if i == 1 else ('⭐' if i == 2 else '')
                md.append(f'| {i} | {bar} {r["name"]} | {r["total_ret"]:+.2f}% | '
                          f'{r["mdd"]:+.2f}% | {r["calmar"]:+.2f} | {r["sharpe"]:+.2f} |')
            md.append('')
            for j, r in enumerate(seg[:3]):
                top3[r['name']] = top3.get(r['name'], 0) + 1
                if j == 0: win[r['name']] = win.get(r['name'], 0) + 1

        md.append('### 跨情境統計')
        md.append('')
        md.append('**🏆 奪冠次數**')
        for k, v in sorted(win.items(), key=lambda x: -x[1]):
            md.append(f'- {k}: {v}')
        md.append('')
        md.append('**進前 3 次數**')
        for k, v in sorted(top3.items(), key=lambda x: -x[1]):
            md.append(f'- {k}: {v}')
        md.append('')

    return '\n'.join(md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=180)
    ap.add_argument('--rebalance', choices=['monthly', 'quarterly', 'never'], default='monthly')
    ap.add_argument('--segments', type=int, default=0,
                    help='切成 N 段分別跑（牛/熊/盤整 regime 分析）；0 = 只跑全期')
    ap.add_argument('--source', choices=['yahoo', 'shioaji'], default='yahoo',
                    help='yahoo = 還原權息（長期可信）；shioaji = raw 未還原（短期/即時）')
    ap.add_argument('--save', help='CSV 輸出路徑')
    args = ap.parse_args()

    if args.source == 'yahoo':
        print(f'[stockbond] Yahoo 還原權息（auto_adjust）抓 0050.TW / 00679B.TWO 約 {args.days} 天...', file=sys.stderr)
        d_0050 = dict(fetch_yahoo_adj('0050.TW',   args.days))
        d_bond = dict(fetch_yahoo_adj('00679B.TWO', args.days))
    else:
        _load_env()
        import shioaji as sj
        print(f'[stockbond] Shioaji login（raw 未還原）...', file=sys.stderr)
        api = sj.Shioaji()
        api.login(api_key=os.environ['SHIOAJI_API_KEY'].strip(),
                  secret_key=os.environ['SHIOAJI_SECRET_KEY'].strip(),
                  contracts_timeout=60_000)
        c_0050   = api.Contracts.Stocks.TSE['0050']
        c_00679b = api.Contracts.Stocks.OTC['00679B']
        print(f'[stockbond] 抓 0050 / 00679B 各 {args.days} 天...', file=sys.stderr)
        d_0050 = dict(fetch_daily(api, c_0050,   args.days))
        d_bond = dict(fetch_daily(api, c_00679b, args.days))
        api.logout()

    common = sorted(set(d_0050) & set(d_bond))
    if len(common) < 30:
        print(f'共同交易日太少（{len(common)}），無法回測', file=sys.stderr)
        return 1

    dates = common
    p_0050 = [d_0050[d] for d in dates]
    p_bond = [d_bond[d] for d in dates]
    print(f'[stockbond] 共 {len(dates)} 個共同交易日: {dates[0]} → {dates[-1]}', file=sys.stderr)

    portfolios = [
        ('pure_stock_100',   1.00),
        ('combo_80_20',      0.80),
        ('combo_60_40',      0.60),
        ('combo_50_50',      0.50),
        ('pure_bond_0',      0.00),
    ]

    # 0050 拆分警告：只在 raw Shioaji 來源、且區間跨越 2025-06 時提醒
    if args.source == 'shioaji':
        SPLIT_DATE = date(2025, 6, 1)
        if dates[0] < SPLIT_DATE < dates[-1]:
            print()
            print('⚠️  資料區間跨越 0050 拆分日（2025-06），Shioaji kbars 為未還原權息價。'
                  '建議改用 --source yahoo 取得乾淨還原序列。', file=sys.stderr)

    results = []
    for name, w in portfolios:
        eq = simulate_portfolio(p_0050, p_bond, dates, w, rebalance=args.rebalance)
        s = _stats(eq)
        results.append((name, w, eq, s))

    # 排序 by Calmar
    results.sort(key=lambda r: r[3]['calmar'], reverse=True)

    print()
    print('━' * 88)
    print(f'股債組合回測 — 全期（{dates[0]} → {dates[-1]}, {len(dates)} 天, rebalance={args.rebalance}）')
    print(f'0050: {p_0050[0]:.2f} → {p_0050[-1]:.2f} ({(p_0050[-1]/p_0050[0]-1)*100:+.2f}%)   '
          f'00679B: {p_bond[0]:.2f} → {p_bond[-1]:.2f} ({(p_bond[-1]/p_bond[0]-1)*100:+.2f}%)')
    print('━' * 88)
    print(f'  {"排名":>4}  {"組合":>16}  │ {"總報酬":>10} {"年化":>10} {"MaxDD":>10} {"Calmar":>8} {"Sharpe":>8} {"年化波動":>10}')
    print('─' * 88)
    for i, (name, w, eq, s) in enumerate(results, 1):
        bar = ' 🏆' if i == 1 else ('  ⭐' if i == 2 else '   ')
        print(f'  {i:>4}  {name:>16}{bar}│ '
              f'{s["total_ret"]:>+9.2f}% {s["annual"]:>+9.2f}% '
              f'{s["mdd"]:>+9.2f}% {s["calmar"]:>+8.2f} {s["sharpe"]:>+8.2f} {s["vol"]:>+9.2f}%')
    print('━' * 88)

    # ─── 分段（牛/熊/盤整 regime 對照）────────────────
    if args.segments and args.segments > 1:
        n = len(dates)
        chunk = n // args.segments
        win_count: Dict[str, int] = {}
        top3_count: Dict[str, int] = {}
        for q in range(args.segments):
            s_i = q * chunk
            e_i = (q + 1) * chunk - 1 if q < args.segments - 1 else n - 1
            sl_dates = dates[s_i:e_i + 1]
            sl_a     = p_0050[s_i:e_i + 1]
            sl_b     = p_bond[s_i:e_i + 1]
            ret_a = (sl_a[-1] / sl_a[0] - 1) * 100 if sl_a[0] else 0
            ret_b = (sl_b[-1] / sl_b[0] - 1) * 100 if sl_b[0] else 0
            regime = _label_regime(ret_a)
            seg_stats = run_segment(f'Q{q+1}', sl_dates, sl_a, sl_b, portfolios, args.rebalance)
            print()
            print(f'### Q{q+1}: {regime}（{sl_dates[0]} → {sl_dates[-1]}, '
                  f'0050 {ret_a:+.2f}%, 00679B {ret_b:+.2f}%）')
            print(f'  {"排名":>4}  {"組合":>16}  │ {"總報酬":>10} {"MaxDD":>10} {"Calmar":>8} {"Sharpe":>8}')
            print('─' * 70)
            for i, r in enumerate(seg_stats, 1):
                bar = ' 🏆' if i == 1 else ('  ⭐' if i == 2 else '   ')
                print(f'  {i:>4}  {r["name"]:>16}{bar}│ '
                      f'{r["total_ret"]:>+9.2f}% {r["mdd"]:>+9.2f}% '
                      f'{r["calmar"]:>+8.2f} {r["sharpe"]:>+8.2f}')
            for j, r in enumerate(seg_stats[:3]):
                top3_count[r['name']] = top3_count.get(r['name'], 0) + 1
                if j == 0:
                    win_count[r['name']] = win_count.get(r['name'], 0) + 1

        # 跨情境統計
        print()
        print('━' * 70)
        print('跨情境統計')
        print('━' * 70)
        print('🏆 奪冠次數:')
        for k, v in sorted(win_count.items(), key=lambda x: -x[1]):
            print(f'   {k}: {v} 次')
        print('進前 3 次數:')
        for k, v in sorted(top3_count.items(), key=lambda x: -x[1]):
            print(f'   {k}: {v}')
        print('━' * 70)

    if args.save:
        out_path = Path(args.save)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open('w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            header = ['date', 'p_0050', 'p_00679b'] + [r[0] for r in results]
            w.writerow(header)
            for i, d in enumerate(dates):
                row = [d.isoformat(), p_0050[i], p_bond[i]] + [r[2][i] for r in results]
                w.writerow(row)
        print(f'[stockbond] 寫入 {out_path}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
