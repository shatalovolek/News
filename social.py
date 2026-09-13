"""
Social signals for the Newsroom: StockTwits, Reddit and Google Trends.

Each company gets output/social_<TICKER>.json with the raw posts of the last 30 days
(de-duplicated) plus the Google Trends series, and summarize() turns that into the
numbers the page shows: mentions per day, bullish share, buzz vs the 30-day average,
top posts.

Reddit needs a free API app (https://www.reddit.com/prefs/apps, type "script"):
    REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET   in the environment
Without them Reddit is skipped (the public JSON endpoints block servers).
"""
import collections
import datetime as dt
import json
import os
import re
import sys
import time

import requests
import warnings

warnings.filterwarnings("ignore", category=FutureWarning)  # pytrends/pandas noise

UA = "stock-newsroom/1.0 (research; contact shatalov@beat-trade.com)"
H = {"User-Agent": UA, "Accept": "application/json"}
ST_H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://stocktwits.com", "Referer": "https://stocktwits.com/"}
KEEP_DAYS = 30
SUBREDDITS = "stocks+wallstreetbets+investing+options+StockMarket+ValueInvesting+Daytrading+securityanalysis"


def social_file(out_dir, ticker):
    return os.path.join(out_dir, f"social_{ticker}.json")


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"stocktwits": [], "reddit": [], "trends": [], "updated": None}


# ------------------------------------------------------------------ StockTwits
def fetch_stocktwits(ticker, max_pages=8, since_days=7):
    """Public symbol stream, newest first, 30 messages per page."""
    out, cursor_max = [], None
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)
    for _ in range(max_pages):
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
        params = {"max": cursor_max} if cursor_max else {}
        r = requests.get(url, params=params, headers=ST_H, timeout=25)
        if r.status_code == 404:
            return out  # symbol unknown to StockTwits
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]!r}")
        j = r.json()
        for m in j.get("messages", []):
            when = dt.datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
            if when < cutoff:
                return out
            sent = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            out.append({
                "id": m["id"], "time": when.isoformat(), "body": (m.get("body") or "")[:280],
                "sentiment": sent, "likes": ((m.get("likes") or {}).get("total") or 0),
                "user": (m.get("user") or {}).get("username", ""),
                "followers": (m.get("user") or {}).get("followers", 0),
                "url": f"https://stocktwits.com/{(m.get('user') or {}).get('username','')}/message/{m['id']}",
            })
        cur = j.get("cursor") or {}
        if not cur.get("more"):
            break
        cursor_max = cur.get("max")
        time.sleep(0.5)
    return out


# ---------------------------------------------------------------------- Reddit
_reddit_token = {"value": None, "expires": 0}


