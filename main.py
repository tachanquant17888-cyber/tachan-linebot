from fastapi import FastAPI, Request, Header, HTTPException
from linebot.v3.messaging import (
    ApiClient, Configuration, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage as TextMsg
)
from linebot.v3.messaging.exceptions import ApiException
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent, JoinEvent
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from groq import Groq
from typing import Optional
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo
import requests
import urllib3
import re
import time
import json
import os
import threading
from upstash_redis import Redis




urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

configuration = Configuration(access_token=os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

TZ = ZoneInfo("Asia/Taipei")

# ── Upstash Redis ─────────────────────────────
try:
    redis_client = Redis.from_env()
    print("✅ Upstash Redis 已連線")
except Exception as e:
    print(f"⚠️ Upstash Redis 連線失敗: {e}")
    redis_client = None

# ── TWSE 休市日 ───────────────────────────────
# [新增] 從 TWSE Open API 抓取當年休市日，供 get_last_trading_day 使用
TWSE_HOLIDAY_API = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
_holidays: set[str] = set()  # 民國日期字串，例如 "1150619"；啟動時載入

# Redis key 定義
USERS_KEY = "linebot:users"
RESULT_KEY_PREFIX = "mops:result:"          # 完整訊息 (TTL 7天)
RAW_TEXTS_KEY_PREFIX = "mops:raw:"          # MOPS 原始文字，Groq 尚未分析 (TTL 7天)

# Fallback
_users_fallback: set = set()
_users_lock = threading.Lock()


# ── Redis 讀寫 ────────────────────────────────
RESULT_TTL_SECONDS = 7 * 24 * 60 * 60      # 7 天
RAW_TEXTS_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 天


def get_cached_result(date_key: str) -> Optional[str]:
    if redis_client is None:
        return None
    try:
        key = f"{RESULT_KEY_PREFIX}{date_key}"
        result = redis_client.get(key)
        if result:
            print(f"[Redis HIT] {date_key}")
            return result
        print(f"[Redis MISS] {date_key}")
        return None
    except Exception as e:
        print(f"[Redis] get_cached_result 失敗: {e}")
        return None


def set_cached_result(date_key: str, result: str):
    if redis_client is None:
        return
    try:
        key = f"{RESULT_KEY_PREFIX}{date_key}"
        redis_client.set(key, result, ex=RESULT_TTL_SECONDS)
        print(f"[Redis SET] {date_key} (TTL {RESULT_TTL_SECONDS}s)")
    except Exception as e:
        print(f"[Redis] set_cached_result 失敗: {e}")


def get_cached_raw_texts(date_key: str) -> Optional[dict]:
    """回傳 {company_id: {"name":..., "text":...}} 或 None(未命中)"""
    if redis_client is None:
        return None
    try:
        key = f"{RAW_TEXTS_KEY_PREFIX}{date_key}"
        val = redis_client.get(key)
        if val is not None:
            print(f"[Redis RAW HIT] {date_key}")
            return json.loads(val)
        print(f"[Redis RAW MISS] {date_key}")
        return None
    except Exception as e:
        print(f"[Redis] get_cached_raw_texts 失敗: {e}")
        return None


def set_cached_raw_texts(date_key: str, raw_texts: dict):
    if redis_client is None:
        return
    try:
        key = f"{RAW_TEXTS_KEY_PREFIX}{date_key}"
        redis_client.set(key, json.dumps(raw_texts, ensure_ascii=False), ex=RAW_TEXTS_TTL_SECONDS)
        print(f"[Redis RAW SET] {date_key} ({len(raw_texts)} 筆)")
    except Exception as e:
        print(f"[Redis] set_cached_raw_texts 失敗: {e}")
        raise


# ── 訂閱名單管理 ──────────────────────────────
def load_users() -> list:
    if redis_client is None:
        with _users_lock:
            return list(_users_fallback)
    try:
        users = redis_client.smembers(USERS_KEY)
        return list(users) if users else []
    except Exception as e:
        print(f"[Redis] load_users 失敗,使用 fallback: {e}")
        with _users_lock:
            return list(_users_fallback)


def save_user(user_id: str):
    if redis_client is None:
        with _users_lock:
            _users_fallback.add(user_id)
        return
    try:
        redis_client.sadd(USERS_KEY, user_id)
        print(f"[Redis] 新增訂閱者: {user_id}")
    except Exception as e:
        print(f"[Redis] save_user 失敗: {e}")
        with _users_lock:
            _users_fallback.add(user_id)


def remove_user(user_id: str):
    if redis_client is None:
        with _users_lock:
            _users_fallback.discard(user_id)
        return
    try:
        redis_client.srem(USERS_KEY, user_id)
        print(f"[Redis] 移除訂閱者: {user_id}")
    except Exception as e:
        print(f"[Redis] remove_user 失敗: {e}")
        with _users_lock:
            _users_fallback.discard(user_id)


# ── 成長率計算 ────────────────────────────────
def calc_revenue_growth(data: dict) -> str:
    a = data.get("latest_month_revenue")
    b = data.get("latest_quarter_revenue")
    m_label = data.get("latest_month_label") or ""
    q_label = data.get("latest_quarter_label") or ""

    m_tag = f"({m_label})" if m_label else ""
    q_tag = f"({q_label})" if q_label else ""

    if a is None or b is None:
        return "⚠️ 無法計算營收月增率(缺少營收數據)"
    if b == 0:
        return "⚠️ 無法計算營收月增率(上季營收為零)"

    avg = b / 3
    growth = ((a - avg) / avg) * 100
    sign = "+" if growth >= 0 else ""

    return (
        f"最近一月營收{m_tag}:{a:,.2f} 百萬\n"
        f"上季{q_tag}月均營收:{avg:,.2f} 百萬\n"
        f"月營收較上季月均成長:{sign}{growth:.2f}%"
    )


def calc_eps_growth(data: dict) -> str:
    m = data.get("latest_month_eps")
    q = data.get("latest_quarter_eps")
    m_label = data.get("latest_month_label") or ""
    q_label = data.get("latest_quarter_label") or ""

    m_tag = f"({m_label})" if m_label else ""
    q_tag = f"({q_label})" if q_label else ""

    if m is None or q is None:
        return "⚠️ 無法計算 EPS 月增率(缺少 EPS 數據)"

    avg = q / 3

    if avg == 0:
        return (
            f"最近一月 EPS{m_tag}:{m:+.2f} 元\n"
            f"上季{q_tag}月均 EPS:{avg:+.2f} 元\n"
            f"⚠️ 上季月均 EPS 為零,無法計算成長率"
        )

    growth = ((m - avg) / abs(avg)) * 100
    sign = "+" if growth >= 0 else ""

    return (
        f"最近一月 EPS{m_tag}:{m:+.2f} 元\n"
        f"上季{q_tag}月均 EPS:{avg:+.2f} 元\n"
        f"月 EPS 較上季月均成長:{sign}{growth:.2f}%"
    )


# ── Groq 分析 ────────────────────────────────
def analyze_with_groq_single(raw_text: str, company_id: str, company_name: str) -> str:
    """單次 Groq 呼叫:同時取得 EPS 描述 + 財務數字"""
    system_prompt = "你是資料擷取系統,只輸出純 JSON,不輸出任何其他文字或 markdown。"
    user_prompt = f"""從以下文本擷取財務資訊,回傳純 JSON:

<raw_text>
{raw_text}
</raw_text>

回傳格式:
{{
  "is_bond": true/false,
  "latest_month_eps": 數字或null,
  "latest_quarter_eps": 數字或null,
  "latest_month_label": "X月" 或 null,
  "latest_quarter_label": "第X季" 或 null,
  "latest_month_revenue": 數字或null,
  "latest_quarter_revenue": 數字或null
}}

欄位對應規則(重要):
- 表格欄位有兩種格式:
  (A) 欄位名稱為「最近一月」或「近一月」、「最近一季」或「近一季」或「最近一期」
  (B) 欄位名稱為民國年份,如「115年03月」(月)、「115年第1季」(季)
  → 兩種格式都適用,取第一個數字欄(最新期),跳過與去年比較欄
- 「單月」區段的數字 → latest_month_*;「單季」區段的數字 → latest_quarter_*
- 「每股盈餘」或「EPS」或「稅後盈餘」或「每股淨利」或「稅前盈餘」或「稅前EPS」那列 → 取出 latest_month_eps 和 latest_quarter_eps
- 「營業收入」那列 → 取出 latest_month_revenue 和 latest_quarter_revenue(單位:百萬),注意取得數值不要把","認作小數點"."
- is_bond: 文本【主體】是可轉債/公司債資訊(無股票財務表格)才填 true;若主體是股票財務表格、只是附帶提及可轉債代號則填 false
- EPS跟每股盈餘如果是虧損請填負數,如 (0.19) → -0.19
- 括號數字 = 負數,例如 (0.06) → -0.06、(19.32) → -19.32
- 營收單位為百萬元
- 百分比欄位(如「與去年同期增減%」)不是我們要的,要跳過
- latest_month_label: 只填月份數字,例如「3月」、「11月」(從欄位標題擷取,不含年份)
- latest_quarter_label: 只填季別,例如「第1季」、「第4季」(從欄位標題擷取)
- 月份/季別必須從文本擷取,不可推算
- 找不到填 null"""

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=250,
            temperature=0,
        )
        text = response.choices[0].message.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)

        if data.get("is_bond"):
            return ""

        revenue_text = calc_revenue_growth(data)
        eps_growth_text = calc_eps_growth(data)

        return (
            f"【{company_id} {company_name}】\n"
            f"{eps_growth_text}\n\n"
            f"{revenue_text}"
        )

    except Exception as e:
        print(f"Groq 分析失敗 ({company_id}): {e}")
        return f"【{company_id} {company_name}】\n⚠️ 無法取得分析"


