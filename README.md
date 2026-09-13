# Daily stock news agent

Every day this program collects the news about each company in `companies.json`
(Bloom Energy, Amazon, Google, SanDisk to start), pulls the share price, renders one
PNG "news photo" per company and rebuilds a Newsroom web page with one tab per company.

## Add or remove a company
Edit `companies.json`. Each entry needs `ticker`, `name`, `exchange` and a Google News
`query`. Example:
```
{"ticker": "NVDA", "name": "Nvidia", "exchange": "NASDAQ", "query": "Nvidia OR \"NASDAQ:NVDA\""}
```
Then run `python3 be_news_agent.py --backfill --only NVDA` once to fill its history.

## What you get
- `output/<TICKER>_news_YYYY-MM-DD.png` – the daily picture per company (price, day/month change, 1‑month
  sparkline, every headline with source, time and a green/red/grey sentiment dot)
- `output/<TICKER>_latest.png` – always the most recent picture
- `output/<TICKER>_news_YYYY-MM-DD.json` – the raw headlines with links
- `output/history_<TICKER>.json` – every headline ever collected (de-duplicated, keeps growing)
- `output/news.html` – the Newsroom web page: 90-day price + news-volume chart, and all
  headlines in chronological order with search, tone and source filters, one tab per
  company. Open it in any browser. It is rebuilt from `page_template.html` on every run.
  Hosted copy (snapshot, republished on request): https://claude.ai/code/artifact/e238151f-6b79-44d6-b122-b1f2236dd26c
- a macOS notification, and the Newsroom page opens in your browser automatically

## Run it by hand
```
python3 be_news_agent.py              # last 24h, opens the Newsroom page
python3 be_news_agent.py --only BE    # one company only
python3 be_news_agent.py --hours 72   # wider window
python3 be_news_agent.py --no-open    # save only
python3 be_news_agent.py --backfill   # also pull everything the feeds still hold (~30 days) into history
```
Quiet days: if fewer than 3 headlines are found in the window it automatically
widens to 72 hours.

## Daily schedule
`install_schedule.sh` installs a macOS launchd job (label `com.alexdrone.be-news-agent`).
```
./install_schedule.sh          # every day at 08:00
./install_schedule.sh 7 30     # every day at 07:30
```
Log: `output/agent.log`. If the Mac is asleep at that time, launchd runs the job the
next time it wakes.

Remove: `launchctl bootout gui/$(id -u)/com.alexdrone.be-news-agent`
and delete `~/Library/LaunchAgents/com.alexdrone.be-news-agent.plist`.

## Web page tabs
- **Summary** – every tracked company at a glance (price, day change, 30-day sparkline,
  headline counts) plus the most important news of the last 3 days across all companies,
  ranked by a heuristic (real events from major outlets up, holdings filings and
  stock-tip listicles down), and the top 3 stories per company.
- **One tab per company** – price + news-volume chart and the full chronological log.
- **+ Add company** – type a ticker or a name. When the page is served by `server.py`
  the company is looked up on Yahoo, added, its news collected and a tab appears. On a
  static copy the panel shows the command to run instead:
  `python3 be_news_agent.py --add NVDA` (or `--remove NVDA`).

## Running it as a website: https://news.beat-trade.com
Live since 12 Sep 2026 on Render (service `stock-newsroom`, Starter instance, Frankfurt blueprint,
1 GB disk at `/data`). Render URL: https://stock-newsroom.onrender.com
`server.py` serves the page, refreshes the news every `REFRESH_HOURS` (default 2) and
provides the add/remove API. Locally:
```
python3 server.py            # http://127.0.0.1:8030/
```
Deploy the same way as the poker project, but Render alone is enough (no Vercel needed,
the page is served by the Python service):
1. Create a GitHub repo (for example `shatalovolek/NEWS`), push this folder to `main`.
   Check the commit author first: `git log -1 --format='%an <%ae>'` should show
   `Alex <shatalov@beat-trade.com>` (set with `git config user.name/user.email`).
2. Render dashboard → **New → Blueprint** → pick the repo. `render.yaml` creates the
   service `stock-newsroom` in Frankfurt on the Starter instance with a 1 GB disk at `/data`
   (`DATA_DIR=/data`), so histories, prices, pictures and the company list survive deploys.
3. Render → the service → **Settings → Custom Domains → Add** `news.beat-trade.com`.
   Render shows the CNAME target (`stock-newsroom.onrender.com` or similar).
4. Bluehost → cPanel → **Domains → Zone Editor** (not "Subdomains") → Add CNAME record:
   Name `news`, Value = the target from step 3.
5. Check: `./scripts_check_domain.sh news.beat-trade.com`.

If the service is ever moved back to the free plan: it sleeps after 15 idle minutes and its
disk is wiped on every deploy, so histories restart from what the feeds hold (~30 days) and
the company list resets to the repo's `companies.json`. Free workaround for the list: let
the server commit it to GitHub.
1. GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token. Repository access: only `News`. Permissions: Contents → Read and write.
2. Render → the service → Environment → Add environment variable: `GITHUB_TOKEN` = the token.
   (`GITHUB_REPO` defaults to `shatalovolek/News`.) Save; Render restarts the service.
