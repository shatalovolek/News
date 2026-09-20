#!/usr/bin/env python3
"""
Daily stock-news agent (Bloom Energy, Amazon, Google, SanDisk, ... see companies.json).

For every company in companies.json it collects the day's news from Google News and
Yahoo Finance (plus Tavily search when TAVILY_API_KEY is set, and Finnhub company news,
analyst recommendations and the earnings calendar when FINNHUB_API_KEY is set), pulls the share price,
renders a PNG "news photo" per company, keeps a growing history, and rebuilds
output/news.html (one tab per company).

Usage:
    python3 be_news_agent.py               # last 24h, save, open the page, notify
    python3 be_news_agent.py --hours 48    # wider window
    python3 be_news_agent.py --no-open     # just save (used by the scheduler)
    python3 be_news_agent.py --backfill    # pull everything the feeds still hold
    python3 be_news_agent.py --only BE     # one company

Add or remove companies by editing companies.json (ticker, name, exchange, query).
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse

import requests
from PIL import Image, ImageDraw, ImageFont

import social
import edgar
import finnhub
import relative
import articles
import shorts

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("DATA_DIR") or os.path.join(HERE, "output")   # Render: mount a disk at /data
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh) stock-news-agent/1.0"}
COMPANIES_SEED = os.path.join(HERE, "companies.json")                  # shipped with the code
COMPANIES_FILE = os.path.join(OUT_DIR, "companies.json")               # live list (edited by the UI)
TEMPLATE = os.path.join(HERE, "page_template.html")
PAGE = os.path.join(OUT_DIR, "news.html")
SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"


def load_companies():
    path = COMPANIES_FILE if os.path.exists(COMPANIES_FILE) else COMPANIES_SEED
    with open(path) as f:
        return json.load(f)


def save_companies(companies):
    os.makedirs(OUT_DIR, exist_ok=True)
    text = json.dumps(companies, indent=2, ensure_ascii=False) + "\n"
    with open(COMPANIES_FILE, "w") as f:
        f.write(text)
    if os.path.abspath(COMPANIES_FILE) != os.path.abspath(COMPANIES_SEED) and OUT_DIR == os.path.join(HERE, "output"):
        with open(COMPANIES_SEED, "w") as f:   # keep the repo copy in sync when running locally
            f.write(text)
    github_save("companies.json", text, "Companies list updated from the site")


def github_save(path, text, message):
    """Commit a file to the GitHub repo so it survives Render's ephemeral disk.
    Needs GITHUB_TOKEN (fine-grained token, Contents: read/write) and GITHUB_REPO (owner/name).
    The commit message carries [skip render] so Render does not redeploy for it."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO", "shatalovolek/News")
    if not token:
        return False
    import base64
    api = f"https://api.github.com/repos/{repo}/contents/{path}"
    hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "stock-newsroom"}
    try:
        cur = requests.get(api, headers=hdr, timeout=20)
        sha = cur.json().get("sha") if cur.status_code == 200 else None
        if sha and base64.b64decode(cur.json().get("content", "")).decode() == text:
            return True  # unchanged
        body = {"message": f"[skip render] {message}", "content": base64.b64encode(text.encode()).decode()}
        if sha:
            body["sha"] = sha
        r = requests.put(api, headers=hdr, json=body, timeout=30)
        r.raise_for_status()
        print(f"[info] {path} committed to {repo}", file=sys.stderr)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[warn] GitHub save of {path} failed: {e}", file=sys.stderr)
        return False


