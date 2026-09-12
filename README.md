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

## Running it as a website (news.beat-trade.com)
`server.py` serves the page, refreshes the news every `REFRESH_HOURS` (default 3) and
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
   service `stock-newsroom` in Frankfurt on the free plan.
3. Render → the service → **Settings → Custom Domains → Add** `news.beat-trade.com`.
   Render shows the CNAME target (`stock-newsroom.onrender.com` or similar).
4. Bluehost → cPanel → **Domains → Zone Editor** (not "Subdomains") → Add CNAME record:
   Name `news`, Value = the target from step 3.
5. Check: `./scripts_check_domain.sh news.beat-trade.com`.

Free-plan notes: the service sleeps after 15 minutes without visitors (first visit then
takes ~1 minute to wake, after which it re-collects the news), and its disk is wiped on
every deploy, so histories restart from what the feeds still hold (~30 days) and
companies added from the page are lost. To keep them, attach a Render Disk
(Settings → Disks, mount path `/data`, 1 GB) and set `DATA_DIR=/data` in `render.yaml`.

## Sources
Google News RSS (query "Bloom Energy" OR "NYSE:BE") and Yahoo Finance headline RSS,
de‑duplicated; price from Yahoo Finance chart API. No API keys needed.
Sentiment dots are a simple keyword heuristic, not investment advice.

Requires Python 3 with `requests` and `Pillow` (already installed on this Mac).