# ── MOPS 爬蟲 ────────────────────────────────
def _create_mops_session():
    """建立 MOPS 用的 requests.Session"""
    session = requests.Session()
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/146.0.0.0 Safari/537.36",
        "Origin": "https://mops.twse.com.tw",
        "Referer": "https://mops.twse.com.tw/mops/web/t05st02",
        "content-type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    })
    session.get("https://mops.twse.com.tw/mops/web/t05st02")
    return session



NOTICE_KEYWORDS = ("注意", "證券近期","異常")
# 可轉債/公司債公告的類別關鍵字 — 符合此條件的公告直接排除，不抓明細
BOND_CATEGORY_KEYWORDS = ("可轉換公司債", "轉換公司債", "普通公司債", "海外可轉換公司債")

def _fetch_notice_items(session, year: str, month: str, day: str) -> Optional[list]:
    """
    從 MOPS 抓取注意清單原始資料
    回傳 notice_items list,無資料回傳空 list,解析失敗回傳 None
    """
    response = session.post(
        "https://mops.twse.com.tw/mops/api/t05st02",
        json={"year": year, "month": month, "day": day},
    )

    print(f"[MOPS] HTTP {response.status_code}, body長度: {len(response.text)}")

    if not response.text.strip():
        print(f"[MOPS] 回應為空，MOPS 可能暫時無回應")
        return []

    try:
        data = response.json()
        if not data or "result" not in data or data["result"] is None:
            return []
        result_val = data["result"]
        if not isinstance(result_val, dict):
            print(f"[MOPS] result 型態異常: {type(result_val)} → {result_val}")
            return []
        rows = result_val.get("data", [])
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        print(f"解析 MOPS 資料時發生錯誤: {e}")
        print(f"[MOPS] 原始 HTTP 狀態碼: {response.status_code}")
        print(f"[MOPS] 原始回應內容(前500字): {response.text[:500]}")
        return None

    if not rows:
        return []

    notice_items = []
    for row in rows:
        category = row[4]
        # 符合注意關鍵字，且公告類別本身不是可轉債/公司債
        if any(kw in category for kw in NOTICE_KEYWORDS) and \
           not any(bk in category for bk in BOND_CATEGORY_KEYWORDS):
            notice_items.append({
                "company_id": row[2],
                "company_name": row[3],
                "params": row[5]["parameters"],
            })
        elif any(kw in category for kw in NOTICE_KEYWORDS):
            print(f"[MOPS] 跳過可轉債公告: {row[2]} {row[3]} → {category[:40]}")

    if notice_items:
        filtered_summary = ", ".join(f"{it['company_id']} {it['company_name']}" for it in notice_items)
        print(f"[MOPS] 符合關鍵字({'/'.join(NOTICE_KEYWORDS)})共 {len(notice_items)} 筆: {filtered_summary}")
    else:
        print(f"[MOPS] 無符合關鍵字({'/'.join(NOTICE_KEYWORDS)})的項目")

    return notice_items




