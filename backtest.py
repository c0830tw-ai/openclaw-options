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
        """以當日 BS 估價（用滾動 HV 當 IV）。長 put 的當前價值（NT$）。"""
        if T_days <= 0:
            return max(0.0, self.strike - S) * self.qty * TXO_MULTIPLIER
        T = T_days / 365
        return bs_price(S, self.strike, T, hv, is_put=True, r=RF) * self.qty * TXO_MULTIPLIER


@dataclass
class CallPosition:
    """短部位 sell call。"""
    entry_date: date
    expiry: date
    strike: float
    qty: int
    entry_premium: float       # 點數（賣方收）
    iv_at_entry: float

    def buyback_value(self, S: float, T_days: int, hv: float) -> float:
        """買回所需成本（NT$）。短 call 的「義務」金額。"""
        if T_days <= 0:
            return max(0.0, S - self.strike) * self.qty * TXO_MULTIPLIER
        T = T_days / 365
        return bs_price(S, self.strike, T, hv, is_put=False, r=RF) * self.qty * TXO_MULTIPLIER


@dataclass
class StrategyResult:
    dates: List[date] = field(default_factory=list)
    tx:    List[float] = field(default_factory=list)
    hv:    List[float] = field(default_factory=list)
    # put leg
    put_value:  List[float] = field(default_factory=list)
    put_strike: List[Optional[float]] = field(default_factory=list)
    put_dte:    List[Optional[int]]   = field(default_factory=list)
    put_cash_paid: List[float] = field(default_factory=list)   # 累積 net 已付 (paid - received)
    # call leg (short)
    call_liab:    List[float] = field(default_factory=list)    # mark-to-market 義務金額
    call_strike:  List[Optional[float]] = field(default_factory=list)
    call_dte:     List[Optional[int]]   = field(default_factory=list)
    call_cash:    List[float] = field(default_factory=list)    # 累積 net 已收 (received - paid)
    # equity
    long_value:        List[float] = field(default_factory=list)
    equity_naked:      List[float] = field(default_factory=list)
    equity_put_only:   List[float] = field(default_factory=list)
    equity_collar:     List[float] = field(default_factory=list)
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


def _find_put_strike_at_delta(S: float, T: float, sigma: float,
                               target_delta: float = DELTA_TARGET,
                               step: float = 100) -> float:
    """OTM put：從 ATM 往下移到 |delta| ≤ target。"""
    K = S
    while K > S * 0.5:
        d = bs_delta(S, K, T, sigma, is_put=True, r=RF)
        if abs(d) <= target_delta:
            return K
        K -= step
    return K


def _find_call_strike_at_delta(S: float, T: float, sigma: float,
                                target_delta: float = DELTA_TARGET,
                                step: float = 100) -> float:
    """OTM call：從 ATM 往上移到 delta ≤ target。"""
    K = S
    while K < S * 1.5:
        d = bs_delta(S, K, T, sigma, is_put=False, r=RF)
        if d <= target_delta:
            return K
        K += step
    return K


# 向後相容
_find_strike_at_delta = _find_put_strike_at_delta


