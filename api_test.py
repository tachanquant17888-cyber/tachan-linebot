"""
api_test.py — 手動測試：抓今日 MOPS 注意清單並印出所有中間資訊
用法:
    python api_test.py           → 抓今日 (自動轉民國年)
    python api_test.py 1150527   → 指定民國日期 7 碼
"""
import sys
import json
import time
import os
import urllib3
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from groq import Groq

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

TZ = ZoneInfo("Asia/Taipei")
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

SEP = "=" * 60
SEP2 = "-" * 40

# ── 日期工具 ─────────────────────────────────────────────────

def to_roc_ymd(d: datetime):
    return str(d.year - 1911), str(d.month).zfill(2), str(d.day).zfill(2)

def parse_arg_date(arg: str):
    """7 碼民國日期 → (year_roc, month, day)"""
    if len(arg) != 7 or not arg.isdigit():
        raise ValueError(f"日期格式錯誤，請輸入 7 碼民國日期，例如 1150527，收到: {arg!r}")
    return arg[:3], arg[3:5], arg[5:7]

# ── MOPS Session ─────────────────────────────────────────────

def create_session():
    session = requests.Session()
    session.verify = False
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "Origin":  "https://mops.twse.com.tw",
        "Referer": "https://mops.twse.com.tw/mops/web/t05st02",
        "content-type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    })
    print("  取得 MOPS Cookie …")
    session.get("https://mops.twse.com.tw/mops/web/t05st02")
    print("  Cookie 取得完成")
    return session

# ── MOPS 注意清單 ─────────────────────────────────────────────

NOTICE_KEYWORDS = ("注意", "證券近期","異常")
# 可轉債/公司債公告的類別關鍵字 — 符合此條件的公告直接排除
BOND_CATEGORY_KEYWORDS = ("可轉換公司債", "轉換公司債", "普通公司債", "海外可轉換公司債")

def fetch_notice_items(session, year: str, month: str, day: str):
    url = "https://mops.twse.com.tw/mops/api/t05st02"
    payload = {"year": year, "month": month, "day": day}

    print(f"\n{SEP}")
    print(f"[MOPS] POST {url}")
    print(f"[MOPS] Payload: {json.dumps(payload, ensure_ascii=False)}")

    resp = session.post(url, json=payload)

    print(f"\n[MOPS] HTTP 狀態碼 : {resp.status_code}")
    print(f"[MOPS] 回應長度    : {len(resp.text)} 字元")
    print(f"[MOPS] Content-Type: {resp.headers.get('Content-Type', 'N/A')}")

    # ── 印出完整原始 response body ──
    print(f"\n{'─'*20} 原始 Response Body {'─'*20}")
    try:
        pretty = json.dumps(resp.json(), ensure_ascii=False, indent=2)
        # 截斷超長欄位，避免大量資料淹沒輸出
        if len(pretty) > 3000:
            print(pretty[:3000])
            print(f"\n  … (共 {len(pretty)} 字，已截斷顯示前 3000 字)")
        else:
            print(pretty)
    except Exception:
        print(resp.text[:3000])

    if not resp.text.strip():
        print("\n⚠️  MOPS 回應為空")
        return []

    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        print(f"\n⚠️  JSON 解析失敗: {e}")
        return None

    if not data or "result" not in data or data["result"] is None:
        print("\n⚠️  result 欄位不存在或為 null")
        return []

    result_val = data["result"]
    if not isinstance(result_val, dict):
        print(f"\n⚠️  result 型態異常: {type(result_val)}")
        return []

    rows = result_val.get("data", [])
    if not rows:
        print("\n⚠️  data 為空清單，當日無注意清單")
        return []

    print(f"\n[MOPS] 原始 rows 共 {len(rows)} 筆（含所有公告種類）")

    notice_items = []
    print(f"\n{'─'*20} 過濾符合關鍵字 {NOTICE_KEYWORDS} {'─'*20}")
    for i, row in enumerate(rows):
        cat   = row[4] if len(row) > 4 else "?"
        cid   = row[2] if len(row) > 2 else "?"
        cname = row[3] if len(row) > 3 else "?"
        is_notice = any(kw in cat for kw in NOTICE_KEYWORDS)
        is_bond   = any(bk in cat for bk in BOND_CATEGORY_KEYWORDS)

        if is_notice and is_bond:
            marker = "🔕"  # 注意關鍵字命中，但是可轉債 → 排除
        elif is_notice:
            marker = "✅"
        else:
            marker = "  "

        print(f"  {marker} [{i+1:3d}] {cid} {cname:<10} | 類別: {cat}")

        if is_notice and not is_bond:
            notice_items.append({
                "company_id":   cid,
                "company_name": cname,
                "params":       row[5]["parameters"],
            })

    print(f"\n[MOPS] 符合注意關鍵字: {len(notice_items)} 筆（可轉債公告已排除）")
    return notice_items