def _fetch_raw_texts(session, notice_items: list) -> dict[str, dict]:
    """逐一抓 MOPS 明細，回傳 {company_id: {"name":..., "text":...}}，不呼叫 Groq"""
    raw_texts = {}
    for item in notice_items:
        cid = item["company_id"]
        cname = item["company_name"]
        params = item["params"]

        try:
            detail_resp = session.post(
                "https://mops.twse.com.tw/mops/api/t05st02_detail",
                json=params,
            )
            detail = detail_resp.json()
        except Exception as e:
            print(f"抓取 {cid} 明細失敗: {e}")
            continue

        if detail.get("code") != 200:
            continue

        for row in detail["result"]["data"]:
            new_text = row[9]
            if cid in raw_texts:
                # 同公司有多筆公告（例如同時有股票注意 + 另一筆）→ 拼接，Groq 一次分析全部
                raw_texts[cid]["text"] += "\n\n" + new_text
                print(f"[RAW] {cid} {cname}: 拼接第二筆（共 {len(raw_texts[cid]['text'])} 字）")
            else:
                raw_texts[cid] = {"name": cname, "text": new_text}
                print(f"[RAW] {cid} {cname}: {len(new_text)} 字")

        time.sleep(2)

    return raw_texts


def _analyze_raw_texts(raw_texts: dict[str, dict]) -> dict[str, str]:
    """對每筆 raw_text 呼叫 Groq 分析，回傳 {company_id: block_text}"""
    results = {}
    for cid, item in raw_texts.items():
        block = analyze_with_groq_single(item["text"], cid, item["name"])
        if block:
            results[cid] = block
    return results


