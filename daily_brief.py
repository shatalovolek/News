#!/usr/bin/env python3
"""
Daily AI brief written by Claude Code (headless), one brief per tracked company.

No API key: this runs the Claude Code CLI on the Mac under the user's subscription
(`claude -p`), the same way a chat session would. Steps:
  1. build a compact dossier per company from what the agent already collected in output/
     (headlines of the last 3 days with ids, article excerpts, price, fundamentals,
     relative performance, analysts, short interest, EDGAR, StockTwits);
  2. ask Claude for one structured brief per company (JSON schema enforced);
  3. write output/brief_<TICKER>.json (headline ids resolved to titles/links);
  4. upload the briefs to the website with the agent's --push-briefs path.

Usage:  python3 daily_brief.py [--only BE,AMZN] [--no-push] [--dossier-only] [--model NAME]
Env:    CLAUDE_BIN    path to the claude binary (default: `claude` on PATH, else the newest
                      VS Code extension bundle under ~/.vscode/extensions)
        BRIEF_MODEL   model alias/id for the CLI (default: the CLI's default)
        BRIEF_DAYS    headline window in days (default 3)
"""
import argparse
import datetime as dt
import glob
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import be_news_agent as agent  # noqa: E402
import articles, edgar, finnhub, shorts, social  # noqa: E402

DAYS = int(os.environ.get("BRIEF_DAYS", 3))

SCHEMA = {
    "type": "object",
    "properties": {
        "briefs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "summary": {"type": "string", "description": "3-5 sentences: what happened in the period, the price reaction, and why. Concrete figures, no hype."},
                    "tone": {"type": "string", "enum": ["positive", "negative", "mixed"]},
                    "themes": {"type": "array", "items": {"type": "string"}, "description": "2-4 short labels"},
                    "key_events": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "what": {"type": "string"},
                                "why_it_matters": {"type": "string"},
                                "headline_ids": {"type": "array", "items": {"type": "integer"}, "description": "ids from the HEADLINES list of this company"},
                            },
                            "required": ["what", "why_it_matters", "headline_ids"],
                        },
                        "description": "0-4 material events; empty when there was no real news",
                    },
                    "risks": {"type": "array", "items": {"type": "string"}, "description": "0-3 risks or open questions"},
                    "watch": {"type": "string", "description": "one sentence: what to watch next"},
                },
                "required": ["ticker", "summary", "tone", "themes", "key_events", "risks", "watch"],
            },
        }
    },
    "required": ["briefs"],
}

INSTRUCTIONS = """You are writing the daily brief for a private stock-news page. Below is a dossier per company:
price moves, fundamentals, performance versus sector and peers, analyst counts, short interest,
SEC filings and insider trades, StockTwits counts with top posts, the headlines of the last {days} days
(newest first, with an [id] and a heuristic importance score) and excerpts of the most important
articles.

Write one brief per company, in English, for an experienced investor who reads this page daily:
- Report, do not advise. No "buy"/"sell", no price predictions of your own.
- Be concrete: dates, dollar amounts, percentages from the dossier. Do not invent facts that are
  not in the dossier; if there was no real news, say so and keep key_events empty.
- Explain the price move when the dossier supports an explanation; otherwise say there was no
  company-specific trigger.
- Treat law-firm class-action releases, listicles and stock-tip pieces as noise unless something
  new happened.
- tone: positive / negative / mixed for the period as a whole.
- key_events: 0-4 material items, each with the ids of the headlines that support it.
- risks: 0-3 items. watch: one sentence.

Today is {today}. Return only the JSON described by the schema, one entry per company, in the
dossier's order.
"""


