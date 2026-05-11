# tachan-linebot — main.py 邏輯總覽

## 一、系統角色

LINE Bot，每日早上自動推播 MOPS（公開資訊觀測站）的「注意清單」EPS 分析，並支援使用者手動查詢當日 / 指定日期。

### 外部依賴

| 依賴 | 用途 |
|------|------|
| LINE Messaging API | 推播 / 回覆 |
| Upstash Redis | 訂閱者名單、Raw 文字快取、分析結果快取、分散式 Lock |
| Groq (`llama-3.3-70b-versatile`) | 從 MOPS 明細抽出 EPS / 營收 JSON |
| MOPS `t05st02` API | 注意清單列表 + 明細 |
| TWSE Open API (`holidaySchedule`) | 當年休市日清單，供 `get_last_trading_day` 跳過國定假日 |
| APScheduler | 排程 (Mon–Fri 07:30，在 Render 上執行) |
| GitHub Actions | 排程 (Mon–Fri 07:00 UTC+8)，負責 MOPS 爬蟲預熱 |
| FastAPI + uvicorn | Webhook server（Render 上常駐） |

---

## 二、Redis Key 設計

| Key | 型別 | TTL | 用途 |
|-----|------|-----|------|
| `linebot:users` | SET | 無 | 訂閱者 user/group/room id |
| `mops:raw:{date_key}` | STRING (JSON) | 7 天 | GitHub Actions 預抓的 MOPS 原始文字，尚未 Groq 分析 |
| `mops:result:{date_key}` | STRING | 7 天 | Groq 分析完成後的完整推播訊息 |
| `mops:lock:{date_key}` | STRING | 5 min | 分散式 Lock，防止同一 date_key 並發打 MOPS |

`date_key` 格式：民國年/月/日，例如 `115/04/23`。

---

## 三、雙執行環境架構

```
GitHub Actions (Mon–Fri 07:00)          Render (常駐)
  fetch_job.py                            main.py (APScheduler Mon–Fri 07:30)
       │                                         │
       │  0. fetch_twse_holidays()               │  0. fetch_twse_holidays()  ← lifespan 啟動時
       │  1. MOPS 清單 API                       │  3. fetch_eps()
       │  2. MOPS 明細 API (each company)        │     └─ miss mops:result:
       │  ← sleep 2s 間隔 →                      │     └─ hit  mops:raw:  ← GitHub Actions 存的
       │                                         │     └─ _analyze_raw_texts() → Groq
       ▼                                         │     └─ set mops:result:
  set mops:raw:{date}                            ▼
                                         _push_to_all_users()
```

**Fallback**：若 GitHub Actions 失敗（`mops:raw:` 無資料），Render 的 `fetch_eps()` 會自動全流程自己跑（MOPS + Groq）。

---

## 四、函式分層

### A. Redis 存取

- `get_cached_result` / `set_cached_result` — 完整分析結果快取 (TTL 7天)
- `get_cached_raw_texts` / `set_cached_raw_texts` — MOPS 原始文字快取 (TTL 7天)
- `_acquire_lock` / `_release_lock` — 分散式 Lock (TTL 5min，SET NX)
- `load_users` / `save_user` / `remove_user` — 訂閱者管理（含記憶體 fallback）

### B. 資料處理

- `calc_revenue_growth` — 月營收 vs. 上季月均
- `calc_eps_growth` — 月 EPS vs. 上季月均

### C. Groq 分析

- `analyze_with_groq_single` — 單次呼叫，從 raw_text 抽出 JSON → 計算成長率 → 回傳公司區塊文字

### D. MOPS 爬蟲

- `_create_mops_session` — 建 session（含 cookie 暖機）
- `_fetch_notice_items` — 抓清單，過濾關鍵字 `("注意", "證券近期")`
- `_fetch_raw_texts` — 逐家抓 MOPS 明細取得 raw_text，每家 sleep 2s，**不呼叫 Groq**
- `_analyze_raw_texts` — 對每筆 raw_text 呼叫 Groq 分析，**不打 MOPS**
- `_format_push_message` — 組訊息（header + 各公司區塊）

