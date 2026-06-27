# Codex 任務：把強弱評分整合進選擇權 App

## 目標
把 `strength_rules.py` 接到現有選擇權 dashboard / Firebase payload。

目前後端主檔 `shioaji_collar.py` 已經會計算：

- RSI
- KD
- MACD / MACD signal / MACD hist
- MA20 / MA60
- ADX
- Bollinger Band
- Regime Detector

這次要新增的是「強弱判斷」，用來回答：

> 現在是強勢、偏強、盤整、偏弱、弱勢？  
> 5 分K訊號該當進場、拉回、反彈，還是避險？

---

## 新增模組
已新增：

```text
strength_rules.py
```

主要函式：

```python
compute_strength_score(...)
summarize_multi_timeframe_strength(...)
```

---

## 建議後端整合點

### 1. 在 `shioaji_collar.py` import

```python
from strength_rules import compute_strength_score, summarize_multi_timeframe_strength
```

---

### 2. 在 `fetch_kbars()` 的 result 裡新增日K強弱

`fetch_kbars()` 目前已有：

```python
result = {
    'atr': atr,
    'bb_upper': bb_u,
    'bb_lower': bb_l,
    'ma20': ma,
    'hv': hv,
    'adx': adx,
    'days': n_days,
    'rsi': _calc_rsi(closes),
    'closes': closes,
    'dates': daily['dates'],
}
```

建議在 KD / MACD 算完後加：

```python
try:
    result['strength'] = compute_strength_score(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=getattr(kbars, 'Volume', None) or getattr(kbars, 'volume', None),
        rsi=result.get('rsi'),
        macd=result.get('macd'),
        macd_hist=result.get('macd_hist'),
        ma20=result.get('ma20'),
    )
    log.info(
        f"{stock_code} 強弱: {result['strength']['emoji']} "
        f"{result['strength']['label']} "
        f"{result['strength']['score']}/{result['strength']['max_score']}"
    )
except Exception as _se:
    log.warning(f'{stock_code} strength 計算失敗（{_se}），略過')
```

注意：目前 `_MergedKbars` 只保留 `Open/High/Low/Close`，如果要完整 OBV，需要把 `Volume` 一起合併。

---

### 3. 修改 `_MergedKbars` 支援 Volume

目前：

```python
__slots__ = ('ts', 'Open', 'High', 'Low', 'Close')
```

建議改成：

```python
__slots__ = ('ts', 'Open', 'High', 'Low', 'Close', 'Volume')
```

初始化：

```python
self.ts, self.Open, self.High, self.Low, self.Close, self.Volume = [], [], [], [], [], []
```

合併時加：

```python
vols = getattr(kb, 'Volume', None) or getattr(kb, 'volume', None) or []
merged.Volume.append(vols[i] if i < len(vols) else 0)
```

排序時也同步排序：

```python
merged.Volume = [merged.Volume[i] for i in order]
```

---

### 4. 60 / 30 / 5 分K整合

如果目前 TX `fetch_hv_tx()` 已有：

```python
closes_30m = _resample_intraday(kbars, 30)
closes_60m = _resample_intraday(kbars, 60)
```

建議進一步做：

```python
strength_60m = compute_strength_score(closes=closes_60m)
strength_30m = compute_strength_score(closes=closes_30m)
# 若有 5m resample：
closes_5m = _resample_intraday(kbars, 5)
strength_5m = compute_strength_score(closes=closes_5m)

put_refs['strength_mtf'] = summarize_multi_timeframe_strength(
    strength_60m=strength_60m,
    strength_30m=strength_30m,
    strength_5m=strength_5m,
)
```

若 5 分K資料不足，前端仍可先顯示 60 / 30 分K。

---

## 前端顯示建議

新增卡片名稱：

```text
強弱判斷
```

欄位：

```text
🟢 強勢 7/9
方向：bullish
建議：順勢偏多；Pullback 優先找多點，不宜逆勢賣 Call 太近。
```

展開後顯示 9 個檢查項：

1. 價格在 MA20 上方
2. MA20 上彎
3. 高點墊高
4. 低點墊高
5. RSI > 50
6. MACD 在 0 軸上方
7. MACD 柱狀體轉正或放大
8. OBV 突破前高或墊高
9. 突破量能放大

---

## 選擇權策略對應

| 強弱 | App 建議 |
|---|---|
| 強勢 7-9 | 偏多；BPS 可做但賣 Put 不要太近；Covered Call 不要壓太近 |
| 偏強 5-6 | 等回測不破；可小倉 BPS / Bull Call Spread |
| 盤整 3-4 | 箱型、KD、布林；可考慮 IC，但注意事件與 IV |
| 偏弱 1-2 | 補 Put / Put Spread；減少賣 Put |
| 弱勢 0 | 優先防守；停止賣 Put，偏向買 Put / Bear Put Spread |

---

## 驗收條件

1. `python -m py_compile strength_rules.py` 通過。
2. 現有 `shioaji_collar.py` 執行不因 strength 欄位失敗而中斷。
3. Firebase latest payload 裡出現：

```json
{
  "strength": {
    "score": 7,
    "max_score": 9,
    "label": "強勢",
    "bias": "bullish",
    "emoji": "🟢",
    "action": "...",
    "checks": {}
  }
}
```

4. dashboard 顯示強弱分數與操作建議。
5. 若 OBV/Volume 缺資料，分數仍可運作，只是 OBV 與量能項不加分。
