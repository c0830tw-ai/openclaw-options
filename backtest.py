"""
backtest.py — 合成回測 collar 月選 put hedge 策略

策略：每月 1 號（或月選結算後 +1 天）買 N 口 TXO put，DTE ~30、delta ~0.10
持倉至到期或下次 roll。每日 mark-to-market 用 BS 估價（用 20 日 HV 當 IV）。

用法：
  python3 backtest.py                     # 合成價格 252 個交易日
  python3 backtest.py --csv tx_hist.csv   # 自帶 CSV (date,close)
  python3 backtest.py --shioaji           # 從 Shioaji 抓真實 TX 歷史
"""
import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

# 復用 shioaji_collar 的 BS 工具
sys.path.insert(0, str(Path(__file__).resolve().parent))
from shioaji_collar import bs_price, bs_delta, _norm_cdf  # noqa: E402

TXO_MULTIPLIER = 50          # NT$/point
HEDGE_LOTS     = 5           # 假設恆持 5 口 put 對應 ~1000 萬名目
DELTA_TARGET   = 0.10        # OTM put delta 絕對值
DTE_TARGET     = 30          # 月選平均 DTE
HV_PERIOD      = 20          # 滾動 HV 視窗
RF             = 0.0         # Black-76 (futures r=0)
NOTIONAL_QTY   = 5           # naked-long 口數（對照組）；模擬 5 口 0050 期


@dataclass
class PutPosition:
    entry_date: date
    expiry: date
    strike: float
    qty: int
    entry_premium: float       # 點數
    iv_at_entry: float

    def value_at(self, S: float, T_days: int, hv: float) -> float:
        """以當日 BS 估價（用滾動 HV 當 IV）。"""
        if T_days <= 0:
            return max(0.0, self.strike - S) * self.qty * TXO_MULTIPLIER
        T = T_days / 365
        return bs_price(S, self.strike, T, hv, is_put=True, r=RF) * self.qty * TXO_MULTIPLIER


@dataclass
class StrategyResult:
    dates: List[date] = field(default_factory=list)
    tx:    List[float] = field(default_factory=list)
    hv:    List[float] = field(default_factory=list)
    put_value:  List[float] = field(default_factory=list)
    put_strike: List[Optional[float]] = field(default_factory=list)
    put_dte:    List[Optional[int]]   = field(default_factory=list)
    cash_paid:  List[float] = field(default_factory=list)   # 累積已付 premium
    long_value: List[float] = field(default_factory=list)
    equity_collar: List[float] = field(default_factory=list)
    equity_naked:  List[float] = field(default_factory=list)
    rolls: List[dict] = field(default_factory=list)


def _rolling_hv(prices: List[float], period: int = HV_PERIOD) -> List[float]:
    """滾動年化 HV（log returns × √252）。前 period 天用前段平均填補。"""
    out = [0.0] * len(prices)
    rets = [0.0] * len(prices)
    for i in range(1, len(prices)):
        rets[i] = math.log(prices[i] / prices[i - 1]) if prices[i - 1] > 0 else 0
    for i in range(len(prices)):
        start = max(0, i - period + 1)
        sample = rets[start:i + 1]
        if len(sample) < 2:
            out[i] = 0.20   # 預設
            continue
        mean = sum(sample) / len(sample)
        var = sum((r - mean) ** 2 for r in sample) / max(1, len(sample) - 1)
        out[i] = math.sqrt(var * 252)
    return out


def _find_strike_at_delta(S: float, T: float, sigma: float,
                          target_delta: float = DELTA_TARGET,
                          step: float = 100) -> float:
    """從 ATM 往下移 strike，找 |delta| 最接近 target 的（線性掃描）。"""
    K = S
    while K > S * 0.5:
        d = bs_delta(S, K, T, sigma, is_put=True, r=RF)
        if abs(d) <= target_delta:
            return K
        K -= step
    return K


