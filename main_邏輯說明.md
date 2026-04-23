# tachan-linebot — main.py 邏輯總覽

## 一、系統角色

LINE Bot,平日自動推播 MOPS (公開資訊觀測站) 的「注意清單」EPS 分析,並支援使用者手動查詢當日 / 指定日期。

### 外部依賴

| 依賴 | 用途 |
|------|------|
| LINE Messaging API | 推播 / 回覆 |
| Upstash Redis | 訂閱者名單、查詢結果快取、17:00 基準公司代號 |
| Groq (`llama-3.3-70b-versatile`) | 從 MOPS 明細抽出 EPS / 營收 JSON |
| MOPS `t05st02` API | 注意清單列表 + 明細 |
| APScheduler | 排程 (08:30 / 17:00) |
| FastAPI + uvicorn | Webhook server |

---

## 二、Redis Key 設計

| Key | 型別 | TTL | 用途 |
|-----|------|-----|------|
| `linebot:users` | SET | 無 | 訂閱者 user/group/room id |
| `mops:result:{date_key}` | STRING | 24 hr | 當日完整推播訊息(17:00 寫 → 08:30 覆蓋) |
| `mops:companies:{date_key}` | SET | 24 hr | 17:00 抓到的公司代號 (差量比對基準) |

`date_key` 格式:民國年/月/日,例如 `115/04/23`。

### 寫入 / 讀取矩陣

| 動作 | `mops:result:*` | `mops:companies:*` |
|-----|-----------------|-------------------|
| 17:00 排程 | ✓ 寫入 (當日完整訊息) | ✓ 寫入 (當日公司 SET,baseline) |
| 08:30 排程 | ✓ 覆蓋 (08:30 完整版) | ✗ 不動 |
| 使用者查「今日/日期」 | ✓ **讀** (hit 直接回,miss 抓但不寫) | ✗ |

重點:**SET 只有 17:00 會寫**,08:30 排程只讀不寫。沒有其他路徑會動 SET,baseline 絕對不漂移。

---

## 三、函式分層

### A. Redis 存取 (L53–145)

- `get_cached_result` / `set_cached_result` — 完整訊息快取
- `get_pushed_companies` / `add_pushed_companies` — 17:00 基準 SET
- `load_users` / `save_user` / `remove_user` — 訂閱者管理 (含記憶體 fallback)

### B. 資料處理 (L148–207)

- `calc_revenue_growth` — 月營收 vs. 上季月均
- `calc_eps_growth` — 月 EPS vs. 上季月均

### C. Groq 分析 (L210–273)

- `analyze_with_groq_single` — 單次呼叫,回傳公司區塊文字

### D. MOPS 爬蟲 (L277–391)

- `_create_mops_session` — 建 session (含 cookie)
- `_fetch_notice_items` — 抓清單,過濾關鍵字 `("注意", "證券近期")`
- `_analyze_notice_items` — 逐家抓明細 → 丟 Groq,每家 sleep 2s
- `_format_push_message` — 組訊息 (含 `is_delta` 差量標記)

### E. 業務流程 (L394–476)

- `fetch_eps(y, m, d, record_pushed=False) -> str`
  - `record_pushed=False` (手動查):先讀 cache,hit 回,miss 抓 MOPS 回,**不寫 cache / SET**
  - `record_pushed=True` (17:00 排程):強制抓 MOPS,寫 cache + 寫 SET
- `fetch_eps_delta(y, m, d) -> tuple[str, Optional[str]]`
  - 讀 17:00 baseline SET
  - 抓 MOPS + Groq 分析完整版
  - **成功抓到資料就覆蓋 cache** (08:30 完整版)
  - 回傳 4 種狀態:

  | status | message | 意義 |
  |--------|---------|-----|
  | `"new"` | 差量訊息 | 有新增公司 |
  | `"no_new"` | `None` | 有資料但無新增 |
  | `"no_data"` | `None` | 該日 MOPS 無注意清單 |
  | `"error"` | `None` | 解析失敗 |

### F. LINE 推播

- `push_final_result` — 推單一 target,遇 400/403/404 自動移除
- `send_immediate_reply` — reply token 即時回覆
- `_push_to_all_users` — 推所有訂閱者

### G. 背景任務

- `task_fetch_and_push` — 使用者「今日 / 日期查詢」的背景執行

### H. 排程

`scheduled_push(mode)`:
- `mode='previous'` (08:30) → 差量推播
- `mode='today'` (17:00) → 全量推播

