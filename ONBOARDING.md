# Options Collar 操作手冊

> 給未來自己 / 接手者看的「日常怎麼用」指南。
> 部署/運維請看 [README_collar.md](./README_collar.md)。
> 專案規則請看 [CLAUDE.md](./CLAUDE.md)。

---

## 1 分鐘總覽

這是一個 **0050 + 2330 collar 對沖** 的決策助手：
- **抓資料**：永豐 Shioaji API → TX、TXO、股價、IV
- **算決策**：Greeks、健診、結構推薦、roll 建議
- **追風險**：drawdown、IV 百分位、200 萬法則對齊度
- **回顧**：每日 P&L 拆解、歷史事件影響、交易日記
- **自動化**：每日 Telegram 早晚報、月度 backfill

**操作模式：你是用戶，系統是顧問。永遠是你下單，系統不自動下單。**

---

## 進入點

開瀏覽器：
```
http://localhost:8081           # 本機
http://192.168.50.92:8081       # 區網
http://100.116.108.93:8081      # Tailscale 遠端
```

或從選單頁 `http://localhost:8080` 點 **Options** icon。

---

## 一日工作流

### 🌅 08:00 — Telegram morning report 自動推
- 📌 今日重點（事件 / 換倉 / IV 極端 / hedge 失衡）
- 📊 行情 + IV 百分位
- 🧮 Greeks 解讀（Δ TX 變動 / θ 月成本 / ν IV 變動）
- 📅 近 5 天事件
- ⚠ active alerts

> **看完馬上知道今天該做什麼，不用主動開頁面。**

### 🕐 開盤後（9:00-13:30）— 開 dashboard 看細節
- 「🎯 今日行動」section 已展開：
  - 📅 行事曆 — TXO 結算、週選、事件一目了然
  - 🎯 Regime Advisor — 當前情境推薦 SOP
  - 🎯 Collar Dashboard — 兩腳觸發紅黃綠
  - 🛠️ Order Helper — 推薦結構自動生成階梯限價
  - 🔧 換倉建議 — DTE ≤ 5 自動跳出
- 「🛡️ 部位健康」也展開：IV 百分位、drawdown、健診、Greeks

### 🌆 15:00 — Telegram evening report 自動推
- 📊 今日結果（TX/IV 對昨變化）
- 🧮 P&L 拆解（Δ/θ/ν 貢獻）
- 📓 今日交易紀錄
- 🎯 明日重點 + 健診走勢

### 📝 下單時：用 CLI 紀錄

```bash
# 開倉（含 thesis）
python3 add_trade.py add buy_to_open "TXO 202605F2 39000P" 3 50 \
    --book hedge --thesis "FOMC 前 hedge，預期 IV spike + TX 軟"

# 平倉（含 outcome 反思）
python3 add_trade.py close T20260508-1700-001 80 \
    --outcome "FOMC 鷹派意外，put 漲到 80" --thesis-correct 1
```

> Order Helper 卡片有「📋 複製命令」鍵，已含正確語法 + thesis。

---

## 月度工作流

### 1. 結算翌日 10:00–11:30 — 月選換倉

依 SOP「月選 put 動態履約」：
1. 看 Dashboard「Collar Dashboard」推薦近月 put 履約
2. 點「Order Helper」展開 → 看 3 段階梯限價
3. 平掉舊月 → 階梯買新月（每段等 8 分鐘）
4. 用 add_trade.py 紀錄（含 thesis）

### 2. 月初 — 看 backtest_optimize 校準

```bash
python3 backtest_optimize.py --shioaji --days 365
```
看 1 年最佳 (DTE, Δ, strategy)。如果連續幾個月 best params 都遠離當前 SOP，考慮調整。

### 3. 月底 — 看 Trade Journal + Performance Attribution

- Dashboard「📈 績效追蹤」展開
- TradeJournalPanel：thesis 命中率
- PnLAttribution：本月損益拆解（Δ vs θ vs ν 各貢獻多少）

---

## Dashboard 5 大區塊速查

| Section | 內容 | 預設 |
|---|---|---|
| 🎯 今日行動 | 行事曆、事件、Regime Advisor、Collar Dashboard、Order Helper、Pre-trade Sim、換倉建議 | 展開 |
| 🛡️ 部位健康 | IV 百分位、Drawdown、Risk Limits、健診、Greeks、Stress Test | 展開 |
| 💡 市場機會 | IndicatorGrid、本週機會（spreads）| 收起 |
| 📋 持倉明細 | 卷商即時持倉、Portfolio Breakdown | 收起 |
| 📈 績效追蹤 | P&L 拆解、Trade Journal、歷史事件、趨勢、Ledger | 收起 |

---

## CLI 工具速查

| 命令 | 用途 |
|---|---|
| `python3 add_trade.py add ...` | 開倉紀錄（含 thesis）|
| `python3 add_trade.py close <id> <price>` | 平倉 + 算 P&L |
| `python3 add_trade.py list --open` | 看未平倉 |
| `python3 add_trade.py summary` | P&L 彙整 |
| `python3 backtest.py --shioaji` | 真實 1 年回測 |
| `python3 backtest_optimize.py --shioaji` | 參數最佳化 sweep |
| `python3 backtest_regime.py --shioaji --quarters 4` | 多情境分段回測 |
| `python3 events_sync.py` | 手動更新 FOMC/CPI/TSMC |
| `python3 event_analysis.py` | 歷史事件 P&L 解析 |
| `python3 iv_percentile.py --backfill` | 重灌 IV 歷史（HV proxy）|
| `python3 morning_report.py --print` | 預覽早報內容 |
| `python3 evening_report.py --print` | 預覽晚報內容 |

