"""
Relative performance: the company vs the S&P 500 (SPY), its sector ETF and a few peers.
Answers "was this move company-specific or did the whole sector move?".
"""
import datetime as dt

SECTOR_ETF = {"Technology": "XLK", "Communication Services": "XLC", "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP",
              "Energy": "XLE", "Financial Services": "XLF", "Healthcare": "XLV", "Industrials": "XLI", "Basic Materials": "XLB",
              "Real Estate": "XLRE", "Utilities": "XLU"}
INDUSTRY_ETF = {"Semiconductors": "SMH", "Semiconductor Equipment & Materials": "SMH", "Software - Application": "IGV",
                "Software - Infrastructure": "IGV", "Aerospace & Defense": "ITA", "Internet Retail": "XLY",
                "Electronic Gaming & Multimedia": "XLC", "Computer Hardware": "XLK", "Solar": "TAN", "Biotechnology": "XBI",
                "Oil & Gas E&P": "XOP", "Gold": "GDX", "Banks - Regional": "KRE", "Airlines": "JETS", "Homebuilding": "XHB"}
ETF_NAMES = {"SPY": "S&P 500", "XLK": "Technology", "XLC": "Communication", "XLY": "Consumer discretionary", "XLP": "Consumer staples",
             "XLE": "Energy", "XLF": "Financials", "XLV": "Health care", "XLI": "Industrials", "XLB": "Materials", "XLRE": "Real estate",
             "XLU": "Utilities", "SMH": "Semiconductors", "IGV": "Software", "ITA": "Aerospace & defense", "TAN": "Solar",
             "XBI": "Biotech", "XOP": "Oil & gas", "GDX": "Gold miners", "KRE": "Regional banks", "JETS": "Airlines", "XHB": "Homebuilders"}


def etf_for(sector, industry):
    return INDUSTRY_ETF.get(industry or "") or SECTOR_ETF.get(sector or "") or "SPY"


def _ret(series, days):
    """Return over the last `days` calendar days from a [{t, c}] series, in %."""
    if not series:
        return None
    last = series[-1]
    target = last["t"] - days * 86400 * 1000
    base = next((p for p in series if p["t"] >= target), series[0])
    if base is last or not base["c"]:
        return None
    return (last["c"] - base["c"]) / base["c"] * 100


def compare(company_ticker, series_by_ticker, benchmark, peers, names):
    """series_by_ticker: {ticker: [{t, c}, ...]} covering ~3 months. Returns the page dict."""
    windows = [("1d", 1), ("1w", 7), ("1m", 30), ("3m", 92)]
    rows = []
    if benchmark == "SPY":
        benchmark = None           # no sector ETF known: SPY plays the index role only
    order = [t for t in [company_ticker, benchmark, "SPY"] if t] + [p for p in peers if p not in (company_ticker, benchmark, "SPY")]
    for t in order:
        s = series_by_ticker.get(t)
        if not s:
            continue
        rows.append({"ticker": t, "name": names.get(t, t), "role": "company" if t == company_ticker else "sector" if t == benchmark else "index" if t == "SPY" else "peer",
                     "returns": {w: (round(_ret(s, d), 2) if _ret(s, d) is not None else None) for w, d in windows}})
    comp = next((r for r in rows if r["role"] == "company"), None)
    sec = next((r for r in rows if r["role"] == "sector"), None)
    idx = next((r for r in rows if r["role"] == "index"), None)
    excess = {}
    if comp:
        for w, _ in windows:
            c = comp["returns"].get(w)
            excess[w] = {"vs_sector": round(c - sec["returns"][w], 2) if sec and c is not None and sec["returns"].get(w) is not None else None,
                         "vs_index": round(c - idx["returns"][w], 2) if idx and c is not None and idx["returns"].get(w) is not None else None}
    # indexed series (base 100 at the first common date) for the chart
    lines = []
    for r in rows:
        s = series_by_ticker[r["ticker"]]
        base = s[0]["c"] or 1
        lines.append({"ticker": r["ticker"], "role": r["role"], "points": [{"t": p["t"], "v": round(p["c"] / base * 100, 2)} for p in s]})
    return {"benchmark": benchmark, "benchmark_name": names.get(benchmark, benchmark) if benchmark else None, "rows": rows, "excess": excess, "lines": lines,
            "as_of": dt.datetime.now().astimezone().isoformat()}


