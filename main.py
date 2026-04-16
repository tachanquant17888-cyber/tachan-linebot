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
# USERS_FILE = "users.json"

# ── Upstash Redis ─────────────────────────────
# 用 from_env() 會自動讀 UPSTASH_REDIS_REST_URL 和 UPSTASH_REDIS_REST_TOKEN
try:
    redis_client = Redis.from_env()
    print("✅ Upstash Redis 已連線")
except Exception as e:
    print(f"⚠️ Upstash Redis 連線失敗,將 fallback 到記憶體: {e}")
    redis_client = None

# Redis key 名稱
USERS_KEY = "linebot:users"

# Fallback:Redis 連不到時用記憶體(重啟會消失,但至少不會 crash)
_users_fallback: set = set()
_users_lock = threading.Lock()

# ── Cache ─────────────────────────────────────
_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_TTL_HOURS = 1


def get_cache(date_key: str) -> Optional[str]:
    with _cache_lock:
        entry = _cache.get(date_key)
        if entry and datetime.now() < entry["expired_at"]:
            print(f"[Cache HIT] {date_key}")
            return entry["result"]
    print(f"[Cache MISS] {date_key}")
    return None


def set_cache(date_key: str, result: str):
    with _cache_lock:
        _cache[date_key] = {
            "result": result,
            "expired_at": datetime.now() + timedelta(hours=CACHE_TTL_HOURS)
        }
    print(f"[Cache SET] {date_key}")


# ── 訂閱名單管理(thread-safe)──────────────────
_users_lock = threading.Lock()

# === 原始
# def load_users() -> list:
#     if not os.path.exists(USERS_FILE):
#         return []
#     try:
#         with open(USERS_FILE, "r") as f:
#             return json.load(f)
#     except (json.JSONDecodeError, OSError) as e:
#         print(f"[Warn] users.json 讀取失敗: {e}")
#         return []


def load_users() -> list:
    """從 Redis 讀取訂閱者列表"""
    if redis_client is None:
        with _users_lock:
            return list(_users_fallback)
    try:
        # smembers 回傳 set,轉成 list
        users = redis_client.smembers(USERS_KEY)
        return list(users) if users else []
    except Exception as e:
        print(f"[Redis] load_users 失敗,使用 fallback: {e}")
        with _users_lock:
            return list(_users_fallback)


# ===原始
# def save_user(user_id: str):
#     with _users_lock:
#         users = load_users()
#         if user_id not in users:
#             users.append(user_id)
#             with open(USERS_FILE, "w") as f:
#                 json.dump(users, f)

def save_user(user_id: str):
    """加入訂閱者(Redis SET 自動去重)"""
    if redis_client is None:
        with _users_lock:
            _users_fallback.add(user_id)
        return
    try:
        redis_client.sadd(USERS_KEY, user_id)
        print(f"[Redis] 新增訂閱者: {user_id}")
    except Exception as e:
        print(f"[Redis] save_user 失敗,寫入 fallback: {e}")
        with _users_lock:
            _users_fallback.add(user_id)


# === 原始
# def remove_user(user_id: str):
#     with _users_lock:
#         users = load_users()
#         if user_id in users:
#             users.remove(user_id)
#             with open(USERS_FILE, "w") as f:
#                 json.dump(users, f)

def remove_user(user_id: str):
    """移除訂閱者"""
    if redis_client is None:
        with _users_lock:
            _users_fallback.discard(user_id)
        return
    try:
        redis_client.srem(USERS_KEY, user_id)
        print(f"[Redis] 移除訂閱者: {user_id}")
    except Exception as e:
        print(f"[Redis] remove_user 失敗,從 fallback 移除: {e}")
        with _users_lock:
            _users_fallback.discard(user_id)