### E. 交易日工具

- `fetch_twse_holidays() -> set[str]` — 從 TWSE Open API 抓當年所有休市日，回傳民國日期字串 set（例如 `{"1150619", "1150101"}`）
- `to_roc_date_str(d) -> str` — datetime → 民國日期字串（7碼），用於與 `_holidays` 比對
- `to_roc_ymd(d) -> tuple` — datetime → 民國 (year, month, day) tuple，給 MOPS API 傳參用
- `get_last_trading_day(now) -> datetime` — 從 now 往回找最近交易日，跳過週末 + `_holidays` 國定假日

> `_holidays`：模組層級 `set[str]`，啟動時由 `lifespan`（Render）或 `fetch_job.py`（GitHub Actions）各自呼叫 `fetch_twse_holidays()` 載入，確保兩端計算出相同的 `date_key`。

### F. 業務流程

- `fetch_eps(y, m, d) -> str`
  1. 讀 `mops:result:` — hit → 直接回
  2. 嘗試取 Lock — 取不到 → 輪詢等待最多 90s
  3. Double-check `mops:result:` — hit → 釋放 Lock 回
  4. 讀 `mops:raw:` — hit → 跳到步驟 6
  5. Miss → 自己抓 MOPS（`_fetch_notice_items` + `_fetch_raw_texts`）
  6. `_analyze_raw_texts` → Groq 分析
  7. 組訊息 → 寫 `mops:result:`（TTL 7天）
  8. 釋放 Lock → 回

### G. LINE 推播

- `push_final_result` — 推單一 target，遇 400/403/404 自動移除失效訂閱者
- `send_immediate_reply` — reply token 即時回覆
- `_push_to_all_users` — 推所有訂閱者

### H. 背景任務

- `task_fetch_and_push` — 使用者查詢的背景執行版本（目前未啟用，留作日後 push_message 配額恢復後切回）

### I. 排程

`scheduled_push()`：
- 每日（Mon–Fri）07:30 呼叫 `get_last_trading_day` 取得前一交易日，再呼叫 `fetch_eps()` 取得分析結果
- GitHub Actions 在 07:00 已存好 `mops:raw:`，07:30 只需做 Groq 分析
- 若該日無注意清單則不推播

---

## 五、主要流程

### 5.1 每日推播（正常路徑）

```
GitHub Actions 07:00 (Mon–Fri)
  → fetch_job.py
    → fetch_twse_holidays()          ← 載入當年休市日到 _holidays
    → get_last_trading_day(now)      ← 跳過週末 + 國定假日
    → _create_mops_session()
    → _fetch_notice_items()          ← MOPS 清單 API
    → _fetch_raw_texts()             ← MOPS 明細 API (N 家 × 1次)
    → set_cached_raw_texts()         ← 存 mops:raw:{date}

APScheduler 觸發 (Mon–Fri 07:30)
  → scheduled_push()
    → get_last_trading_day(now)      ← 跳過週末 + 國定假日（_holidays 已在 lifespan 載入）
    → fetch_eps(y, m, d)
        → miss mops:result:
        → acquire lock
        → hit  mops:raw:             ← GitHub Actions 存的
        → _analyze_raw_texts() → Groq (N 家)
        → set mops:result:{date}
        → release lock
    → _push_to_all_users(訊息)
```

### 5.2 排程時間設計

排程設定為 **Mon–Fri 07:30**，`get_last_trading_day` 負責往回找正確的交易日：

| 排程觸發日 | 抓取的交易日 |
|------------|--------------|
| 週一 07:30 | 上週五（跳過週六、週日） |
| 週二 07:30 | 週一 |
| 週三 07:30 | 週二 |
| 週四 07:30 | 週三 |
| 週五 07:30 | 週四 |

**遇到國定假日範例（端午節 6/19 週五）：**