def run_backtest(prices: List[Tuple[date, float]],
                 hedge_lots: int = HEDGE_LOTS,
                 notional_qty: int = NOTIONAL_QTY,
                 sell_call: bool = True,
                 dte_target: int = DTE_TARGET,
                 delta_target: float = DELTA_TARGET) -> StrategyResult:
    """執行月選 hedge 回測。同時追蹤 3 種策略：
       1. naked      — 純長部位（無 hedge）
       2. put_only   — 長部位 + 買 put
       3. collar     — 長部位 + 買 put + 賣 call（如 sell_call=True）
    """
    if not prices:
        raise ValueError('empty price series')

    pxs    = [p[1] for p in prices]
    hv_arr = _rolling_hv(pxs)

    res = StrategyResult()
    cur_put:  Optional[PutPosition]  = None
    cur_call: Optional[CallPosition] = None
    # net cash flows (累積)
    put_paid   = 0.0   # 累積買 put 支出 - 結算/平倉收回
    call_cash  = 0.0   # 累積賣 call 收入 - 買回/履約支付
    last_roll_month = None

    for i, (dt, S) in enumerate(prices):
        hv = max(0.10, hv_arr[i])

        # === 1. 到期處理 ===
        if cur_put and dt >= cur_put.expiry:
            payoff = max(0, cur_put.strike - S) * cur_put.qty * TXO_MULTIPLIER
            put_paid -= payoff
            res.rolls.append({'date': dt.isoformat(), 'action': 'put_expire',
                              'strike': cur_put.strike, 'payoff_ntd': round(payoff)})
            cur_put = None
        if cur_call and dt >= cur_call.expiry:
            assigned = max(0, S - cur_call.strike) * cur_call.qty * TXO_MULTIPLIER
            call_cash -= assigned
            res.rolls.append({'date': dt.isoformat(), 'action': 'call_expire',
                              'strike': cur_call.strike, 'assigned_ntd': round(assigned)})
            cur_call = None

        # === 2. 月初 roll ===
        if dt.month != (last_roll_month or 0):
            # close put for roll
            if cur_put:
                T_left = max(1, (cur_put.expiry - dt).days)
                close_v = cur_put.value_at(S, T_left, hv)
                put_paid -= close_v
                res.rolls.append({'date': dt.isoformat(), 'action': 'put_close',
                                  'strike': cur_put.strike, 'value_ntd': round(close_v)})
                cur_put = None
            # close call (buy back) for roll
            if cur_call:
                T_left = max(1, (cur_call.expiry - dt).days)
                buyback = cur_call.buyback_value(S, T_left, hv)
                call_cash -= buyback
                res.rolls.append({'date': dt.isoformat(), 'action': 'call_buyback',
                                  'strike': cur_call.strike, 'cost_ntd': round(buyback)})
                cur_call = None

            # open new put
            expiry = dt + timedelta(days=dte_target)
            T = dte_target / 365
            new_put_K = _find_put_strike_at_delta(S, T, hv, target_delta=delta_target)
            put_prem = bs_price(S, new_put_K, T, hv, is_put=True, r=RF)
            cost = put_prem * hedge_lots * TXO_MULTIPLIER
            put_paid += cost
            cur_put = PutPosition(
                entry_date=dt, expiry=expiry, strike=new_put_K,
                qty=hedge_lots, entry_premium=put_prem, iv_at_entry=hv,
            )
            res.rolls.append({'date': dt.isoformat(), 'action': 'put_open',
                              'strike': new_put_K, 'premium_pt': round(put_prem, 1),
                              'cost_ntd': round(cost), 'expiry': expiry.isoformat()})

            # open new short call (only for collar strategy)
            if sell_call:
                new_call_K = _find_call_strike_at_delta(S, T, hv, target_delta=delta_target)
                call_prem = bs_price(S, new_call_K, T, hv, is_put=False, r=RF)
                received = call_prem * hedge_lots * TXO_MULTIPLIER
                call_cash += received
                cur_call = CallPosition(
                    entry_date=dt, expiry=expiry, strike=new_call_K,
                    qty=hedge_lots, entry_premium=call_prem, iv_at_entry=hv,
                )
                res.rolls.append({'date': dt.isoformat(), 'action': 'call_sell',
                                  'strike': new_call_K, 'premium_pt': round(call_prem, 1),
                                  'received_ntd': round(received), 'expiry': expiry.isoformat()})
            last_roll_month = dt.month

        # === 3. Mark-to-market ===
        long_v = S * notional_qty * TXO_MULTIPLIER
        put_v, put_strike, put_dte = 0.0, None, None
        call_liab, call_strike, call_dte = 0.0, None, None
        if cur_put:
            T_left = max(0, (cur_put.expiry - dt).days)
            put_v = cur_put.value_at(S, T_left, hv)
            put_strike, put_dte = cur_put.strike, T_left
        if cur_call:
            T_left = max(0, (cur_call.expiry - dt).days)
            call_liab = cur_call.buyback_value(S, T_left, hv)
            call_strike, call_dte = cur_call.strike, T_left

        equity_naked    = long_v
        equity_put_only = long_v + put_v - put_paid
        equity_collar   = equity_put_only + call_cash - call_liab

        res.dates.append(dt)
        res.tx.append(S)
        res.hv.append(hv)
        res.put_value.append(put_v)
        res.put_strike.append(put_strike)
        res.put_dte.append(put_dte)
        res.put_cash_paid.append(put_paid)
        res.call_liab.append(call_liab)
        res.call_strike.append(call_strike)
        res.call_dte.append(call_dte)
        res.call_cash.append(call_cash)
        res.long_value.append(long_v)
        res.equity_naked.append(equity_naked)
        res.equity_put_only.append(equity_put_only)
        res.equity_collar.append(equity_collar)

    return res


