"""
Finnhub (https://finnhub.io): company news by ticker, analyst recommendation trends and the
earnings calendar with consensus estimates. Opt-in: needs FINNHUB_API_KEY in the environment
or a one-line finnhub.key file next to this script (gitignored, like tavily.key).

Everything here is a no-op that returns [] / None when the key is missing or a call fails,
so the rest of the agent keeps working without it. Free tier: 60 calls/min, personal use.
"""
import datetime as dt
import json
import os
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://finnhub.io/api/v1"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh) stock-news-agent/1.0"}


def api_key():
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(os.path.join(HERE, "finnhub.key")) as f:
            return f.read().strip()
    except OSError:
        return ""


def _get(path, **params):
    key = api_key()
    if not key:
        return None
    r = requests.get(f"{API}/{path}", params={**params, "token": key}, headers=HEADERS, timeout=25)
    if r.status_code == 429:
        raise RuntimeError("rate limited (60 calls/min on the free plan)")
    if r.status_code == 403:
        raise RuntimeError("403: endpoint not available on this plan")
    if r.status_code >= 400:   # do not echo the URL: it carries the token
        raise RuntimeError(f"HTTP {r.status_code} from /{path}")
    return r.json()


# ----------------------------------------------------------------------------- news
def company_news(company, hours):
    """Headlines for one ticker in the last `hours`, shaped like the agent's feed items
    (title, link, source, time, feed). Finnhub aggregates Yahoo, MarketWatch, SeekingAlpha,
    press releases and more, so this fills gaps the RSS feeds miss."""
    if not api_key():
        return []
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(hours=hours)
    try:
        data = _get("company-news", symbol=company["ticker"],
                    **{"from": start.date().isoformat(), "to": now.date().isoformat()})
    except Exception as e:  # noqa: BLE001
        print(f"[warn] Finnhub news for {company['ticker']} failed: {e}", file=sys.stderr)
        return []
    items = []
    for a in data or []:
        title = (a.get("headline") or "").strip()
        url = (a.get("url") or "").strip()
        ts = a.get("datetime")
        if not title or not url or not ts:
            continue
        when = dt.datetime.fromtimestamp(int(ts), dt.timezone.utc)
        src = (a.get("source") or "Finnhub").strip()
        items.append({"title": title, "link": url, "source": src, "time": when, "feed": "Finnhub"})
    return items


# -------------------------------------------------------------------------- analysts
def analysts_file(out_dir, ticker):
    return os.path.join(out_dir, f"finnhub_{ticker}.json")


def load(out_dir, ticker):
    try:
        with open(analysts_file(out_dir, ticker)) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def update_analysts(company, out_dir):
    """Latest recommendation counts (strong buy .. strong sell, monthly history) and the next
    earnings date with EPS / revenue estimates. Saved to finnhub_<T>.json; the previous copy
    is kept when a call fails. Returns the record or None."""
    if not api_key():
        return None
    ticker = company["ticker"]
    prev = load(out_dir, ticker) or {}
    rec = {"updated": dt.datetime.now().astimezone().isoformat(), "source": "Finnhub"}

    try:
        trend = _get("stock/recommendation", symbol=ticker) or []
        trend = sorted(trend, key=lambda t: t.get("period", ""))
        rec["recommendations"] = [
            {"period": t.get("period"), "strong_buy": t.get("strongBuy", 0), "buy": t.get("buy", 0),
             "hold": t.get("hold", 0), "sell": t.get("sell", 0), "strong_sell": t.get("strongSell", 0)}
            for t in trend[-6:]]
    except Exception as e:  # noqa: BLE001
        print(f"[warn] Finnhub recommendations for {ticker} failed: {e}", file=sys.stderr)
        rec["recommendations"] = prev.get("recommendations", [])

    try:
        today = dt.date.today()
        cal = _get("calendar/earnings", symbol=ticker, **{"from": (today - dt.timedelta(days=100)).isoformat(),
                                                        "to": (today + dt.timedelta(days=120)).isoformat()})
        rows = sorted((cal or {}).get("earningsCalendar", []), key=lambda r: r.get("date", ""))
        nxt = next((r for r in rows if r.get("date", "") >= today.isoformat()), None)
        last = next((r for r in reversed(rows) if r.get("date", "") < today.isoformat()), None)
        rec["next_earnings"] = _earn(nxt)
        rec["last_earnings"] = _earn(last)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] Finnhub earnings calendar for {ticker} failed: {e}", file=sys.stderr)
        rec["next_earnings"] = prev.get("next_earnings")
        rec["last_earnings"] = prev.get("last_earnings")
    if not rec.get("last_earnings"):
        # the free calendar only lists upcoming dates; earnings surprises cover the last quarters
        try:
            sur = sorted(_get("stock/earnings", symbol=ticker) or [], key=lambda r: r.get("period", ""))
            if sur and sur[-1].get("actual") is not None:
                q = sur[-1]
                rec["last_earnings"] = {"date": q.get("period"), "period_end": True, "hour": "",
                                        "quarter": f"Q{q['quarter']} {q['year']}" if q.get("quarter") and q.get("year") else "",
                                        "eps_estimate": q.get("estimate"), "eps_actual": q.get("actual"),
                                        "revenue_estimate": None, "revenue_actual": None}
        except Exception as e:  # noqa: BLE001
            print(f"[warn] Finnhub earnings surprises for {ticker} failed: {e}", file=sys.stderr)

    if not rec.get("recommendations") and not rec.get("next_earnings") and not rec.get("last_earnings"):
        return prev or None
    os.makedirs(out_dir, exist_ok=True)
    with open(analysts_file(out_dir, ticker), "w") as f:
        json.dump(rec, f, indent=1)
    return rec


def _earn(row):
    if not row:
        return None
    hour = {"bmo": "before market open", "amc": "after market close", "dmh": "during market hours"}.get(row.get("hour") or "", "")
    return {"date": row.get("date"), "hour": hour,
            "quarter": f"Q{row['quarter']} {row['year']}" if row.get("quarter") and row.get("year") else "",
            "eps_estimate": row.get("epsEstimate"), "eps_actual": row.get("epsActual"),
            "revenue_estimate": row.get("revenueEstimate"), "revenue_actual": row.get("revenueActual")}


def summarize(rec):
    """Compact view for the page: latest counts, consensus label, change vs 3 months ago, earnings."""
    if not rec:
        return None
    recs = rec.get("recommendations") or []
    latest = recs[-1] if recs else None
    out = {"updated": rec.get("updated"), "source": rec.get("source", "Finnhub"),
           "next_earnings": rec.get("next_earnings"), "last_earnings": rec.get("last_earnings"),
           "history": recs}
    if latest:
        total = sum(latest[k] for k in ("strong_buy", "buy", "hold", "sell", "strong_sell"))
        score = ((latest["strong_buy"] * 1 + latest["buy"] * 2 + latest["hold"] * 3 + latest["sell"] * 4
                  + latest["strong_sell"] * 5) / total) if total else None
        label = (None if score is None else "Strong buy" if score < 1.5 else "Buy" if score < 2.5
                 else "Hold" if score < 3.5 else "Sell" if score < 4.5 else "Strong sell")
        out.update({"period": latest["period"], "counts": latest, "analysts": total,
                    "score": round(score, 2) if score is not None else None, "consensus": label})
        if len(recs) >= 4:
            old = recs[-4]
            out["bullish_change"] = (latest["strong_buy"] + latest["buy"]) - (old["strong_buy"] + old["buy"])
    return out



