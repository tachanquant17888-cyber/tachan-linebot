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

ADMIN_USER_IDS = {
    uid.strip() for uid in os.environ.get("ADMIN_USER_IDS", "").split(",") if uid.strip()
}

TZ = ZoneInfo("Asia/Taipei")

# ── Upstash Redis ─────────────────────────────
try:
    redis_client = Redis.from_env()
    print("✅ Upstash Redis 已連線")
except Exception as e:
    print(f"⚠️ Upstash Redis 連線失敗: {e}")
    redis_client = None

# Redis key 定義
USERS_KEY = "linebot:users"
RESULT_KEY_PREFIX = "mops:result:"          # 當日完整訊息 (TTL 24hr)
COMPANIES_KEY_PREFIX = "mops:companies:"    # 17:00 基準公司代號 SET (TTL 24hr)

# Fallback
_users_fallback: set = set()
_users_lock = threading.Lock()


# ── Redis 讀寫 ────────────────────────────────
RESULT_TTL_SECONDS = 24 * 60 * 60       # 24 小時
COMPANIES_TTL_SECONDS = 24 * 60 * 60    # 24 小時


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


def get_pushed_companies(date_key: str) -> set:
    """取得該日期已推播過的公司代號集合"""
    if redis_client is None:
        return set()
    try:
        key = f"{COMPANIES_KEY_PREFIX}{date_key}"
        members = redis_client.smembers(key)
        return set(members) if members else set()
    except Exception as e:
        print(f"[Redis] get_pushed_companies 失敗: {e}")
        return set()


def add_pushed_companies(date_key: str, company_ids: list[str]):
    """把公司代號加入已推播集合 (Redis SET, TTL 24hr)"""
    if redis_client is None or not company_ids:
        return
    try:
        key = f"{COMPANIES_KEY_PREFIX}{date_key}"
        redis_client.sadd(key, *company_ids)
        redis_client.expire(key, COMPANIES_TTL_SECONDS)
        print(f"[Redis] 已記錄 {len(company_ids)} 家公司至 {key}")
    except Exception as e:
        print(f"[Redis] add_pushed_companies 失敗: {e}")


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
- 表格通常有「最近一月」或「近一月」和「最近一季」或「最近一期」或「近一季」兩欄數字
- 「每股盈餘」或「EPS」那列 → 取出 latest_month_eps 和 latest_quarter_eps
- 「營業收入」那列 → 取出 latest_month_revenue 和 latest_quarter_revenue(單位:百萬)
- is_bond: 若含「公司債」或「可轉債」填 true,其餘填false
- EPS跟每股盈餘如果是虧損請填負數,如 (0.19) → -0.19
- 括號數字 = 負數,例如 (0.06) → -0.06、(19.32) → -19.32
- 營收單位為百萬元
- 百分比欄位(如「與去年同期增減%」)不是我們要的,要跳過
- latest_month_label: 只填月份數字,例如「1月」、「11月」(不含年份)
- latest_quarter_label: 只填季別,例如「第1季」、「第4季」
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
    })
    session.get("https://mops.twse.com.tw/mops/web/t05st02")
    return session



NOTICE_KEYWORDS = ("注意", "證券近期")

def _fetch_notice_items(session, year: str, month: str, day: str) -> Optional[list]:
    """
    從 MOPS 抓取注意清單原始資料
    回傳 notice_items list,無資料回傳空 list,解析失敗回傳 None
    """
    response = session.post(
        "https://mops.twse.com.tw/mops/api/t05st02",
        json={"year": year, "month": month, "day": day},
    )

    try:
        data = response.json()
        print(f"response : {data}")
        if not data or "result" not in data or data["result"] is None:
            return []
        rows = data["result"].get("data", [])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"解析 MOPS 資料時發生錯誤: {e}")
        return None

    if not rows:
        return []

    notice_items = []
    for row in rows:
        if any(kw in row[4] for kw in NOTICE_KEYWORDS):
            notice_items.append({
                "company_id": row[2],
                "company_name": row[3],
                "params": row[5]["parameters"],
            })
    return notice_items




def _analyze_notice_items(
    session,
    notice_items: list,
    skip_company_ids: Optional[set] = None,
) -> dict[str, str]:
    """
    逐一抓明細 + Groq 分析
    skip_company_ids: 要跳過的公司代號 (差量比對用,目前未啟用)
    回傳 dict: { company_id: block_text }
    """
    results = {}
    for item in notice_items:
        cid = item["company_id"]
        cname = item["company_name"]
        params = item["params"]

        if skip_company_ids and cid in skip_company_ids:
            print(f"[Skip] {cid} {cname} (已推播過)")
            continue

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
            raw_text = row[9]
            block = analyze_with_groq_single(raw_text, cid, cname)
            if block:
                results[cid] = block

        time.sleep(2)

    return results


def _format_push_message(date_key: str, blocks: dict[str, str], is_delta: bool = False) -> str:
    """組合推播訊息"""
    if not blocks:
        return ""

    tag = "（早盤補充）" if is_delta else ""
    header = (
        f"公開資訊觀測站-注意清單{tag}\n"
        f"查詢日期:{date_key}\n"
        f"共 {len(blocks)} 筆\n"
        + "─" * 13
    )
    body = ("\n" + "─" * 13 + "\n").join(blocks.values())
    return f"{header}\n\n{body}"


