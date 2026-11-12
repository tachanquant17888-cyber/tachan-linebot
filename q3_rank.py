"""
Q3 財報 EPS+營收 QoQ 雙成長排行（由 quarter_finance.ipynb 整合，只用正規表達式、不呼叫 Groq）

流程：
1. MOPS 重大訊息(t05st02)抓指定日期、主旨含「第三季/第3季」且含「財務報告/財務報表」的公告（排除召開日期類）
2. 從說明抓「1月1日累計至本期止基本每股盈餘」(regex)，抓不到的直接略過並列在訊息尾端
3. Q3 單季 EPS = 累計 EPS − finlab Q1 − Q2
4. 留 EPS QoQ > 0 且營收 QoQ > 0（finlab 月營收加總成季），依「EPS成長% − 營收成長%」排序
5. 轉成文字訊息（同 notebook 的 line_text）

需要環境變數 FINLAB_API_TOKEN（finlab 會自動讀取登入）。
"""
import json
import re
import time

import pandas as pd

QUARTER_KEYWORDS = ("第三季", "第3季")
REPORT_KEYWORDS = ("財務報告", "財務報表")
EXCLUDE_KEYWORDS = ("召開", "日期為")

# 例：「10.1月1日累計至本期止基本每股盈餘(損失) (元):0.92」；虧損可能是 -0.12 或 (0.12)
EPS_PATTERN = re.compile(r"1月1日累計至本期止基本每股盈餘[^:：\n]*[:：]\s*([-−]?\(?[\d,]*\.?\d+\)?)")


# ── MOPS ─────────────────────────────────────
def _is_target_title(title: str) -> bool:
    return (any(kw in title for kw in QUARTER_KEYWORDS)
            and any(kw in title for kw in REPORT_KEYWORDS)
            and not any(kw in title for kw in EXCLUDE_KEYWORDS))


def _same_date(row_date: str, year: str, month: str, day: str) -> bool:
    """row_date 格式如 '115/10/28'，用數字比較避免補零差異"""
    try:
        return [int(x) for x in row_date.split("/")] == [int(year), int(month), int(day)]
    except ValueError:
        return False


def _fetch_quarter_items(session, year: str, month: str, day: str) -> list:
    """抓當日重大訊息，只留發言日期 = 指定日期且主旨符合的項目
    （MOPS 查某天會連前一天的公告一起回傳，所以用 row[0] 再過濾）"""
    response = session.post(
        "https://mops.twse.com.tw/mops/api/t05st02",
        json={"year": year, "month": month, "day": day},
        timeout=30,
    )
    if not response.text.strip():
        print(f"[Q3] MOPS {year}/{month}/{day} 回應為空")
        return []
    try:
        result_val = response.json().get("result")
        rows = result_val.get("data", []) if isinstance(result_val, dict) else []
    except (json.JSONDecodeError, AttributeError) as e:
        print(f"[Q3] MOPS 解析失敗: {e}, 內容(前500字): {response.text[:500]}")
        return []

    day_rows = [row for row in rows if _same_date(row[0], year, month, day)]
    items = [{
        "date": row[0],
        "company_id": row[2],
        "company_name": row[3],
        "params": row[5]["parameters"],
    } for row in day_rows if _is_target_title(row[4])]
    print(f"[Q3] MOPS {year}/{month}/{day} 回傳 {len(rows)} 筆，當日 {len(day_rows)} 筆，符合 {len(items)} 筆")
    return items


def _fetch_detail_text(session, params: dict):
    """抓單筆公告明細，回傳「說明」內文（row[9]）；連線/解析失敗回傳 None（不寫入快取，下次重抓）"""
    try:
        detail = session.post(
            "https://mops.twse.com.tw/mops/api/t05st02_detail",
            json=params,
            timeout=30,
        ).json()
    except Exception as e:
        print(f"[Q3] 抓取 {params.get('companyId')} 明細失敗: {e}")
        return None
    if detail.get("code") != 200:
        return None
    return "\n\n".join(row[9] for row in detail["result"]["data"])


def parse_eps(content: str):
    """從說明抓「1月1日累計至本期止基本每股盈餘」，抓不到回傳 None"""
    m = EPS_PATTERN.search(content)
    if not m:
        return None
    raw = m.group(1).replace(",", "").replace("−", "-")
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1]
    try:
        return float(raw)
    except ValueError:
        return None


# ── finlab ───────────────────────────────────
def _load_q1_q2_eps(western_year: int) -> pd.DataFrame:
    """finlab 單季 EPS，回傳 index=股號、columns=[Q1, Q2]"""
    from finlab import data
    eps = data.get("financial_statement:每股盈餘")
    q = pd.DataFrame(eps.loc[[f"{western_year}-Q1", f"{western_year}-Q2"]]).T
    q.columns = ["Q1", "Q2"]
    return q


def _load_quarter_revenue(western_year: int, stock_ids) -> pd.DataFrame:
    """finlab 月營收(千元)加總成 Q2、Q3 季營收；月營收 index 是次月公布日，先往前推一個月"""
    from finlab import data
    rev = data.get("monthly_revenue:當月營收")
    rev = pd.DataFrame(rev[[s for s in stock_ids if s in rev.columns]])
    rev.index = (pd.DatetimeIndex(rev.index) - pd.DateOffset(months=1)).to_period("M")

    def quarter_sum(months):
        periods = [pd.Period(f"{western_year}-{m:02d}", "M") for m in months]
        return rev.reindex(periods).sum(min_count=3)   # 三個月都到齊才算

    return pd.DataFrame({"Q2營收": quarter_sum([4, 5, 6]), "Q3營收": quarter_sum([7, 8, 9])})


