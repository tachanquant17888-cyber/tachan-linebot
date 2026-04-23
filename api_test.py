"""
測試 MOPS API 回應內容
打 t05st02 抓注意清單 + t05st02_detail 抓明細
"""
import json
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

YEAR, MONTH, DAY = "115", "04", "22"

NOTICE_KEYWORDS = ("注意", "證券近期")


def create_session():
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


def main():
    session = create_session()

    print("=" * 60)
    print(f"查詢日期: 民國 {YEAR}/{MONTH}/{DAY}")
    print("=" * 60)

    # ── Step 1: t05st02 注意清單 ──
    print("\n[Step 1] POST /mops/api/t05st02")
    resp = session.post(
        "https://mops.twse.com.tw/mops/api/t05st02",
        json={"year": YEAR, "month": MONTH, "day": DAY},
    )
    print(f"HTTP status: {resp.status_code}")

    try:
        data = resp.json()
    except Exception as e:
        print(f"⚠️ 無法解析 JSON: {e}")
        print(f"Raw text: {resp.text[:2000]}")
        return

    print("\n[t05st02 完整 response]")
    print(json.dumps(data, ensure_ascii=False, indent=2))

    if not data or "result" not in data or data["result"] is None:
        print("\n⚠️ result 為空")
        return

    rows = data["result"].get("data", [])
    print(f"\n總筆數: {len(rows)}")

    notice_items = []
    for idx, row in enumerate(rows):
        kw_hit = any(kw in row[4] for kw in NOTICE_KEYWORDS)
        print(f"  [{idx}] company_id={row[2]} name={row[3]} title={row[4]!r} 命中關鍵字={kw_hit}")
        if kw_hit:
            notice_items.append({
                "company_id": row[2],
                "company_name": row[3],
                "params": row[5]["parameters"],
            })

    print(f"\n命中關鍵字筆數: {len(notice_items)}")

    # ── Step 2: 對每家打 detail ──
    for item in notice_items:
        cid = item["company_id"]
        cname = item["company_name"]
        params = item["params"]

        print("\n" + "-" * 60)
        print(f"[Step 2] {cid} {cname} → POST /mops/api/t05st02_detail")
        print(f"params: {json.dumps(params, ensure_ascii=False)}")

        detail_resp = session.post(
            "https://mops.twse.com.tw/mops/api/t05st02_detail",
            json=params,
        )
        try:
            detail = detail_resp.json()
        except Exception as e:
            print(f"⚠️ detail JSON 解析失敗: {e}")
            print(f"Raw: {detail_resp.text[:500]}")
            continue

        print(f"[detail response]")
        print(json.dumps(detail, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