def fetch_eps(year: str, month: str, day: str, record_pushed: bool = False) -> str:
    """
    抓 MOPS 當日注意清單並回傳完整訊息

    record_pushed=False (使用者手動查):
        先讀 cache,hit 回,miss 抓但不寫 cache / SET
    record_pushed=True  (17:00 排程):
        強制抓 MOPS,寫 cache + 寫 baseline SET
    """
    date_key = f"{year}/{month}/{day}"

    if not record_pushed:
        cached = get_cached_result(date_key)
        if cached:
            return cached

    session = _create_mops_session()
    notice_items = _fetch_notice_items(session, year, month, day)

    if notice_items is None:
        return f"⚠️ 無法解析 {date_key} 的資料"

    if not notice_items:
        result = f"{date_key} 無注意清單資料"
        if record_pushed:
            set_cached_result(date_key, result)
        return result

    blocks = _analyze_notice_items(session, notice_items)

    if not blocks:
        return "⚠️ 無法取得詳細 EPS 資料"

    result = _format_push_message(date_key, blocks)

    if record_pushed:
        set_cached_result(date_key, result)
        add_pushed_companies(date_key, list(blocks.keys()))

    return result


def fetch_eps_delta(year: str, month: str, day: str) -> tuple[str, Optional[str]]:
    """
    差量查詢 (早盤 8:30 排程專用)
    只跟 17:00 寫入的 SET 比對,不更新 SET。
    成功抓到完整資料時,會用新版覆蓋 cache (讓 08:30 後查昨日可拿到最新完整版)。

    回傳 (status, message):
      ("new",     message) 有新增,message 為差量推播訊息
      ("no_new",  None)    有資料但無新增
      ("no_data", None)    該日 MOPS 無注意清單
      ("error",   None)    MOPS 解析或擷取失敗
    """
    date_key = f"{year}/{month}/{day}"

    baseline = get_pushed_companies(date_key)
    print(f"[Delta] {date_key} 17:00 基準 {len(baseline)} 家: {baseline}")

    session = _create_mops_session()
    notice_items = _fetch_notice_items(session, year, month, day)

    if notice_items is None:
        print(f"[Delta] 無法解析 {date_key} 的資料")
        return ("error", None)

    if not notice_items:
        print(f"[Delta] {date_key} 無注意清單資料")
        return ("no_data", None)

    all_blocks = _analyze_notice_items(session, notice_items)

    if not all_blocks:
        print(f"[Delta] 分析後無有效資料")
        return ("error", None)

    # 用 08:30 抓到的完整版覆蓋 17:00 的 cache
    full_message = _format_push_message(date_key, all_blocks)
    set_cached_result(date_key, full_message)

    new_blocks = {k: v for k, v in all_blocks.items() if k not in baseline}
    print(f"[Delta] 全部 {len(all_blocks)} 家, 相對 17:00 新增 {len(new_blocks)} 家")

    if not new_blocks:
        print(f"[Delta] {date_key} 無新增公司")
        return ("no_new", None)

    return ("new", _format_push_message(date_key, new_blocks, is_delta=True))


# ── 交易日工具 ────────────────────────────────
def get_last_trading_day(now: datetime) -> datetime:
    d = now - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
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


# ── 手動 diff:只回給觸發者,不廣播 ───────────
def _run_diff_reply(target_id: str):
    now_taipei = datetime.now(TZ)
    if now_taipei.weekday() >= 5:
        push_final_result(target_id, "今日為週末,無排程資料")
        return

    target_date = get_last_trading_day(now_taipei)
    y, m, d = to_roc_ymd(target_date)
    date_key = f"{y}/{m}/{d}"

    status, delta_message = fetch_eps_delta(y, m, d)

    if status == "new":
        push_final_result(target_id, delta_message)
    elif status == "no_new":
        push_final_result(target_id, f"{date_key} 比對後無更新資料")
    elif status == "no_data":
        push_final_result(target_id, f"{date_key} 無注意清單資料")
    else:
        push_final_result(
            target_id,
            "⚠️ MOPS 公開資訊觀測站系統已更換,暫時無法取得資料"
        )


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
def scheduled_push(mode: str = 'previous'):
    """
    mode='previous': 早盤 8:30 → 差量推播 (只推昨天 17:00 沒推到的新增公司)
    mode='today'   : 盤後 17:00 → 全量推播 (抓當日全部注意清單)
    """
    now_taipei = datetime.now(TZ)
    print(f"[排程觸發] mode={mode}, 當前台灣時間: {now_taipei}")

    if now_taipei.weekday() >= 5:
        print(f"[Skip] 今日 {now_taipei.date()} 為週末")
        return

    if mode == 'today':
        # ── 盤後 17:00:全量推播 ──
        y, m, d = to_roc_ymd(now_taipei)
        print(f"[排程-盤後] 查詢今日 {y}/{m}/{d}")
        message = fetch_eps(y, m, d, record_pushed=True)

        if "無注意清單資料" in message or "查無資料" in message:
            print(f"[Info] {y}/{m}/{d} 無注意清單,取消推播")
            return

        _push_to_all_users(message)

    else:
        # ── 早盤 8:30:差量推播 ──
        # get_last_trading_day 已處理週末 (週一→週五)
        target_date = get_last_trading_day(now_taipei)
        y, m, d = to_roc_ymd(target_date)
        date_key = f"{y}/{m}/{d}"
        print(f"[排程-早盤] 差量查詢 {date_key}")

        status, delta_message = fetch_eps_delta(y, m, d)

        if status == "new":
            _push_to_all_users(delta_message)
        elif status == "no_new":
            _push_to_all_users(f"{date_key} 比對後無更新資料")
        elif status == "no_data":
            _push_to_all_users(f"{date_key} 無注意清單資料")
        else:  # error
            _push_to_all_users(
                "⚠️ MOPS 公開資訊觀測站系統已更換,暫時無法取得今日資料"
            )


