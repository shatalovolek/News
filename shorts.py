"""
Short interest: how much of the float is sold short, days to cover, and how it moves.

Sources: the Finviz snapshot (short % of float, short ratio, shares short, float) taken on every
run and stored as a dated snapshot; for Nasdaq-listed names the official bi-monthly FINRA series
from Nasdaq's API (settlement date, shares short, average volume, days to cover). Store:
output/short_<TICKER>.json = {"history": [...nasdaq rows...], "snapshots": [...], "updated"}.
"""
import datetime as dt
import json
import os
import re
import sys

import requests

NASDAQ_H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
            "Accept": "application/json", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}


def short_file(out_dir, ticker):
    return os.path.join(out_dir, f"short_{ticker}.json")


def load(out_dir, ticker):
    try:
        with open(short_file(out_dir, ticker)) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"history": [], "snapshots": [], "updated": None, "nasdaq": None}


def _num(s):
    """'19.21M' -> 19210000, '6.94%' -> 6.94, '1.28' -> 1.28."""
    if s is None:
        return None
    s = str(s).replace(",", "").replace("$", "").strip()
    m = re.match(r"^(-?[\d.]+)\s*([KMB%]?)", s)
    if not m:
        return None
    v = float(m.group(1))
    return v * {"K": 1e3, "M": 1e6, "B": 1e9, "%": 1, "": 1}[m.group(2)]


def fetch_nasdaq(ticker):
    r = requests.get(f"https://api.nasdaq.com/api/quote/{ticker}/short-interest", params={"assetclass": "stocks"},
                     headers=NASDAQ_H, timeout=25)
    r.raise_for_status()
    j = r.json()
    data = j.get("data")
    if not data:
        return None   # not Nasdaq-listed
    rows = []
    for x in (data.get("shortInterestTable") or {}).get("rows") or []:
        try:
            d = dt.datetime.strptime(x["settlementDate"], "%m/%d/%Y").date().isoformat()
        except Exception:  # noqa: BLE001
            continue
        rows.append({"date": d, "interest": _num(x.get("interest")), "adv": _num(x.get("avgDailyShareVolume")),
                     "dtc": float(x.get("daysToCover") or 0)})
    rows.sort(key=lambda r: r["date"])
    return rows


def update_short(company, fund_values, out_dir):
    """Add today's Finviz snapshot; refresh the Nasdaq series (if listed there). Returns the store."""
    t = company["ticker"]
    store = load(out_dir, t)
    today = dt.date.today().isoformat()
    v = fund_values or {}
    snap = {"date": today, "short_float": _num(v.get("short_float")), "short_ratio": _num(v.get("short_ratio")),
            "short_shares": _num(v.get("short_shares")), "float_shares": _num(v.get("float_shares"))}
    if any(snap[k] is not None for k in ("short_float", "short_ratio", "short_shares")):
        store["snapshots"] = [s for s in store.get("snapshots", []) if s["date"] != today] + [snap]
        store["snapshots"] = sorted(store["snapshots"], key=lambda s: s["date"])[-120:]
    try:
        rows = fetch_nasdaq(t)
        store["nasdaq"] = rows is not None
        if rows:
            store["history"] = rows
    except Exception as e:  # noqa: BLE001
        print(f"[warn] nasdaq short interest for {t}: {str(e)[:100]}", file=sys.stderr)
    store["updated"] = dt.datetime.now().astimezone().isoformat()
    os.makedirs(out_dir, exist_ok=True)
    with open(short_file(out_dir, t), "w") as f:
        json.dump(store, f)
    return store


def merge(store, incoming):
    """Server side: take the Nasdaq series and snapshots uploaded from the Mac."""
    if not incoming:
        return store
    if incoming.get("history"):
        store["history"] = incoming["history"]
        store["nasdaq"] = True
    by_date = {s["date"]: s for s in store.get("snapshots", [])}
    for s in incoming.get("snapshots", []):
        by_date.setdefault(s["date"], s)
    store["snapshots"] = sorted(by_date.values(), key=lambda s: s["date"])[-120:]
    return store


def summarize(store):
    snaps = store.get("snapshots", [])
    hist = store.get("history", [])
    cur = snaps[-1] if snaps else {}
    out = {"updated": store.get("updated"), "nasdaq": store.get("nasdaq"),
           "short_float": cur.get("short_float"), "short_ratio": cur.get("short_ratio"),
           "short_shares": cur.get("short_shares"), "float_shares": cur.get("float_shares"),
           "as_of": cur.get("date"), "change_pct": None, "change_since": None, "series": [], "series_kind": None}
    if hist:
        last = hist[-1]
        out["series"] = [{"d": r["date"], "v": r["interest"], "dtc": r["dtc"]} for r in hist[-24:]]
        out["series_kind"] = "FINRA bi-monthly, shares short"
        out["official_shares"] = last["interest"]
        out["official_dtc"] = last["dtc"]
        out["official_date"] = last["date"]
        if len(hist) > 1 and hist[-2]["interest"]:
            out["change_pct"] = round((last["interest"] - hist[-2]["interest"]) / hist[-2]["interest"] * 100, 1)
            out["change_since"] = hist[-2]["date"]
        if out["float_shares"] and last["interest"]:
            out["official_float_pct"] = round(last["interest"] / out["float_shares"] * 100, 2)
    elif len(snaps) > 1:
        pts = [s for s in snaps if s.get("short_float") is not None]
        out["series"] = [{"d": s["date"], "v": s["short_float"]} for s in pts[-60:]]
        out["series_kind"] = "Finviz snapshots, % of float"
        prev = next((s for s in reversed(pts[:-1]) if s["short_float"] != pts[-1]["short_float"]), None)
        if prev and prev["short_float"]:
            out["change_pct"] = round((pts[-1]["short_float"] - prev["short_float"]) / prev["short_float"] * 100, 1)
            out["change_since"] = prev["date"]
    return out


def describe(sm):
    if not sm or sm.get("short_float") is None and not sm.get("official_shares"):
        return "SHORT INTEREST: no data"
    bits = []
    if sm.get("short_float") is not None:
        bits.append(f"{sm['short_float']:.1f}% of float short")
    if sm.get("short_ratio") is not None:
        bits.append(f"days to cover {sm['short_ratio']:.1f}")
    if sm.get("official_shares"):
        bits.append(f"FINRA {sm['official_date']}: {sm['official_shares']/1e6:.1f}M shares short")
    if sm.get("change_pct") is not None:
        bits.append(f"change vs previous report {sm['change_pct']:+.1f}% (since {sm['change_since']})")
    return "SHORT INTEREST: " + ", ".join(bits)