---

## 自動化排程（launchd）

| Label | 排程 | 功能 |
|---|---|---|
| `com.openclaw.gdrive.backup`   | 每日 03:00 | Google Drive 備份 |
| `com.openclaw.events_sync`     | 每日 03:30 | 抓 FOMC + 估算 CPI + TSMC 法說 |
| `com.openclaw.event_analysis`  | 每月 1 日 04:00 | 算歷史事件 P&L |
| `com.openclaw.morning_report`  | 每日 08:00 | Telegram 早報 |
| `com.openclaw.evening_report`  | 每日 15:00 | Telegram 晚報 |

```bash
# 查看狀態
launchctl list | grep openclaw

# 手動觸發測試
launchctl start com.openclaw.morning_report
```

plist 檔案在 `launchd/` 下，部署到 `~/Library/LaunchAgents/` 後 `launchctl load`。

---

## 核心 SOP 規則

### 200 萬法則（hedge 比例）
**每 200 萬台股名目 ≈ 1 口 TXO put**。系統 `recommended_put_lots` 已自動算好。

### 動態履約（月選 put）
每月選結算後選新履約價，距現價 **-6% ~ -8%**（≈ Δ -0.10）。系統 `selected_options.put.strike` 就是當期推薦。

### 布林軌道使用範圍
- ❌ 選擇權 decision 不用 BB
- ✅ 履約價選擇用日 K BB（系統內部 `compute_target_strikes` 自動）

### 分情境策略偏好（backtest 驗證）
| Regime | 最佳 SOP |
|---|---|
| 🐂 強勢牛市（月 >+10%）| DTE 30, Δ 0.05, collar |
| 🐃 中等牛市 | DTE 45, Δ 0.10, collar |
| 😴 盤整 | DTE 21, Δ 0.15, collar |
| 🐃↓ 中等熊市 | DTE 21, Δ 0.15, put_only |
| 🐻 強勢熊市 | DTE 15, Δ 0.20, put_only |

> Regime Advisor 卡片會自動偵測當前情境並對照你實際持倉差異。

---

## 設定檔

| 檔案 | 用途 |
|---|---|
| `.env` | Shioaji 金鑰、Firebase URL、Telegram token（gitignored）|
| `positions.json` | 你的核心持倉（口數、cost basis）（gitignored）|
| `events.json` | 手動事件清單（commit 進 repo；FOMC/CPI 由 auto 處理）|
| `alerts_config.json` | 風險閾值、限額（gitignored；可從 .example 複製）|

### 自動產生 / gitignored 的快取檔
- `latest_collar.json` — 最新 refresh 結果
- `daily_snapshots.json` — 每日 snapshot 序列
- `trades_ledger.json` — 交易紀錄
- `events_auto.json` — 自動抓的事件
- `event_history.json` — 歷史事件 P&L
- `iv_history.json` — IV 百分位歷史

---

## 故障排除

**Telegram 沒收到推播**
1. 檢查 `.env` 有 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`
2. `python3 morning_report.py --force --print` 看 console 有沒有錯
3. `tail logs/morning_report.log` 看排程結果

**Dashboard 顯示舊資料**
- 開瀏覽器時自動重新抓；強制刷新可重新 reload
- 看頁面右上角時間戳判斷新鮮度
- 「⚠ 資料 X 小時未更新」紅字時表示沒在跑 server

**Shioaji login 失敗**
- 看 `logs/collar.log` 末尾錯誤訊息
- 常見：CA 過期（每年需重簽）、API key 失效（網銀重新申請）

**新增 / 修改 launchd job**
編輯 `launchd/com.openclaw.<job>.plist` 後：
```bash
launchctl unload ~/Library/LaunchAgents/com.openclaw.<job>.plist
cp launchd/com.openclaw.<job>.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.openclaw.<job>.plist
```

---

## 模組速覽（給開發者）

| 模組 | 職責 |
|---|---|
| `shioaji_collar.py` | 主腳本，抓資料 + 計算 + 寫 latest_collar.json |
| `server.py` | Port 8081 HTTP server，瀏覽器開啟時觸發 refresh |
| `health_check.py` | 4 規則健診評分 |
| `collar_dashboard.py` | per-leg 觸發 + 結構推薦 |
| `regime_advisor.py` | 情境偵測 + SOP 推薦 |
| `roll_advisor.py` | 換倉建議（含階梯限價）|
| `risk_limits.py` | 6 項限額即時監控 |
| `drawdown_tracker.py` | 從 peak 追當前 DD |
| `iv_percentile.py` | IV 在過去 252 天百分位 |
| `performance_attribution.py` | 每日 P&L Δ/θ/ν 拆解 |
| `events.py` / `events_sync.py` | 事件 loader / auto fetcher |
| `event_analysis.py` | 歷史事件 TX 影響統計 |
| `trade_journal.py` | 交易日記聚合 |
| `order_helper.py` | 推薦結構 → 階梯限價 + add_trade 命令 |
| `backtest.py` | 1 年合成 / 真實回測 |
| `backtest_optimize.py` | DTE × Δ × strategy 參數 sweep |
| `backtest_regime.py` | 多情境分段回測 |
| `morning_report.py` / `evening_report.py` | Telegram 早晚報 |
| `snapshot.py` | daily snapshot 寫入 + trend |
| `alerts.py` | alert 規則 evaluation + Telegram 推送 |
| `broker_sync.py` | 抓券商即時持倉 |
| `ledger.py` | trades_ledger.json 讀寫 + P&L 計算 |