def _format_push_message(date_key: str, blocks: dict[str, str]) -> str:
    """組合推播訊息"""
    if not blocks:
        return ""

    header = (
        f"公開資訊觀測站-注意清單\n"
        f"查詢日期:{date_key}\n"
        f"共 {len(blocks)} 筆\n"
        + "─" * 13
    )
    body = ("\n" + "─" * 13 + "\n").join(blocks.values())
    return f"{header}\n\n{body}"


LOCK_KEY_PREFIX = "mops:lock:"
LOCK_TTL_SECONDS = 300  # 最多鎖 5 分鐘


def _acquire_lock(date_key: str) -> bool:
    """用 Redis SET NX 取得 lock，成功回 True"""
    if redis_client is None:
        return True
    try:
        key = f"{LOCK_KEY_PREFIX}{date_key}"
        result = redis_client.set(key, "1", ex=LOCK_TTL_SECONDS, nx=True)
        return result is not None
    except Exception as e:
        print(f"[Lock] acquire 失敗: {e}")
        return True


def _release_lock(date_key: str):
    if redis_client is None:
        return
    try:
        redis_client.delete(f"{LOCK_KEY_PREFIX}{date_key}")
    except Exception as e:
        print(f"[Lock] release 失敗: {e}")


def fetch_eps(year: str, month: str, day: str) -> str:
    """抓 MOPS 注意清單並回傳完整訊息,有 cache 先回 cache,否則抓 MOPS 後寫入 cache"""
    date_key = f"{year}/{month}/{day}"

    cached = get_cached_result(date_key)
    if cached:
        return cached

    if not _acquire_lock(date_key):
        print(f"[Lock] {date_key} 已有其他程序在抓，等待後回 cache")
        for _ in range(30):
            time.sleep(3)
            cached = get_cached_result(date_key)
            if cached:
                return cached
        return f"⚠️ 查詢 {date_key} 逾時，請稍後再試"

    try:
        # double-check：lock 拿到後再確認一次 cache
        cached = get_cached_result(date_key)
        if cached:
            return cached

        # 優先讀 GitHub Actions 預先存好的 raw_texts
        raw_texts = get_cached_raw_texts(date_key)

        if raw_texts is None:
            # fallback：自己抓 MOPS
            session = _create_mops_session()
            notice_items = _fetch_notice_items(session, year, month, day)

            if notice_items is None:
                return f"⚠️ 無法解析 {date_key} 的資料"

            if not notice_items:
                result = f"{date_key} 無注意清單資料"
                set_cached_result(date_key, result)
                return result

            raw_texts = _fetch_raw_texts(session, notice_items)

        if not raw_texts:
            result = f"{date_key} 無注意清單資料"
            set_cached_result(date_key, result)
            return result

        blocks = _analyze_raw_texts(raw_texts)

        if not blocks:
            return "⚠️ 無法取得詳細 EPS 資料"

        result = _format_push_message(date_key, blocks)
        set_cached_result(date_key, result)
        return result
    finally:
        _release_lock(date_key)