---

## 四、主要流程

### 4.1 盤後 17:00 全量推播

```
APScheduler 觸發 (mon-fri 17:00)
 → scheduled_push(mode='today')
   → 週末 skip
   → fetch_eps(今日 y/m/d, record_pushed=True)
     → 抓 MOPS → Groq 分析 → 組訊息
     → set mops:result:{今日} = 完整訊息 (TTL 24h)
     → sadd mops:companies:{今日} = 公司 IDs (TTL 24h)
   → _push_to_all_users(完整訊息)
```

### 4.2 早盤 08:30 差量推播

```
APScheduler 觸發 (mon-fri 08:30)
 → scheduled_push(mode='previous')
   → 週末 skip
   → target_date = get_last_trading_day(now)    ← 週一 → 週五
   → fetch_eps_delta(y, m, d)
     → 讀 mops:companies:{昨日}  (17:00 baseline)
     → 抓 MOPS → Groq 分析完整版
     → set mops:result:{昨日} = 完整版  ← 覆蓋 17:00 那版
     → diff = 完整版 - baseline
     → SET 不動
   → 依 status 推不同訊息:
     new     → 差量訊息
     no_new  → 「{date_key} 比對後無更新資料」
     no_data → 「{date_key} 無注意清單資料」
     error   → 「⚠️ MOPS 公開資訊觀測站系統已更換,暫時無法取得今日資料」
```

### 4.3 使用者互動

支援指令:

| 輸入 | 動作 |
|-----|------|
| `訂閱` | `save_user(target_id)` |
| `取消訂閱` | `remove_user(target_id)` |
| `指令` | 回使用說明 |
| `今日` / `today` | 查今天 (有 cache 直接回,否則背景跑 `task_fetch_and_push`) |
| 7 碼數字 (如 `1150423`) | 查指定日期 (同上,走 cache) |

**cache miss 的手動查詢**:走 `fetch_eps(record_pushed=False)`,**不寫** cache / SET,下一位使用者查同一天還是會再打一次 MOPS。

---

## 五、設計重點

### 5.1 為什麼 SET 只有 17:00 寫?

因為沒有對使用者開放的 `diff` 指令,每個 date_key 的 SET 生命週期很單純:
- 17:00 寫入一次 (TTL 24hr)
- 隔天 08:30 讀取一次
- 用完就自然過期 (或被隔天 17:00 同 date_key 蓋掉)

沒有其他路徑會碰 SET,baseline 是絕對純淨的 17:00 snapshot,`fetch_eps_delta` 的結果永遠穩定。

### 5.2 為什麼 cache STRING 會被 08:30 覆蓋?

08:30 抓到的是「17:00 之後到隔天早上」累積的完整清單,比 17:00 那版更新。使用者 08:30 之後查「昨日」應該拿到最新完整版(包含 8:30 補上的公司)。

### 5.3 為什麼使用者手動查不寫 cache?

避免 cache 污染。使用者可能在 12:00 查「今日」,那時 MOPS 資料還不完整;如果寫 cache,17:00 排程雖然會覆蓋,但 12:00 → 17:00 之間其他使用者會看到舊資料。乾脆不寫,每個手動查就當面抓一次。

### 5.4 TTL 為什麼都是 24 小時?

- 17:00 寫 → 隔天 17:00 自然過期
- 08:30 覆蓋 STRING 時 TTL 重設,再活 24hr
- 任何時候使用者查,都能拿到最近一次排程寫的結果
- SET 同步 24hr,隔天 17:00 前會被新的 17:00 覆蓋(SADD 會把新公司加進去,實務上會更新 expire)

---

## 六、潛在風險 / 值得觀察的點

| # | 嚴重度 | 說明 |
|---|-------|------|
| 6.1 | 低 | 08:30 若 MOPS 掛了 (`error`) → cache 不動,維持 17:00 版本(安全) |
| 6.2 | 低 | 17:00 排程如果整個失敗 (沒寫 SET),隔天 08:30 會把「昨日全部」當新增推(因 baseline=∅) |
| 6.3 | 低 | `_analyze_notice_items` 的 `skip_company_ids` 參數從未被啟用 (差量仍會重跑全部 Groq) |
| 6.4 | 低 | `handle_message` 若 `event.source.user_id` 為 None(罕見),`save_user(None)` 會被寫入 Redis |
| 6.5 | 低 | 多 worker (uvicorn) 部署時排程會重複觸發,Render 預設 1 worker 沒問題 |
