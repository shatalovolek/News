#!/usr/bin/env python3
"""
Web server for the Daily Stock Newsroom (news.beat-trade.com).

Serves the Newsroom page, refreshes the news itself on a timer, and exposes a small
API so companies can be added or removed from the page.

    python3 server.py                      # http://127.0.0.1:8030/
    python3 server.py --host 0.0.0.0 --port 10000   # what Render runs

Environment:
    PORT           port to listen on (Render sets it)
    DATA_DIR       where histories, pictures and companies.json live (mount a Render disk here)
    REFRESH_HOURS  how often to re-collect news while running (default 2)
    GITHUB_TOKEN   fine-grained GitHub token (Contents: read/write on the repo). With it, companies
                   added or removed on the site are committed to companies.json in the repo, so they
                   survive Render's disk wipe on every deploy. GITHUB_REPO defaults to shatalovolek/News.

Routes:
    GET  /                      the Newsroom page
    GET  /api/health            {"ok": true, ...}
    GET  /api/companies         tracked companies
    POST /api/companies         {"query": "NVDA"}  -> adds it, collects its news, rebuilds the page
    DELETE /api/companies/NVDA  stops tracking it
    POST /api/refresh           re-collect everything now
    GET  /pictures/BE_latest.png  the daily picture per company
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import be_news_agent as agent

LOCK = threading.Lock()          # one collection run at a time
STATE = {"last_run": None, "last_error": None, "running": False}


def refresh(only=None, backfill=False):
    with LOCK:
        STATE["running"] = True
        try:
            agent.run_all(hours=24, backfill=backfill, only=only, verbose=False, pictures=True)
            STATE["last_run"] = time.time()
            STATE["last_error"] = None
        except Exception as e:  # noqa: BLE001
            STATE["last_error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            STATE["running"] = False


def scheduler(hours):
    refresh()                                   # first collection at start-up
    while True:
        time.sleep(hours * 3600)
        refresh()


def _social_errors():
    """Per-company social source errors from the last run (helps debug blocked sources)."""
    import social
    out = {}
    for c in agent.load_companies():
        errs = social._load(social.social_file(agent.OUT_DIR, c["ticker"])).get("errors") or {}
        if errs:
            out[c["ticker"]] = errs
    return out


def _mounted(path):
    """True when `path` is its own mount point (a Render disk), False for a plain folder."""
    try:
        return os.path.ismount(path)
    except Exception:  # noqa: BLE001
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "newsroom/1.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def log_message(self, fmt, *args):  # quieter logs
        if "/api/health" not in (args[0] if args else ""):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            if not os.path.exists(agent.PAGE):
                return self._send(503, b"<p>Collecting news for the first time, reload in a minute.</p>",
                                  "text/html; charset=utf-8", {"Retry-After": "60"})
            with open(agent.PAGE, "rb") as f:
                body = f.read()
            # the template is head-less (the hosted artifact adds its own skeleton); browsers need
            # a real document with the viewport meta, otherwise phones render the desktop layout
            doc = (b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                   b'<meta name="viewport" content="width=device-width, initial-scale=1">'
                   b'<link rel="icon" href="data:,"></head><body>' + body + b'</body></html>')
            return self._send(200, doc, "text/html; charset=utf-8")
        if path == "/api/health":
            return self._send(200, {"ok": True, "last_run": STATE["last_run"], "running": STATE["running"],
                                    "last_error": STATE["last_error"], "companies": len(agent.load_companies()),
                                    "persistent": bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("DATA_DIR")),
                                    "data_dir": agent.OUT_DIR, "disk_mounted": _mounted(agent.OUT_DIR),
                                    "social_errors": _social_errors()})
        if path == "/api/companies":
            return self._send(200, agent.load_companies())
        if path.startswith("/pictures/"):
            name = os.path.basename(path)
            fp = os.path.join(agent.OUT_DIR, name)
            if name.endswith((".png", ".json")) and os.path.exists(fp):
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "image/png" if name.endswith(".png") else "application/json")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/companies":
            q = (self._json_body().get("query") or "").strip()
            if not q:
                return self._send(400, {"error": "Give a ticker or a company name."})
            try:
                c, created = agent.add_company(q)
            except ValueError as e:
                return self._send(404, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._send(502, {"error": f"Lookup failed: {e}"})
            if created:
                threading.Thread(target=refresh, kwargs={"only": c["ticker"]}, daemon=True).start()
            return self._send(201 if created else 200, {"company": c, "created": created,
                              "message": (f"Added {c['name']} ({c['ticker']}). Collecting its news now, reload in ~30 seconds."
                                          if created else f"{c['name']} ({c['ticker']}) is already tracked.")})
        if path == "/api/refresh":
            if STATE["running"]:
                return self._send(202, {"message": "Already collecting."})
            threading.Thread(target=refresh, daemon=True).start()
            return self._send(202, {"message": "Collecting news for every company, reload in a minute."})
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/companies/"):
            t = path.rsplit("/", 1)[1].upper()
            if not agent.remove_company(t):
                return self._send(404, {"error": f"{t} is not tracked."})
            threading.Thread(target=refresh, kwargs={"only": ""}, daemon=True).start()  # rebuild page only
            return self._send(200, {"message": f"Stopped tracking {t}."})
        self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8030)))
    ap.add_argument("--refresh-hours", type=float, default=float(os.environ.get("REFRESH_HOURS", 2)))
    args = ap.parse_args()
    os.makedirs(agent.OUT_DIR, exist_ok=True)
    threading.Thread(target=scheduler, args=(args.refresh_hours,), daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Newsroom on http://{args.host}:{args.port}/  data in {agent.OUT_DIR}  refresh every {args.refresh_hours}h")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
