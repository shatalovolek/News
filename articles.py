"""
Full-text reading of the most important articles, so the brief sees more than headlines.

For each company: take the last 3 days of headlines, rank by importance, resolve the link
(Google News links are decoded through Google's own endpoint), download the page and extract
the article text with trafilatura. Results are cached per link in output/articles_<TICKER>.json;
a link is fetched once (failures are retried after a day). Sites that block robots simply
yield nothing and are skipped.
"""
import datetime as dt
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
     "Accept-Language": "en-US,en;q=0.9", "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
MAX_NEW = 6            # new articles fetched per company per run
DAYS = 3
MIN_CHARS = 400        # shorter extractions are treated as failed (paywall stubs, cookie walls)
KEEP_CHARS = 6000
MIN_IMPORTANCE = 2.5
RETRY_HOURS = 24
WORKERS = 4


def articles_file(out_dir, ticker):
    return os.path.join(out_dir, f"articles_{ticker}.json")


def load(out_dir, ticker):
    try:
        with open(articles_file(out_dir, ticker)) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def decode_google(url):
    """news.google.com/rss/articles/<id> -> the publisher URL, via Google's batchexecute endpoint."""
    m = re.search(r"/articles/([^?/]+)", url)
    if not m:
        return None
    aid = m.group(1)
    r = requests.get(f"https://news.google.com/articles/{aid}", headers=H, timeout=20)
    sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
    if not sg or not ts:
        return None
    payload = ('[[["Fbv4je","[\\"garturlreq\\",[[\\"en-US\\",\\"US\\",[\\"FINANCE_TOP_INDICES\\",\\"WEB_TEST_1_0_0\\"],'
               'null,null,1,1,\\"US:en\\",null,180,null,null,null,null,null,0,null,null,[1608992183,723341000]],'
               '\\"en-US\\",\\"US\\",1,[2,3,4,8],1,0,\\"655000234\\",0,0,null,0],\\"%s\\",%s,\\"%s\\"]",null,"generic"]]]'
               % (aid, ts.group(1), sg.group(1)))
    r = requests.post("https://news.google.com/_/DotsSplashUi/data/batchexecute", data={"f.req": payload},
                      headers={**H, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}, timeout=20)
    body = r.text.split("\n\n", 1)[-1]
    try:
        return json.loads(json.loads(body)[0][2])[1]
    except Exception:  # noqa: BLE001
        return None


def resolve(url):
    if "news.google.com" in url:
        return decode_google(url)
    return url


def extract(html):
    try:
        import trafilatura
    except ImportError:
        return None
    return trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True)


def read_one(link):
    """Returns a cache record for one headline link."""
    rec = {"fetched": dt.datetime.now().astimezone().isoformat(), "ok": False, "url": None, "chars": 0, "text": "", "error": None}
    try:
        real = resolve(link)
        if not real:
            rec["error"] = "could not resolve link"
            return rec
        rec["url"] = real
        r = requests.get(real, headers=H, timeout=25)
        if r.status_code != 200:
            rec["error"] = f"HTTP {r.status_code}"
            return rec
        text = extract(r.text) or ""
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) < MIN_CHARS:
            rec["error"] = "no readable text (paywall, cookie wall or script-only page)"
            return rec
        rec.update(ok=True, chars=len(text), text=text[:KEEP_CHARS])
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {str(e)[:100]}"
    return rec


def candidates(hist, store, days=DAYS):
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    retry_before = (dt.datetime.now().astimezone() - dt.timedelta(hours=RETRY_HOURS)).isoformat()
    out = []
    for h in sorted([h for h in hist if h["time"] >= cutoff], key=lambda h: (-h.get("importance", 0), h["time"]), reverse=False):
        if h.get("importance", 0) < MIN_IMPORTANCE:
            continue
        rec = store.get(h["link"])
        if rec and (rec.get("ok") or rec.get("fetched", "") > retry_before):
            continue
        out.append(h)
    out.sort(key=lambda h: -h.get("importance", 0))
    return out


def update_articles(company, hist, out_dir, max_new=MAX_NEW):
    """Fetch up to max_new new articles for the company; returns the store."""
    t = company["ticker"]
    store = load(out_dir, t)
    todo = candidates(hist, store)[:max_new]
    if todo:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for h, rec in zip(todo, ex.map(lambda h: read_one(h["link"]), todo)):
                rec["title"] = h["title"]
                rec["source"] = h["source"]
                store[h["link"]] = rec
        # keep the store small: drop entries older than 30 days
        cutoff = (dt.datetime.now().astimezone() - dt.timedelta(days=30)).isoformat()
        store = {k: v for k, v in store.items() if v.get("fetched", "") >= cutoff}
        os.makedirs(out_dir, exist_ok=True)
        with open(articles_file(out_dir, t), "w") as f:
            json.dump(store, f, ensure_ascii=False)
        ok = sum(1 for h in todo if store[h["link"]]["ok"])
        print(f"    articles: read {ok} of {len(todo)} new", file=sys.stderr) if ok or todo else None
    return store


def merge_store(store, incoming):
    """Server side: merge article records uploaded from the Mac (which is not blocked by publishers)."""
    for k, v in (incoming or {}).items():
        cur = store.get(k)
        if not cur or (v.get("ok") and not cur.get("ok")):
            store[k] = v
    return store


def block_for_brief(store, heads, max_articles=6, max_chars=1800):
    """ARTICLE TEXT block for the brief input; heads are the headline dicts with their [id] order."""
    parts = []
    for i, h in enumerate(heads):
        rec = store.get(h["link"])
        if rec and rec.get("ok") and rec.get("text"):
            body = " ".join(rec["text"].split())[:max_chars]
            parts.append(f"[{i}] {h['source']}: {body}")
        if len(parts) >= max_articles:
            break
    if not parts:
        return None
    return "ARTICLE TEXT (excerpts of the articles behind some headline ids above):\n" + "\n\n".join(parts)


def read_set(store):
    return {k for k, v in store.items() if v.get("ok")}