# ── MOPS 明細 ─────────────────────────────────────────────────

def fetch_raw_texts(session, notice_items: list) -> dict:
    raw_texts = {}
    url = "https://mops.twse.com.tw/mops/api/t05st02_detail"

    for item in notice_items:
        cid   = item["company_id"]
        cname = item["company_name"]
        params = item["params"]

        print(f"\n{SEP2}")
        print(f"[DETAIL] POST {url}")
        print(f"[DETAIL] Params: {json.dumps(params, ensure_ascii=False)}")

        try:
            resp = session.post(url, json=params)
        except Exception as e:
            print(f"  ⚠️  連線失敗: {e}")
            continue

        print(f"[DETAIL] HTTP {resp.status_code} | 長度 {len(resp.text)} 字")

        try:
            detail = resp.json()
        except Exception as e:
            print(f"  ⚠️  JSON 解析失敗: {e}")
            continue

        if detail.get("code") != 200:
            print(f"  ⚠️  回應 code={detail.get('code')}，跳過")
            continue

        for row in detail["result"]["data"]:
            new_text = row[9]
            if cid in raw_texts:
                # 同公司有多筆股票注意公告 → 拼接，Groq 一次分析全部
                raw_texts[cid]["text"] += "\n\n" + new_text
                print(f"\n  {'─'*10} [{cid}] {cname} 拼接第二筆 {'─'*10}")
                print(f"  （目前共 {len(raw_texts[cid]['text'])} 字）")
            else:
                raw_texts[cid] = {"name": cname, "text": new_text}
                print(f"\n  {'─'*10} [{cid}] {cname} 原始文字 {'─'*10}")
                preview = new_text[:600]
                print(preview)
                if len(new_text) > 600:
                    print(f"  … (共 {len(new_text)} 字，已截斷顯示前 600 字)")

        time.sleep(2)

    return raw_texts

# ── Groq 分析 ─────────────────────────────────────────────────

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
            f"⚠️ 上季月均 EPS 為零，無法計算成長率"
        )
    growth = ((m - avg) / abs(avg)) * 100
    sign = "+" if growth >= 0 else ""
    return (
        f"最近一月 EPS{m_tag}:{m:+.2f} 元\n"
        f"上季{q_tag}月均 EPS:{avg:+.2f} 元\n"
        f"月 EPS 較上季月均成長:{sign}{growth:.2f}%"
    )


