# fetch_job.py — GitHub Actions 專用，只抓 MOPS raw_texts 存入 Redis，不呼叫 Groq
from main import (
    _create_mops_session, _fetch_notice_items, _fetch_raw_texts,
    set_cached_raw_texts, to_roc_ymd, get_last_trading_day,
)
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")

if __name__ == "__main__":
    now = datetime.now(TZ)
    target = get_last_trading_day(now)
    y, m, d = to_roc_ymd(target)
    date_key = f"{y}/{m}/{d}"
    print(f"[fetch_job] 抓取 {date_key}")

    session = _create_mops_session()
    notice_items = _fetch_notice_items(session, y, m, d)

    if notice_items is None:
        print("[fetch_job] MOPS 解析失敗，不寫 Redis")
    elif not notice_items:
        print("[fetch_job] 無符合項目，寫入空結果")
        set_cached_raw_texts(date_key, {})
    else:
        raw_texts = _fetch_raw_texts(session, notice_items)
        set_cached_raw_texts(date_key, raw_texts)
        print(f"[fetch_job] 完成，已存 {len(raw_texts)} 筆 raw_texts 到 Redis")
