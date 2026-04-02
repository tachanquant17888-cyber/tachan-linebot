from fastapi import FastAPI, Request, Header, HTTPException
from linebot.v3.messaging import (
    ApiClient, Configuration, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage as TextMsg
)
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from groq import Groq
import requests
import urllib3
import re
import time
import json
import os
from datetime import datetime

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

app = FastAPI()

configuration = Configuration(access_token=os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])

# groq 連線
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

USERS_FILE = "users.json"

# ── 訂閱名單管理 ──────────────────────────────
def load_users():
    if not os.path.exists(USERS_FILE):
        return []
    with open(USERS_FILE, "r") as f:
        return json.load(f)

def save_user(user_id):
    users = load_users()
    if user_id not in users:
        users.append(user_id)
        with open(USERS_FILE, "w") as f:
            json.dump(users, f)

def remove_user(user_id):
    users = load_users()
    if user_id in users:
        users.remove(user_id)
        with open(USERS_FILE, "w") as f:
            json.dump(users, f)


# ---Groq 分析---
def analyze_with_groq(raw_text: str, company_id: str, company_name: str) -> str:
    prompt = f"""以下是台灣公開資訊觀測站的重大訊息原始文字：

{raw_text}

請從中擷取以下四個資訊，並嚴格按照格式回覆，不要加任何多餘說明：

最近一月(x月)：每股盈餘/每股虧損 x.xx 元
與去年同期增減：xxx%
最近一季(第x季)：每股盈餘/每股虧損 x.xx 元
與去年同期增減：xxx%

注意：
- 若為負數或括號表示的數字如 (0.19)，代表虧損，請填「每股虧損」
- 月份與季度請從文字中判斷實際期間，例如「115年2月」、「114年第4季」
- 增減%若為負數請加負號，例如 -42%"""

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
        )
        text = response.choices[0].message.content.strip()
        return f"【{company_id} {company_name}】\n{text}"
    except Exception as e:
        print(f"Groq 分析失敗 {company_id}: {e}")
        return f"【{company_id} {company_name}】\n⚠️ 無法取得分析"

# ── MOPS 爬蟲 ────────────────────────────────
def fetch_eps(year: str, month: str, day: str) -> str:
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

    rows = response.json()["result"]["data"]
    notice_items = [row for row in rows if "注意" in row[4]]

    if not notice_items:
        return f"{year}/{month}/{day} 無注意清單資料"

    blocks = []
    for item in notice_items:
        company_id = item[2]
        company_name = item[3]
        params = item[5]["parameters"]

        detail_resp = session.post(
            "https://mops.twse.com.tw/mops/api/t05st02_detail",
            json=params,
            headers=headers,
        )
        detail = detail_resp.json()

        if detail["code"] != 200:
            continue

        for row in detail["result"]["data"]:
            raw_text = row[9]
            block = analyze_with_groq(raw_text, company_id, company_name)
            blocks.append(block)

        time.sleep(0.5)

    if not blocks:
        return "⚠️ 無法取得詳細 EPS 資料"

    header = (
        f"📊 MOPS 注意清單\n"
        f"📅 {year}/{month}/{day}\n"
        f"共 {len(blocks)} 筆\n"
        + "─" * 10
    )
    body = ("\n" + "─" * 10 + "\n").join(blocks)
    return f"{header}\n\n{body}"


# ── 排程推播 ──────────────────────────────────
def scheduled_push():
    now = datetime.now()
    year = str(now.year - 1911)
    month = str(now.month).zfill(2)
    day = str(now.day).zfill(2)

    message = fetch_eps(year, month, day)
    users = load_users()

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
                print(f"推播失敗 {user_id}: {e}")


# ── Webhook ───────────────────────────────────
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


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    if text == "訂閱":
        save_user(user_id)
        reply = "✅ 已訂閱！每天早上 9:00 自動推播 EPS 注意清單"

    elif text == "取消訂閱":
        remove_user(user_id)
        reply = "❌ 已取消訂閱"

    elif text in ["今日", "today"]:
        now = datetime.now()
        year = str(now.year - 1911)
        month = str(now.month).zfill(2)
        day = str(now.day).zfill(2)
        reply = fetch_eps(year, month, day)

    elif re.match(r"^\d{7}$", text):
        year = text[:3]
        month = text[3:5]
        day = text[5:7]
        reply = fetch_eps(year, month, day)

    else:
        reply = (
            "📋 指令說明：\n"
            "  今日 → 查今天 EPS 注意清單\n"
            "  1150327 → 查指定日期（民國年月日）\n"
            "  訂閱 → 每天 9:00 自動推播\n"
            "  取消訂閱 → 停止推播"
        )

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMsg(type='text', text=reply)]
            )
        )


# ── 啟動 ─────────────────────────────────────
if __name__ == "__main__":
    from pyngrok import ngrok, conf
    import uvicorn

    conf.get_default().auth_token = os.environ['NGROK_AUTHTOKEN']

    scheduler = BackgroundScheduler(timezone="Asia/Taipei")
    scheduler.add_job(scheduled_push, 'cron', hour=9, minute=0)
    scheduler.start()

    public_url = ngrok.connect(8000)
    print(f"\n✅ Webhook URL: {public_url}/webhook\n")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)