def lookup_ticker(query):
    """Resolve a ticker or company name via Yahoo search. Returns a company dict or None."""
    r = requests.get(SEARCH_URL, params={"q": query, "quotesCount": 6, "newsCount": 0},
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    quotes = [q for q in r.json().get("quotes", []) if q.get("quoteType") in ("EQUITY", "ETF")]
    if not quotes:
        return None
    exact = [q for q in quotes if q.get("symbol", "").upper() == query.upper()]
    q = (exact or quotes)[0]
    name = (q.get("shortname") or q.get("longname") or q["symbol"])
    name = re.sub(r",?\s*(Inc\.?|Corp\.?|Corporation|Ltd\.?|plc|PLC|Co\.|Company|Holdings?|N\.V\.|S\.A\.|AG|SE)\s*$", "", name).strip()
    ticker = q["symbol"].upper()
    exch = q.get("exchDisp") or q.get("exchange") or ""
    exch = {"NasdaqGS": "NASDAQ", "NasdaqGM": "NASDAQ", "NasdaqCM": "NASDAQ", "NYSE": "NYSE"}.get(exch, exch)
    return {
        "ticker": ticker, "name": name, "exchange": exch,
        "query": f'"{name}" OR "{exch}:{ticker}"' if exch else f'"{name}" OR {ticker}',
        "match": f"{re.escape(name)}|{re.escape(ticker)}",
        "sector": q.get("sector"), "industry": q.get("industry"),
        "benchmark": relative.etf_for(q.get("sector"), q.get("industry")),
    }


DEFAULT_PEERS = {"BE": ["PLUG", "FCEL", "GEV"], "AMZN": ["MSFT", "GOOGL", "WMT"], "GOOGL": ["META", "MSFT", "AMZN"],
                 "SNDK": ["MU", "WDC", "STX"], "WDC": ["STX", "SNDK", "MU"], "TTWO": ["EA", "RBLX", "NTDOY"],
                 "MU": ["SNDK", "WDC", "NVDA"], "SPCX": ["RKLB", "ASTS", "LMT"], "SFTBY": ["ARM", "BABA", "NVDA"], "SOBKY": []}


def enrich_company(c):
    """Fill sector/industry/benchmark/peers for a company record (idempotent). Returns True if changed."""
    changed = False
    if not c.get("benchmark") or not c.get("sector"):
        try:
            info = lookup_ticker(c["ticker"])
            if info and info["ticker"] == c["ticker"]:
                for k in ("sector", "industry", "benchmark"):
                    if info.get(k) and not c.get(k):
                        c[k] = info[k]
                        changed = True
        except Exception as e:  # noqa: BLE001
            print(f"[warn] enrich {c['ticker']}: {e}", file=sys.stderr)
        if not c.get("benchmark"):
            c["benchmark"] = "SPY"
            changed = True
    if "peers" not in c:
        c["peers"] = DEFAULT_PEERS.get(c["ticker"], [])   # edit companies.json by hand for others
        changed = True
    return changed


def add_company(query):
    """Add a company by ticker or name. Returns (company, created)."""
    companies = load_companies()
    c = lookup_ticker(query)
    if not c:
        raise ValueError(f"No listed company found for '{query}'")
    for existing in companies:
        if existing["ticker"] == c["ticker"]:
            return existing, False
    enrich_company(c)
    companies.append(c)
    save_companies(companies)
    return c, True


def remove_company(ticker):
    companies = load_companies()
    keep = [c for c in companies if c["ticker"] != ticker.upper()]
    if len(keep) == len(companies):
        return False
    save_companies(keep)
    return True


def feeds_for(c):
    return {
        "Google News": ("https://news.google.com/rss/search?q=" + quote_plus(c["query"])
                        + "&hl=en-US&gl=US&ceid=US:en"),
        "Yahoo Finance": (f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={c['ticker']}"
                          "&region=US&lang=en-US"),
    }


def chart_url(ticker):
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1y&interval=1d"


def relevant(company, title):
    """A headline must actually mention the company (regex 'match' in companies.json)."""
    pat = company.get("match") or re.escape(company["name"])
    return re.search(pat, title, re.I) is not None


def history_file(ticker):
    return os.path.join(OUT_DIR, f"history_{ticker}.json")

POS_WORDS = r"""climb|climbs|surge|surges|jump|jumps|soar|soars|rally|rallies|gain|gains|
rise|rises|beat|beats|upgrade|upgrades|upgraded|buy|record|wins|win|deal|partnership|
contract|expands|expansion|boost|boosts|bull|bullish|strong|higher|momentum|outperform|
breakout|added|joins|entry|nears buy|top pick|opportunity|growth|profit|profits|upside|trading up|moved up|moves up|rating raised""".replace("\n", "")
NEG_WORDS = r"""fall|falls|fell|drop|drops|slide|slides|plunge|plunges|sink|sinks|tumble|tumbles|
decline|declines|miss|misses|downgrade|downgrades|downgraded|sell|lawsuit|cut|cuts|loss|
losses|warn|warns|bear|bearish|risk|risks|overvalued|too high|short|weak|lower|concern|
concerns|slump|slumps|crash|crashes|dump|dumps|bubble|sell-off|selloff|caution|class action|investigation|trading down|moved down|moves down|rating lowered|wipeout""".replace("\n", "")
POS_RE = re.compile(rf"\b({POS_WORDS})\b", re.I)
BIG_WORDS = r"""earnings|revenue|guidance|forecast|acquisition|acquire|acquires|merger|lawsuit|class action|
SEC|antitrust|FTC|DOJ|CEO|CFO|contract|deal|partnership|S&P 500|S&P 100|upgrade|upgrades|downgrade|downgrades|
layoffs|launch|launches|tariff|tariffs|record|billion|investigation|recall|outage|strike|dividend|buyback|
split|IPO|spin-off|guidance|quarter|Q[1-4]|results|profit|loss|shares (?:jump|surge|fall|drop|plunge|soar)""".replace("\n", "")
BIG_RE = re.compile(rf"\b({BIG_WORDS})\b", re.I)
NOISE_RE = re.compile(r"stock holdings|stock position|sells? off shares|sells? \d|buys? \d|shares (?:sold|bought|purchased) by|"
                      r"price prediction|should you buy|is it too late|prediction:|\d+ (?:stocks|reasons)|"
                      r"here'?s what happened|trading (?:up|down) \d|key drivers|history says|"
                      r"if you invested|could turn|millionaire", re.I)
TOP_SOURCES = re.compile(r"reuters|bloomberg|cnbc|wall street journal|wsj|financial times|barron|"
                         r"globenewswire|pr newswire|business wire|businesswire|techcrunch|the verge|"
                         r"the information|axios|associated press|\bap\b", re.I)
NEG_RE = re.compile(rf"\b({NEG_WORDS})\b", re.I)


# --------------------------------------------------------------------------- data
def fetch_feed(name, url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {name} feed failed: {e}", file=sys.stderr)
        return []
    items = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate") or ""
        src = it.findtext("source")
        try:
            when = parsedate_to_datetime(pub).astimezone(dt.timezone.utc)
        except Exception:  # noqa: BLE001
            continue
        # Google News appends " - Source" to titles
        if src and title.endswith(f" - {src}"):
            title = title[: -len(src) - 3].strip()
        elif not src and " - " in title and name == "Google News":
            title, src = title.rsplit(" - ", 1)
        items.append(
            {"title": title, "link": link, "source": (src or name).strip(),
             "time": when, "feed": name}
        )
    return items


def fetch_tavily(company, hours):
    """Tavily news search: broader/fresher coverage than the RSS feeds. Opt-in via TAVILY_API_KEY."""
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return []
    time_range = "month" if hours > 24 * 7 else "week"
    try:
        r = requests.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {key}"},
            json={"query": company["query"], "topic": "news", "time_range": time_range,
                  "max_results": 20, "include_published_date": True},
            timeout=25,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] Tavily search failed: {e}", file=sys.stderr)
        return []
    items = []
    for res in data.get("results", []):
        pub = res.get("published_date")
        try:
            when = parsedate_to_datetime(pub).astimezone(dt.timezone.utc) if pub else None
        except Exception:  # noqa: BLE001
            when = None
        if not when:
            continue
        url = res.get("url", "")
        src = urlparse(url).netloc.removeprefix("www.") or "Tavily"
        items.append({"title": (res.get("title") or "").strip(), "link": url,
                      "source": src, "time": when, "feed": "Tavily"})
    return items


def norm(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:80]


def collect_news(company, hours):
    all_items = []
    for name, url in feeds_for(company).items():
        all_items.extend(fetch_feed(name, url))
    all_items.extend(fetch_tavily(company, hours))
    all_items.extend(finnhub.company_news(company, hours))
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=hours)
    seen, fresh = {}, []
    for it in sorted(all_items, key=lambda x: x["time"], reverse=True):
        key = norm(it["title"])
        if it["time"] < cutoff or not relevant(company, it["title"]):
            continue
        if key in seen:
            kept = seen[key]
            if "news.google.com" in kept["link"] and "news.google.com" not in it["link"]:
                kept["link"] = it["link"]          # direct publisher link beats a Google redirect
            continue
        seen[key] = it
        it["score"] = len(POS_RE.findall(it["title"])) - len(NEG_RE.findall(it["title"]))
        it["importance"] = importance(it)
        fresh.append(it)
    return fresh