# ── Groq 單次分析(EPS + 營收數字 + 成長率)────
def calc_revenue_growth(data: dict) -> str:
    a = data.get("latest_month_revenue")
    b = data.get("latest_quarter_revenue")
    m_label = data.get("latest_month_label") or ""
    q_label = data.get("latest_quarter_label") or ""

    # label 顯示用,有就加括號,沒有就空字串
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
            max_tokens=300,
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
def fetch_eps(year: str, month: str, day: str) -> str:
    date_key = f"{year}/{month}/{day}"

    cached = get_cache(date_key)
    if cached:
        return cached

    session = requests.Session()
    session.verify = False
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Origin": "https://mops.twse.com.tw",
        "Referer": "https://mops.twse.com.tw/mops/web/t05st02",
        "content-type": "application/json",
    }

    session.get("https://mops.twse.com.tw/mops/web/t05st02", headers=headers)
    response = session.post(
        "https://mops.twse.com.tw/mops/api/t05st02",
        json={"year": year, "month": month, "day": day},
        headers=headers,
    )

    print(f"response : {response.json()}")

    try:
        data = response.json()
        if not data or "result" not in data or data["result"] is None:
            result = f"⚠️ {year}/{month}/{day} 查無資料或網站回傳異常"
            set_cache(date_key, result)
            return result
        rows = data["result"].get("data", [])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"解析 MOPS 資料時發生錯誤: {e}")
        return f"⚠️ 無法解析 {year}/{month}/{day} 的資料"

    if not rows:
        result = f"{year}/{month}/{day} 無注意清單資料"
        set_cache(date_key, result)
        return result

    notice_items = [row for row in rows if "注意" in row[4]]

    if not notice_items:
        result = f"{year}/{month}/{day} 無注意清單資料"
        set_cache(date_key, result)
        return result

    blocks = []
    for item in notice_items:
        company_id = item[2]
        company_name = item[3]
        params = item[5]["parameters"]

        try:
            detail_resp = session.post(
                "https://mops.twse.com.tw/mops/api/t05st02_detail",
                json=params,
                headers=headers,
            )
            detail = detail_resp.json()
        except Exception as e:
            print(f"抓取 {company_id} 明細失敗: {e}")
            continue

        if detail.get("code") != 200:
            continue

        for row in detail["result"]["data"]:
            raw_text = row[9]
            block = analyze_with_groq_single(raw_text, company_id, company_name)
            if block:
                blocks.append(block)

        # 避免被 MOPS 封 IP
        time.sleep(2)

    if not blocks:
        result = "⚠️ 無法取得詳細 EPS 資料"
        set_cache(date_key, result)
        return result

    header = (
        f"公開資訊觀測站-注意清單\n"
        f"查詢日期:{year}/{month}/{day}\n"
        f"共 {len(blocks)} 筆\n"
        + "─" * 13
    )
    body = ("\n" + "─" * 13 + "\n").join(blocks)
    result = f"{header}\n\n{body}"

    set_cache(date_key, result)
    return result


# ── 交易日工具 ────────────────────────────────
def get_last_trading_day(now: datetime) -> datetime:
    """
    回傳「上一個可能的交易日」(僅處理週末,國定假日由 fetch_eps 回傳的
    '無資料' 訊息判斷後,由 scheduled_push 繼續往前找)
    """
    d = now - timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


def to_roc_ymd(d: datetime) -> tuple[str, str, str]:
    return str(d.year - 1911), str(d.month).zfill(2), str(d.day).zfill(2)


# ── LINE 推播輔助 ─────────────────────────────
def _is_invalid_target_error(e: Exception) -> bool:
    """判斷是否為使用者封鎖 / ID 失效,這類錯誤應清掉該訂閱者"""
    if isinstance(e, ApiException):
        # 400 通常是 invalid user id / blocked
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
    """背景執行爬蟲,完成後 Push。包 try/except 避免 thread 靜默死亡"""
    print(f"[Task] 背景抓取 {year}/{month}/{day} → {target_id}")
    try:
        result = fetch_eps(year, month, day)
    except Exception as e:
        print(f"[Task] fetch_eps 失敗: {e}")
        result = f"⚠️ 查詢 {year}/{month}/{day} 失敗: {type(e).__name__}"
    push_final_result(target_id, result)


# ── 排程推播 ─────────────────────────────────
def scheduled_push(mode: str = 'previous'):
    """
    排程推播
    mode='previous': 推前一交易日(早上 08:30 用,會回推找有資料的交易日)
    mode='today'   : 推今日資料(下午 17:00 用,不回推)
    """
    now_taipei = datetime.now(TZ)
    print(f"[排程觸發] mode={mode}, 當前台灣時間: {now_taipei}")

    # 週末不推
    if now_taipei.weekday() >= 5:
        print(f"[Skip] 今日 {now_taipei.date()} 為週末")
        return

    if mode == 'today':
        # 下午盤後:直接抓今天,不回推
        y, m, d = to_roc_ymd(now_taipei)
        print(f"[排程-盤後] 查詢今日 {y}/{m}/{d}")
        message = fetch_eps(y, m, d)

        # 今日無資料就不推(避免騷擾)
        if "無注意清單資料" in message or "查無資料" in message:
            print(f"[Info] {y}/{m}/{d} 無注意清單,取消推播")
            return
    else:
        # 早上:推前一交易日,最多回推 7 天(處理連假)
        target_date = get_last_trading_day(now_taipei)
        message = None
        for _ in range(7):
            y, m, d = to_roc_ymd(target_date)
            print(f"[排程-早盤] 嘗試查詢 {y}/{m}/{d}")
            msg = fetch_eps(y, m, d)

            if "無注意清單資料" in msg or "查無資料" in msg:
                print(f"[排程] {y}/{m}/{d} 無資料,往前一天")
                target_date -= timedelta(days=1)
                while target_date.weekday() >= 5:
                    target_date -= timedelta(days=1)
                continue

            message = msg
            break

        if not message:
            print("[Info] 回推 7 天仍無資料,取消推播")
            return

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


