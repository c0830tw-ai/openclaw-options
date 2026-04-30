# collar-trading — OpenClaw 選擇權領口策略工具

## 專案目的

自動抓取台積電（2330）、0050 現價與 TXO 選擇權鏈，計算最佳領式（Collar）結構，推送到 Firebase，並透過本地網頁即時顯示。

持倉假設：
- **2330**：持有現股，對應 1 口大台股期（股本 2000 股）
- **0050**：持有 2 口 0050 ETF 股期（每口 10,000 單位）
- **TXO**：以 TAIEX 選擇權做 beta 調整後的領式避險

---

## 目錄結構

```
collar-trading/
├── shioaji_collar.py   # 主腳本：抓資料 → 計算 → 推 Firebase
├── server.py           # 本地 HTTP server，瀏覽器開啟時自動觸發腳本
├── probe_shioaji.py    # 診斷腳本：印出 Shioaji 合約結構與欄位
├── run_collar.sh       # 手動執行包裝（載入 .env 後跑主腳本）
├── web/
│   └── index.html      # React 前端（CDN，無需 build）
├── .env                # 金鑰（gitignored）
├── .env.example        # 環境變數範本
├── firebase-key.json   # Firebase service account（gitignored）
└── logs/               # collar.log（gitignored）
```

---

## 環境設定

```bash
# 安裝依賴
python3 -m pip install shioaji firebase-admin

# 複製並填入金鑰
cp .env.example .env
# 填入 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY / FIREBASE_URL
# 放入 firebase-key.json（從 Firebase Console 下載 service account）
```

`.env` 必要欄位：
```
SHIOAJI_API_KEY=...
SHIOAJI_SECRET_KEY=...
FIREBASE_CRED=./firebase-key.json
FIREBASE_URL=https://option-3047e-default-rtdb.asia-southeast1.firebasedatabase.app
```

---

## 啟動方式

```bash
# 啟動本地 server（瀏覽器開啟時自動抓資料）
python3 server.py

# 然後開瀏覽器
open http://localhost:8080
```

- 開啟頁面時立即觸發一次 `shioaji_collar.py`
- 網頁開著時每 5 分鐘自動重抓
- 關閉瀏覽器後停止

手動單次執行：
```bash
export $(grep -v '^#' .env | xargs) && python3 shioaji_collar.py
# 或
./run_collar.sh
```

---

## 核心策略邏輯（shioaji_collar.py）

### 履約價選擇（動態）

1. 抓 2330 日K（從 1 分鐘 K 棒重採樣）
2. 計算 ATR(14)、Bollinger Band(20)、歷史波動率(HV)
3. DTE 縮放 ATR 乘數（DTE 越短乘數越小）：
   ```
   Put 目標 = min(BB下軌, 現價 - mult×ATR)
   Call 目標 = max(BB上軌, 現價 + mult×ATR)
   ```
4. 將 2330 目標換算成 TAIEX 點位（via beta）
5. 用 Black-Scholes 過濾：選 abs(delta) ≤ 0.10 的合約
6. kbar 失敗時退回靜態備援（protect_pct=-15.7%, cap_pct=+10%）

### 口數計算（beta 調整）

```
名目價值 = 口數 × 每口股數 × 現價
beta調整比例 = 名目 × beta / (TAIEX × 50)
```

- 2330（1口大台，2000股）：beta_ratio ≈ 2.6 → 2–3 口 TXO
- 0050（2口股期，10,000單位/口）：beta_ratio ≈ 0.9 → 1 口 TXO

### 三種結構

| 名稱 | 組合 | 特性 |
|---|---|---|
| symmetric | NC/NP 對稱 | 最高保護，最低 credit |
| skewed | 2C/1P 偏賣方 | 中度保護，較高 credit |
| covered_call | 純 Covered Call | 無保護，最高 credit |

---

## Shioaji API 注意事項

| 項目 | 正確用法 | 錯誤（已修） |
|---|---|---|
| TXO 合約 | `api.Contracts.Options.TXO`，用 `delivery_month` 篩月份 | `api.Contracts.Options.TXO202506` 不存在 |
| 加權指數 | `api.Contracts.Indexs.TSE['TSE001']` | `TSE['001']` |
| K 棒 | `api.kbars()` 只有 1 分鐘，需自行重採樣成日K | 無 resolution 參數 |
| Snapshot | `buy_price` = bid，`sell_price` = ask | |
| 金鑰種類 | 正式盤金鑰（simulation=False），模擬盤報 400 | |

---

## Firebase

- **專案**：option-3047e
- **RTDB 路徑**：
  - `/trading/2330/collar/latest`：最新完整資料
  - `/trading/2330/collar/history/{timestamp}`：歷史快照
- **安全規則**：目前測試模式（需改為生產規則）

---

## 前端（web/index.html）

- 純 HTML，CDN React 18 + Babel，直接開瀏覽器或透過 server.py
- 透過 Firebase REST API 讀取資料（不需 Firebase JS SDK）
- `server.py` 啟動時才有自動觸發功能，直接開 file:// 仍可顯示舊資料

### 市場時段標示

| 狀態 | 時間 | 說明 |
|---|---|---|
| 日盤 | 09:00–13:30 | 股票＋期貨＋選擇權 |
| 盤後 | 14:00–14:30 | 股票盤後固定價格 |
| 夜盤 | 15:00–05:00 | TXO 期貨夜盤 |
| 休市 | 其餘 | 週日全休，週六至 05:00 |
