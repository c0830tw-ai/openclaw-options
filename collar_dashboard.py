"""
collar_dashboard.py — collar 整合儀表板

針對用戶當前持倉計算：
  1. put leg 觸發條件（DTE、距現價、IV）
  2. call leg 觸發條件（σ 距、趨勢、DTE）
  3. 結構推薦（5 種變體選 1 + 理由）

純讀取已 enrich 的 result，不需 API。
"""
import math
from typing import Any, Dict, List, Optional


def _safe(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _eval_put_leg(data: Dict[str, Any]) -> Dict[str, Any]:
    pg_legs = _safe(data, 'portfolio_greeks', 'legs', default=[]) or []
    long_puts = [L for L in pg_legs
                 if L.get('right') == 'put' and (L.get('qty_signed') or 0) > 0]
    if not long_puts:
        return {
            'has_position': False,
            'triggers': [{'level': 'medium', 'msg': '🛡️ 沒有 long put — 失去下行保護'}],
        }

    # 取口數最多者代表
    leg = max(long_puts, key=lambda L: L.get('qty_signed') or 0)
    bs_s = _safe(data, 'market', 'tx_futures', default=0) or 0
    K    = float(leg.get('strike') or 0)
    if K <= 0 or bs_s <= 0:
        return {'has_position': True, 'triggers': [{'level': 'medium', 'msg': '資料不完整'}]}

    distance_pct = (bs_s - K) / bs_s * 100
    dte = leg.get('dte') or 0

    triggers: List[Dict[str, str]] = []
    if dte <= 5:
        triggers.append({'level': 'high',   'msg': f'⏰ DTE {dte}d ≤ 5，建議 roll 到遠月'})
    elif dte <= 10:
        triggers.append({'level': 'medium', 'msg': f'📅 DTE {dte}d，下週可開始準備 roll'})
    if distance_pct < 3:
        triggers.append({'level': 'high',   'msg': f'🚨 距現價 -{distance_pct:.1f}%（已被追到，下移履約）'})
    elif distance_pct < 5:
        triggers.append({'level': 'medium', 'msg': f'⚠️ 距現價 -{distance_pct:.1f}%（接近警戒）'})
    elif distance_pct > 12:
        triggers.append({'level': 'medium', 'msg': f'📉 距現價 -{distance_pct:.1f}%（保護薄，可上移）'})
    if not triggers:
        triggers.append({'level': 'ok', 'msg': '✓ 部位健康，無動作需求'})

    return {
        'has_position': True,
        'qty':          int(leg.get('qty_signed') or 0),
        'strike':       int(K),
        'dte':          dte,
        'distance_pct': round(distance_pct, 2),
        'iv':           round(leg.get('iv') or 0, 4),
        'triggers':     triggers,
    }


def _eval_call_leg(data: Dict[str, Any]) -> Dict[str, Any]:
    pg_legs = _safe(data, 'portfolio_greeks', 'legs', default=[]) or []
    short_calls = [L for L in pg_legs
                   if L.get('right') == 'call' and (L.get('qty_signed') or 0) < 0]
    if not short_calls:
        return {
            'has_position': False,
            'triggers': [{'level': 'ok', 'msg': '✓ 無 short call（put-only collar）'}],
        }

    leg = max(short_calls, key=lambda L: -(L.get('qty_signed') or 0))
    bs_s = _safe(data, 'market', 'tx_futures', default=0) or 0
    K    = float(leg.get('strike') or 0)
    if K <= 0 or bs_s <= 0:
        return {'has_position': True, 'triggers': [{'level': 'medium', 'msg': '資料不完整'}]}

    distance_pct = (K - bs_s) / bs_s * 100
    dte = leg.get('dte') or 0
    iv  = leg.get('iv') or 0.20

    # σ 距：BS 一年 vol × √T
    T = max(1, dte) / 365
    sigma_pts = bs_s * iv * math.sqrt(T)
    sigma_dist = (K - bs_s) / sigma_pts if sigma_pts > 0 else 99

    weekly_change  = _safe(data, 'trend', 'changes', 'vs_week_ago', 'tx_delta_pct', default=0) or 0
    monthly_change = _safe(data, 'trend', 'changes', 'vs_month_start', 'tx_delta_pct', default=0) or 0

    triggers: List[Dict[str, str]] = []
    if sigma_dist < 0.5:
        triggers.append({'level': 'high',   'msg': f'🚨 距現價僅 {sigma_dist:.2f}σ（被軋風險高，buyback）'})
    elif sigma_dist < 1.0:
        triggers.append({'level': 'medium', 'msg': f'⚠️ 距現價 {sigma_dist:.2f}σ（接近警戒）'})
    if weekly_change > 3:
        triggers.append({'level': 'high',   'msg': f'📈 TX 週漲 +{weekly_change:.1f}%（趨勢上漲，買回 call）'})
    elif monthly_change > 5:
        triggers.append({'level': 'medium', 'msg': f'📈 TX 月漲 +{monthly_change:.1f}%（注意動能）'})
    if dte <= 5:
        triggers.append({'level': 'medium', 'msg': f'⏰ DTE {dte}d，roll 或平倉'})
    if not triggers:
        triggers.append({'level': 'ok', 'msg': f'✓ 部位健康（{sigma_dist:.2f}σ），繼續收 theta'})

    return {
        'has_position':  True,
        'qty':           int(-(leg.get('qty_signed') or 0)),
        'strike':        int(K),
        'dte':           dte,
        'distance_pct':  round(distance_pct, 2),
        'sigma_distance': round(sigma_dist, 2),
        'iv':            round(iv, 4),
        'triggers':      triggers,
    }


def _recommend_structure(data: Dict[str, Any]) -> Dict[str, Any]:
    """從 5 種結構（symmetric / skewed / covered_call / defensive / protective_put）擇一推薦。
    決策因子：趨勢方向 + IV 高低 + 已知偏好（牛市別賣 call — 已驗證 backtest）"""
    iv = data.get('iv_used') or 0.20
    iv_pct = iv * 100 if iv < 1 else iv
    weekly  = _safe(data, 'trend', 'changes', 'vs_week_ago',    'tx_delta_pct', default=0) or 0
    monthly = _safe(data, 'trend', 'changes', 'vs_month_start', 'tx_delta_pct', default=0) or 0

    # 強上漲：趨勢牛市（已驗證 backtest，sell call 砍 30% 上漲）
    if monthly > 5 or weekly > 3:
        return {
            'structure': 'protective_put',
            'label':     '純保護 (protective_put)',
            'reason':    f'TX 月 {monthly:+.1f}% / 週 {weekly:+.1f}%（趨勢上漲）',
            'why':       '上漲中賣 call 必被軋（backtest 驗證砍 30% 上漲），只持 put',
            'conf':      'high',
        }

    # 強下跌：趨勢空頭
    if monthly < -5 or weekly < -3:
        return {
            'structure': 'defensive',
            'label':     '防禦 (defensive)',
            'reason':    f'TX 月 {monthly:+.1f}% / 週 {weekly:+.1f}%（趨勢下跌）',
            'why':       '下跌中加大 put 比例（half call vs full put），不錯失反彈',
            'conf':      'high',
        }

    # 高 IV + 盤整：賣方有利
    if iv_pct >= 30:
        return {
            'structure': 'skewed',
            'label':     '偏賣方 (skewed)',
            'reason':    f'IV {iv_pct:.1f}% 偏高 + 盤整',
            'why':       '高 IV 賣方收 vega 划算，多賣 put + 平衡 call',
            'conf':      'medium',
        }

    # 低 IV：保險便宜，純買 put
    if iv_pct < 20:
        return {
            'structure': 'protective_put',
            'label':     '純保護 (protective_put)',
            'reason':    f'IV {iv_pct:.1f}% 偏低',
            'why':       '低 IV 保險便宜，賣方收益薄；等 IV 回升再加 sell',
            'conf':      'medium',
        }

    # 中 IV + 盤整：對稱 collar
    return {
        'structure': 'symmetric',
        'label':     '對稱 (symmetric)',
        'reason':    f'盤整 (TX ±{abs(monthly):.1f}%) + IV {iv_pct:.1f}%',
        'why':       '對稱 collar：put 保護 + call 收 premium 平衡',
        'conf':      'medium',
    }


def evaluate(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not data:
        return None
    return {
        'put_leg':                _eval_put_leg(data),
        'call_leg':               _eval_call_leg(data),
        'recommended_structure':  _recommend_structure(data),
    }
