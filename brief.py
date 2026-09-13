"""
AI brief per company: what happened, in what tone, what matters, what to watch.

Uses the Claude API (Anthropic SDK). Needs ANTHROPIC_API_KEY in the environment.
Inputs: the last 3 days of headlines, StockTwits counts and top messages, price moves,
fundamentals. Output: a validated JSON object (structured outputs), cached in
output/brief_<TICKER>.json and regenerated every BRIEF_HOURS (default 12).

Environment:
    ANTHROPIC_API_KEY   required
    BRIEF_MODEL         default claude-opus-5   (claude-sonnet-5 is ~2.5x cheaper)
    BRIEF_LANG          language of the text, default "en" ("ru" for Russian)
    BRIEF_HOURS         regenerate after this many hours, default 12
"""
import datetime as dt
import hashlib
import json
import os
import sys

MODEL = os.environ.get("BRIEF_MODEL", "claude-opus-5")
LANG = os.environ.get("BRIEF_LANG", "en")
HOURS = float(os.environ.get("BRIEF_HOURS", 12))
LANG_NAMES = {"en": "English", "ru": "Russian", "de": "German", "uk": "Ukrainian"}

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "3-5 sentences: what actually happened in the period and why it matters for the stock. Plain language, specific, no filler."},
        "tone": {"type": "string", "enum": ["positive", "neutral", "negative", "mixed"]},
        "tone_score": {"type": "number", "description": "-1 (clearly negative) to 1 (clearly positive), 0 neutral"},
        "themes": {"type": "array", "items": {"type": "string"}, "description": "2-5 short theme labels, 1-3 words each"},
        "key_events": {"type": "array", "items": {"type": "object", "properties": {
            "what": {"type": "string"}, "why_it_matters": {"type": "string"},
            "headline_ids": {"type": "array", "items": {"type": "integer"}}},
            "required": ["what", "why_it_matters", "headline_ids"], "additionalProperties": False},
            "description": "Up to 4 real events (not opinion pieces or holdings filings), most important first"},
        "retail_mood": {"type": "string", "description": "One sentence on what retail investors on StockTwits are saying, or 'no data' if none"},
        "risks": {"type": "array", "items": {"type": "string"}, "description": "0-3 concrete near-term risks or open questions"},
        "watch": {"type": "string", "description": "One sentence: the next concrete thing to watch (date, event, level)"},
        "noise_share": {"type": "number", "description": "0-1: share of the headlines that were filler (filings, listicles, stock-tip pieces)"},
        "whats_new": {"type": "string", "description": "1-2 sentences: what is new compared with the PREVIOUS BRIEF in the input. 'First brief' if there is none; 'Nothing material' if nothing changed."},
        "upcoming_events": {"type": "array", "items": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD; use the year from NOW if the input gives only day and month"},
            "what": {"type": "string"}, "source": {"type": "string", "description": "headline source or 'filing' or 'fundamentals'"}},
            "required": ["date", "what", "source"], "additionalProperties": False},
            "description": "Dated future events mentioned in the input (earnings, index inclusion, product launches, shareholder votes, lock-up expiries, court dates). Only real dates from the input, at most 6."},
    },
    "required": ["summary", "tone", "tone_score", "themes", "key_events", "retail_mood", "risks", "watch", "noise_share", "whats_new", "upcoming_events"],
    "additionalProperties": False,
}

SYSTEM = """You write the daily brief for one listed company on a private investing dashboard.
You get the recent headlines (each with an id, source, time and a heuristic importance score),
retail-investor chatter from StockTwits, the price move and a few fundamentals.

Rules:
- Report what happened; do not give investment advice or price predictions.
- Separate real events (results, deals, products, index changes, lawsuits, guidance, analyst actions with substance) from filler
  (institutional holdings filings, "should you buy" pieces, listicles, generic market-wrap mentions). Filler never becomes a key event.
- If nothing material happened, say so plainly in the summary; do not inflate.
- Attribute claims to their source when they are opinions ("Seeking Alpha argues...").
- Tone is about the news flow of the period, not the long-term story.
- Be concrete: numbers, names, dates from the input. Never invent facts not present in the input.
- When ARTICLE TEXT is given for a headline id, prefer the article's facts over the headline's wording; quote numbers from it.
- SEC filings and insider transactions in the input are official; an open-market insider buy or sale is always worth a mention.
- Relative performance tells you whether a move was company-specific or sector-wide; say which.
- Write in {language}. Keep the summary under 120 words."""


