# fetch_job.py
from main import fetch_eps, to_roc_ymd, get_last_trading_day
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")

if __name__ == "__main__":
    now = datetime.now(TZ)
    target = get_last_trading_day(now)
    y, m, d = to_roc_ymd(target)
    print(f"[fetch_job] 抓取 {y}/{m}/{d}")
    result = fetch_eps(y, m, d)
    print(f"[fetch_job] 完成，寫入 Redis:\n{result}")