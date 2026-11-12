# fetch_q3_job.py — GitHub Actions 專用：07:00 預抓 Q3 財報公告的累計 EPS 存入 Redis（不推播、不呼叫 finlab）
# Render 08:30 推播時直接讀快取，只補抓沒抓到的，避免在 Render 上長時間爬 MOPS 途中重啟
import sys
from datetime import datetime

import main as main_module
from main import (
    TZ, Q3_RANK_END_DATE, redis_client,
    _create_mops_session, fetch_twse_holidays, q3_rank_dates,
)
from q3_rank import fetch_announcements

if __name__ == "__main__":
    now = datetime.now(TZ)
    if now > datetime.strptime(Q3_RANK_END_DATE, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ):
        print(f"[fetch_q3_job] 已過 Q3 排行截止日 {Q3_RANK_END_DATE[:10]}，不抓取")
        sys.exit(0)
    if redis_client is None:
        print("[fetch_q3_job] Redis 未連線，無法存快取")
        sys.exit(1)

    try:
        main_module._holidays = fetch_twse_holidays()
        print(f"[fetch_q3_job] TWSE 休市日載入完成，共 {len(main_module._holidays)} 筆")
    except Exception as e:
        print(f"[fetch_q3_job] ⚠️ 無法載入 TWSE 休市日（將只跳過週末）: {e}")

    dates = q3_rank_dates(today_only=False)
    print(f"[fetch_q3_job] 抓取 {', '.join('/'.join(d) for d in dates)}")
    session = _create_mops_session()
    total = 0
    for y, m, d in dates:
        df = fetch_announcements(session, y, m, d, redis_client)
        total += len(df)
    print(f"[fetch_q3_job] 完成，共 {total} 筆 Q3 公告（已存 Redis）")