From then on every add/remove on the site is committed to `companies.json` with
`[skip render]` in the message, so it does not trigger a deploy, and the next deploy
starts from the updated list. The add panel warns when persistence is off.
With the disk in place the token is optional; it only adds a copy of the list in the repo.

## AI brief (Claude)
With `ANTHROPIC_API_KEY` set on the server, `brief.py` writes a brief per company: a summary of
what happened in the last 3 days, tone, themes, key events linked to their sources, retail mood
from StockTwits, risks and what to watch. It reads the headlines, StockTwits counts and top
messages, price moves and fundamentals shown on the page; it never sees Reddit. Output is a
validated JSON object (structured outputs). Briefs are cached in `output/brief_<TICKER>.json` and
regenerated every `BRIEF_HOURS` (default 12), or after 2 hours when new headlines arrived, or
on demand with `python3 be_news_agent.py --brief`.

Settings (Render → Environment): `ANTHROPIC_API_KEY` (required), `BRIEF_LANG` (`en` default,
`ru` for Russian), `BRIEF_MODEL` (`claude-opus-5` default; `claude-sonnet-5` is about 2.5x cheaper),
`BRIEF_HOURS`. Cost with Opus 5 is roughly 2-3 cents per brief; with 9 companies and two briefs a
day that is around $0.45/day. `/api/health` → `briefs` shows generation times, token usage and
errors per company.

## SEC filings and insiders (EDGAR)
`edgar.py` reads the official EDGAR feed for each company with a CIK: filings of the last 120 days
(8-K with item names, 10-Q/10-K, shelf registrations and prospectuses, 13D/13G stakes, Form 4/144) and
parses Form 4 insider transactions (open-market buys and sales, grants, option exercises, gifts).
The page shows 90-day insider buy/sell totals, the last 8-K, share-offering and large-holder flags,
the filing list and the transaction table; the brief gets the same facts. Foreign ADRs without a CIK
show "no SEC registrant". The SEC requires a User-Agent with a contact address (set in `edgar.py`).

## Versus sector and peers
`relative.py` compares each company with SPY, a sector/industry ETF (chosen from Yahoo's sector and
industry, `relative.SECTOR_ETF` / `INDUSTRY_ETF`) and up to 3 peers over 1 day, 1 week, 1 month and
3 months, with an indexed 3-month chart. `benchmark` and `peers` live in `companies.json` and are
filled automatically when a company is added (Yahoo for the sector; Claude suggests peers when the
API key is set; `DEFAULT_PEERS` in the agent covers the initial list). Edit them by hand any time.

## Calendar, what's new, weekly digest
- **Calendar** (Summary tab): next 60 days across all companies: estimated earnings dates from
  StockAnalysis plus dated events Claude extracts from the headlines (index inclusions, launches,
  votes, lock-ups).
- **New since the previous brief**: every brief receives the previous one and reports what changed.
  Brief history is kept in `output/brief_history_<TICKER>.json` (30 entries).
- **Week in review** (Summary tab): one cross-company digest generated from the week's briefs,
  relative performance and calendar; regenerated when older than 6 days, or with
  `python3 be_news_agent.py --digest`.

## Social section (StockTwits, Google Trends)
Each company tab has a "Social" block: StockTwits messages in the last 24 h and 7 days with the
bullish share (from the tags users set themselves), Reddit posts and upvotes in the big finance
subreddits, a buzz ratio (last 24 h vs the average fully-covered day), a 30-day mentions chart,
Google search interest for 90 days, and the top posts. Data accumulates with every refresh
(`output/social_<TICKER>.json`, 30 days kept).

**StockTwits is blocked for datacenter addresses** (Cloudflare challenge), so the server cannot read
it. The Mac can: `install_schedule.sh` runs the agent every 2 hours (08:05–22:05) with `--push`, which
collects StockTwits at home and uploads it to the site (`POST /api/social/upload`). Setup once:
`push.json` next to the script holds `{"url": "https://news.beat-trade.com", "token": "..."}` and the
same token is set on Render as `UPLOAD_TOKEN`. With `--push` the Mac also adopts the site's company
list, so companies added on the site get StockTwits data too.

Reddit is switched off (decided 13 Sep 2026): Reddit now requires an approved Data API access
request before any app can be created, and a site on a company domain may count as commercial.
The code path stays in `social.py`; if approval is ever obtained, set `REDDIT_CLIENT_ID` and
`REDDIT_CLIENT_SECRET` on Render and the Reddit tile, posts and legend reappear by themselves.
Google Trends uses the unofficial pytrends client; when Google rate-limits the server the
previous series is kept.

## Sources
Google News RSS (query "Bloom Energy" OR "NYSE:BE") and Yahoo Finance headline RSS,
de‑duplicated; price from Yahoo Finance chart API. No API keys needed.
Sentiment dots are a simple keyword heuristic, not investment advice.

Requires Python 3 with `requests` and `Pillow` (already installed on this Mac).
