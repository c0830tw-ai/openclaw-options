"""
events_sync.py — 自動抓取/估算高影響事件，寫入 events_auto.json

來源：
  - FOMC：scrape federalreserve.gov（穩定，真實日期）
  - 美國 CPI：用「每月第二個週三」公式估算（BLS 實際公布可能 ±1-3 天）
  - 台積電法說：仍由 events.json 手動維護（4 次/年，未自動）

CLI：
  python3 events_sync.py        # 抓當年 + 明年，覆寫 events_auto.json
"""
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple


_HERE = Path(__file__).resolve().parent
OUT_PATH = _HERE / 'events_auto.json'

FED_URL = 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'
TWSE_ANNOUNCE_URL = 'https://openapi.twse.com.tw/v1/opendata/t187ap04_L'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}

_MONTHS = {m: i + 1 for i, m in enumerate([
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
])}
_MONTH_PAT = '|'.join(_MONTHS.keys())


def _http_get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', errors='ignore')


def fetch_fomc(min_year: int = None) -> List[Dict[str, Any]]:
    """爬 Fed FOMC 行事曆。失敗回空 list。"""
    if min_year is None:
        min_year = datetime.now().year
    try:
        html = _http_get(FED_URL)
    except Exception as e:
        print(f'[fomc] fetch failed: {e}', file=sys.stderr)
        return []

    # 頁面結構：「20XX FOMC Meetings」標題之間是該年的會議區塊
    parts = re.split(r'(\d{4})\s+FOMC\s+Meetings', html)
    if len(parts) < 3:
        print('[fomc] pattern not matched (Fed 改版？)', file=sys.stderr)
        return []

    out: List[Dict[str, Any]] = []
    # 必須 day-range（如 "January 27-28"）以排除 "(Released February 18, 2026)" 公告日
    date_pat = re.compile(rf'\b({_MONTH_PAT})\s+(\d{{1,2}})[–—\-/](\d{{1,2}})\b')
    for i in range(1, len(parts) - 1, 2):
        try:
            year = int(parts[i])
        except ValueError:
            continue
        if year < min_year:
            continue
        content = parts[i + 1]
        # 把 HTML 標籤先剝掉，避免 "Released MM DD, YYYY" 仍可能殘留
        text = re.sub(r'<[^>]+>', ' ', content)
        text = re.sub(r'\s+', ' ', text)
        # 截斷到下一個年度標題（防 2028 dates 從 2027 區塊洩漏）
        nx = re.search(r'\d{4}\s+FOMC\s+Meetings', text)
        if nx:
            text = text[:nx.start()]

        for m in date_pat.finditer(text):
            month_name = m.group(1)
            day = int(m.group(3))   # 取兩天會議的第二天（決議日）
            try:
                dt = date(year, _MONTHS[month_name], day)
            except (ValueError, KeyError):
                continue
            out.append({
                'date':    dt.strftime('%Y-%m-%d'),
                'type':    'fomc',
                'name':    f'FOMC 利率決議（{month_name[:3]}）',
                'impact':  'high',
                'iv_risk': 'high',
                'note':    'Fed 利率政策；台北時間凌晨 02:00 Powell 記者會',
                '_source': 'fed_scraped',
            })

    # 同月 FOMC 不可能有兩次：dedup by (year, month)，保留較晚的日期
    by_month: Dict[str, Dict[str, Any]] = {}
    for ev in out:
        key = ev['date'][:7]  # YYYY-MM
        if key not in by_month or ev['date'] > by_month[key]['date']:
            by_month[key] = ev
    deduped = sorted(by_month.values(), key=lambda e: e['date'])
    print(f'[fomc] scraped {len(deduped)} dates from Fed', file=sys.stderr)
    return deduped


def _second_wednesday(year: int, month: int) -> date:
    """每月第二個週三（美國 CPI 典型公布日）。"""
    d = date(year, month, 1)
    while d.weekday() != 2:           # 0=Mon, 2=Wed
        d += timedelta(days=1)
    return d + timedelta(days=7)


def estimate_us_cpi_dates(year: int) -> List[Dict[str, Any]]:
    """美國 CPI 估算：每月第二個週三公布前一月份數據。
    BLS 實際 schedule 可能 ±1-3 天；用戶可在 events.json 手動覆蓋。"""
    out: List[Dict[str, Any]] = []
    for month in range(1, 13):
        d = _second_wednesday(year, month)
        target_month = month - 1 if month > 1 else 12
        out.append({
            'date':    d.strftime('%Y-%m-%d'),
            'type':    'cpi_us',
            'name':    f'美國 CPI ({target_month} 月) [估算]',
            'impact':  'high',
            'iv_risk': 'medium',
            'note':    '日期為估算（每月第二個週三）；實際 BLS 行事曆可能 ±1-3 天',
            '_source': 'us_cpi_estimated',
        })
    return out


def _roc_to_date(roc_y: str, m: str, d: str) -> Optional[date]:
    """民國年（115）→ 西元（2026）。"""
    try:
        return date(int(roc_y) + 1911, int(m), int(d))
    except (ValueError, TypeError):
        return None