| 排程觸發日 | 回溯過程 | 抓取的交易日 |
|------------|----------|--------------|
| 週一 6/22 07:30 | 6/21(日)→ 6/20(六)→ 6/19(五,假日) → 6/18(四) | **6/18 週四** |

### 5.3 使用者互動

支援指令：

| 輸入 | 動作 |
|-----|------|
| `訂閱` | `save_user(target_id)` |
| `取消訂閱` | `remove_user(target_id)` |
| `指令` | 回使用說明 |
| `今日` / `today` | 查今天（有 cache 直接回，否則走 `fetch_eps()` 完整流程） |
| 7 碼數字（如 `1150423`） | 查指定日期（同上） |

> 註：目前因 LINE push_message 月配額考量，使用者查詢採「同步查 + reply_message」（不計配額但需等待）。原本的「背景查 + push_message」程式碼以註解保留，日後配額恢復可切回。

### 5.4 加入群組 / 聊天室

Bot 被加入群組 / 聊天室時觸發 `JoinEvent`，自動 `save_user(target_id)` 並回覆說明訊息。

---

## 六、設計重點

### 6.1 為什麼拆成 GitHub Actions + Render 兩層？

MOPS 網站有 IP 請求頻率限制，Render 的固定 IP 若高頻爬蟲容易被封。改成：
- GitHub Actions 負責爬蟲（每天只跑一次，IP 分散）
- Render 負責 Groq 分析（無外部 IP 限制疑慮）

若 GitHub Actions 失敗，Render 可自動 fallback 全流程。

### 6.2 為什麼需要分散式 Lock？

使用者同時查詢同一 date_key 時，兩個請求可能同時 miss cache，導致重複打 MOPS。
Lock 確保同一 date_key 只有一個請求執行完整流程，其他請求輪詢等待結果。

### 6.3 為什麼 cache TTL 是 7 天？

- GitHub Actions 07:00 存 raw_texts → Render 07:30 消費，間隔 30 分鐘
- 使用者在當天內查同一天都能 hit cache，避免重複打 MOPS / Groq
- 連假期間（最長約 4–5 天）結束後，用戶仍可查假期前最後交易日，7 天確保不提早過期

### 6.4 為什麼需要 TWSE 假日 API？

`get_last_trading_day` 原本只跳週末，遇到平日國定假日（端午、勞動節等）時，GitHub Actions 和 Render 會各自計算出錯誤的日期，導致：
- GitHub Actions 存錯 date_key 到 Redis
- Render 計算正確 date_key 但 Redis 找不到資料，被迫 fallback 自己打 MOPS

改成兩端都先載入 `_holidays` 後呼叫 `get_last_trading_day`，保證兩端 date_key 一致。

### 6.5 為什麼簡化掉 17:00 + 08:30 雙排程？

舊版設計：17:00 抓當日寫 baseline → 隔天 08:30 做差量推播。

問題：
- MOPS 17:00 資料不一定是最終版，隔天常有新增
- 維護兩排程 + baseline SET + 差量比對邏輯成本高

簡化後：只有隔天 07:30 拿已穩定的當日完整清單推播。

---

## 七、潛在風險 / 值得觀察的點

| # | 嚴重度 | 說明 |
|---|-------|------|
| 7.1 | 中 | GitHub Actions 失敗時 Render 07:30 fallback 自己跑 MOPS，此時 Render IP 仍有被限速風險 |
| 7.2 | 低 | `_fetch_raw_texts` 若明細 data 有多列，目前只取最後一列的 raw_text |
| 7.3 | 低 | 07:30 若 Groq 掛了 → 不寫 `mops:result:`，使用者下次查會再試一次 |
| 7.4 | 低 | `handle_message` 若 `event.source.user_id` 為 None（罕見），`save_user(None)` 會被寫入 Redis |
| 7.5 | 低 | 多 worker（uvicorn）部署時排程會重複觸發，Render 預設 1 worker 沒問題 |
| 7.6 | 低 | TWSE 假日 API 若年底未更新隔年資料，跨年初幾天 `_holidays` 可能漏掉新年假日；載入失敗時退化成只跳週末 |