def run_backtest(prices: List[Tuple[date, float]],
                 hedge_lots: int = HEDGE_LOTS,
                 notional_qty: int = NOTIONAL_QTY) -> StrategyResult:
    """執行月選 put hedge 回測。"""
    if not prices:
        raise ValueError('empty price series')

    dates  = [p[0] for p in prices]
    pxs    = [p[1] for p in prices]
    hv_arr = _rolling_hv(pxs)

    # naked-long base notional：用第一天價格 × notional_qty × multiplier 模擬常持倉
    base_notional = pxs[0] * notional_qty * TXO_MULTIPLIER

    res = StrategyResult()
    cur_put: Optional[PutPosition] = None
    cum_paid = 0.0       # 累積支付 premium（NT$）
    cum_received = 0.0   # 累積到期收回 / 平倉收回（NT$）
    last_roll_month = None

    for i, (dt, S) in enumerate(prices):
        hv = max(0.10, hv_arr[i])     # IV 不要太低
        # 1. 處理到期：若有 put 且今天 ≥ expiry，結算
        if cur_put and dt >= cur_put.expiry:
            payoff = max(0, cur_put.strike - S) * cur_put.qty * TXO_MULTIPLIER
            cum_received += payoff
            res.rolls.append({
                'date':   dt.isoformat(),
                'action': 'expire',
                'strike': cur_put.strike,
                'payoff_ntd': payoff,
                'tx_at_expiry': S,
            })
            cur_put = None

        # 2. 月初 roll：每個自然月第一個交易日建倉
        if dt.month != (last_roll_month or 0):
            # close existing if any（mark-to-market）
            if cur_put:
                T_left_days = max(1, (cur_put.expiry - dt).days)
                close_value = cur_put.value_at(S, T_left_days, hv)
                cum_received += close_value
                res.rolls.append({
                    'date':   dt.isoformat(),
                    'action': 'close_for_roll',
                    'strike': cur_put.strike,
                    'value':  close_value,
                })
                cur_put = None

            # 開新倉：DTE_TARGET 天到期
            expiry = dt + timedelta(days=DTE_TARGET)
            T = DTE_TARGET / 365
            new_strike = _find_strike_at_delta(S, T, hv)
            entry_prem = bs_price(S, new_strike, T, hv, is_put=True, r=RF)
            cost = entry_prem * hedge_lots * TXO_MULTIPLIER
            cum_paid += cost
            cur_put = PutPosition(
                entry_date=dt, expiry=expiry, strike=new_strike,
                qty=hedge_lots, entry_premium=entry_prem, iv_at_entry=hv,
            )
            res.rolls.append({
                'date':   dt.isoformat(),
                'action': 'open',
                'strike': new_strike,
                'iv':     round(hv, 4),
                'premium_pt': round(entry_prem, 1),
                'cost_ntd':   round(cost),
                'expiry':     expiry.isoformat(),
            })
            last_roll_month = dt.month

        # 3. mark-to-market
        long_v = S * notional_qty * TXO_MULTIPLIER
        put_v  = 0.0
        put_strike = None
        put_dte = None
        if cur_put:
            T_left_days = max(0, (cur_put.expiry - dt).days)
            put_v = cur_put.value_at(S, T_left_days, hv)
            put_strike = cur_put.strike
            put_dte = T_left_days

        equity_collar = long_v + put_v - cum_paid + cum_received
        equity_naked  = long_v   # 對照組：無 hedge

        res.dates.append(dt)
        res.tx.append(S)
        res.hv.append(hv)
        res.put_value.append(put_v)
        res.put_strike.append(put_strike)
        res.put_dte.append(put_dte)
        res.cash_paid.append(cum_paid - cum_received)
        res.long_value.append(long_v)
        res.equity_collar.append(equity_collar)
        res.equity_naked.append(equity_naked)

    return res


