# fetch_job.py — GitHub Actions 專用，只抓 MOPS raw_texts 存入 Redis，不呼叫 Groq
import sys
import main as main_module
from main import (
    _create_mops_session, _fetch_notice_items, _fetch_raw_texts,
    set_cached_raw_texts, to_roc_ymd, get_last_trading_day,
    fetch_twse_holidays,  # [新增] 載入 TWSE 休市日，確保與 Render 7:30 計算同一個交易日
)
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")

if __name__ == "__main__":
    # [新增] 載入當年休市日後寫回 main._holidays，
    # 讓 get_last_trading_day 能跳過國定假日（與 Render lifespan 行為一致）
    try:
        main_module._holidays = fetch_twse_holidays()
        print(f"[fetch_job] TWSE 休市日載入完成，共 {len(main_module._holidays)} 筆")
    except Exception as e:
        print(f"[fetch_job] ⚠️ 無法載入 TWSE 休市日（將只跳過週末）: {e}")

    now = datetime.now(TZ)
    target = get_last_trading_day(now)
    y, m, d = to_roc_ymd(target)
    date_key = f"{y}/{m}/{d}"
    print(f"[fetch_job] 抓取 {date_key}")

    session = _create_mops_session()
    notice_items = _fetch_notice_items(session, y, m, d)

    if notice_items is None:
        # MOPS 解析失敗：exit(1) 讓 GitHub Actions 標記為失敗，觸發警示
        print("[fetch_job] MOPS 解析失敗，不寫 Redis")
        sys.exit(1)

    if not notice_items:
        print("[fetch_job] 無符合項目，寫入空結果")
        set_cached_raw_texts(date_key, {})
        sys.exit(0)

    raw_texts = _fetch_raw_texts(session, notice_items)

    if not raw_texts:
        # 明細全部抓失敗：不寫入空 dict，讓 Render 7:30 可以 fallback 自己抓 MOPS
        print("[fetch_job] ⚠️ 所有明細抓取失敗，不寫 Redis（Render 將 fallback）")
        sys.exit(1)

    set_cached_raw_texts(date_key, raw_texts)
    print(f"[fetch_job] 完成，已存 {len(raw_texts)} 筆 raw_texts 到 Redis")