def report(res: StrategyResult) -> dict:
    """三策略對照：naked / put_only / collar"""
    if not res.equity_naked:
        return {}

    def _max_dd(eq: List[float]) -> float:
        peak = eq[0]
        m = 0.0
        for v in eq:
            peak = max(peak, v)
            if peak > 0:
                m = min(m, (v - peak) / peak)
        return m

    base = res.equity_naked[0]
    n_days = len(res.dates)
    yrs = n_days / 252

    def _stats(eq):
        ret = (eq[-1] - base) / base
        ann = (1 + ret) ** (1 / max(yrs, 0.01)) - 1
        return ret * 100, ann * 100, _max_dd(eq) * 100

    n_ret, n_ann, n_dd = _stats(res.equity_naked)
    p_ret, p_ann, p_dd = _stats(res.equity_put_only)
    c_ret, c_ann, c_dd = _stats(res.equity_collar)

    put_net  = res.put_cash_paid[-1] if res.put_cash_paid else 0
    call_net = res.call_cash[-1] if res.call_cash else 0
    has_call = sum(1 for r in res.rolls if r['action'] == 'call_sell') > 0

    rolls_open    = sum(1 for r in res.rolls if r['action'] == 'put_open')
    put_expiries  = [r for r in res.rolls if r['action'] == 'put_expire']
    call_expiries = [r for r in res.rolls if r['action'] == 'call_expire']
    put_hits  = sum(1 for r in put_expiries  if (r.get('payoff_ntd') or 0) > 0)
    call_hits = sum(1 for r in call_expiries if (r.get('assigned_ntd') or 0) > 0)

    metrics = {
        'period_days':       n_days,
        'period_years':      round(yrs, 2),
        'tx_start':          round(res.tx[0], 0),
        'tx_end':            round(res.tx[-1], 0),
        'tx_change_pct':     round((res.tx[-1] / res.tx[0] - 1) * 100, 2),
        'naked':    {'total_return_pct': round(n_ret, 2), 'annualized_pct': round(n_ann, 2), 'max_dd_pct': round(n_dd, 2)},
        'put_only': {'total_return_pct': round(p_ret, 2), 'annualized_pct': round(p_ann, 2), 'max_dd_pct': round(p_dd, 2)},
        'collar':   {'total_return_pct': round(c_ret, 2), 'annualized_pct': round(c_ann, 2), 'max_dd_pct': round(c_dd, 2)},
        'put_net_cost_ntd':       round(put_net),
        'call_net_received_ntd':  round(call_net),
        'rolls':                  rolls_open,
        'put_hit_rate_pct':       round(put_hits / len(put_expiries) * 100, 1) if put_expiries else 0,
        'call_assigned_pct':      round(call_hits / len(call_expiries) * 100, 1) if call_expiries else 0,
    }

    print()
    print('━' * 60)
    print(f'回測期間：{res.dates[0]} → {res.dates[-1]}  ({n_days} 個交易日)')
    print(f'TX: {res.tx[0]:.0f} → {res.tx[-1]:.0f}  ({metrics["tx_change_pct"]:+.2f}%)')
    print('━' * 60)
    print(f'{"":10} │ {"裸長部位":>10} │ {"put-only":>10} │ {"collar":>10}')
    print(f'{"總報酬":10} │ {n_ret:+9.2f}% │ {p_ret:+9.2f}% │ {c_ret:+9.2f}%')
    print(f'{"年化":10} │ {n_ann:+9.2f}% │ {p_ann:+9.2f}% │ {c_ann:+9.2f}%')
    print(f'{"最大回檔":10} │ {n_dd:+9.2f}% │ {p_dd:+9.2f}% │ {c_dd:+9.2f}%')
    print('━' * 60)
    print(f'Put leg 淨現金流（負=hedge 成本）：{-put_net:>+12,.0f} NT')
    if has_call:
        print(f'Call leg 淨現金流（正=收 premium）：{call_net:>+11,.0f} NT')
        print(f'Collar 整體 hedge 淨：{call_net - put_net:>+24,.0f} NT')
    print(f'換倉次數：{rolls_open}  |  Put hit {put_hits}/{len(put_expiries)} ({metrics["put_hit_rate_pct"]}%)', end='')
    if has_call:
        print(f'  |  Call 被軋 {call_hits}/{len(call_expiries)} ({metrics["call_assigned_pct"]}%)')
    else:
        print()
    print()
    print('💡 解讀：')
    if c_ret > n_ret + 1:
        print(f'   collar 勝過裸長 {c_ret - n_ret:+.2f}%（call 收入抵掉 put 成本）')
    elif c_ret < n_ret - 3:
        print(f'   collar 落後裸長 {c_ret - n_ret:+.2f}%（call 被軋 / put 純成本）')
    else:
        print(f'   collar vs 裸長 {c_ret - n_ret:+.2f}%（call premium ≈ put 成本）')
    if c_ret < p_ret - 2:
        print(f'   collar < put-only {c_ret - p_ret:+.2f}%：賣 call 限制了上漲（call 被深度 ITM）')
    elif c_ret > p_ret + 1:
        print(f'   collar > put-only {c_ret - p_ret:+.2f}%：call premium 是免費 yield')
    if abs(n_dd - c_dd) > 2:
        print(f'   最大回檔縮減 {abs(n_dd - c_dd):.1f}%（{n_dd:.1f}% → {c_dd:.1f}%）')
    print('━' * 60)

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
def _load_env() -> None:
    """讀 .env，避免直接依賴 shell 已 export。"""
    import os
    env_path = Path(__file__).resolve().parent / '.env'
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def fetch_tx_history_shioaji(days: int = 365) -> List[Tuple[date, float]]:
    """用 Shioaji api.kbars() 抓 TXFR1（連續契約）1-min K，重採成日 K。
    分塊抓避免單次請求太大。"""
    import os
    _load_env()
    import shioaji as sj
    print(f'[backtest] Shioaji login...', file=sys.stderr)
    api = sj.Shioaji()
    api.login(
        api_key=os.environ['SHIOAJI_API_KEY'],
        secret_key=os.environ['SHIOAJI_SECRET_KEY'],
        contracts_timeout=60_000,
    )
    # TXFR1 連續期貨
    contract = next((c for c in api.Contracts.Futures.TXF if c.symbol == 'TXFR1'), None)
    if not contract:
        api.logout()
        raise RuntimeError('TXFR1 contract not found')
    print(f'[backtest] contract: {contract.code} ({contract.symbol})', file=sys.stderr)

    from collections import OrderedDict
    daily: OrderedDict = OrderedDict()
    end = datetime.now().date()
    remaining = days
    chunk_count = 0
    while remaining > 0:
        chunk_days = min(60, remaining)
        start = end - timedelta(days=chunk_days)
        chunk_count += 1
        try:
            print(f'[backtest] chunk {chunk_count}: {start} → {end} ({chunk_days}d)...', file=sys.stderr)
            kb = api.kbars(contract=contract,
                           start=start.strftime('%Y-%m-%d'),
                           end=end.strftime('%Y-%m-%d'))
            ts_arr = list(kb.ts)
            close_arr = list(kb.Close)
            n_bars = len(ts_arr)
            new_days = set()
            for ts_ns, c in zip(ts_arr, close_arr):
                d = datetime.fromtimestamp(ts_ns / 1e9).date()
                daily[d] = float(c)   # last close of day overwrites
                new_days.add(d)
            print(f'[backtest]   got {n_bars} 1-min bars → {len(new_days)} unique days', file=sys.stderr)
        except Exception as e:
            print(f'[backtest] chunk failed: {e}', file=sys.stderr)
            break
        end = start - timedelta(days=1)
        remaining -= chunk_days

    out = sorted(daily.items())
    api.logout()
    return out


