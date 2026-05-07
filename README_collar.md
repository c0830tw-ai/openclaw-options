# Collar Trading — 部署與運維指南

> 本檔聚焦「怎麼跑、怎麼維護」。專案結構、策略邏輯、API 用法請看 [CLAUDE.md](./CLAUDE.md)。

## 一、首次部署

### 1. 套件
```bash
cd ~/openclaw/options
python3 -m pip install shioaji firebase-admin holidays python-dotenv
```
需求：Python 3.9+，macOS 對應 wheel。

### 2. 認證

**永豐 API 金鑰**（`.env`）：
```bash
cp .env.example .env
# 編輯填入 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY / FIREBASE_URL
```
> 永豐 API：登入網銀「API 中心」申請 → 完成 e 憑證綁定 → 取金鑰。

**Firebase service account**（`firebase-key.json`）：
1. Firebase Console → 專案設定 → 服務帳戶 → 「產生新的私密金鑰」
2. 下載 JSON，重新命名為 `firebase-key.json` 放到 `options/`
3. RTDB 規則加入：
   ```json
   { "rules": { "trading": { ".read": "auth != null", ".write": "auth != null" } } }
   ```

**持倉**（選用，`positions.json`）：
```bash
cp positions.json.example positions.json
# 編輯 large_futures_lots / lots_0050 / lot_size_0050
```
不建立則用 `Config` 內建預設（1 口大台、2 口 0050 股期）。

### 3. 首次測試
```bash
set -a; source .env; set +a
python3 shioaji_collar.py
```

**成功的訊號**（任何時段都應該看到這幾行）：
```
[INFO] Shioaji logged in
[INFO] 近月 ATM IV: xx.x% (atm_market)         ← 確認用即時 IV
[INFO] Beta 2330: 0.xxx (computed_60d, …)       ← 動態 beta 校準
[INFO] Put ask: xxx  Call bid: xxx               ← 真實報價（含夜盤）
[INFO] Pushed to /trading/2330/collar/latest
```
若看到 `iv_source: hv_fallback` 或 `beta_source: config_default` 反覆出現，看[疑難排解](#疑難排解)。

---

## 二、日常啟動

預設用法（推薦）：
```bash
python3 server.py        # 自動觸發 + 5 分鐘自動 refresh，port 8081
open http://localhost:8081
```
打開瀏覽器即抓資料；關閉瀏覽器即停。

也可走 cron 排程：
```cron
# 日盤每小時，收盤再跑一次
5 9-13 * * 1-5  cd ~/openclaw/options && /bin/bash -lc 'set -a; source .env; set +a; python3 shioaji_collar.py' >> logs/collar.log 2>&1
35 13 * * 1-5   cd ~/openclaw/options && /bin/bash -lc 'set -a; source .env; set +a; python3 shioaji_collar.py' >> logs/collar.log 2>&1
# 夜盤（TXO 夜盤有真實報價）
5 15-23 * * 1-5 cd ~/openclaw/options && /bin/bash -lc 'set -a; source .env; set +a; python3 shioaji_collar.py' >> logs/collar.log 2>&1
5 0-4 * * 2-6   cd ~/openclaw/options && /bin/bash -lc 'set -a; source .env; set +a; python3 shioaji_collar.py' >> logs/collar.log 2>&1
```

---

## 三、健康檢查

打開 `latest_collar.json` 應該有：
| 欄位 | 應該長什麼樣 | 異常時 |
|---|---|---|
| `quotes_source` | `live` | 持續 `bs_estimate` → snapshot 抓不到，看下方 |
| `market_session` | `day` / `night` / `closed` | — |
| `iv_used` | 0.15–0.50（ATM IV）| 太極端 → 算錯或行情異常 |
| `iv_source` | `atm_market` | `hv_fallback` 表示 ATM snapshot 失敗 |
| `betas.beta_2330_source` | `computed_60d` | `config_default_oor(...)` 表示算出值越界，已退回預設 |
| `dte_trading` | 5–25（月選）/ 1–5（週選）| — |
| `targets.adx_regime` | `trend` / `neutral` / `range` | `unknown` 表示沒有 K 棒指標 |

---

## 四、Telegram 通知（選用）

新建 `notify.py`：
```python
import os, requests
def telegram_notify(data):
    s = data['structures'][0]   # symmetric
    msg = (f"📊 2330 領式更新\n"
           f"近月 IV {data['iv_used']*100:.1f}%  Beta {data['betas']['beta_2330']:.2f}\n"
           f"{s['calls']}C@{s['call_strike']:.0f} / {s['puts']}P@{s['put_strike']:.0f}\n"
           f"月淨收 {s['monthly_net']:,.0f}  保護 {s['protection_pct']:.0f}%")
    requests.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        json={'chat_id': os.environ['TELEGRAM_CHAT_ID'], 'text': msg},
    )
```
在 `shioaji_collar.py` 結尾呼叫，或包裝成獨立 watcher 過濾條件再發（例如 `monthly_net < 0` 才警告）。

---

## 五、疑難排解

**Shioaji 登入失敗 / 卡住**
- e 憑證過期（永豐每年到期）→ 網銀重新申請
- 永豐每天 06:00 重啟 API gateway，**05:50–06:10 跑會失敗** → 排程避開
- API 金鑰換過？檢查 `.env` 是不是舊金鑰

**`iv_source = hv_fallback` 持續出現**
- snapshot ATM call/put 都拿不到 bid/ask
- 通常發生在剛開盤 5 分鐘、或合約剛掛上（首日、結算日後）
- 短暫沒關係，下一個 refresh 通常會恢復；持續 30 分鐘以上才需要查

**`beta_source = config_default_oor(...)` 持續出現**
- 60 日迴歸算出值在 [0.3, 3.0] 之外（通常因 K 棒採樣異常或資料太少）
- 已自動退回 `CFG.beta` 預設，不影響運作
- 偶發 OK；長期出現代表 `_resample_daily` 對某商品的日期切割有 bug

**找不到 TXO 月份**
- 結算日後到下個月選掛牌前的空窗（罕見）
- 假日連續期 → `TAIWAN_HOLIDAYS` 可能漏列，看 `taiwan_holidays_cache.json`（若年份過舊會自動重抓）

**Firebase push 失敗**
- 服務帳戶 JSON 路徑錯 → 檢查 `FIREBASE_CRED` 與 `firebase-key.json` 位置
- RTDB 規則沒給寫入權 → 改回測試模式 (`.read/.write: true`) 排除問題後再收緊

**夜盤資料看起來很舊**
- 確認 `latest_collar.json.timestamp` 是最近的
- 若是，但前端顯示舊：`server.py` 的 5 分鐘 cache 還沒過，按 refresh
- 若 timestamp 也舊，cron 沒跑 → 看 `logs/collar.log`

---

## 六、注意事項

1. **此腳本只「讀取」資料、不下單**，不會誤觸發實際交易
2. **保證金與現金需另外監控**，腳本不檢查可用資金
3. **TXO 夜盤有真實報價**（與舊版說明相反），`quotes_source` 會在 snapshot 拿到 bid/ask 時自動標記 `live`
4. **持倉變動時**改 `positions.json` 即可，不需重啟 server