def analyze_with_groq(raw_text: str, cid: str, cname: str) -> str:
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
- 「營業收入」那列 → 取出 latest_month_revenue 和 latest_quarter_revenue(單位:百萬)
- is_bond: 文本【主體】是可轉債/公司債資訊(無股票財務表格)才填 true
- EPS跟每股盈餘如果是虧損請填負數,如 (0.19) → -0.19
- 括號數字 = 負數,例如 (0.06) → -0.06
- 百分比欄位不是我們要的,要跳過
- latest_month_label: 只填月份數字,例如「3月」、「11月」
- latest_quarter_label: 只填季別,例如「第1季」、「第4季」
- 月份/季別必須從文本擷取,不可推算
- 找不到填 null"""

    print(f"\n  [Groq] 送出請求 → llama-3.3-70b-versatile …")

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=250,
            temperature=0,
        )
        raw_groq = response.choices[0].message.content.strip()

        print(f"\n  [Groq] 原始回應:")
        print(f"  {raw_groq}")

        # usage 資訊
        usage = response.usage
        if usage:
            print(f"\n  [Groq] Token 使用量: "
                  f"prompt={usage.prompt_tokens}, "
                  f"completion={usage.completion_tokens}, "
                  f"total={usage.total_tokens}")

        cleaned = raw_groq.replace("```json", "").replace("```", "").strip()
        data = json.loads(cleaned)

        print(f"\n  [Groq] 解析後 JSON:")
        print(f"  {json.dumps(data, ensure_ascii=False, indent=4)}")

        if data.get("is_bond"):
            print(f"  → is_bond=true，跳過此筆")
            return ""

        revenue_text   = calc_revenue_growth(data)
        eps_growth_text = calc_eps_growth(data)

        block = (
            f"【{cid} {cname}】\n"
            f"{eps_growth_text}\n\n"
            f"{revenue_text}"
        )
        print(f"\n  [最終推播文字]:\n{block}")
        return block

    except json.JSONDecodeError as e:
        print(f"  ⚠️  JSON 解析失敗: {e}")
        return f"【{cid} {cname}】\n⚠️ 無法取得分析"
    except Exception as e:
        print(f"  ⚠️  Groq 呼叫失敗: {e}")
        return f"【{cid} {cname}】\n⚠️ 無法取得分析"

# ── Main ─────────────────────────────────────────────────────

def main():
    # 決定查詢日期
    if len(sys.argv) > 1:
        try:
            year, month, day = parse_arg_date(sys.argv[1])
        except ValueError as e:
            print(f"❌ {e}")
            sys.exit(1)
    else:
        now = datetime.now(TZ)
        year, month, day = to_roc_ymd(now)

    date_key = f"{year}/{month}/{day}"
    print(f"\n{SEP}")
    print(f"  api_test.py — 查詢日期: {date_key}")
    print(SEP)

    # 1. 建立 Session
    print(f"\n[Step 1] 建立 MOPS Session")
    session = create_session()

    # 2. 抓注意清單
    print(f"\n[Step 2] 抓取 MOPS 注意清單")
    notice_items = fetch_notice_items(session, year, month, day)

    if notice_items is None:
        print("\n❌ MOPS 解析失敗，結束測試")
        sys.exit(1)

    if not notice_items:
        print(f"\n✅ {date_key} 無注意清單資料，測試完成")
        sys.exit(0)

    # 3. 抓各公司明細 raw_text
    print(f"\n{SEP}")
    print(f"[Step 3] 抓取 {len(notice_items)} 筆明細 raw_text")
    raw_texts = fetch_raw_texts(session, notice_items)

    if not raw_texts:
        print("\n⚠️  所有明細抓取失敗")
        sys.exit(1)

    # 4. Groq 分析
    print(f"\n{SEP}")
    print(f"[Step 4] 呼叫 Groq 分析 ({len(raw_texts)} 筆)")

    blocks = {}
    for cid, item in raw_texts.items():
        print(f"\n{SEP2}")
        print(f"  分析: {cid} {item['name']}")
        block = analyze_with_groq(item["text"], cid, item["name"])
        if block:
            blocks[cid] = block

    # 5. 印出最終整合訊息
    print(f"\n{SEP}")
    print("[Step 5] 最終推播訊息預覽")
    print(SEP)
    if not blocks:
        print("⚠️ 無有效分析結果")
    else:
        header = (
            f"公開資訊觀測站-注意清單\n"
            f"查詢日期:{date_key}\n"
            f"共 {len(blocks)} 筆\n"
            + "─" * 13
        )
        body = ("\n" + "─" * 13 + "\n").join(blocks.values())
        final_msg = f"{header}\n\n{body}"
        print(final_msg)

    print(f"\n{SEP}")
    print("  測試完成 ✅")
    print(SEP)


if __name__ == "__main__":
    main()
