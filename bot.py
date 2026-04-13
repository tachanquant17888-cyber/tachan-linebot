import os
import json
import re
import requests
import urllib3
from fastapi import FastAPI, Request, Header, HTTPException
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor

from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage as TextMsg,
)

from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent, JoinEvent

from groq import Groq

urllib3.disable_warnings()
load_dotenv()

# ===============================
# 基本設定
# ===============================

app = FastAPI()

configuration = Configuration(
    access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
)

handler = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])

groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

TZ = ZoneInfo("Asia/Taipei")

USERS_FILE = "users.json"

CACHE = {}
CACHE_TTL = 3600


# ===============================
# Cache
# ===============================

def get_cache(key):
    if key in CACHE:
        if datetime.now() < CACHE[key]["expire"]:
            return CACHE[key]["data"]
    return None


def set_cache(key, data):
    CACHE[key] = {
        "data": data,
        "expire": datetime.now() + timedelta(seconds=CACHE_TTL),
    }


# ===============================
# 訂閱管理
# ===============================

def load_users():
    if not os.path.exists(USERS_FILE):
        return []

    with open(USERS_FILE) as f:
        return json.load(f)


def save_user(uid):
    users = load_users()

    if uid not in users:
        users.append(uid)

        with open(USERS_FILE, "w") as f:
            json.dump(users, f)


def remove_user(uid):
    users = load_users()

    if uid in users:
        users.remove(uid)

        with open(USERS_FILE, "w") as f:
            json.dump(users, f)


# ===============================
# LLM 批次分析
# ===============================

def analyze_batch(rows):

    texts = [r[9] for r in rows]

    prompt = f"""
請分析以下多筆台股重大訊息
抓取 EPS 與營收資訊

輸出格式

公司|月EPS|季EPS|月營收|季營收

文本:
{texts}
"""

    try:

        res = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )

        return res.choices[0].message.content

    except Exception as e:

        print("LLM error", e)

        return ""


# ===============================
# 成長率
# ===============================

def calc_growth(month, quarter):

    if month is None or quarter is None:
        return "N/A"

    avg = quarter / 3

    if avg == 0:
        return "N/A"

    g = ((month - avg) / abs(avg)) * 100

    return f"{g:+.2f}%"


# ===============================
# MOPS detail
# ===============================

def fetch_detail(session, headers, item):

    cid = item[2]
    cname = item[3]

    params = item[5]["parameters"]

    try:

        r = session.post(
            "https://mops.twse.com.tw/mops/api/t05st02_detail",
            json=params,
            headers=headers,
        )

        data = r.json()

        if data["code"] != 200:
            return []

        return [
            (cid, cname, row[9])
            for row in data["result"]["data"]
        ]

    except Exception as e:

        print("detail error", e)

        return []


# ===============================
# 主查詢
# ===============================

def fetch_eps(year, month, day):

    key = f"{year}/{month}/{day}"

    cached = get_cache(key)

    if cached:
        return cached

    session = requests.Session()

    session.verify = False

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://mops.twse.com.tw",
        "Referer": "https://mops.twse.com.tw",
        "content-type": "application/json",
    }

    session.get(
        "https://mops.twse.com.tw/mops/web/t05st02",
        headers=headers,
    )

    r = session.post(
        "https://mops.twse.com.tw/mops/api/t05st02",
        json={
            "year": year,
            "month": month,
            "day": day,
        },
        headers=headers,
    )

    rows = r.json()["result"]["data"]

    notice = [r for r in rows if "注意" in r[4]]

    if not notice:
        return "無資料"

    # parallel 抓 detail
    details = []

    with ThreadPoolExecutor(max_workers=8) as ex:

        futures = [
            ex.submit(fetch_detail, session, headers, i)
            for i in notice
        ]

        for f in futures:
            details.extend(f.result())

    blocks = []

    for cid, cname, text in details:

        blocks.append(
            f"【{cid} {cname}】\n{text[:120]}"
        )

    msg = "\n────────────\n".join(blocks)

    header = f"""
公開資訊觀測站
日期 {year}/{month}/{day}
共 {len(blocks)} 筆
────────────
"""

    result = header + msg

    set_cache(key, result)

    return result


# ===============================
# Scheduler
# ===============================

def scheduled_push():

    now = datetime.now(TZ) - timedelta(days=1)

    if now.weekday() >= 5:
        return

    y = str(now.year - 1911)
    m = str(now.month).zfill(2)
    d = str(now.day).zfill(2)

    msg = fetch_eps(y, m, d)

    users = load_users()

    with ApiClient(configuration) as api_client:

        api = MessagingApi(api_client)

        for u in users:

            try:

                api.push_message(
                    PushMessageRequest(
                        to=u,
                        messages=[TextMsg(type="text", text=msg)],
                    )
                )

            except Exception as e:

                print("push error", e)


scheduler = BackgroundScheduler(timezone="Asia/Taipei")

scheduler.add_job(scheduled_push, "cron", hour=8, minute=20)

scheduler.start()


# ===============================
# webhook
# ===============================

@app.post("/webhook")
async def webhook(request: Request, x_line_signature: str = Header(...)):

    body = await request.body()

    try:

        handler.handle(body.decode(), x_line_signature)

    except Exception as e:

        raise HTTPException(400, str(e))

    return {"ok": True}


# ===============================
# Join group
# ===============================

@handler.add(JoinEvent)
def join(event):

    src = event.source

    if src.type == "group":

        gid = src.group_id

        save_user(gid)

        msg = "Bot已加入並自動訂閱"

        with ApiClient(configuration) as api_client:

            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=gid,
                    messages=[TextMsg(type="text", text=msg)],
                )
            )


# ===============================
# message handler
# ===============================

@handler.add(MessageEvent, message=TextMessageContent)
def msg(event):

    text = event.message.text.strip()

    src = event.source

    uid = src.user_id if src.type == "user" else None

    # 群組限制
    if src.type == "group":

        if not text.startswith("eps"):
            return

        text = text.replace("eps", "").strip()

    if text == "訂閱":

        save_user(uid)

        reply = "已訂閱"

    elif text == "取消訂閱":

        remove_user(uid)

        reply = "已取消"

    elif text in ["今日", "today"]:

        now = datetime.now(TZ)

        y = str(now.year - 1911)
        m = str(now.month).zfill(2)
        d = str(now.day).zfill(2)

        reply = fetch_eps(y, m, d)

    elif re.match(r"\d{7}", text):

        y = text[:3]
        m = text[3:5]
        d = text[5:7]

        reply = fetch_eps(y, m, d)

    else:

        return

    with ApiClient(configuration) as api_client:

        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMsg(type="text", text=reply)],
            )
        )


# ===============================
# health check
# ===============================

@app.get("/")
def health():
    return {"status": "ok"}