# ── TWSE 休市日載入 ───────────────────────────
def fetch_twse_holidays() -> set[str]:
    """
    [新增] 從 TWSE Open API 抓取當年所有休市日，回傳民國日期字串 set。
    判斷條件：Description 含「休市/放假/停止交易」，或 Name 含「市場無交易」
    （部分休市日 description 為空，需改由 name 欄位判斷，例如春節前結算日）
    """
    resp = requests.get(TWSE_HOLIDAY_API, timeout=10)
    resp.raise_for_status()
    holidays = set()
    for item in resp.json():
        desc = item.get("Description", "")
        name = item.get("Name", "")
        if "休市" in desc or "放假" in desc or "停止交易" in desc or "市場無交易" in name:
            holidays.add(item["Date"])
    return holidays


# ── 交易日工具 ────────────────────────────────
def to_roc_date_str(d: datetime) -> str:
    """[新增] datetime → 民國日期字串（7碼），例如 '1150619'，用於與 TWSE 休市日比對"""
    return f"{d.year - 1911}{d.month:02d}{d.day:02d}"


def get_last_trading_day(now: datetime) -> datetime:
    # [修改] 原本只跳週末；現在額外比對 _holidays（TWSE 國定假日），
    # 確保不落在例假日（如端午節週五、補假週一等）
    d = now - timedelta(days=1)
    while True:
        if d.weekday() >= 5:                  # 跳過週六(5)、週日(6)
            d -= timedelta(days=1)
            continue
        if to_roc_date_str(d) in _holidays:   # 跳過國定假日
            d -= timedelta(days=1)
            continue
        break
    return d


def to_roc_ymd(d: datetime) -> tuple[str, str, str]:
    return str(d.year - 1911), str(d.month).zfill(2), str(d.day).zfill(2)


# ── LINE 推播輔助 ─────────────────────────────
def _is_invalid_target_error(e: Exception) -> bool:
    if isinstance(e, ApiException):
        return e.status in (400, 403, 404)
    return False