def _j(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def dossier_for(c):
    """Text block for one company plus the ordered headline list ([id] = index)."""
    t = c["ticker"]
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)).isoformat()
    lines = [f"===== {c['name']} ({c['exchange']}:{t}) ====="]
    pr = agent.load_cached_price(t) or {}
    if pr.get("price"):
        lines.append(f"PRICE ${pr['price']:.2f}, day {pr.get('day_change_pct', 0):+.1f}%, month {pr.get('month_change_pct', 0):+.1f}%, "
                     f"YTD {(pr.get('ytd_change_pct') or 0):+.1f}% (last close in the data)")
    fund = (_j(agent.fundamentals_file(t)) or {}).get("values") or {}
    keep = [(k, fund[k]) for k in ("market_cap", "pe", "fwd_pe", "ps", "rev_growth", "net_margin", "earnings", "target", "short_float") if fund.get(k)]
    if keep:
        lines.append("FUNDAMENTALS: " + ", ".join(f"{k}={v}" for k, v in keep))
    rel = agent.load_relative(t)
    if rel and rel.get("rows"):
        lines.append("RELATIVE: " + "; ".join(f"{r['ticker']} ({r['role']}) " + " ".join(f"{w}:{x:+.1f}%" for w, x in r["returns"].items() if x is not None)
                                             for r in rel["rows"]))
    an = finnhub.summarize(finnhub.load(agent.OUT_DIR, t)) or {}
    if an.get("counts"):
        lines.append(f"ANALYSTS (Finnhub {an.get('period')}): {an.get('consensus')} {an['counts']}; next earnings {an.get('next_earnings')}; last {an.get('last_earnings')}")
    sm = shorts.summarize(shorts.load(agent.OUT_DIR, t)) or {}
    sm = {k: sm.get(k) for k in ("short_float", "short_ratio", "change_pct", "change_since") if sm.get(k) is not None}
    if sm:
        lines.append(f"SHORT INTEREST: {sm}")
    es = edgar.summarize(edgar._load(agent.OUT_DIR, t)) or {}
    if es.get("cik"):
        lines.append(f"EDGAR last {es['days']} days: insider open-market buys {es['buys']['n']} (${es['buys']['value']:,.0f}), "
                     f"sales {es['sells']['n']} (${es['sells']['value']:,.0f})")
        for x in es.get("filings", [])[:5]:
            items = ", ".join(i["name"] for i in x.get("items", []) if i.get("name"))
            lines.append(f"  filing {x['date']} {x['form']} {x.get('label', '')}" + (f": {items}" if items else ""))
        for x in es.get("insider", [])[:4]:
            lines.append(f"  insider {x['date']} {x['owner']} ({x.get('title') or 'insider'}): {x['kind']} {x['shares']:,.0f} sh @ ${x['price']:.2f}")
    so = social.summarize(social._load(social.social_file(agent.OUT_DIR, t))) or {}
    if so.get("last7", {}).get("st"):
        l24, l7 = so["last24"], so["last7"]
        lines.append(f"STOCKTWITS: {l24['st']} messages in 24h ({l24.get('bull')} bullish / {l24.get('bear')} bearish tagged), {l7['st']} in 7 days, buzz vs average day {so.get('buzz')}")
        for p in so.get("top_stocktwits", [])[:4]:
            lines.append(f"  - ({p.get('sentiment') or 'untagged'}) {' '.join(p['body'].split())[:150]}")
    else:
        lines.append("STOCKTWITS: no data")
    hist = [h for h in agent.load_history(t) if h["time"] >= cutoff and agent.relevant(c, h["title"])]
    hist.sort(key=lambda h: (-h.get("importance", 0), h["time"]))
    heads = hist[:40]
    lines.append(f"HEADLINES last {DAYS} days ({len(hist)} total, top {len(heads)} by importance):")
    for i, h in enumerate(heads):
        lines.append(f"  [{i}] {h['time'][5:16]} | imp {h.get('importance', 0)} | {h['source']} | {h['title']}")
    if not heads:
        lines.append("  (none)")
    store = articles.load(agent.OUT_DIR, t)
    n = 0
    for i, h in enumerate(heads):
        r = store.get(h["link"])
        if r and r.get("ok") and r.get("text"):
            body = " ".join(r["text"].split())
            if len(body) < 300 or body.lower().startswith(("oops", "as you were browsing", "please stand by")):
                continue  # scraped a bot wall, not the article
            lines.append(f"  ARTICLE for [{i}] ({h['source']}): {body[:1500]}")
            n += 1
        if n >= 3:
            break
    return "\n".join(lines), heads, len(hist)