def report(res: StrategyResult) -> dict:
    """計算 metrics 並印出總結。"""
    if not res.equity_collar:
        return {}

    def _max_dd(eq: List[float]) -> float:
        peak = eq[0]
        max_dd = 0.0
        for v in eq:
            peak = max(peak, v)
            if peak > 0:
                max_dd = min(max_dd, (v - peak) / peak)
        return max_dd

    base = res.equity_naked[0]
    final_collar = res.equity_collar[-1]
    final_naked  = res.equity_naked[-1]
    n_days = len(res.dates)
    yrs = n_days / 252

    ret_collar = (final_collar - base) / base
    ret_naked  = (final_naked - base) / base
    annual_collar = (1 + ret_collar) ** (1 / max(yrs, 0.01)) - 1
    annual_naked  = (1 + ret_naked)  ** (1 / max(yrs, 0.01)) - 1

    dd_collar = _max_dd(res.equity_collar)
    dd_naked  = _max_dd(res.equity_naked)

    # cash_paid[-1] = 累積支付 - 累積回收。正 = 淨支付 (hedge 成本)，負 = 淨收回 (hedge 賺錢)
    net_cost = res.cash_paid[-1] if res.cash_paid else 0
    n_rolls  = sum(1 for r in res.rolls if r['action'] == 'open')

    # Hit rate：put 結算時 ITM 的比例
    expiries = [r for r in res.rolls if r['action'] == 'expire']
    hits = sum(1 for r in expiries if (r.get('payoff_ntd') or 0) > 0)
    hit_rate = hits / len(expiries) if expiries else 0

    metrics = {
        'period_days':        n_days,
        'period_years':       round(yrs, 2),
        'tx_start':           round(res.tx[0], 0),
        'tx_end':             round(res.tx[-1], 0),
        'tx_change_pct':      round((res.tx[-1] / res.tx[0] - 1) * 100, 2),

        'collar_total_return_pct':  round(ret_collar * 100, 2),
        'naked_total_return_pct':   round(ret_naked  * 100, 2),
        'collar_annualized_pct':    round(annual_collar * 100, 2),
        'naked_annualized_pct':     round(annual_naked  * 100, 2),

        'collar_max_drawdown_pct':  round(dd_collar * 100, 2),
        'naked_max_drawdown_pct':   round(dd_naked  * 100, 2),

        'hedge_net_cost_ntd':       round(net_cost),    # 正=支付, 負=賺錢
        'hedge_avg_monthly_ntd':    round(net_cost / max(yrs * 12, 1)),
        'rolls':                    n_rolls,
        'put_hits_at_expiry':       hits,
        'put_hit_rate_pct':         round(hit_rate * 100, 1),
    }

    print()
    print('━' * 50)
    print(f'回測期間：{res.dates[0]} → {res.dates[-1]}  ({n_days} 個交易日)')
    print('━' * 50)
    print(f'TX:       {res.tx[0]:.0f} → {res.tx[-1]:.0f}  ({metrics["tx_change_pct"]:+.2f}%)')
    print()
    print(f'                    │  collar 策略   │  裸長部位')
    print(f'  總報酬           │  {metrics["collar_total_return_pct"]:+8.2f}%   │  {metrics["naked_total_return_pct"]:+8.2f}%')
    print(f'  年化報酬         │  {metrics["collar_annualized_pct"]:+8.2f}%   │  {metrics["naked_annualized_pct"]:+8.2f}%')
    print(f'  最大回檔         │  {metrics["collar_max_drawdown_pct"]:+8.2f}%   │  {metrics["naked_max_drawdown_pct"]:+8.2f}%')
    print()
    cost_label = '淨成本（hedge 支出）' if net_cost > 0 else '淨收益（hedge 賺錢）'
    sign = '' if net_cost > 0 else '−'
    print(f'Hedge {cost_label}：{abs(metrics["hedge_net_cost_ntd"]):>9,} NT')
    print(f'月均：             {metrics["hedge_avg_monthly_ntd"]:>+10,} NT')
    print(f'換倉次數：         {metrics["rolls"]} 次')
    print(f'Put 到期 hit rate：{metrics["put_hits_at_expiry"]}/{len(expiries)}  ({metrics["put_hit_rate_pct"]}%)')
    print()
    # 觀點摘要
    diff = metrics['collar_total_return_pct'] - metrics['naked_total_return_pct']
    if diff > 0:
        print(f'💡 collar 比裸長部位多保住 {diff:+.2f}%（drawdown 也少 {abs(metrics["collar_max_drawdown_pct"] - metrics["naked_max_drawdown_pct"]):.2f}%）')
    elif diff < -2:
        print(f'💡 collar 因 hedge 成本拖累 {-diff:.2f}%（在無大跌的市場 hedge 是純成本）')
    else:
        print(f'💡 collar vs 裸 差異 {diff:+.2f}%（hedge 在這段期間影響中性）')
    print('━' * 50)

    return metrics


# ── 合成價格產生器 ───────────────────────────────────────
def synthetic_prices(days: int = 252, start: float = 42000.0,
                     mu: float = 0.08, sigma: float = 0.20,
                     seed: int = 42) -> List[Tuple[date, float]]:
    """GBM with occasional jumps (calibrated 給台股 ~ 8% 年報酬, 20% vol)。"""
    import random
    random.seed(seed)
    dt = 1 / 252
    prices = [start]
    out: List[Tuple[date, float]] = []
    today = datetime.now().date()
    for i in range(days):
        z = random.gauss(0, 1)
        ret = (mu - 0.5 * sigma ** 2) * dt + sigma * math.sqrt(dt) * z
        # 5% 機率小跳空（±2σ-3σ 風險事件）
        if random.random() < 0.05:
            ret += random.choice([-1, 1]) * random.uniform(0.02, 0.05)
        prices.append(prices[-1] * math.exp(ret))
        out.append((today - timedelta(days=days - i), prices[-1]))
    return out