# ── Lifespan ──────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = BackgroundScheduler(timezone="Asia/Taipei")
    scheduler.add_job(
        scheduled_push, 'cron',
        day_of_week='mon-fri', hour=8, minute=30,
        id='push_morning'
    )
    scheduler.add_job(
        scheduled_push, 'cron',
        day_of_week='mon-fri', hour=17, minute=00,
        args=['today'],
        id='push_afternoon'
    )
    scheduler.start()
    print("✅ Scheduler 已啟動 (平日 08:30 差量推播 / 17:00 全量推播)")
    yield
    scheduler.shutdown()
    print("🛑 Scheduler 已關閉")


app = FastAPI(lifespan=lifespan)


@app.get("/")
def health_check():
    return {"status": "ok"}


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
                    "平日早上 8:30 推播前一交易日注意股票的EPS（補充 17:00 後新增）\n"
                    "平日下午 17:00 推播當日交易日注意股票的EPS\n"
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

    is_keyword = text in ["訂閱", "取消訂閱", "今日", "today", "指令", "diff"]
    is_date_query = bool(re.match(r"^\d{7}$", text))
    if not (is_keyword or is_date_query):
        return

    if text == "diff":
        if user_id not in ADMIN_USER_IDS:
            return
        # ── 原始做法：背景查詢 + push_message（計月配額）── 下個月初改回來
        # send_immediate_reply(event.reply_token, "🔍 手動差量查詢中...")
        # threading.Thread(
        #     target=_run_diff_reply,
        #     args=(target_id,),
        #     daemon=True,
        # ).start()

        # ── 替代做法：同步查詢 + reply_message（不計配額，但需等待）──
        now_taipei = datetime.now(TZ)
        if now_taipei.weekday() >= 5:
            send_immediate_reply(event.reply_token, "今日為週末,無排程資料")
        else:
            target_date = get_last_trading_day(now_taipei)
            y, m, d = to_roc_ymd(target_date)
            date_key = f"{y}/{m}/{d}"
            status, delta_message = fetch_eps_delta(y, m, d)
            if status == "new":
                send_immediate_reply(event.reply_token, delta_message)
            elif status == "no_new":
                send_immediate_reply(event.reply_token, f"{date_key} 比對後無更新資料")
            elif status == "no_data":
                send_immediate_reply(event.reply_token, f"{date_key} 無注意清單資料")
            else:
                send_immediate_reply(event.reply_token, "⚠️ MOPS 公開資訊觀測站系統已更換,暫時無法取得資料")
        return

    if text == "訂閱":
        save_user(target_id)
        send_immediate_reply(
            event.reply_token,
            "✅ 已訂閱!\n"
            "平日早上 8:30 自動推播前一日注意股EPS（補充 17:00 後新增）\n"
            "平日下午 17:00 自動推播當日注意股EPS"
        )

    elif text == "取消訂閱":
        remove_user(target_id)
        send_immediate_reply(event.reply_token, "❌ 已取消訂閱")

    elif text == "指令":
        send_immediate_reply(event.reply_token, (
            "📋 指令說明:\n"
            "  今日 → 查今天 EPS 注意清單\n"
            "  1150327 → 查指定日期\n"
            "  訂閱 → 每天 8:30 / 17:00 自動推播\n"
            "  取消訂閱 → 停止推播"
        ))

    elif text in ["今日", "today"] or re.match(r"^\d{7}$", text):
        if text in ["今日", "today"]:
            now = datetime.now(TZ)
            y, m, d = to_roc_ymd(now)
        else:
            y, m, d = text[:3], text[3:5], text[5:7]

        date_key = f"{y}/{m}/{d}"
        cached = get_cached_result(date_key)
        if cached:
            send_immediate_reply(event.reply_token, cached)
        else:
            # ── 原始做法：背景查詢 + push_message（計月配額）── 下個月初改回來
            # send_immediate_reply(
            #     event.reply_token,
            #     f"🔍 正在查詢 {date_key} 資料,請稍候約 1 分鐘..."
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
    # uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False) # 本機
    uvicorn.run("main_v1:app", host="0.0.0.0", port=port, reload=False)  # Render
