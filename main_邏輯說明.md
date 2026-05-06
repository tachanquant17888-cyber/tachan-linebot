# tachan-linebot — main.py 邏輯總覽

## 一、系統角色

LINE Bot,每日早上自動推播 MOPS (公開資訊觀測站) 的「注意清單」EPS 分析,並支援使用者手動查詢當日 / 指定日期。

### 外部依賴

| 依賴 | 用途 |
|------|------|
| LINE Messaging API | 推播 / 回覆 |
| Upstash Redis | 訂閱者名單、查詢結果快取 |
| Groq (`llama-3.3-70b-versatile`) | 從 MOPS 明細抽出 EPS / 營收 JSON |
| MOPS `t05st02` API | 注意清單列表 + 明細 |
| APScheduler | 排程 (Tue–Sat 07:30) |
| FastAPI + uvicorn | Webhook server |

---

## 二、Redis Key 設計

| Key | 型別 | TTL | 用途 |
|-----|------|-----|------|
| `linebot:users` | SET | 無 | 訂閱者 user/group/room id |
| `mops:result:{date_key}` | STRING | 24 hr | 該日完整推播訊息 |

`date_key` 格式:民國年/月/日,例如 `115/04/23`。

### 寫入 / 讀取矩陣

| 動作 | `mops:result:*` |
|-----|-----------------|
| 07:30 排程 (查前一交易日) | ✓ 讀 → miss 才抓 MOPS 並寫入 |
| 使用者查「今日 / 日期」 | ✓ 讀 → miss 才抓 MOPS 並寫入 |

不論排程或使用者觸發,都走同一條路徑:**hit cache 即回,miss 抓 MOPS + 寫 cache**。

---

## 三、函式分層

### A. Redis 存取

- `get_cached_result` / `set_cached_result` — 完整訊息快取 (TTL 24h)
- `load_users` / `save_user` / `remove_user` — 訂閱者管理 (含記憶體 fallback)

### B. 資料處理

- `calc_revenue_growth` — 月營收 vs. 上季月均
- `calc_eps_growth` — 月 EPS vs. 上季月均

### C. Groq 分析

- `analyze_with_groq_single` — 單次呼叫,回傳公司區塊文字

### D. MOPS 爬蟲

- `_create_mops_session` — 建 session (含 cookie)
- `_fetch_notice_items` — 抓清單,過濾關鍵字 `("注意", "證券近期")`
- `_analyze_notice_items` — 逐家抓明細 → 丟 Groq,每家 sleep 2s
- `_format_push_message` — 組訊息 (header + 各公司區塊)

### E. 業務流程

- `fetch_eps(y, m, d) -> str`
  - 先讀 cache,hit 直接回
  - miss → 抓 MOPS → Groq 分析 → 組訊息 → 寫 cache (TTL 24h) → 回
  - 抓不到資料時的訊息(`無注意清單資料`)也會寫 cache

### F. LINE 推播

- `push_final_result` — 推單一 target,遇 400/403/404 自動移除失效訂閱者
- `send_immediate_reply` — reply token 即時回覆
- `_push_to_all_users` — 推所有訂閱者

### G. 背景任務

- `task_fetch_and_push` — 使用者查詢的背景執行版本(目前未啟用,留作日後 push_message 配額恢復後切回)

### H. 排程

`scheduled_push()`:
- 每日 (Tue–Sat) 07:30 抓「前一交易日」資料
- 寫入 cache + 推播給所有訂閱者
- 若該日無注意清單則不推播

---

## 四、主要流程

### 4.1 每日 07:30 推播

```
APScheduler 觸發 (tue-sat 07:30)
 → scheduled_push()
   → target_date = get_last_trading_day(now)
       Tue → Mon, Wed → Tue, Thu → Wed, Fri → Thu, Sat → Fri
   → fetch_eps(y, m, d)
     → 讀 mops:result:{date_key}
       hit  → 直接回(代表已抓過,例如使用者搶先查)
       miss → 抓 MOPS → Groq 分析 → 組訊息 → set cache (TTL 24h)
   → 若訊息含「無注意清單資料」/「查無資料」→ 不推播
   → 否則 _push_to_all_users(訊息)
```

### 4.2 排程時間設計

排程設定為 **Tue–Sat 07:30** 而非 Mon–Fri,理由是「隔天抓昨日」:

| 排程觸發日 | 抓取的交易日 |
|------------|--------------|
| 週二 07:30 | 週一 |
| 週三 07:30 | 週二 |
| 週四 07:30 | 週三 |
| 週五 07:30 | 週四 |
| 週六 07:30 | 週五 |

完整覆蓋週一到週五五個交易日,週日無資料可抓所以不排程。

### 4.3 使用者互動

支援指令:

| 輸入 | 動作 |
|-----|------|
| `訂閱` | `save_user(target_id)` |
| `取消訂閱` | `remove_user(target_id)` |
| `指令` | 回使用說明 |
| `今日` / `today` | 查今天 (有 cache 直接回,否則同步查 MOPS) |
| 7 碼數字 (如 `1150423`) | 查指定日期 (同上) |

> 註:目前因 LINE push_message 月配額考量,使用者查詢採「同步查 + reply_message」(不計配額但需等待約 1 分鐘)。原本的「背景查 + push_message」程式碼以註解保留,日後配額恢復可切回。

### 4.4 加入群組 / 聊天室

Bot 被加入群組 / 聊天室時觸發 `JoinEvent`,自動 `save_user(target_id)` 並回覆說明訊息。

---

## 五、設計重點

### 5.1 為什麼簡化掉 17:00 + 08:30 雙排程?

舊版設計:17:00 抓當日寫 baseline → 隔天 08:30 抓昨日做差量推播。

問題:
- MOPS 17:00 的資料不一定是當日最終版,常常隔天還會有新增
- 維護兩個排程 + baseline SET + 差量比對邏輯成本高
- 使用者其實只在乎「最終完整的注意清單」

簡化後:只有隔天 07:30 抓一次,拿到的就是已穩定的當日完整清單,推播給訂閱者。

### 5.2 為什麼 cache TTL 是 24 小時?

- 排程寫入後到隔天同時段自然過期
- 期間使用者查同一天都能 hit cache,避免重複打 MOPS
- 24hr 足夠覆蓋所有重複查詢場景

### 5.3 為什麼使用者查詢也會寫 cache?

簡化後沒有 baseline、沒有差量,cache 不會被「污染」概念影響。使用者查的就是最終完整版,寫入 cache 後其他使用者直接共用結果即可。

---

## 六、潛在風險 / 值得觀察的點

| # | 嚴重度 | 說明 |
|---|-------|------|
| 6.1 | 低 | 07:30 若 MOPS 掛了 → 不寫 cache,使用者下次查會再試一次 |
| 6.2 | 低 | `handle_message` 若 `event.source.user_id` 為 None(罕見),`save_user(None)` 會被寫入 Redis |
| 6.3 | 低 | 多 worker (uvicorn) 部署時排程會重複觸發,Render 預設 1 worker 沒問題 |
| 6.4 | 低 | 連假後第一個交易日的隔天才會推播,中間幾天沒有資料屬正常 |