# ── Lifespan(必須在 FastAPI 建立前定義)──────
@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = BackgroundScheduler(timezone="Asia/Taipei")
    scheduler.add_job(
        scheduled_push, 'cron',
        day_of_week='mon-fri', hour=8, minute=30,
        id='push_morning'
    )

    # 下午 17:00:推播「今日」自結資訊
    scheduler.add_job(
        scheduled_push, 'cron',
        day_of_week='mon-fri', hour=17, minute=00,
        args=['today'],
        id='push_afternoon'
    )
    scheduler.start()
    print("✅ Scheduler 已啟動 (平日 08:30 推前一日 / 17:00 推今日)")
    yield
    scheduler.shutdown()
    print("🛑 Scheduler 已關閉")


# ── FastAPI app(只建立一次!)─────────────────
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
                    "平日早上 8:30 推播前一交易日注意股票的EPS\n"
                    "平日下午 17:00 推播當日交易日注意股票的EPS \n"
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

    # 過濾非關鍵字 / 非日期格式,完全不回話
    is_keyword = text in ["訂閱", "取消訂閱", "今日", "today", "指令"]
    is_date_query = bool(re.match(r"^\d{7}$", text))
    if not (is_keyword or is_date_query):
        return

    if text == "訂閱":
        save_user(target_id)
        send_immediate_reply(
            event.reply_token,
            "✅ 已訂閱!平日早上 8:30 自動推播前一日注意股EPS。 \n 平日下午 17:00 自動推播當日注意股EPS"
        )

    elif text == "取消訂閱":
        remove_user(target_id)
        send_immediate_reply(event.reply_token, "❌ 已取消訂閱")

    elif text == "指令":
        send_immediate_reply(event.reply_token, (
            "📋 指令說明:\n"
            "  今日 → 查今天 EPS 注意清單\n"
            "  1150327 → 查指定日期\n"
            "  訂閱 → 每天 8:30 自動推播\n"
            "  取消訂閱 → 停止推播"
        ))

    elif text in ["今日", "today"] or re.match(r"^\d{7}$", text):
        if text in ["今日", "today"]:
            now = datetime.now(TZ)
            y, m, d = to_roc_ymd(now)
        else:
            y, m, d = text[:3], text[3:5], text[5:7]

        date_key = f"{y}/{m}/{d}"
        cached = get_cache(date_key)
        if cached:
            send_immediate_reply(event.reply_token, cached)
        else:
            send_immediate_reply(
                event.reply_token,
                f"🔍 正在查詢 {date_key} 資料,請稍候約 1 分鐘..."
            )
            thread = threading.Thread(
                target=task_fetch_and_push,
                args=(y, m, d, target_id),
                daemon=True,
            )
            thread.start()


# ── 啟動 ─────────────────────────────────────
# 開發模式:python main.py (會啟動 ngrok + reload,但 reload=True 會讓
#           scheduler 跑兩份,因此開發時排程會重複觸發,僅測試用)
# 正式模式:uvicorn main:app --host 0.0.0.0 --port 8000
#           (不要開 reload,scheduler 才會只跑一次)
if __name__ == "__main__":
    from pyngrok import ngrok, conf
    import uvicorn

    conf.get_default().auth_token = os.environ['NGROK_AUTHTOKEN']
    public_url = ngrok.connect(8000)
    print(f"\n✅ Webhook URL: {public_url}/webhook\n")
    print("⚠️ 開發模式:reload=True 會造成 scheduler 重複啟動,僅供測試\n")

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False) # 本地
    fetch_eps('115','04','15')
    # uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False) # Render