def brief_file(out_dir, ticker):
    return os.path.join(out_dir, f"brief_{ticker}.json")


def load_brief(out_dir, ticker):
    try:
        with open(brief_file(out_dir, ticker)) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def build_input(company, hist, social, price, fund, days=3, extra=None, previous=None):
    """Compact plain-text input for the model plus the headline id map."""
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = (now - dt.timedelta(days=days)).isoformat()
    heads = [h for h in hist if h["time"] >= cutoff][:60]
    lines = [f"COMPANY: {company['name']} ({company['exchange']}: {company['ticker']})",
             f"NOW: {now.strftime('%Y-%m-%d %H:%M UTC')}", ""]
    if price and price.get("price"):
        lines.append(f"PRICE: ${price['price']:.2f}, day {price.get('day_change_pct', 0):+.1f}%, "
                     f"month {price.get('month_change_pct', 0):+.1f}%, YTD {(price.get('ytd_change_pct') or 0):+.1f}%")
    if fund and fund.get("values"):
        v = fund["values"]
        keep = [(k, v[k]) for k in ("market_cap", "pe", "fwd_pe", "ps", "rev_growth", "net_margin", "earnings", "target") if v.get(k)]
        lines.append("FUNDAMENTALS: " + ", ".join(f"{k}={val}" for k, val in keep))
    lines.append("")
    lines.append(f"HEADLINES (last {days} days, newest first; importance 0-10 is a heuristic):")
    for i, h in enumerate(heads):
        t = dt.datetime.fromisoformat(h["time"]).strftime("%m-%d %H:%M")
        lines.append(f"[{i}] {t} | {h['source']} | imp {h.get('importance', 0)} | {h['title']}")
    if not heads:
        lines.append("(none)")
    lines.append("")
    if social and social.get("last7", {}).get("st"):
        l24, l7 = social["last24"], social["last7"]
        lines.append(f"STOCKTWITS: {l24['st']} messages in 24h ({l24['bull']} bullish / {l24['bear']} bearish tagged), "
                     f"{l7['st']} in 7 days ({l7['bull']}/{l7['bear']}), buzz vs average day: {social.get('buzz')}")
        for p in social.get("top_stocktwits", [])[:8]:
            body = " ".join(p["body"].split())[:160]
            lines.append(f"  - ({p.get('sentiment') or 'untagged'}, {p.get('likes', 0)} likes) {body}")
    else:
        lines.append("STOCKTWITS: no data")
    if social and social.get("trend_now") is not None:
        lines.append(f"GOOGLE SEARCH INTEREST: {social['trend_now']} now vs 90-day peak {social.get('trend_peak')}")
    for block in (extra or []):
        if block:
            lines.append("")
            lines.append(block)
    if previous and previous.get("data"):
        lines.append("")
        lines.append(f"PREVIOUS BRIEF (generated {previous['generated'][:16]}): {previous['data']['summary']}")
        if previous["data"].get("watch"):
            lines.append(f"PREVIOUS 'WATCH': {previous['data']['watch']}")
    return "\n".join(lines), heads


def edgar_block(es):
    """Text block from edgar.summarize() output."""
    if not es or not es.get("cik"):
        return "SEC FILINGS: none (no SEC registrant for this ticker)"
    lines = [f"SEC FILINGS (last {es['days']} days): insider open-market buys {es['buys']['n']} (${es['buys']['value']:,.0f}, {es['buys']['people']} people), "
             f"sales {es['sells']['n']} (${es['sells']['value']:,.0f}, {es['sells']['people']} people)"]
    for f in es.get("filings", [])[:8]:
        items = ", ".join(i["name"] for i in f.get("items", []) if i.get("name"))
        lines.append(f"  - {f['date']} {f['form']} {f['label']}" + (f": {items}" if items else ""))
    for x in es.get("insider", [])[:5]:
        lines.append(f"  - insider {x['date']} {x['owner']} ({x['title'] or 'insider'}): {x['kind']} {x['shares']:,.0f} sh @ ${x['price']:.2f}")
    return "\n".join(lines)