def push_final_result(target_id: str, message_text: str):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            line_bot_api.push_message(
                PushMessageRequest(
                    to=target_id,
                    messages=[TextMsg(type='text', text=message_text)]
                )
            )
        except Exception as e:
            print(f"Push Message 失敗 ({target_id}): {e}")
            if _is_invalid_target_error(e):
                print(f"[Clean] 移除失效訂閱者 {target_id}")
                remove_user(target_id)


def send_immediate_reply(token: str, text: str):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=token,
                    messages=[TextMsg(type='text', text=text)]
                )
            )
        except Exception as e:
            print(f"Reply 失敗: {e}")


# ── 背景任務 ─────────────────────────────────
def task_fetch_and_push(year: str, month: str, day: str, target_id: str):
    print(f"[Task] 背景抓取 {year}/{month}/{day} → {target_id}")
    try:
        result = fetch_eps(year, month, day)
    except Exception as e:
        print(f"[Task] fetch_eps 失敗: {e}")
        result = f"⚠️ 查詢 {year}/{month}/{day} 失敗: {type(e).__name__}"
    push_final_result(target_id, result)


# ── 推播到所有訂閱者 ─────────────────────────
def _push_to_all_users(message: str):
    users = load_users()
    if not users:
        print("[Info] 目前無訂閱用戶")
        return

    print(f"[排程] 推播給 {len(users)} 位用戶")
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        for user_id in users:
            try:
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMsg(type='text', text=message)]
                    )
                )
            except Exception as e:
                print(f"推播失敗給 {user_id}: {e}")
                if _is_invalid_target_error(e):
                    print(f"[Clean] 移除失效訂閱者 {user_id}")
                    remove_user(user_id)


# ── 排程推播 ─────────────────────────────────
def scheduled_push():
    """每日 07:30 讀取 raw_texts → Groq 分析 → 推播給所有訂閱者"""
    now_taipei = datetime.now(TZ)
    print(f"[排程觸發] 當前台灣時間: {now_taipei}")
    target_date = get_last_trading_day(now_taipei)
    y, m, d = to_roc_ymd(target_date)
    date_key = f"{y}/{m}/{d}"
    print(f"[排程] 查詢前一交易日 {date_key}")

    try:
        message = fetch_eps(y, m, d)
    except Exception as e:
        # fetch_eps 拋出未預期例外時，推播錯誤通知讓用戶知道，避免靜默失敗
        print(f"[排程] fetch_eps 例外: {e}")
        _push_to_all_users(f"⚠️ 今日推播失敗（{date_key}）\n錯誤：{type(e).__name__}")
        return

    if "無注意清單資料" in message or "查無資料" in message:
        print(f"[Info] {date_key} 無注意清單，取消推播")
        return

    _push_to_all_users(message)


# ── Lifespan ──────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    # [新增] 啟動時從 TWSE API 載入當年休市日，存入 _holidays 供 get_last_trading_day 使用
    global _holidays
    try:
        _holidays = fetch_twse_holidays()
        print(f"✅ TWSE 休市日載入完成，共 {len(_holidays)} 筆")
    except Exception as e:
        # 載入失敗時 _holidays 維持空 set，退化成只跳週末（不中斷服務）
        print(f"⚠️ 無法載入 TWSE 休市日（將只跳過週末）: {e}")

    scheduler = BackgroundScheduler(timezone="Asia/Taipei")
    scheduler.add_job(
        scheduled_push, 'cron',
        day_of_week='mon-fri', hour=7, minute=30,
        id='push_morning'
    )
    scheduler.start()
    print("✅ Scheduler 已啟動 (每日 07:30 推播前一交易日注意清單)")
    yield
    scheduler.shutdown()
    print("🛑 Scheduler 已關閉")


app = FastAPI(lifespan=lifespan)


@app.get("/")
def health_check():
    return {"status": "ok"}