def importance(it):
    """0-10 heuristic: real news beats filings, listicles and stock-tip fluff."""
    t = it["title"]
    v = 3.0
    v += min(3, len(BIG_RE.findall(t)))
    v += 1.5 if TOP_SOURCES.search(it["source"]) else 0
    v += 0.5 * abs(it.get("score", 0))
    v -= 3 if NOISE_RE.search(t) else 0
    v -= 1 if re.search(r"marketbeat|simplywall|chartmill|tradingkey|tickerreport|etf daily", it["source"], re.I) else 0
    return round(max(0, min(10, v)), 1)


def price_file(ticker):
    return os.path.join(OUT_DIR, f"prices_{ticker}.json")


def load_cached_price(ticker):
    try:
        with open(price_file(ticker)) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def fetch_price(ticker):
    """Price + series from Yahoo; on failure (rate limit, outage) the last good copy on disk."""
    data = None
    for attempt in range(3):
        try:
            data = _fetch_price_yahoo(ticker)
            break
        except Exception as e:  # noqa: BLE001
            print(f"[warn] price fetch {ticker} attempt {attempt + 1} failed: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    if data:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(price_file(ticker), "w") as f:
            json.dump(data, f)
        return data
    cached = load_cached_price(ticker)
    if cached:
        print(f"[warn] using cached prices for {ticker}", file=sys.stderr)
    return cached


def _fetch_price_yahoo(ticker):
    if ticker:
        r = requests.get(chart_url(ticker), headers=HEADERS, timeout=25)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        meta = res["meta"]
        raw = res["indicators"]["quote"][0]["close"]
        full = [{"t": t * 1000, "c": round(c, 2)} for t, c in zip(res["timestamp"], raw) if c is not None]
        price = meta.get("regularMarketPrice")
        year = dt.datetime.now().year
        prev_year = [p for p in full if dt.datetime.fromtimestamp(p["t"] / 1000).year < year]
        ytd_base = prev_year[-1]["c"] if prev_year else (full[0]["c"] if full else None)  # last close of previous year
        ytd_pct = (price - ytd_base) / ytd_base * 100 if ytd_base and price else None
        series = full[-64:]            # ~3 months for the chart
        closes = [p["c"] for p in series]
        prev = meta.get("chartPreviousClose") or (closes[-2] if len(closes) > 1 else price)
        prev_day = closes[-2] if len(closes) > 1 else prev
        return {
            "price": price,
            "day_change_pct": (price - prev_day) / prev_day * 100 if prev_day else 0.0,
            "month_change_pct": (price - closes[-22]) / closes[-22] * 100 if len(closes) > 22 else 0.0,
            "closes": closes[-22:],
            "series": series,
            "ytd_change_pct": ytd_pct,
            "currency": meta.get("currency", "USD"),
        }


# -------------------------------------------------------------------- fundamentals
# Which figures to show, in this order: (our key, Finviz label, StockAnalysis label, kind)
FUND_FIELDS = [
    ("market_cap",  "Market Cap",     "Market Cap",             "money"),
    ("pe",          "P/E",            "PE Ratio",               "x"),
    ("fwd_pe",      "Forward P/E",    "Forward PE",             "x"),
    ("peg",         "PEG",            "PEG Ratio",              "x"),
    ("ps",          "P/S",            "PS Ratio",               "x"),
    ("pb",          "P/B",            "PB Ratio",               "x"),
    ("ev_ebitda",   "EV/EBITDA",      "EV / EBITDA",            "x"),
    ("eps",         "EPS (ttm)",      "EPS (Diluted)",          "usd"),
    ("revenue",     "Sales",          "Revenue",                "money"),
    ("net_income",  "Income",         "Net Income",             "money"),
    ("rev_growth",  "Sales Y/Y TTM",  "Revenue Growth (YoY)",   "pct"),
    ("gross_margin","Gross Margin",   "Gross Margin",           "pct"),
    ("oper_margin", "Oper. Margin",   "Operating Margin",       "pct"),
    ("net_margin",  "Profit Margin",  "Profit Margin",          "pct"),
    ("roe",         "ROE",            "Return on Equity (ROE)", "pct"),
    ("debt_equity", "Debt/Eq",        "Debt / Equity",          "x"),
    ("div_yield",   "Dividend TTM",   "Dividend Yield",         "text"),
    ("beta",        "Beta",           "Beta (5Y)",              "x"),
    ("range_52w",   "52W Range",      "52-Week Range",          "text"),
    ("short_float", "Short Float",    "Short % of Float",       "pct"),
    ("short_ratio", "Short Ratio",    "Short Ratio (days to cover)", "x"),
    ("short_shares","Short Interest", "Short Interest",         "shares"),
    ("float_shares","Shs Float",      "Float",                  "shares"),
    ("shares_out",  "Shs Outstand",   "Shares Outstanding",     "shares"),
    ("target",      "Target Price",   "Price Target",           "usd"),
    ("earnings",    "Earnings",       "Earnings Date",          "text"),
    ("perf_year",   "Perf Year",      "52-Week Price Change",   "pct"),
]
TAGS_RE = re.compile(r"<[^>]+>")


def _strip(html):
    return re.sub(r"\s+", " ", TAGS_RE.sub("", html)).replace("&amp;", "&").strip()


def _finviz(ticker):
    r = requests.get(f"https://finviz.com/quote.ashx?t={ticker}&p=d", headers=HEADERS, timeout=25)
    r.raise_for_status()
    pairs = re.findall(r'<div class="snapshot-td-label">(.*?)</div>.*?<div class="snapshot-td-content">(.*?)</div>', r.text, re.S)
    table = {_strip(k): _strip(v) for k, v in pairs}
    if "P/E" not in table:
        raise ValueError("Finviz table not found")
    out = {key: table.get(fl) for key, fl, _, _ in FUND_FIELDS}
    lo, hi = table.get("52W Low", ""), table.get("52W High", "")   # "61.37 349.32%" -> first number
    if lo and hi:
        out["range_52w"] = f"{lo.split()[0]} - {hi.split()[0]}"
    if out.get("div_yield"):  # Finviz: "0.00 (0.00%)" -> keep the percentage
        m = re.search(r"\(([^)]*)\)", out["div_yield"])
        out["div_yield"] = m.group(1) if m else out["div_yield"]
    return out


def _stockanalysis(ticker):
    r = requests.get(f"https://stockanalysis.com/stocks/{ticker.lower()}/statistics/", headers=HEADERS, timeout=25)
    r.raise_for_status()
    html = re.sub(r"<!--.*?-->", "", r.text, flags=re.S)
    rows = re.findall(r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>", html, re.S)
    table = {_strip(k): _strip(v) for k, v in rows}
    if "PE Ratio" not in table:
        raise ValueError("StockAnalysis table not found")
    return {key: table.get(sl) for key, _, sl, _ in FUND_FIELDS}


def fundamentals_file(ticker):
    return os.path.join(OUT_DIR, f"fundamentals_{ticker}.json")


def _next_earnings(ticker):
    """StockAnalysis' 'Earnings Date' is the next (estimated or confirmed) report; Finviz's
    'Earnings' is the last one until the next is confirmed."""
    try:
        return _stockanalysis(ticker).get("earnings")
    except Exception:  # noqa: BLE001
        return None


def fetch_fundamentals(ticker):
    """Valuation and quality figures as display strings. Finviz first, StockAnalysis second,
    the last successful copy on disk third. Returns {"source":..., "as_of":..., "values":{...}}."""
    for name, fn in (("Finviz", _finviz), ("StockAnalysis", _stockanalysis)):
        try:
            vals = fn(ticker)
            vals = {k: v for k, v in vals.items() if v not in (None, "", "-", "- -", "n/a", "N/A")}
            if vals and name == "Finviz":
                nxt = _next_earnings(ticker)
                if nxt:
                    vals["earnings_last"] = vals.get("earnings")
                    vals["earnings"] = nxt
            if vals:
                data = {"source": name, "as_of": dt.datetime.now().astimezone().isoformat(),
                        "values": vals}
                os.makedirs(OUT_DIR, exist_ok=True)
                with open(fundamentals_file(ticker), "w") as f:
                    json.dump(data, f, indent=1)
                return data
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {name} fundamentals for {ticker} failed: {e}", file=sys.stderr)
    try:
        with open(fundamentals_file(ticker)) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------------ render
NAVY = (16, 28, 48)
CARD = (255, 255, 255)
BG = (243, 245, 249)
TEXT = (28, 32, 40)
MUTED = (110, 118, 132)
GREEN = (28, 158, 92)
RED = (214, 60, 60)
GREY = (170, 176, 186)
ACCENT = (0, 120, 212)


FONT_CANDIDATES = [  # (regular, bold, ttc index for regular/bold)
    ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Helvetica.ttc", (0, 1)),          # macOS
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", (0, 0)),  # Debian/Ubuntu
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", (0, 0)),  # Fedora/Alpine
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", (0, 0)),
]
_FONT_CACHE = {}


def font(size, bold=False):
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    f = None
    for reg, bld, idx in FONT_CANDIDATES:
        path, index = (bld, idx[1]) if bold else (reg, idx[0])
        try:
            f = ImageFont.truetype(path, size, index=index)
            break
        except Exception:  # noqa: BLE001
            continue
    if f is None:  # Pillow >= 10.1 ships a scalable default font; older ones give a bitmap font
        try:
            f = ImageFont.load_default(size=size)
        except TypeError:
            f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


PLAIN = str.maketrans({"\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                       "\u2026": "...", "\u00a0": " ", "\u2022": "*", "\u2011": "-", "\u2012": "-"})


def plain(text):
    """Typographic punctuation -> ASCII, so no font fallback ever shows a box."""
    return text.translate(PLAIN)


def wrap(draw, text, fnt, max_w):
    words, lines, cur = plain(text).split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def sparkline(closes, w, h, up):
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if len(closes) < 2:
        return img
    lo, hi = min(closes), max(closes)
    span = (hi - lo) or 1
    pts = []
    for i, c in enumerate(closes):
        x = i * (w - 4) / (len(closes) - 1) + 2
        y = h - 4 - (c - lo) / span * (h - 8)
        pts.append((x, y))
    col = GREEN if up else RED
    fill = col + (40,)
    d.polygon(pts + [(pts[-1][0], h), (pts[0][0], h)], fill=fill)
    d.line(pts, fill=col + (255,), width=3, joint="curve")
    return img


def render(company, items, price, hours, out_path, fund=None):
    W, PAD = 1240, 40
    local_now = dt.datetime.now().astimezone()
    title_f, h1_f, body_f, small_f, big_f = font(38, True), font(24, True), font(22), font(17), font(46, True)
    max_items = 14
    shown = items[:max_items]

    # measure headline block height
    scratch = ImageDraw.Draw(Image.new("RGB", (W, 10)))
    text_w = W - 2 * PAD - 60
    rows = []
    for it in shown:
        lines = wrap(scratch, it["title"], body_f, text_w)
        rows.append((it, lines))
    headline_h = sum(28 * len(l) + 34 for _, l in rows) or 60
    H = 150 + 190 + 70 + 70 + headline_h + 90

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # header
    d.rectangle([0, 0, W, 140], fill=NAVY)
    d.text((PAD, 30), f"{company['name']}  ({company['exchange']}: {company['ticker']})", font=title_f, fill=(255, 255, 255))
    d.text((PAD, 84), f"Daily news brief  -  {local_now.strftime('%A, %d %B %Y  %H:%M %Z')}"
           f"  -  last {hours}h", font=font(20), fill=(190, 200, 220))

    # price card
    y = 170
    d.rounded_rectangle([PAD, y, W - PAD, y + 170], radius=18, fill=CARD, outline=(225, 229, 236))
    if price and price["price"]:
        up = price["day_change_pct"] >= 0
        col = GREEN if up else RED
        d.text((PAD + 30, y + 22), "Share price", font=small_f, fill=MUTED)
        d.text((PAD + 30, y + 48), f"${price['price']:,.2f}", font=big_f, fill=TEXT)
        ax, ay = PAD + 30, y + 118
        tri = [(ax, ay + 16), (ax + 18, ay + 16), (ax + 9, ay)] if up else [(ax, ay), (ax + 18, ay), (ax + 9, ay + 16)]
        d.polygon(tri, fill=col)
        d.text((ax + 28, y + 112), f"{price['day_change_pct']:+.2f}% vs previous close",
               font=h1_f, fill=col)
        mup = price["month_change_pct"] >= 0
        d.text((PAD + 520, y + 22), "Last month", font=small_f, fill=MUTED)
        d.text((PAD + 520, y + 48), f"{price['month_change_pct']:+.1f}%", font=font(34, True),
               fill=GREEN if mup else RED)
        if price.get("ytd_change_pct") is not None:
            yup = price["ytd_change_pct"] >= 0
            d.text((PAD + 520, y + 98), "Year to date", font=small_f, fill=MUTED)
            d.text((PAD + 520, y + 120), f"{price['ytd_change_pct']:+.1f}%", font=font(26, True),
                   fill=GREEN if yup else RED)
        sp = sparkline(price["closes"], 480, 120, mup)
        img.paste(sp, (W - PAD - 510, y + 25), sp)
    else:
        d.text((PAD + 30, y + 60), "Price unavailable", font=h1_f, fill=MUTED)

    # key ratios under the price card (wrapped to the card width)
    y += 190
    if fund and fund.get("values"):
        v = fund["values"]
        bits = [f"{lbl} {v[k]}" for k, lbl in (("market_cap", "Mkt cap"), ("pe", "P/E"), ("fwd_pe", "Fwd P/E"),
                                                 ("ps", "P/S"), ("ev_ebitda", "EV/EBITDA"), ("net_margin", "Net margin"),
                                                 ("rev_growth", "Rev growth"), ("earnings", "Earnings")) if v.get(k)]
        rf, line, lines = font(18), "", []
        for b in bits:
            trial = f"{line}   -   {b}" if line else b
            if d.textlength(trial, font=rf) <= W - 2 * PAD:
                line = trial
            else:
                lines.append(line)
                line = b
        if line:
            lines.append(line)
        for ln in lines:
            d.text((PAD, y), plain(ln), font=rf, fill=MUTED)
            y += 26
        y += 14
    else:
        y += 10

    # sentiment summary
    pos = sum(1 for i in items if i["score"] > 0)
    neg = sum(1 for i in items if i["score"] < 0)
    neu = len(items) - pos - neg
    d.text((PAD, y), f"{len(items)} headlines", font=h1_f, fill=TEXT)
    x = PAD + d.textlength(f"{len(items)} headlines", font=h1_f) + 30
    for label, n, col in (("positive", pos, GREEN), ("negative", neg, RED), ("neutral", neu, GREY)):
        d.ellipse([x, y + 7, x + 16, y + 23], fill=col)
        s = f"{n} {label}"
        d.text((x + 24, y), s, font=font(21), fill=MUTED)
        x += d.textlength(s, font=font(21)) + 60

    # headlines
    y += 55
    if not rows:
        d.text((PAD, y + 10), f"No {company['name']} news found in the last {hours} hours.",
               font=body_f, fill=MUTED)
    for it, lines in rows:
        col = GREEN if it["score"] > 0 else RED if it["score"] < 0 else GREY
        d.ellipse([PAD, y + 8, PAD + 14, y + 22], fill=col)
        for ln in lines:
            d.text((PAD + 32, y), ln, font=body_f, fill=TEXT)
            y += 28
        stamp = it["time"].astimezone().strftime("%a %d %b %H:%M")
        d.text((PAD + 32, y + 2), plain(f"{it['source']}  -  {stamp}"), font=small_f, fill=MUTED)
        y += 34
    if len(items) > max_items:
        d.text((PAD, y), f"+ {len(items) - max_items} more headlines (see the .json file)",
               font=small_f, fill=MUTED)
        y += 26

    # footer
    d.line([PAD, H - 55, W - PAD, H - 55], fill=(220, 224, 232), width=2)
    d.text((PAD, H - 42), "Sources: Google News, Yahoo Finance  -  Sentiment = keyword heuristic, "
           "not investment advice  -  generated by be_news_agent.py", font=font(15), fill=MUTED)

    img.save(out_path, "PNG", optimize=True)
    return out_path


# ---------------------------------------------------------------- history + page
def load_history(ticker):
    try:
        with open(history_file(ticker)) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


def update_history(company, items):
    """Merge today's items into history_<ticker>.json (all news ever seen, de-duplicated)."""
    ticker = company["ticker"]
    hist = [h for h in load_history(ticker) if relevant(company, h["title"])]
    seen = {norm(h["title"]) for h in hist}
    for it in items:
        k = norm(it["title"])
        if k in seen:
            continue
        seen.add(k)
        hist.append({**it, "time": it["time"].isoformat()})
    hist.sort(key=lambda h: h["time"], reverse=True)
    with open(history_file(ticker), "w") as f:
        json.dump(hist, f, indent=1)
    return hist


def build_page(companies, results):
    """Render output/news.html: one tab per company, all headlines in chronology."""
    try:
        tpl = open(TEMPLATE).read()
    except FileNotFoundError:
        print("[warn] page_template.html missing, page not built", file=sys.stderr)
        return None
    data = {"generated": dt.datetime.now().astimezone().isoformat(), "companies": []}
    for c in companies:
        hist, price = results.get(c["ticker"], (load_history(c["ticker"]), None))[:2]
        if price is None:
            price = load_cached_price(c["ticker"])
        hist = [h for h in hist if relevant(c, h["title"])]
        fund = results.get(c["ticker"], (None, None, None))[2] if c["ticker"] in results else None
        if fund is None:
            try:
                with open(fundamentals_file(c["ticker"])) as f:
                    fund = json.load(f)
            except Exception:  # noqa: BLE001
                fund = None
        read = articles.read_set(articles.load(OUT_DIR, c["ticker"]))
        data["companies"].append({
            "ticker": c["ticker"], "name": c["name"], "exchange": c["exchange"],
            "articles_read": len(read),
            "fundamentals": fund,
            "social": social.summarize(social._load(social.social_file(OUT_DIR, c["ticker"]))),
            "edgar": edgar.summarize(edgar._load(OUT_DIR, c["ticker"])),
            "short": shorts.summarize(shorts.load(OUT_DIR, c["ticker"])),
            "brief": load_brief(c["ticker"]),
            "analysts": finnhub.summarize(finnhub.load(OUT_DIR, c["ticker"])),
            "relative": load_relative(c["ticker"]),
            "calendar": calendar_for(c),
            "benchmark": c.get("benchmark"), "peers": c.get("peers", []),
            "ytd_change_pct": (price or {}).get("ytd_change_pct"),
            "closes": (price or {}).get("series", []),
            "items": [{"title": h["title"], "link": h["link"], "source": h["source"],
                       "time": h["time"], "score": h["score"],
                       "importance": h.get("importance", importance(h)),
                       **({"read": True} if h["link"] in read else {})} for h in hist],
        })
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    with open(PAGE, "w") as f:
        f.write(tpl.replace("__DATA__", blob))
    return PAGE


# --------------------------------------------------------------------------- main
def push_config():
    """Where to upload social data: push.json next to the script, or PUSH_URL / PUSH_TOKEN."""
    try:
        with open(os.path.join(HERE, "push.json")) as f:
            cfg = json.load(f)
        if cfg.get("url") and cfg.get("token"):
            return cfg
    except Exception:  # noqa: BLE001
        pass
    if os.environ.get("PUSH_URL") and os.environ.get("PUSH_TOKEN"):
        return {"url": os.environ["PUSH_URL"], "token": os.environ["PUSH_TOKEN"]}
    return None


def run_all(hours=24, backfill=False, only=None, verbose=True, pictures=True, push=None):
    """Collect news for every company, update histories, render pictures, rebuild the page.
    Returns (companies, summary list). Used by the CLI and by server.py."""
    os.makedirs(OUT_DIR, exist_ok=True)
    companies = load_companies()
    if any([enrich_company(c) for c in companies]):   # list: enrich every company, not just the first
        save_companies(companies)
    wanted = {t.strip().upper() for t in only.split(",")} if only else None
    day = dt.datetime.now().strftime("%Y-%m-%d")
    results, summary = {}, []
    series_cache = {}   # ticker -> price series, shared across companies (SPY, sector ETFs, peers)

    def series_for(t):
        if t not in series_cache:
            pr = fetch_price(t)
            series_cache[t] = (pr or {}).get("series") or []
        return series_cache[t]

    for c in companies:
        if wanted and c["ticker"] not in wanted:
            continue
        items = collect_news(c, hours)
        win = hours
        if len(items) < 3 and win < 72:  # quiet day / weekend: widen the window
            win = 72
            items = collect_news(c, win)
        price = fetch_price(c["ticker"])
        series_cache[c["ticker"]] = (price or {}).get("series") or []
        fund = fetch_fundamentals(c["ticker"])
        # analyst recommendations + earnings calendar (Finnhub, opt-in)
        try:
            finnhub.update_analysts(c, OUT_DIR)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] finnhub analysts for {c['ticker']} failed: {e}", file=sys.stderr)
        # short interest (Finviz snapshot + Nasdaq/FINRA series)
        try:
            shorts.update_short(c, (fund or {}).get("values"), OUT_DIR)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] short interest for {c['ticker']} failed: {e}", file=sys.stderr)
        # SEC filings + insiders
        try:
            edgar.update_edgar(c, OUT_DIR)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] edgar for {c['ticker']} failed: {e}", file=sys.stderr)
        # relative performance vs sector ETF, SPY, peers
        try:
            tickers = [c["ticker"], c.get("benchmark") or "SPY", "SPY"] + list(c.get("peers") or [])
            names = {c["ticker"]: c["name"], "SPY": "S&P 500"}
            names.update({t: relative.ETF_NAMES.get(t, t) for t in tickers})
            for pc in companies:
                names[pc["ticker"]] = pc["name"]
            rel = relative.compare(c["ticker"], {t: series_for(t) for t in dict.fromkeys(tickers)}, c.get("benchmark") or "SPY",
                                   list(c.get("peers") or []), names)
            with open(os.path.join(OUT_DIR, f"relative_{c['ticker']}.json"), "w") as f:
                json.dump(rel, f)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] relative for {c['ticker']} failed: {e}", file=sys.stderr)
        try:
            store = social.update_social(c, OUT_DIR, max_pages=12 if push else 8)
            if push:
                try:
                    store["articles"] = articles.load(OUT_DIR, c["ticker"])   # publishers rarely block a home connection
                    store["short"] = shorts.load(OUT_DIR, c["ticker"])         # Nasdaq's API may block the server too
                    store["brief"] = load_brief(c["ticker"])                   # written in Claude Code, lives on the Mac
                    msg = social.push_store(store, c["ticker"], push["url"], push["token"])
                    if verbose:
                        print(f"    pushed social -> {msg.get('message')}")
                except Exception as e:  # noqa: BLE001
                    print(f"[warn] push social for {c['ticker']} failed: {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] social for {c['ticker']} failed: {e}", file=sys.stderr)
        if backfill or not os.path.exists(history_file(c["ticker"])):
            hist = update_history(c, collect_news(c, 24 * 30))   # first run: pull everything the feeds hold
        else:
            hist = update_history(c, items)
        results[c["ticker"]] = (hist, price, fund)
        # full text of the most important articles (cached per link)
        try:
            articles.update_articles(c, hist, OUT_DIR)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] articles for {c['ticker']} failed: {e}", file=sys.stderr)
        if pictures:
            png = os.path.join(OUT_DIR, f"{c['ticker']}_news_{day}.png")
            render(c, items, price, win, png, fund)
            Image.open(png).save(os.path.join(OUT_DIR, f"{c['ticker']}_latest.png"))
            with open(os.path.join(OUT_DIR, f"{c['ticker']}_news_{day}.json"), "w") as f:
                json.dump({"generated": dt.datetime.now().astimezone().isoformat(), "hours": win,
                           "price": {k: v for k, v in (price or {}).items() if k not in ("closes", "series")},
                           "items": [{**i, "time": i["time"].isoformat()} for i in items]}, f, indent=2)
        if verbose:
            print(f"{c['ticker']:6s} {len(items):3d} headlines today · {len(hist):4d} in history · fundamentals: {fund['source'] if fund else 'none'}")
            for it in items[:5]:
                print(f"    [{'+' if it['score'] > 0 else '-' if it['score'] < 0 else ' '}] {it['title'][:90]}  ({it['source']})")
        summary.append(f"{c['ticker']} {len(items)}")

    page = build_page(companies, results)
    if page and verbose:
        print(f"page {page}")
    return companies, summary