# ── Redis 快取（每抓一家就存，重啟後可續抓；16:30 抓過的 08:00 直接沿用）──
CACHE_KEY_PREFIX = "q3:eps:"          # hash：field=股號，value=累計EPS（JSON，抓不到為 null）
CACHE_TTL_SECONDS = 14 * 24 * 60 * 60


def _load_cache(redis, cache_key: str) -> dict:
    if redis is None:
        return {}
    try:
        return {cid: json.loads(v) for cid, v in (redis.hgetall(cache_key) or {}).items()}
    except Exception as e:
        print(f"[Q3] 讀取快取 {cache_key} 失敗: {e}")
        return {}


def _save_cache(redis, cache_key: str, company_id: str, eps):
    if redis is None:
        return
    try:
        redis.hset(cache_key, company_id, json.dumps(eps))
        redis.expire(cache_key, CACHE_TTL_SECONDS)
    except Exception as e:
        print(f"[Q3] 寫入快取 {cache_key} 失敗: {e}")


# ── 主流程 ───────────────────────────────────
def fetch_announcements(session, year: str, month: str, day: str, redis=None) -> pd.DataFrame:
    """year 為民國年；只用 regex 抓累計 EPS，抓不到的 累計eps 為 NaN
    redis 有給時，已快取的公司不再抓明細，新抓的逐筆寫入快取"""
    items = _fetch_quarter_items(session, year, month, day)
    cache_key = f"{CACHE_KEY_PREFIX}{year}{month.zfill(2)}{day.zfill(2)}"
    cache = _load_cache(redis, cache_key)
    hits = 0
    for item in items:
        params = item.pop("params")
        cid = item["company_id"]
        if cid in cache:
            item["累計eps"] = cache[cid]
            hits += 1
            continue
        text = _fetch_detail_text(session, params)
        item["累計eps"] = parse_eps(text) if text is not None else None
        if text is not None:
            _save_cache(redis, cache_key, cid, item["累計eps"])
        print(f"[Q3] {cid} {item['company_name']}: 累計EPS={item['累計eps']}")
        time.sleep(2)
    print(f"[Q3] {year}/{month}/{day} 快取命中 {hits}/{len(items)} 筆")
    return pd.DataFrame(items, columns=["date", "company_id", "company_name", "累計eps"])


def build_rank(df: pd.DataFrame, western_year: int) -> pd.DataFrame:
    df = df.dropna(subset=["累計eps"]).join(_load_q1_q2_eps(western_year), on="company_id")
    df["eps"] = (df["累計eps"] - df["Q1"] - df["Q2"]).round(2)          # Q3 單季 EPS
    q2 = df["Q2"].where(df["Q2"] != 0)
    df["EPS成長%"] = ((df["eps"] - q2) / q2.abs() * 100).round(2)       # Q2 為負時虧損縮小也算正成長
    df = df[df["EPS成長%"] > 0].drop_duplicates("company_id")

    df = df.join(_load_quarter_revenue(western_year, df["company_id"]), on="company_id")
    df["營收成長%"] = ((df["Q3營收"] - df["Q2營收"]) / df["Q2營收"] * 100).round(2)
    df = df[df["營收成長%"] > 0].copy()
    df["成長差%"] = (df["EPS成長%"] - df["營收成長%"]).round(2)
    df = df[(df["成長差%"] >= 0) & (df["eps"] >= 0)]   # 只留成長差 >= 0% 且 Q3 單季 EPS >= 0
    return df.sort_values("成長差%", ascending=False).reset_index(drop=True)


def to_text(df_rank: pd.DataFrame, date_label: str, n_ann: int, missing: list) -> str:
    def rev(v):  # 千元 → 百萬元
        return f"{v / 1000:,.1f}"

    lines = [
        f"📊 {date_label} Q3財報 EPS+營收 QoQ雙成長",
        "排序:EPS成長% − 營收成長%",
        f"Q3 財報公告 {n_ann} 檔，符合 {len(df_rank)} 檔",
    ]
    for i, r in df_rank.iterrows():
        lines += [
            "",
            f"{i + 1}. {r['company_id']} {r['company_name']}",
            f"EPS:Q3 {r['eps']:.2f} / Q2 {r['Q2']:.2f}({r['EPS成長%']:+.2f}%)",
            f"營收:Q3 {rev(r['Q3營收'])} / Q2 {rev(r['Q2營收'])} 百萬({r['營收成長%']:+.2f}%)",
            f"成長差:{r['成長差%']:+.2f}%",
        ]
    if missing:
        lines += ["", f"⚠️ 說明格式無法解析 EPS（未列入）：{'、'.join(missing)}"]
    return "\n".join(lines)


def build_q3_rank_text(session, dates: list, redis=None) -> str:
    """dates 為 [(民國年, 月, 日), ...] 字串 tuple（例如週一時含上週五+週六+週日），合併成一份排行"""
    dates = list(dates)
    first, last = "/".join(dates[0]), "/".join(dates[-1])
    date_label = first if first == last else f"{first}~{last}"
    frames = [fetch_announcements(session, y, m, d, redis) for y, m, d in dates]
    df = pd.concat(frames, ignore_index=True).drop_duplicates("company_id", keep="last")
    if df.empty:
        return f"📊 {date_label} 無 Q3 財報公告"

    missing = [f"{r.company_id} {r.company_name}" for r in df[df["累計eps"].isna()].itertuples()]
    df_rank = build_rank(df, int(dates[-1][0]) + 1911)
    return to_text(df_rank, date_label, len(df), missing)
