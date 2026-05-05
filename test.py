import requests
import json
from datetime import datetime, timedelta
import time




def fetch_EPS():
    session = requests.Session()
    session.verify = False
    year = 115
    month=5
    day=5
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Origin": "https://mops.twse.com.tw",
        "Referer": "https://mops.twse.com.tw/mops/web/t05st02",
        "content-type": "application/json",
        "X-Requested-With": "XMLHttpRequest"
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
            
            return result
        rows = data["result"].get("data", [])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"解析 MOPS 資料時發生錯誤: {e}")
        return f"⚠️ 無法解析 {year}/{month}/{day} 的資料"

    if not rows:
        result = f"{year}/{month}/{day} 無注意清單資料"
        return result

    notice_items = [row for row in rows if "注意" in row[4]]
    
    if not notice_items:
        result = f"{year}/{month}/{day} 無注意清單資料"
        return result

    blocks = []


    print("notice_items",notice_items)

    for item in notice_items:
        # print("item:",item)
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
            if company_id == '4989':
                print("raw_text:",raw_text)
            # block = analyze_with_groq_single(raw_text, company_id, company_name)
            # if block:
            #     blocks.append(block)

        # 避免被 MOPS 封 IP
        time.sleep(2)


fetch_EPS()