def _extract_dates_from_text(text: str) -> List[date]:
    """從說明文字抽出所有可能的日期（ROC 與 AD 兩種格式）。"""
    out: List[date] = []
    # ROC: 115/05/07 或 115年5月7日
    for m in re.finditer(r'(\d{2,3})\s*[/年]\s*(\d{1,2})\s*[/月]\s*(\d{1,2})\s*日?', text):
        d = _roc_to_date(m.group(1), m.group(2), m.group(3))
        if d:
            out.append(d)
    # AD: 2026/05/07 或 2026年5月7日 (4 位數年)
    for m in re.finditer(r'(\d{4})\s*[/年]\s*(\d{1,2})\s*[/月]\s*(\d{1,2})\s*日?', text):
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    return out


def fetch_tsmc_conferences(today: date = None) -> List[Dict[str, Any]]:
    """從 TWSE OpenAPI 抓 2330 重大訊息，過濾「法人說明會」字樣，
    解析未來日期。注意：API 只回當日重大訊息，需要每日累積。"""
    if today is None:
        today = datetime.now().date()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(TWSE_ANNOUNCE_URL, headers=HEADERS),
            timeout=15,
        ) as resp:
            announcements = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f'[tsmc] fetch failed: {e}', file=sys.stderr)
        return []

    out: List[Dict[str, Any]] = []
    seen_dates: set = set()
    for a in announcements:
        if a.get('公司代號') != '2330':
            continue
        subject = (a.get('主旨 ') or a.get('主旨') or '').strip()
        body    = (a.get('說明') or '').strip()
        if not any(k in subject + body for k in ('法人說明會', '法說會')):
            continue
        # 抽日期，留下未來的
        dates = _extract_dates_from_text(body)
        future = [d for d in dates if d >= today]
        if not future:
            continue
        # 取最遠的（通常 body 含發布日期 + 會議日期，取較晚那個）
        conf_d = max(future)
        if conf_d in seen_dates:
            continue
        seen_dates.add(conf_d)
        out.append({
            'date':    conf_d.strftime('%Y-%m-%d'),
            'type':    'earnings',
            'name':    '2330 法人說明會',
            'impact':  'high',
            'iv_risk': 'high',
            'note':    f'TSMC 法說（自 TWSE 重大訊息抓取）：{subject[:60]}',
            '_source': 'twse_2330_scraped',
        })
    print(f'[tsmc] 找到 {len(out)} 筆未來法說會 (從今日 {len(announcements)} 筆重大訊息)', file=sys.stderr)
    return out


def _load_existing_persisted(out_path: Path, sources_to_keep: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """讀現有 events_auto.json，只保留指定 _source 的未來事件。
    用途：每日 sync 不要把昨天抓到的 TSMC 法說洗掉。"""
    if not out_path.exists():
        return []
    try:
        raw = json.loads(out_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return []
    today_str = datetime.now().strftime('%Y-%m-%d')
    keep = []
    for ev in raw.get('events') or []:
        if ev.get('_source') not in sources_to_keep:
            continue
        if (ev.get('date') or '') < today_str:
            continue
        keep.append(ev)
    return keep


def sync(out_path: Path = OUT_PATH, years: Tuple[int, ...] = None) -> int:
    """執行所有來源，寫入 events_auto.json。回傳事件總筆數。"""
    if years is None:
        cur = datetime.now().year
        # 包含過去 1 年（給 event_analysis 用）+ 當年 + 明年
        years = (cur - 1, cur, cur + 1)
    events: List[Dict[str, Any]] = []

    # 保留先前抓到的 TSMC 法說（API 只回當日，需要持久化）
    persisted_tsmc = _load_existing_persisted(out_path, sources_to_keep=('twse_2330_scraped',))
    if persisted_tsmc:
        print(f'[events_sync] 保留 {len(persisted_tsmc)} 筆先前抓到的 TSMC 法說', file=sys.stderr)
    events += persisted_tsmc

    print(f'[events_sync] FOMC scrape ({min(years)}+)...', file=sys.stderr)
    events += fetch_fomc(min_year=min(years))

    for y in years:
        print(f'[events_sync] US CPI estimated ({y})...', file=sys.stderr)
        events += estimate_us_cpi_dates(y)

    print(f'[events_sync] TSMC 2330 法說（TWSE 重大訊息）...', file=sys.stderr)
    new_tsmc = fetch_tsmc_conferences()
    # 合併新舊：dedup by (date, name)
    existing_keys = {(e['date'], e.get('name')) for e in events}
    for ev in new_tsmc:
        if (ev['date'], ev.get('name')) not in existing_keys:
            events.append(ev)

    # Sort by date
    events.sort(key=lambda e: e['date'])

    payload = {
        '_comment':  '由 events_sync.py 自動產生；勿手動修改。手動事件改維護 events.json',
        '_fetched':  datetime.now().isoformat(timespec='seconds'),
        '_sources':  ['fed_scraped', 'us_cpi_estimated', 'twse_2330_scraped'],
        'events':    events,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[events_sync] wrote {len(events)} events → {out_path}', file=sys.stderr)
    return len(events)


if __name__ == '__main__':
    n = sync()
    sys.exit(0 if n > 0 else 1)
