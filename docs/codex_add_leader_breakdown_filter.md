# Codex 任務：把主流股跌破月線風險濾網整合進選擇權 App

## 目標

把本次盤勢分析轉成 App 可用的「主流股 / 千金股風險濾網」。

核心問題：

> 當多檔台股千金股跌破月線，甚至月線下彎時，0050 股期與選擇權策略是否應該從進攻切到防守？

這不是立刻判斷空頭，而是用來偵測：

- 主流高價股籌碼是否鬆動
- AI / 半導體 / PCB / 散熱 / CPO / 設備 / 測試介面等領漲族群是否退潮
- 0050 股期是否不宜加碼
- Covered Call、Put Spread、降槓桿是否應提高權重

---

## 新增模組

已新增：

```text
leader_risk_rules.py
```

主要函式：

```python
compute_leader_breakdown_risk(...)
classify_leader_risk(...)
```

---

## 使用方式

### 1. 後端 import

```python
from leader_risk_rules import compute_leader_breakdown_risk
```

---

### 2. 準備千金股 / 主流股清單

建議先放 App 設定檔或後端常數：

```python
LEADER_STOCKS = [
    "2330",  # 台積電
    "6669",  # 緯穎
    "3653",  # 健策
    "2454",  # 聯發科
    "2382",  # 廣達
    "3017",  # 奇鋐
    "3008",  # 大立光
    # 其他千金股 / AI / 半導體 / PCB / 散熱 / CPO / 設備股
]
```

實作時不一定只限千金股。更好的版本是分成：

| 分類 | 用途 |
|---|---|
| 權值核心 | 台積電、聯發科、台達電等 |
| AI 伺服器 | 緯穎、廣達、技嘉、川湖等 |
| 散熱 / PCB / CPO | 奇鋐、健策、台光電、聯亞等 |
| 高價股 / 千金股 | 偵測市場風險偏好 |

---

### 3. 餵給函式的資料格式

每檔股票需要至少：

```python
{
    "symbol": "6669",
    "name": "緯穎",
    "close": 3000,
    "ma20": 3100,
    "ma20_slope": -1,
    "sector": "AI",
    "role": "leader",
}
```

`ma20_slope` 建議定義：

```python
ma20_slope = ma20_today - ma20_yesterday
```

- `> 0`：月線上彎
- `= 0`：月線走平
- `< 0`：月線下彎

---

### 4. 可選：加入加權指數 / 0050 狀態

```python
index_state = {
    "symbol": "TAIEX",
    "close": taiex_close,
    "ma20": taiex_ma20,
    "ma20_slope": taiex_ma20 - taiex_prev_ma20,
    "ma60": taiex_ma60,
}

core_etf_state = {
    "symbol": "0050",
    "close": etf_0050_close,
    "ma20": etf_0050_ma20,
    "ma20_slope": etf_0050_ma20 - etf_0050_prev_ma20,
    "ma60": etf_0050_ma60,
}
```

---

## 建議後端整合點

在 `shioaji_collar.py` 或 dashboard payload 產生處加入：

```python
leader_risk = compute_leader_breakdown_risk(
    leaders=leader_states,
    index_state=taiex_state,
    core_etf_state=etf_0050_state,
)

put_refs["leader_risk"] = leader_risk
```

如果目前只先支援單一標的，也可以先把這個濾網獨立顯示，不影響原有策略判斷。

---

## 前端顯示建議

新增卡片：

```text
主流股風險濾網
```

顯示範例：

```text
🟠 偏高風險 63/100
千金股 / 主流股群體轉弱：0050 股期不加碼，槓桿降到防守區，優先 Put Spread 與部分 Covered Call。

跌破月線：18 / 30
月線下彎：14 / 30
跌破月線且月線下彎：10 / 30
0050：跌破月線 / 月線下彎
```

展開後顯示：

```text
弱勢主流股：緯穎、健策、奇鋐、台光電、...
建議槓桿：0050 股期實際槓桿 2～3 倍以內
建議避險：槓桿超過 3 倍，加 Put Spread；反彈靠近月線可賣部分 Covered Call
```

---

## 策略對應規則

| 風險狀態 | App 建議 |
|---|---|
| 正常 | 可依原本強弱評分操作，但仍控總槓桿 |
| 中性偏防守 | 不重壓；等待主流股站回月線 |
| 偏高風險 | 0050 股期不加碼；槓桿壓到 2～3 倍；加 Put Spread |
| 高風險 | 先降口數；槓桿壓到 2 倍以下；停止賣近價 Put |

---

## 0050 股期重壓時的 App 提醒文字

當 `leader_risk.bias` 為 `defensive` 或 `risk_off` 時，App 應提示：

```text
主流股轉弱，不建議重壓 0050 股期。
Covered Call 只能降低成本，不是崩盤避險。
若實際槓桿超過 3 倍，應加 Put Spread 或先降口數。
```

---

## 與既有 strength_rules.py 的關係

目前 `strength_rules.py` 已處理：

- 單一標的強弱
- MA20 位置
- MA20 上彎 / 下彎
- 高低點結構
- RSI / MACD / OBV / 量能
- 60 / 30 / 5 分K多週期整合

新增的 `leader_risk_rules.py` 是更上一層：

```text
strength_rules.py：判斷單一標的 / 單一週期強弱
leader_risk_rules.py：判斷主流股群體是否退潮
```

實務上應該兩個一起看：

| 狀況 | 解讀 |
|---|---|
| 0050 強，主流股風險正常 | 可偏多操作 |
| 0050 強，但主流股風險偏高 | 不追價，Covered Call 不賣太近，Put 不歸零 |
| 0050 弱，主流股風險偏高 | 降槓桿 + Put Spread |
| 0050 弱，主流股風險高 | 先風控，不做收租優先 |

---

## 交易解讀原則

本濾網不是叫 App 自動下單，而是用來讓 dashboard 避免在錯誤盤勢給出過度進攻建議。

核心邏輯：

```text
千金股跌破月線
+ 月線下彎
+ 0050 / 加權指數也轉弱
= 主流股籌碼鬆動，0050 股期不加碼，優先防守。
```

風控優先順序：

```text
1. 降低實際槓桿
2. 預留保證金與現金
3. Put Spread 防大跌
4. Covered Call 降低成本
5. 反彈站回月線後再恢復進攻
```

---

## 待辦

- [ ] 在後端建立 `LEADER_STOCKS` 清單。
- [ ] 對每檔 leader 計算 close、MA20、MA20 slope、MA60。
- [ ] 在 dashboard payload 新增 `leader_risk`。
- [ ] 前端新增「主流股風險濾網」卡片。
- [ ] 當 `leader_risk.bias in ("defensive", "risk_off")` 時，降低 App 對賣 Put、重壓 0050 股期的建議權重。
- [ ] 增加回測：比較有 / 沒有 leader risk filter 時，0050 股期 + Covered Call + Put Spread 的回撤差異。