def brief_file(ticker):
    return os.path.join(OUT_DIR, f"brief_{ticker}.json")


def load_brief(ticker):
    """The AI brief written for this company (brief_<T>.json), or None. Written in Claude Code and
    uploaded with --push-briefs; the server only stores and shows it."""
    try:
        with open(brief_file(ticker)) as f:
            b = json.load(f)
        return b if b.get("data") else None
    except Exception:  # noqa: BLE001
        return None


def push_briefs(push, companies=None):
    """Upload every local brief_<T>.json to the site (same endpoint and token as the social upload)."""
    sent = []
    for c in companies or load_companies():
        b = load_brief(c["ticker"])
        if not b:
            continue
        r = requests.post(push["url"].rstrip("/") + "/api/social/upload", json={"ticker": c["ticker"], "source": "mac", "brief": b},
                          headers={"X-Upload-Token": push["token"], "User-Agent": HEADERS["User-Agent"]}, timeout=60)
        r.raise_for_status()
        sent.append(c["ticker"])
    return sent


def load_relative(ticker):
    try:
        with open(os.path.join(OUT_DIR, f"relative_{ticker}.json")) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def calendar_for(c):
    """Dated upcoming events for one company: next earnings (Finnhub's confirmed date first,
    StockAnalysis' estimate otherwise) + events Claude found in the news."""
    events, seen = [], set()
    ne = (finnhub.load(OUT_DIR, c["ticker"]) or {}).get("next_earnings") or {}
    if ne.get("date") and ne["date"] >= dt.date.today().isoformat():
        what = "Earnings report" + (f" ({ne['quarter']})" if ne.get("quarter") else "") + (f", {ne['hour']}" if ne.get("hour") else "")
        events.append({"date": ne["date"], "what": what, "source": "Finnhub"})
        seen.add(ne["date"])
    try:
        with open(fundamentals_file(c["ticker"])) as f:
            v = json.load(f).get("values", {})
        if v.get("earnings"):
            d = dt.datetime.strptime(v["earnings"], "%b %d, %Y").date()
            if d >= dt.date.today() and not seen:
                events.append({"date": d.isoformat(), "what": "Earnings report (estimated until confirmed)", "source": "StockAnalysis"})
                seen.add(d.isoformat())
    except Exception:  # noqa: BLE001
        pass
    events.sort(key=lambda e: e["date"])
    return events


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=24, help="look-back window in hours (default 24)")
    ap.add_argument("--no-open", action="store_true", help="do not open the page after saving")
    ap.add_argument("--no-notify", action="store_true", help="skip macOS notification")
    ap.add_argument("--backfill", action="store_true", help="also pull everything the feeds still hold (~30 days) into the history")
    ap.add_argument("--only", help="ticker(s) to run, comma-separated, e.g. BE,AMZN")
    ap.add_argument("--add", metavar="TICKER_OR_NAME", help="add a company to track (e.g. NVDA or Tesla), then run it")
    ap.add_argument("--remove", metavar="TICKER", help="stop tracking a company")
    ap.add_argument("--push", action="store_true", help="upload collected social data to the website (needs push.json)")
    ap.add_argument("--push-briefs", action="store_true", help="upload the local brief_<T>.json files to the website and exit")
    ap.add_argument("--companies-from", metavar="URL", help="use the company list of a running site instead of the local one")
    args = ap.parse_args()

    push = push_config() if (args.push or args.push_briefs) else None
    if args.push and not push:
        print("[warn] --push given but push.json / PUSH_URL+PUSH_TOKEN missing; not uploading", file=sys.stderr)
    if args.companies_from or (push and not args.companies_from):
        url = (args.companies_from or push["url"]).rstrip("/") + "/api/companies"
        try:
            remote = requests.get(url, headers=HEADERS, timeout=30).json()
            if isinstance(remote, list) and remote:
                save_companies(remote)   # keep the Mac's list identical to the site's
                print(f"company list from site: {[c['ticker'] for c in remote]}")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] could not read company list from {url}: {e}", file=sys.stderr)

    if args.push_briefs:
        if not push:
            sys.exit("--push-briefs needs push.json / PUSH_URL+PUSH_TOKEN")
        print("briefs uploaded:", ", ".join(push_briefs(push)) or "none found")
        return
    if args.remove:
        print("removed" if remove_company(args.remove) else "not in the list", args.remove.upper())
        run_all(args.hours, only="", verbose=False)  # rebuild the page without it
        return
    if args.add:
        c, created = add_company(args.add)
        print(("added" if created else "already tracked") + f": {c['name']} ({c['exchange']}: {c['ticker']})")
        args.only = c["ticker"] if created else args.only

    companies, summary = run_all(args.hours, args.backfill, args.only, push=push)
    page = PAGE if os.path.exists(PAGE) else None

    if sys.platform == "darwin":
        if not args.no_notify:
            subprocess.run(["osascript", "-e",
                            f'display notification "headlines today: {", ".join(summary)}" '
                            f'with title "Daily stock news" sound name "Glass"'], check=False)
        if not args.no_open and page:
            subprocess.run(["open", page], check=False)


if __name__ == "__main__":
    main()