# ── CSV 載入 ──────────────────────────────────────────────
def load_csv(path: str) -> List[Tuple[date, float]]:
    """讀 CSV：header=date,close 或 Date,Close。"""
    import csv
    out = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            d_str = row.get('date') or row.get('Date')
            c_str = row.get('close') or row.get('Close')
            if not d_str or not c_str:
                continue
            try:
                d = datetime.strptime(d_str, '%Y-%m-%d').date()
                out.append((d, float(c_str)))
            except ValueError:
                continue
    return sorted(out, key=lambda x: x[0])


# ── Shioaji 真實 TX history（chunked fetch）──────────────────
def fetch_tx_history_shioaji(days: int = 365) -> List[Tuple[date, float]]:
    """用 Shioaji api.kbars() 抓 TXFR1（連續契約）1-min K，重採成日 K。
    分塊抓避免單次請求太大。"""
    import shioaji as sj
    api = sj.Shioaji()
    api.login(
        api_key=__import__('os').environ['SHIOAJI_API_KEY'],
        secret_key=__import__('os').environ['SHIOAJI_SECRET_KEY'],
    )
    # TXFR1 連續期貨
    contract = next((c for c in api.Contracts.Futures.TXF if c.symbol == 'TXFR1'), None)
    if not contract:
        raise RuntimeError('TXFR1 not found')

    from collections import OrderedDict
    daily: dict = OrderedDict()
    end = datetime.now().date()
    chunks = []
    while days > 0:
        chunk_days = min(60, days)   # 60 天 / chunk
        start = end - timedelta(days=chunk_days)
        try:
            kb = api.kbars(contract=contract,
                           start=start.strftime('%Y-%m-%d'),
                           end=end.strftime('%Y-%m-%d'))
            ts_arr = kb.ts
            close_arr = kb.Close
            for ts_ns, c in zip(ts_arr, close_arr):
                d = datetime.fromtimestamp(ts_ns / 1e9).date()
                daily[d] = float(c)   # last close of day overwrites
        except Exception as e:
            print(f'[backtest] chunk {start}→{end} failed: {e}', file=sys.stderr)
        end = start
        days -= chunk_days

    out = sorted(daily.items())
    api.logout()
    return out


# ── CLI ───────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', help='Path to TX history CSV (date,close)')
    ap.add_argument('--shioaji', action='store_true', help='Fetch live TX history via Shioaji')
    ap.add_argument('--days', type=int, default=252, help='Synthetic series length')
    ap.add_argument('--lots', type=int, default=HEDGE_LOTS, help='Put hedge lots')
    ap.add_argument('--save', help='Save equity curve to CSV')
    args = ap.parse_args()

    if args.csv:
        prices = load_csv(args.csv)
        print(f'[backtest] loaded {len(prices)} days from {args.csv}')
    elif args.shioaji:
        prices = fetch_tx_history_shioaji(days=args.days + 30)
        print(f'[backtest] fetched {len(prices)} days from Shioaji')
    else:
        prices = synthetic_prices(days=args.days)
        print(f'[backtest] synthetic {len(prices)} days')

    if len(prices) < 30:
        print('not enough data', file=sys.stderr)
        return 1

    res = run_backtest(prices, hedge_lots=args.lots)
    metrics = report(res)

    if args.save:
        import csv
        with open(args.save, 'w') as f:
            w = csv.writer(f)
            w.writerow(['date', 'tx', 'hv', 'put_strike', 'put_dte', 'put_value',
                        'long_value', 'cash_paid_cum',
                        'equity_collar', 'equity_naked'])
            for i, d in enumerate(res.dates):
                w.writerow([d.isoformat(), round(res.tx[i], 1), round(res.hv[i], 4),
                            res.put_strike[i], res.put_dte[i], round(res.put_value[i]),
                            round(res.long_value[i]), round(res.cash_paid[i]),
                            round(res.equity_collar[i]), round(res.equity_naked[i])])
        print(f'[backtest] saved equity curve → {args.save}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