def _reddit_auth():
    cid, sec = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not sec:
        return None
    if _reddit_token["value"] and time.time() < _reddit_token["expires"] - 60:
        return _reddit_token["value"]
    r = requests.post("https://www.reddit.com/api/v1/access_token", data={"grant_type": "client_credentials"},
                      auth=(cid, sec), headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    j = r.json()
    _reddit_token.update(value=j["access_token"], expires=time.time() + j.get("expires_in", 3600))
    return _reddit_token["value"]


def fetch_reddit(company, since_days=7):
    """Posts mentioning the company in the big finance subreddits, via the official API."""
    token = _reddit_auth()
    if not token:
        return None  # not configured
    q = f'"{company["name"]}" OR "{company["ticker"]}"'
    r = requests.get(f"https://oauth.reddit.com/r/{SUBREDDITS}/search",
                     params={"q": q, "sort": "new", "t": "month" if since_days > 7 else "week",
                             "limit": 100, "restrict_sr": 1},
                     headers={"User-Agent": UA, "Authorization": f"Bearer {token}"}, timeout=25)
    r.raise_for_status()
    pat = re.compile(company.get("match") or re.escape(company["name"]), re.I)
    out = []
    for ch in r.json()["data"]["children"]:
        d = ch["data"]
        text = f"{d.get('title','')} {d.get('selftext','')[:500]}"
        if not pat.search(text):
            continue
        out.append({
            "id": d["id"], "time": dt.datetime.fromtimestamp(d["created_utc"], dt.timezone.utc).isoformat(),
            "title": d.get("title", "")[:200], "subreddit": d.get("subreddit", ""),
            "score": d.get("score", 0), "comments": d.get("num_comments", 0),
            "url": "https://www.reddit.com" + d.get("permalink", ""),
        })
    return out


# --------------------------------------------------------------- Google Trends
def fetch_trends(name):
    """Daily search interest for the last 90 days (0-100, relative)."""
    try:
        from pytrends.request import TrendReq
    except ImportError:
        return None
    pt = TrendReq(hl="en-US", tz=0, timeout=(10, 25))
    pt.build_payload([name], timeframe="today 3-m")
    df = pt.interest_over_time()
    if df is None or df.empty:
        return []
    return [{"d": idx.strftime("%Y-%m-%d"), "v": int(row[name])} for idx, row in df.iterrows()]


# ------------------------------------------------------------ store + summary
def merge_into(store, key, new):
    """Merge a list of posts into store[key]: de-duplicate by id, refresh engagement, keep 30 days."""
    if new is None:
        return
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=KEEP_DAYS)).isoformat()
    by_id = {p["id"]: p for p in store.get(key, [])}
    for p in new:
        if p["id"] in by_id:
            by_id[p["id"]].update({k: p[k] for k in ("likes", "score", "comments") if k in p})
        else:
            by_id[p["id"]] = p
    store[key] = sorted([p for p in by_id.values() if p["time"] >= cutoff], key=lambda p: p["time"], reverse=True)


def save_store(store, out_dir, ticker):
    os.makedirs(out_dir, exist_ok=True)
    with open(social_file(out_dir, ticker), "w") as f:
        json.dump(store, f)


def update_social(company, out_dir, max_pages=8):
    """Fetch everything for one company, merge into its store, return the store."""
    t = company["ticker"]
    store = _load(social_file(out_dir, t))
    errors = {}
    for key, fn, arg in (("stocktwits", lambda x: fetch_stocktwits(x, max_pages=max_pages), t),
                         ("reddit", fetch_reddit, company)):
        try:
            merge_into(store, key, fn(arg))
        except Exception as e:  # noqa: BLE001
            errors[key] = f"{type(e).__name__}: {str(e)[:160]}"
            print(f"[warn] {key} for {t} failed: {errors[key]}", file=sys.stderr)
    try:
        tr = fetch_trends(company["name"])
        if tr:
            store["trends"] = tr
    except Exception as e:  # noqa: BLE001
        errors["trends"] = f"{type(e).__name__}: {str(e)[:160]}"
        print(f"[warn] trends for {t} failed: {errors['trends']}", file=sys.stderr)
    store["errors"] = errors
    store["updated"] = dt.datetime.now().astimezone().isoformat()
    store["reddit_configured"] = bool(os.environ.get("REDDIT_CLIENT_ID"))
    save_store(store, out_dir, t)
    return store


def absorb_upload(payload, out_dir):
    """Server side: merge posts collected elsewhere (the Mac) into the store. Returns the store."""
    t = payload["ticker"].upper()
    store = _load(social_file(out_dir, t))
    for key in ("stocktwits", "reddit"):
        if payload.get(key):
            merge_into(store, key, payload[key])
    if payload.get("trends"):
        store["trends"] = payload["trends"]
    errs = store.get("errors") or {}
    for key in ("stocktwits", "reddit", "trends"):
        if payload.get(key):
            errs.pop(key, None)          # a source that just arrived by upload is not failing
    store["errors"] = errs
    store["updated"] = dt.datetime.now().astimezone().isoformat()
    store["uploaded_from"] = payload.get("source", "remote")
    save_store(store, out_dir, t)
    return store


