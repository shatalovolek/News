"""
SEC EDGAR for the Newsroom: recent filings, 8-K items, insider transactions (Form 4).

Free, official, no key. The SEC asks for a descriptive User-Agent with a contact address
and at most 10 requests per second. Foreign filers (ADRs such as SoftBank) file 6-K instead
of 8-K and have no Form 4; companies without a CIK (many OTC ADRs) get an empty result.

Store: output/edgar_<TICKER>.json = {"cik", "name", "sic", "filings": [...], "insider": [...], "updated"}
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time

import requests

UA = {"User-Agent": "stock-newsroom/1.0 (personal dashboard; shatalov@beat-trade.com)", "Accept-Encoding": "gzip, deflate"}
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FILING_DAYS = 120
FORM4_MAX = 30                     # newest Form 4 filings parsed per company
INTERESTING = ("8-K", "8-K/A", "6-K", "10-Q", "10-K", "10-K/A", "20-F", "4", "144", "S-3", "S-3ASR", "S-1", "S-1/A",
               "424B5", "424B3", "424B4", "424B2", "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13G", "SCHEDULE 13G/A",
               "SCHEDULE 13D", "SCHEDULE 13D/A", "DEF 14A", "DEFA14A", "8-A12B", "S-8", "F-3", "F-1", "F-3ASR", "SD", "11-K", "ARS")
ITEM_NAMES = {
    "1.01": "Material agreement", "1.02": "Termination of agreement", "1.03": "Bankruptcy", "1.05": "Cybersecurity incident",
    "2.01": "Acquisition or disposition", "2.02": "Results of operations", "2.03": "New debt obligation", "2.04": "Debt acceleration",
    "2.05": "Exit or disposal costs", "2.06": "Material impairment", "3.01": "Listing notice", "3.02": "Unregistered sale of shares",
    "3.03": "Change to shareholder rights", "4.01": "Auditor change", "4.02": "Non-reliance on past financials",
    "5.01": "Change in control", "5.02": "Officer or director change", "5.03": "Bylaw or charter change", "5.07": "Shareholder vote",
    "7.01": "Regulation FD disclosure", "8.01": "Other events", "9.01": "Exhibits",
}
FORM_LABELS = {"8-K": "Current report", "6-K": "Foreign issuer report", "10-Q": "Quarterly report", "10-K": "Annual report",
               "20-F": "Annual report (foreign)", "4": "Insider transaction", "144": "Insider sale notice", "S-3": "Shelf registration",
               "S-3ASR": "Shelf registration", "S-1": "IPO / registration", "424B5": "Prospectus supplement (offering)",
               "424B3": "Prospectus", "424B4": "Prospectus (offering)", "424B2": "Prospectus (offering)", "SC 13D": "Activist stake (>5%)",
               "SC 13G": "Passive stake (>5%)", "SCHEDULE 13G": "Passive stake (>5%)", "SCHEDULE 13D": "Activist stake (>5%)",
               "DEF 14A": "Proxy statement", "S-8": "Employee stock plan", "F-3": "Shelf registration (foreign)", "8-A12B": "Listing registration"}
TX_CODES = {"P": "open-market buy", "S": "open-market sale", "A": "grant/award", "M": "option exercise", "F": "tax withholding",
            "G": "gift", "D": "disposition to issuer", "C": "conversion", "X": "option exercise", "J": "other"}

_cik_map = None


def cik_map():
    global _cik_map
    if _cik_map is None:
        r = requests.get(TICKERS_URL, headers=UA, timeout=30)
        r.raise_for_status()
        _cik_map = {v["ticker"].upper(): (int(v["cik_str"]), v["title"]) for v in r.json().values()}
    return _cik_map


def edgar_file(out_dir, ticker):
    return os.path.join(out_dir, f"edgar_{ticker}.json")


def _load(out_dir, ticker):
    try:
        with open(edgar_file(out_dir, ticker)) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"cik": None, "filings": [], "insider": [], "parsed": [], "updated": None}


def _get(url):
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    time.sleep(0.12)   # stay under 10 req/s
    return r


def _tag(block, tag):
    m = re.search(rf"<{tag}>\s*<value>(.*?)</value>", block, re.S) or re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
    return m.group(1).strip() if m else None


def parse_form4(cik, accession, primary_doc):
    """Open-market and other transactions from one Form 4 filing."""
    acc = accession.replace("-", "")
    doc = primary_doc.split("/")[-1] if primary_doc.startswith("xsl") else primary_doc
    x = _get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}").text
    owner = html.unescape(_tag(x, "rptOwnerName") or "")
    title = html.unescape(_tag(x, "officerTitle") or "") or ("Director" if "<isDirector>1</isDirector>" in x or "<isDirector>true</isDirector>" in x else "")
    if not title and ("<isTenPercentOwner>1</isTenPercentOwner>" in x or "<isTenPercentOwner>true</isTenPercentOwner>" in x):
        title = "10% owner"
    out = []
    for tr in re.findall(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", x, re.S):
        code = _tag(tr, "transactionCode")
        try:
            shares = float(_tag(tr, "transactionShares") or 0)
            price = float(_tag(tr, "transactionPricePerShare") or 0)
        except ValueError:
            shares, price = 0.0, 0.0
        out.append({"date": _tag(tr, "transactionDate"), "owner": owner.title() if owner.isupper() else owner, "title": title,
                    "code": code, "kind": TX_CODES.get(code, code), "shares": shares, "price": price,
                    "value": round(shares * price), "ad": _tag(tr, "transactionAcquiredDisposedCode"),
                    "accession": accession})
    return out


def update_edgar(company, out_dir):
    """Refresh filings + insider transactions for one company. Returns the store."""
    t = company["ticker"]
    store = _load(out_dir, t)
    cik_info = cik_map().get(t)
    if not cik_info:
        store.update({"cik": None, "updated": dt.datetime.now().astimezone().isoformat(), "note": "no SEC registrant for this ticker"})
        _save(store, out_dir, t)
        return store
    cik, name = cik_info
    j = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()
    rec = j["filings"]["recent"]
    cutoff = (dt.date.today() - dt.timedelta(days=FILING_DAYS)).isoformat()
    filings, form4s = [], []
    for form, date, acc, doc, items, desc, rdate in zip(rec["form"], rec["filingDate"], rec["accessionNumber"],
                                                       rec["primaryDocument"], rec["items"], rec["primaryDocDescription"], rec["reportDate"]):
        if date < cutoff:
            break
        if form == "4":
            form4s.append((acc, doc, date))
        if form not in INTERESTING:
            continue
        acc_nd = acc.replace("-", "")
        item_list = [i.strip() for i in (items or "").split(",") if i.strip()]
        filings.append({
            "form": form, "label": FORM_LABELS.get(form, form), "date": date, "report_date": rdate or None,
            "items": [{"code": i, "name": ITEM_NAMES.get(i, "")} for i in item_list if i != "9.01"],
            "description": (desc or "")[:120],
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nd}/{doc}" if doc else f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nd}/",
            "index_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nd}/",
            "accession": acc,
        })
    # insider transactions: parse new Form 4s only
    parsed = set(store.get("parsed", []))
    insider = [tx for tx in store.get("insider", []) if tx.get("date", "") >= cutoff]
    for acc, doc, date in form4s[:FORM4_MAX]:
        if acc in parsed:
            continue
        try:
            insider.extend(parse_form4(cik, acc, doc))
            parsed.add(acc)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] form 4 {acc} for {t}: {e}", file=sys.stderr)
    insider.sort(key=lambda x: (x.get("date") or "", x.get("accession")), reverse=True)
    store.update({"cik": cik, "name": name, "sic": j.get("sicDescription"), "filings": filings, "insider": insider,
                  "parsed": sorted(parsed)[-200:], "updated": dt.datetime.now().astimezone().isoformat(), "note": None})
    _save(store, out_dir, t)
    return store


def _save(store, out_dir, ticker):
    os.makedirs(out_dir, exist_ok=True)
    with open(edgar_file(out_dir, ticker), "w") as f:
        json.dump(store, f)


def summarize(store, days=90):
    """Numbers for the page."""
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    ins = [x for x in store.get("insider", []) if (x.get("date") or "") >= cutoff]
    buys = [x for x in ins if x["code"] == "P"]
    sells = [x for x in ins if x["code"] == "S"]
    filings = store.get("filings", [])
    last_8k = next((f for f in filings if f["form"] in ("8-K", "6-K")), None)
    shelf = [f for f in filings if f["form"] in ("S-3", "S-3ASR", "F-3", "F-3ASR", "424B5", "424B4", "424B2")]
    stakes = [f for f in filings if f["form"].startswith(("SC 13", "SCHEDULE 13"))]
    return {
        "cik": store.get("cik"), "note": store.get("note"), "updated": store.get("updated"), "sic": store.get("sic"),
        "buys": {"n": len(buys), "shares": sum(x["shares"] for x in buys), "value": sum(x["value"] for x in buys),
                 "people": len({x["owner"] for x in buys})},
        "sells": {"n": len(sells), "shares": sum(x["shares"] for x in sells), "value": sum(x["value"] for x in sells),
                  "people": len({x["owner"] for x in sells})},
        "last_8k": last_8k, "shelf": shelf[:3], "stakes": stakes[:3],
        "filings": filings[:14],
        "insider": [x for x in ins if x["code"] in ("P", "S", "A", "M", "G")][:12],
        "days": days,
    }