def claude_bin():
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    if shutil.which("claude"):
        return shutil.which("claude")
    bundles = sorted(glob.glob(os.path.expanduser("~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")),
                     key=lambda p: [int(x) if x.isdigit() else x for x in p.split("claude-code-")[1].split("-")[0].split(".")])
    if bundles:
        return bundles[-1]
    sys.exit("claude binary not found: install Claude Code or set CLAUDE_BIN")


def ask_claude(prompt, model=None):
    cmd = [claude_bin(), "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
           "--tools", "", "--no-session-persistence"]
    if model:
        cmd += ["--model", model]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=1500, cwd=HERE)
    if r.returncode != 0:
        raise RuntimeError(f"claude exited {r.returncode}: {r.stderr[-800:]}")
    out = json.loads(r.stdout)
    data = out.get("structured_output")
    if data is None:  # older CLI: the JSON is the text result
        data = json.loads(out.get("result", "{}"))
    return data, out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="tickers, comma-separated")
    ap.add_argument("--no-push", action="store_true", help="write the files, do not upload")
    ap.add_argument("--dossier-only", action="store_true", help="print the dossier and exit")
    ap.add_argument("--model", default=os.environ.get("BRIEF_MODEL"))
    args = ap.parse_args()

    companies = agent.load_companies()
    if args.only:
        wanted = {t.strip().upper() for t in args.only.split(",")}
        companies = [c for c in companies if c["ticker"] in wanted]
    blocks, heads, counts = [], {}, {}
    for c in companies:
        text, hs, n = dossier_for(c)
        blocks.append(text)
        heads[c["ticker"]] = hs
        counts[c["ticker"]] = n
    dossier = "\n\n".join(blocks)
    if args.dossier_only:
        print(dossier)
        return
    prompt = INSTRUCTIONS.format(days=DAYS, today=dt.date.today().isoformat()) + "\n" + dossier
    print(f"{dt.datetime.now():%H:%M:%S} asking Claude for {len(companies)} briefs ({len(prompt):,} chars of input)…", flush=True)
    data, meta = ask_claude(prompt, args.model)
    now = dt.datetime.now().astimezone().isoformat()
    model_used = ", ".join((meta.get("modelUsage") or {}).keys()) or args.model or "claude-code-default"
    written = []
    for b in data.get("briefs", []):
        t = (b.get("ticker") or "").upper()
        if t not in heads:
            print(f"[warn] brief for unknown ticker {t!r} ignored", file=sys.stderr)
            continue
        hs = heads[t]
        events = []
        for e in b.get("key_events", []):
            events.append({"what": e["what"], "why_it_matters": e.get("why_it_matters", ""),
                           "headlines": [{"title": hs[i]["title"], "link": hs[i]["link"], "source": hs[i]["source"]}
                                         for i in e.get("headline_ids", []) if isinstance(i, int) and 0 <= i < len(hs)]})
        result = {"generated": now, "source": "by Claude in Claude Code (daily)", "model": model_used,
                  "headlines_used": counts[t], "error": None,
                  "usage": {"input": (meta.get("usage") or {}).get("input_tokens"), "output": (meta.get("usage") or {}).get("output_tokens")},
                  "data": {"summary": b["summary"], "tone": b["tone"], "themes": b.get("themes", []), "key_events": events,
                           "risks": b.get("risks", []), "watch": b.get("watch", "")}}
        with open(agent.brief_file(t), "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        written.append(t)
    missing = [c["ticker"] for c in companies if c["ticker"] not in written]
    print(f"{dt.datetime.now():%H:%M:%S} briefs written: {', '.join(written) or 'none'}" + (f"; missing: {', '.join(missing)}" if missing else ""))
    if written and not args.no_push:
        push = agent.push_config()
        if not push:
            print("[warn] push.json missing, briefs not uploaded", file=sys.stderr)
        else:
            sent = agent.push_briefs(push, [c for c in companies if c["ticker"] in written])
            print(f"{dt.datetime.now():%H:%M:%S} uploaded: {', '.join(sent)}")


if __name__ == "__main__":
    main()