def push_store(store, ticker, url, token, source="mac"):
    """Client side: send this company's social store to the server."""
    payload = {"ticker": ticker, "source": source, "stocktwits": store.get("stocktwits", []),
               "reddit": store.get("reddit", []), "trends": store.get("trends", [])}
    r = requests.post(url.rstrip("/") + "/api/social/upload", json=payload,
                      headers={"X-Upload-Token": token, "User-Agent": UA}, timeout=60)
    r.raise_for_status()
    return r.json()


def summarize(store):
    """Numbers for the page. Days are local dates."""
    now = dt.datetime.now().astimezone()
    days = [(now - dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(KEEP_DAYS - 1, -1, -1)]
    daily = {d: {"d": d, "st": 0, "bull": 0, "bear": 0, "rd": 0, "rd_score": 0} for d in days}
    for p in store.get("stocktwits", []):
        d = dt.datetime.fromisoformat(p["time"]).astimezone().strftime("%Y-%m-%d")
        if d in daily:
            daily[d]["st"] += 1
            if p.get("sentiment") == "Bullish":
                daily[d]["bull"] += 1
            elif p.get("sentiment") == "Bearish":
                daily[d]["bear"] += 1
    for p in store.get("reddit", []):
        d = dt.datetime.fromisoformat(p["time"]).astimezone().strftime("%Y-%m-%d")
        if d in daily:
            daily[d]["rd"] += 1
            daily[d]["rd_score"] += p.get("score", 0)
    rows = [daily[d] for d in days]

    def window(hours):
        cut = (now - dt.timedelta(hours=hours)).astimezone(dt.timezone.utc).isoformat()
        st = [p for p in store.get("stocktwits", []) if p["time"] >= cut]
        rd = [p for p in store.get("reddit", []) if p["time"] >= cut]
        bull = sum(1 for p in st if p.get("sentiment") == "Bullish")
        bear = sum(1 for p in st if p.get("sentiment") == "Bearish")
        return {"st": len(st), "bull": bull, "bear": bear,
                "bull_share": round(bull / (bull + bear) * 100) if (bull + bear) else None,
                "rd": len(rd), "rd_score": sum(p.get("score", 0) for p in rd),
                "rd_comments": sum(p.get("comments", 0) for p in rd)}

    last24, last7 = window(24), window(24 * 7)
    # buzz: last 24h vs the average full day we have complete coverage of. A day counts as
    # covered only if it is later than the oldest post we hold (the stream is read newest-first)
    # and earlier than today; needs at least 3 such days, otherwise "still collecting".
    posts = store.get("stocktwits", []) + store.get("reddit", [])
    buzz = None
    if posts:
        oldest = min(p["time"] for p in posts)
        oldest_day = dt.datetime.fromisoformat(oldest).astimezone().strftime("%Y-%m-%d")
        today = now.strftime("%Y-%m-%d")
        full = [r for r in rows if oldest_day < r["d"] < today]
        if len(full) >= 3:
            avg = sum(r["st"] + r["rd"] for r in full) / len(full)
            buzz = round((last24["st"] + last24["rd"]) / avg, 2) if avg else None
    trends = store.get("trends") or []
    top_rd = sorted(store.get("reddit", []), key=lambda p: (p.get("score", 0), p.get("comments", 0)), reverse=True)[:6]
    top_st = sorted([p for p in store.get("stocktwits", []) if p.get("sentiment")],
                    key=lambda p: (p.get("likes", 0), p.get("followers", 0)), reverse=True)[:6]
    return {
        "updated": store.get("updated"), "reddit_configured": store.get("reddit_configured", False),
        "errors": store.get("errors", {}),
        "daily": rows, "last24": last24, "last7": last7, "buzz": buzz,
        "trends": trends[-90:], "trend_now": trends[-1]["v"] if trends else None,
        "trend_peak": max((x["v"] for x in trends), default=None),
        "top_reddit": top_rd, "top_stocktwits": top_st,
    }