def needs_refresh(existing, input_hash, force=False):
    if force or not existing:
        return True
    try:
        age = (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(existing["generated"])).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return True
    if age >= HOURS:
        return True
    return existing.get("input_hash") != input_hash and age >= 2   # new news, but not more often than every 2h


def generate(company, hist, social, price, fund, out_dir, force=False, extra=None, art_store=None):
    """Return the brief dict (cached or fresh). Returns None when no API key is set."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    existing = load_brief(out_dir, company["ticker"])
    if art_store:
        import articles
        _, heads0 = build_input(company, hist, social, price, fund)
        art_block = articles.block_for_brief(art_store, heads0)
        if art_block:
            extra = list(extra or []) + [art_block]
    text, heads = build_input(company, hist, social, price, fund, extra=extra, previous=existing)
    # the hash must not include the previous brief itself, otherwise every brief triggers the next
    base_text, _ = build_input(company, hist, social, price, fund, extra=extra, previous=None)
    input_hash = hashlib.sha1(base_text.encode()).hexdigest()[:12]
    if not needs_refresh(existing, input_hash, force):
        return existing

    import anthropic
    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM.format(language=LANG_NAMES.get(LANG, LANG)),
            messages=[{"role": "user", "content": text}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
    except anthropic.RateLimitError as e:
        return _failed(existing, f"rate limited: {e.message}", out_dir, company)
    except anthropic.APIStatusError as e:
        return _failed(existing, f"API error {e.status_code}: {e.message}", out_dir, company)
    except anthropic.APIConnectionError as e:
        return _failed(existing, f"connection error: {e}", out_dir, company)
    if response.stop_reason == "refusal":
        return _failed(existing, "model declined the request", out_dir, company)
    raw = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _failed(existing, f"unparseable output: {raw[:120]!r}", out_dir, company)
    # resolve headline ids to titles/links so the page can show them
    for ev in data.get("key_events", []):
        ev["headlines"] = [{"title": heads[i]["title"], "link": heads[i]["link"], "source": heads[i]["source"]}
                           for i in ev.get("headline_ids", []) if 0 <= i < len(heads)]
    result = {
        "generated": dt.datetime.now().astimezone().isoformat(), "model": response.model, "lang": LANG,
        "input_hash": input_hash, "headlines_used": len(heads), "error": None,
        "articles_used": text.count("\n\n[") if "ARTICLE TEXT" in text else 0,
        "usage": {"input": response.usage.input_tokens, "output": response.usage.output_tokens},
        "data": data,
    }
    with open(brief_file(out_dir, company["ticker"]), "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    _append_history(out_dir, company["ticker"], result)
    return result


def _append_history(out_dir, ticker, result, keep=30):
    path = os.path.join(out_dir, f"brief_history_{ticker}.json")
    try:
        with open(path) as f:
            hist = json.load(f)
    except Exception:  # noqa: BLE001
        hist = []
    d = result["data"]
    hist.append({"generated": result["generated"], "tone": d["tone"], "tone_score": d["tone_score"], "summary": d["summary"],
                 "themes": d.get("themes", []), "key_events": [e["what"] for e in d.get("key_events", [])], "watch": d.get("watch")})
    with open(path, "w") as f:
        json.dump(hist[-keep:], f, ensure_ascii=False)


def load_history(out_dir, ticker):
    try:
        with open(os.path.join(out_dir, f"brief_history_{ticker}.json")) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


# ------------------------------------------------------------------ weekly digest
DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One sentence naming the week's single most important development across the watchlist"},
        "overview": {"type": "string", "description": "4-6 sentences on the week across all companies: what moved, common themes, what diverged"},
        "by_company": {"type": "array", "items": {"type": "object", "properties": {
            "ticker": {"type": "string"}, "line": {"type": "string", "description": "One sentence for the week"},
            "tone": {"type": "string", "enum": ["positive", "neutral", "negative", "mixed"]}},
            "required": ["ticker", "line", "tone"], "additionalProperties": False}},
        "themes": {"type": "array", "items": {"type": "string"}, "description": "2-5 cross-company themes"},
        "next_week": {"type": "array", "items": {"type": "string"}, "description": "Up to 6 concrete things to watch next week, dated where possible"},
    },
    "required": ["headline", "overview", "by_company", "themes", "next_week"], "additionalProperties": False,
}
DIGEST_SYSTEM = """You write the weekly review for a private watchlist of listed companies. You get, per company, the
daily briefs of the past week (each with a tone), the week's price moves versus sector and index, and the upcoming
calendar. Report what happened; no advice, no predictions. Be concrete and name companies by ticker. Write in {language}."""


def weekly_digest(companies, per_company_text, out_dir, force=False):
    """One cross-company digest per week (regenerated if older than 6 days or forced)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    path = os.path.join(out_dir, "digest.json")
    try:
        with open(path) as f:
            existing = json.load(f)
    except Exception:  # noqa: BLE001
        existing = None
    briefs_now = sum(t.count("- brief ") for t in per_company_text)
    if existing and not force:
        try:
            age = (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(existing["generated"])).total_seconds() / 86400
            # regenerate early once when the previous digest was written before any briefs existed
            if age < 6 and not (existing.get("briefs_seen", 1) == 0 and briefs_now > 0 and age > 0.1):
                return existing
        except Exception:  # noqa: BLE001
            pass
    import anthropic
    client = anthropic.Anthropic()
    now = dt.datetime.now().astimezone()
    text = f"WEEK ENDING {now.strftime('%Y-%m-%d')}\nCOMPANIES: " + ", ".join(f"{c['name']} ({c['ticker']})" for c in companies) + "\n\n" + "\n\n".join(per_company_text)
    try:
        response = client.messages.create(
            model=MODEL, max_tokens=6000, system=DIGEST_SYSTEM.format(language=LANG_NAMES.get(LANG, LANG)),
            messages=[{"role": "user", "content": text}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": DIGEST_SCHEMA}},
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("model declined")
        raw = next((b.text for b in response.content if b.type == "text"), "")
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] weekly digest failed: {type(e).__name__}: {e}", file=sys.stderr)
        if existing:
            existing["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return existing
    result = {"generated": now.isoformat(), "week_ending": now.strftime("%Y-%m-%d"), "model": response.model, "error": None,
              "briefs_seen": briefs_now,
              "usage": {"input": response.usage.input_tokens, "output": response.usage.output_tokens}, "data": data}
    with open(path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    return result


PEERS_SCHEMA = {"type": "object", "properties": {
    "peers": {"type": "array", "items": {"type": "string"}, "description": "3 US-listed ticker symbols of the closest competitors or comparables, most relevant first"},
    "benchmark_etf": {"type": "string", "description": "The single US sector or industry ETF ticker that best tracks this company's group, e.g. SMH, XLI, XLK, XLE, ITA, IGV, TAN"}},
    "required": ["peers", "benchmark_etf"], "additionalProperties": False}


def suggest_peers(company):
    """Ask Claude for 3 peers and a benchmark ETF (used when a company is added). None without a key."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    try:
        r = anthropic.Anthropic().messages.create(
            model=MODEL, max_tokens=1000,
            messages=[{"role": "user", "content": f"Company: {company['name']} ({company.get('exchange','')}: {company['ticker']}). "
                       f"Sector: {company.get('sector') or 'unknown'}, industry: {company.get('industry') or 'unknown'}. "
                       "Give 3 listed peer tickers and the best-fitting US sector/industry ETF."}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": PEERS_SCHEMA}})
        if r.stop_reason == "refusal":
            return None
        return json.loads(next((b.text for b in r.content if b.type == "text"), "{}"))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] peers for {company['ticker']}: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def load_digest(out_dir):
    try:
        with open(os.path.join(out_dir, "digest.json")) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _failed(existing, msg, out_dir, company):
    print(f"[warn] brief for {company['ticker']}: {msg}", file=sys.stderr)
    if existing:
        existing["error"] = msg
        return existing
    result = {"generated": None, "error": msg, "data": None}
    with open(brief_file(out_dir, company["ticker"]), "w") as f:
        json.dump(result, f)
    return result
