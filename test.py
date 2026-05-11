import requests
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time


TZ = ZoneInfo("Asia/Taipei")
TWSE_HOLIDAY_API = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"


def fetch_twse_holidays() -> set[str]:
    """
    抓 TWSE 當年休市日，回傳民國日期字串 set，例如 {"1150101", "1150228"}
    只保留描述含「休市」或「放假」的日期（排除開始交易日等說明列）
    """
    resp = requests.get(TWSE_HOLIDAY_API, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    holidays = set()
    for item in data:
        desc = item.get("Description", "")
        name = item.get("Name", "")
        # 排除「開始交易日」這類說明列，只留真正休市的
        if "休市" in desc or "放假" in desc or "停止交易" in desc or "市場無交易" in name:
            holidays.add(item["Date"])  # e.g. "1150101"
        print(f"  {item['Date']} {item['Weekday']} {name} → {desc.strip()}")

    return holidays


def to_roc_date_str(d: datetime) -> str:
    """datetime → 民國日期字串，例如 '1150507'"""
    roc_year = d.year - 1911
    return f"{roc_year}{d.month:02d}{d.day:02d}"


def get_last_trading_day_with_holidays(now: datetime, holidays: set[str]) -> datetime:
    """
    從 now 往回找最近一個交易日（跳過週末 + 國定假日）
    """
    d = now - timedelta(days=1)
    while True:
        if d.weekday() >= 5:          # 週六(5) 或 週日(6)
            d -= timedelta(days=1)
            continue
        if to_roc_date_str(d) in holidays:  # 國定假日
            d -= timedelta(days=1)
            continue
        break
    return d


# ── 測試 ──────────────────────────────────────
if __name__ == "__main__":
    print("=== 抓取 TWSE 休市日清單 ===")
    holidays = fetch_twse_holidays()
    print(f"\n共 {len(holidays)} 個休市日: {sorted(holidays)}\n")

    # 模擬幾個情境
    test_cases = [
        ("週一（應回傳上週五）",       datetime(2026, 5, 11, 7, 30, tzinfo=TZ)),  # 2026/5/11 週一
        ("週一前一天是假日",            datetime(2026, 2,  9, 7, 30, tzinfo=TZ)),  # 2026/2/9 週一，春節前
        ("普通週三",                    datetime(2026, 5,  6, 7, 30, tzinfo=TZ)),  # 2026/5/6 週三
    ]

    print("=== 情境測試 ===")
    for label, now in test_cases:
        result = get_last_trading_day_with_holidays(now, holidays)
        roc_str = to_roc_date_str(result)
        print(f"{label}")
        print(f"  now={now.strftime('%Y-%m-%d %a')}  →  last_trading={result.strftime('%Y-%m-%d %a')} (民國{roc_str})\n")
