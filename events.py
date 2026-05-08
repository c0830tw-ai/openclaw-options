"""
events.py — 讀取 events.json，回傳即將到來的高影響事件清單

事件來源：events.json（手動維護，倉庫內）
事件用途：
  1. UI 顯示未來 N 天事件 timeline
  2. alerts.py 在事件前 3-5 天 push「IV spike 風險」通知

唯讀模組，不修改外部狀態。
"""
import json
import os
from datetime import datetime, date
from typing import List, Dict, Any, Optional


_HERE = os.path.dirname(os.path.abspath(__file__))
_EVENTS_PATH = os.path.join(_HERE, 'events.json')         # 手動維護
_AUTO_PATH   = os.path.join(_HERE, 'events_auto.json')    # events_sync.py 產生


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y%m%d'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _load_one(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    out: List[Dict[str, Any]] = []
    for ev in raw.get('events') or []:
        d = _parse_date(ev.get('date', ''))
        if not d:
            continue
        out.append({**ev, '_date': d})
    return out


def load_events(path: str = _EVENTS_PATH) -> List[Dict[str, Any]]:
    """讀 events.json + events_auto.json，合併並去重。
    優先序：手動 events.json > 自動 events_auto.json。
    同月份同類型（如同月 cpi_us）只保留手動版本，避免估算重複出現。"""
    out: List[Dict[str, Any]] = []
    seen_exact: set = set()           # (date, name) 完全相同
    seen_type_month: set = set()      # (type, year-month) 用於同月衝突

    for p in (path, _AUTO_PATH):
        for ev in _load_one(p):
            d = ev['_date']
            ek = (d, ev.get('name', ''))
            if ek in seen_exact:
                continue
            tm = (ev.get('type', ''), d.year, d.month)
            # 對 cpi_us / fomc / cpi_tw 這類「每月最多一次」事件做月去重
            if ev.get('type', '') in ('cpi_us', 'fomc', 'cpi_tw') and tm in seen_type_month:
                continue
            seen_exact.add(ek)
            seen_type_month.add(tm)
            out.append(ev)
    out.sort(key=lambda e: e['_date'])
    return out


def upcoming(window_days: int = 14, today: Optional[date] = None,
             path: str = _EVENTS_PATH) -> List[Dict[str, Any]]:
    """回傳今日起 window_days 內、尚未發生的事件（依日期升序）。
    每筆加 days_until 欄位（0 = 今日，1 = 明天）。"""
    today = today or datetime.now().date()
    events = load_events(path)
    out: List[Dict[str, Any]] = []
    for ev in events:
        delta = (ev['_date'] - today).days
        if delta < 0 or delta > window_days:
            continue
        out.append({
            'date':       ev['_date'].strftime('%Y-%m-%d'),
            'days_until': delta,
            'type':       ev.get('type', 'other'),
            'name':       ev.get('name', ''),
            'impact':     ev.get('impact', 'medium'),
            'iv_risk':    ev.get('iv_risk', 'medium'),
            'note':       ev.get('note', ''),
            'source':     ev.get('_source', 'manual'),
        })
    out.sort(key=lambda e: e['days_until'])
    return out