# ── CLI ───────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', help='Path to TX history CSV (date,close)')
    ap.add_argument('--shioaji', action='store_true', help='Fetch live TX history via Shioaji')
    ap.add_argument('--days', type=int, default=252, help='Synthetic series length')
    ap.add_argument('--lots', type=int, default=HEDGE_LOTS, help='Put/call hedge lots')
    ap.add_argument('--no-call', action='store_true', help='Disable sell-call leg (put-only)')
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

    res = run_backtest(prices, hedge_lots=args.lots, sell_call=not args.no_call)
    metrics = report(res)

    if args.save:
        import csv
        with open(args.save, 'w') as f:
            w = csv.writer(f)
            w.writerow(['date', 'tx', 'hv',
                        'put_strike', 'put_dte', 'put_value', 'put_cash_paid',
                        'call_strike', 'call_dte', 'call_liab', 'call_cash',
                        'long_value',
                        'equity_naked', 'equity_put_only', 'equity_collar'])
            for i, d in enumerate(res.dates):
                w.writerow([d.isoformat(), round(res.tx[i], 1), round(res.hv[i], 4),
                            res.put_strike[i], res.put_dte[i],
                            round(res.put_value[i]), round(res.put_cash_paid[i]),
                            res.call_strike[i], res.call_dte[i],
                            round(res.call_liab[i]), round(res.call_cash[i]),
                            round(res.long_value[i]),
                            round(res.equity_naked[i]),
                            round(res.equity_put_only[i]),
                            round(res.equity_collar[i])])
        print(f'[backtest] saved equity curve → {args.save}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
