"""
broker_sync.py — Shioaji 真實持倉同步（read-only）

需要 .env 設好：
  SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY  (login 用)
  SHIOAJI_CA_PATH / SHIOAJI_CA_PASSWD   (activate_ca 用)

未設 CA 變數則 fetch_all_positions 只回現股部分。
"""
import os
import re
import logging
from typing import Dict, Any, List, Optional

log = logging.getLogger('collar.broker')


# ─── Shioaji 代號 → 中文/類別映射 ──────────────────────────────────
def parse_code(code: str) -> Dict[str, str]:
    """Shioaji symbol → {category, name, family}。
    無法辨識的回 raw code，category=unknown。"""
    if not code:
        return {'category': 'unknown', 'name': code, 'family': '?'}

    c = code.upper()

    # 大盤期權
    if c.startswith('TXF'):
        return {'category': 'index_futures',  'name': '台指期', 'family': 'TXF'}
    if c.startswith('MXF'):
        return {'category': 'index_futures',  'name': '小台指', 'family': 'MXF'}
    if c.startswith('TXO'):
        return {'category': 'index_option',   'name': '台指選擇權（月）', 'family': 'TXO'}
    if c[:2] == 'TX' and len(c) > 3 and c[2] in '124':
        return {'category': 'index_option',   'name': f'台指選擇權（週三 {c[2]}）', 'family': c[:3]}
    if c[:3] in ('TXU', 'TXV', 'TXX', 'TXY', 'TXZ'):
        return {'category': 'index_option',   'name': '台指選擇權（週五）', 'family': c[:3]}

    # 個股期貨
    stock_futures_map = {
        'CDF': '大台積電期',
        'QFF': '小台積電期',
        'NYF': '0050 ETF期',
        'QNF': '小聯詠期',
        'GXF': '股期 GXF',  # 等待用戶確認
        'MKF': '股期 MKF',
        'RGF': '股期 RGF',
    }
    if len(c) >= 3 and c[:3] in stock_futures_map:
        return {
            'category': 'stock_futures',
            'name':     stock_futures_map[c[:3]],
            'family':   c[:3],
        }

    # 現股代碼（4-5 位數字 ±字母後綴；00981A、00679B 等 5 位 ETF）
    if re.match(r'^\d{4,5}[A-Z]?$', c):
        # 常見台股可加中文名映射
        name_map = {
            '0050': '元大台灣 50',
            '0056': '元大高股息',
            '00981A': '主動統一台股增長',
            '00679B': '元大美債 20 年',
            '00687B': '國泰美債 20 年',
            '00697B': '元大美債 7-10 年',
            '00720B': '元大投資級公司債',
            '00725B': '國泰投資級公司債',
            '00772B': '中信高評級公司債',
            '00773B': '中信優先金融債',
            '00937B': '群益 ESG 投等債',
            '2330': '台積電',
            '1432': '大魯閣',
            '3653': '健鼎',
            '6757': '台灣虎航',
            '2603': '長榮',
            '3034': '聯詠',
            '3373': '健策',
        }
        # 債券 ETF（代碼結尾 'B'）不屬於台股 long-side hedge 範疇
        is_bond = bool(re.match(r'^\d{4,5}B$', c))
        return {
            'category': 'bond_etf' if is_bond else 'cash_stock',
            'name':     name_map.get(c, code),
            'family':   'bond' if is_bond else 'stock',
        }

    return {'category': 'unknown', 'name': code, 'family': c[:3] if len(c) >= 3 else c}


# ─── CA activate ───────────────────────────────────────────────────
def activate_ca_if_configured(api) -> bool:
    """若環境有 CA 設定就 activate；回 True 表示已 active。"""
    ca_path   = os.environ.get('SHIOAJI_CA_PATH')
    ca_passwd = os.environ.get('SHIOAJI_CA_PASSWD')
    if not ca_path or not ca_passwd:
        log.info('CA 未設定（broker positions 將僅含現股）')
        return False
    try:
        api.activate_ca(ca_path=ca_path, ca_passwd=ca_passwd)
        log.info('CA 已 activate')
        return True
    except Exception as e:
        log.warning(f'CA activate 失敗（{e}）')
        return False


# ─── Fetch positions from all accessible accounts ──────────────────
def fetch_all_positions(api) -> List[Dict[str, Any]]:
    """掃所有 signed=True 帳戶，aggregate 成統一格式 list."""
    out: List[Dict[str, Any]] = []
    me_person_id = None

    try:
        accts = api.list_accounts()
    except Exception as e:
        log.warning(f'list_accounts 失敗: {e}')
        return out

    # 先抓 person_id（從 stock_account），只抓本人帳戶
    try:
        me_person_id = getattr(api.stock_account, 'person_id', None)
    except Exception:
        pass

    for a in accts:
        person_id = getattr(a, 'person_id', None)
        broker_id = getattr(a, 'broker_id', '?')
        acct_id   = getattr(a, 'account_id', '?')
        signed    = getattr(a, 'signed', False)
        if me_person_id and person_id != me_person_id:
            continue
        if not signed:
            log.debug(f'skip unsigned account {broker_id}-{acct_id}')
            continue

        try:
            positions = api.list_positions(a)
        except Exception as e:
            log.warning(f'list_positions({broker_id}-{acct_id}) 失敗: {e}')
            continue

        for p in positions:
            out.append(_normalize(p, broker_id, acct_id))

    log.info(f'broker_sync: 共抓到 {len(out)} 筆持倉')
    return out


def _normalize(p, broker_id: str, acct_id: str) -> Dict[str, Any]:
    code  = getattr(p, 'code', '?')
    info  = parse_code(code)
    direc = str(getattr(p, 'direction', '')).split('.')[-1]
    return {
        'broker_account': f'{broker_id}-{acct_id}',
        'code':       code,
        'category':   info['category'],
        'name':       info['name'],
        'family':     info['family'],
        'direction':  direc,
        'quantity':   getattr(p, 'quantity', 0),
        'price':      float(getattr(p, 'price', 0) or 0),
        'last_price': float(getattr(p, 'last_price', 0) or 0),
        'pnl':        float(getattr(p, 'pnl', 0) or 0),
        'yd_quantity': getattr(p, 'yd_quantity', None),  # stock 才有
    }


# ─── Aggregation summary ───────────────────────────────────────────
def summary(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """彙總給 UI 顯示用。"""
    if not positions:
        return None

    total_pnl = sum(p['pnl'] for p in positions)
    by_cat: Dict[str, Dict[str, Any]] = {}
    for p in positions:
        cat = p['category']
        if cat not in by_cat:
            by_cat[cat] = {'count': 0, 'pnl': 0.0, 'items': []}
        by_cat[cat]['count'] += 1
        by_cat[cat]['pnl']   += p['pnl']
        by_cat[cat]['items'].append(p)

    return {
        'total_positions': len(positions),
        'total_pnl':       round(total_pnl, 0),
        'by_category':     {
            k: {
                'count': v['count'],
                'pnl':   round(v['pnl'], 0),
            }
            for k, v in by_cat.items()
        },
        'positions': positions,
    }