@app.get("/myip")
def myip():
    try:
        ip = requests.get("https://api.ipify.org", timeout=5).text
        return {"outbound_ip": ip}
    except Exception as e:
        return {"error": str(e)}


@app.post("/webhook")
async def webhook(request: Request, x_line_signature: str = Header(...)):
    body = await request.body()
    try:
        handler.handle(body.decode(), x_line_signature)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok"}


# ── Bot 被加入群組 / 聊天室 → 自動訂閱 ────────
@handler.add(JoinEvent)
def handle_join(event):
    target_id = None
    if event.source.type == "group":
        target_id = event.source.group_id
    elif event.source.type == "room":
        target_id = event.source.room_id

    if target_id:
        save_user(target_id)

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMsg(type='text', text=(
                    "✅ 已自動訂閱!\n"
                    "每日早上 7:30 推播前一交易日注意股票的EPS\n"
                    "📋 其他指令:\n"
                    "  今日 → 查今天 EPS 注意清單\n"
                    "  1150327 → 查指定日期\n"
                    "  取消訂閱 → 停止推播"
                ))]
            )
        )


# ── 文字訊息處理 ─────────────────────────────
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    source_type = event.source.type
    if source_type == "group":
        target_id = event.source.group_id
    elif source_type == "room":
        target_id = event.source.room_id
    else:
        target_id = user_id

    text = event.message.text.strip()

    is_keyword = text in ["訂閱", "取消訂閱", "今日", "today", "指令", "前一交易日警示"]
    is_date_query = bool(re.match(r"^\d{7}$", text))
    if not (is_keyword or is_date_query):
        return

    if text == "訂閱":
        save_user(target_id)
        send_immediate_reply(
            event.reply_token,
            "✅ 已訂閱!\n"
            "每日早上 7:30 自動推播前一交易日注意股EPS"
        )

    elif text == "取消訂閱":
        remove_user(target_id)
        send_immediate_reply(event.reply_token, "❌ 已取消訂閱")

    elif text == "指令":
        send_immediate_reply(event.reply_token, (
            "📋 指令說明:\n"
            "  今日 → 查今天 EPS 注意清單\n"
            "  前一交易日警示 → 查前一個交易日注意清單\n"
            "  1150327 → 查指定日期\n"
            "  訂閱 → 每天 7:30 自動推播\n"
            "  取消訂閱 → 停止推播"
        ))

    elif text in ["今日", "today"] or text == "前一交易日警示" or re.match(r"^\d{7}$", text):
        if text in ["今日", "today"]:
            now = datetime.now(TZ)
            y, m, d = to_roc_ymd(now)
        elif text == "前一交易日警示":
            now = datetime.now(TZ)
            y, m, d = to_roc_ymd(get_last_trading_day(now))
        else:
            y, m, d = text[:3], text[3:5], text[5:7]

        # ── 原始做法：背景查詢 + push_message（計月配額）── 下個月初改回來
        # send_immediate_reply(
        #     event.reply_token,
        #     f"🔍 正在查詢 {y}/{m}/{d} 資料,請稍候約 1 分鐘..."
        # )
        # thread = threading.Thread(
        #     target=task_fetch_and_push,
        #     args=(y, m, d, target_id),
        #     daemon=True,
        # )
        # thread.start()

        # ── 替代做法：同步查詢 + reply_message（不計配額，但需等待）──
        result = fetch_eps(y, m, d)
        send_immediate_reply(event.reply_token, result)




# ── 啟動 ─────────────────────────────────────
if __name__ == "__main__":
    from pyngrok import ngrok, conf
    import uvicorn                                                                                                                                                                      
   

    conf.get_default().auth_token = os.environ['NGROK_AUTHTOKEN']
    public_url = ngrok.connect(8000)
    print(f"\n✅ Webhook URL: {public_url}/webhook\n")

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False) # 本機
    # uvicorn.run("main_v1:app", host="0.0.0.0", port=port, reload=False)  # Render
