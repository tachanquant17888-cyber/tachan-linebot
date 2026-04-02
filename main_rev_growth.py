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
from typing import Optional
from datetime import datetime, timedelta
import requests
import urllib3
import re
import time
import json
import os

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

app = FastAPI()

configuration = Configuration(access_token=os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])

groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

USERS_FILE = "users.json"

# ── Cache ─────────────────────────────────────
_cache: dict = {}
CACHE_TTL_HOURS = 1


def get_cache(date_key: str) -> Optional[str]:
    entry = _cache.get(date_key)
    if entry and datetime.now() < entry["expired_at"]:
        print(f"[Cache HIT] {date_key}")
        return entry["result"]
    print(f"[Cache MISS] {date_key}")
    return None


def set_cache(date_key: str, result: str):
    _cache[date_key] = {
        "result": result,
        "expired_at": datetime.now() + timedelta(hours=CACHE_TTL_HOURS)
    }
    print(f"[Cache SET] {date_key}, 過期時間: {_cache[date_key]['expired_at']}")


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


# ── Groq EPS 分析 ─────────────────────────────
def analyze_eps_with_groq(raw_text: str) -> str:
    """讓 LLM 擷取 EPS 資訊，回傳格式化文字"""
    system_prompt = "你是一個嚴格的資料擷取系統，只輸出最終的結構化結果，絕對禁止輸出任何問候語、解釋說明、思考過程或佔位符。"

    user_prompt = f"""請從以下 <raw_text> 標籤內的台灣股市重大訊息中，擷取每股盈餘/虧損資訊。

<raw_text>
{raw_text}
</raw_text>

# 處理邏輯與輸出規則
1. 若 <raw_text> 包含「公司債」或「可轉債」字樣，請直接輸出：
⚠️ 公司債/可轉債訊息，無法提供 EPS 分析
（且不要輸出其他任何文字）

2. 若可正常擷取，請嚴格按照以下格式輸出（不要加標題或前綴文字）：
最近一月(XXX年X月)：每股[盈餘/虧損] X.XX 元
與去年同期增減：XXX%
最近一季(XXX年第X季)：每股[盈餘/虧損] X.XX 元
與去年同期增減：XXX%

3. 若完全無法在文本中找到相關 EPS 資訊，請直接輸出：
⚠️ 無法取得 EPS 資訊

# 格式細則要求
- 月份格式統一：如「115年01月」改為「115年1月」
- 季度格式統一：如「114年第四季」改為「114年第4季」
- 正負值判定：數值為負或帶括號（如 (0.19)），文字需寫「每股虧損 0.19 元」；正數則寫「每股盈餘」
- 小數點對齊：不足兩位請補零，如 0.1 → 0.10，1 → 1.00
- 增減百分比：若為負數請保留負號（如 -42%）；若原文為文字描述（如虧轉盈、年增等）請直接照寫，不要自行換算
- 衝突處理：若有兩筆同一時期的 EPS 資訊，請保留日期較新的那一筆
- 排除項目：不要輸出「最近四季累計」的資訊"""

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
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Groq EPS 分析失敗: {e}")
        return "⚠️ 無法取得 EPS 分析"


# ── Groq 營收數字擷取 ─────────────────────────
def extract_revenue_with_groq(raw_text: str) -> dict:
    """讓 LLM 只負責擷取營收數字，回傳 JSON"""
    system_prompt = "你是資料擷取系統，只輸出純 JSON，不輸出任何其他文字、markdown 標記或解釋。"

    user_prompt = f"""從以下文本中擷取營業收入資訊，以純 JSON 格式回傳：

<raw_text>
{raw_text}
</raw_text>

請擷取：
- latest_month_revenue：最近一月的營業收入數字（單位：百萬元）
- latest_quarter_revenue：最近一季的營業收入數字（單位：百萬元）

回傳格式（純 JSON，不要 markdown 的 ```）：
{{"latest_month_revenue": 數字或null, "latest_quarter_revenue": 數字或null}}

規則：
- 只擷取「營業收入」欄位，不要拿「營業利益」或其他欄位
- 數字請轉為 float，例如 "1,234.56" → 1234.56
- 單位已經是百萬元就直接填，若原文單位不同請自行換算為百萬元
- 找不到就填 null"""

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=100,
            temperature=0,
        )
        text = response.choices[0].message.content.strip()
        # 移除可能的 markdown 包裹
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except (json.JSONDecodeError, Exception) as e:
        print(f"Groq 營收擷取失敗: {e}")
        return {"latest_month_revenue": None, "latest_quarter_revenue": None}


# ── Python 營收成長率計算 ─────────────────────
def calc_revenue_growth(data: dict) -> str:
    """用 Python 做數學運算，確保零誤差"""
    a = data.get("latest_month_revenue")
    b = data.get("latest_quarter_revenue")

    if a is None or b is None:
        return "⚠️ 無法計算營收月增率（缺少營收數據）"

    if b == 0:
        return "⚠️ 無法計算營收月增率（上季營收為零）"

    avg = b / 3
    growth = ((a - avg) / avg) * 100
    sign = "+" if growth >= 0 else ""

    return (
        f"最近一月營收：{a:,.2f} 百萬\n"
        f"上季月均營收：{avg:,.2f} 百萬\n"
        f"月營收較上季月均成長：{sign}{growth:.2f}%"
    )


# ── 整合分析（EPS + 營收） ────────────────────
def analyze_with_groq(raw_text: str, company_id: str, company_name: str) -> str:
    """整合 EPS 擷取 + 營收計算，回傳完整區塊"""

    # Step 1: EPS 分析
    eps_text = analyze_eps_with_groq(raw_text)

    # 公司債/可轉債 → 跳過
    if "公司債/可轉債" in eps_text:
        return ""

    # Step 2: 營收數字擷取（LLM）
    revenue_data = extract_revenue_with_groq(raw_text)

    # Step 3: 營收成長率計算（Python）
    revenue_text = calc_revenue_growth(revenue_data)

    return f"【{company_id} {company_name}】\n{eps_text}\n\n{revenue_text}"


# ── MOPS 爬蟲 ────────────────────────────────
def fetch_eps(year: str, month: str, day: str) -> str:
    date_key = f"{year}/{month}/{day}"

    # 先查 cache
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

    rows = response.json()["result"]["data"]
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
            if block:  # 過濾掉公司債回傳的空字串
                blocks.append(block)

        time.sleep(5)  # 加長間隔，因為現在每筆多一次 Groq call

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