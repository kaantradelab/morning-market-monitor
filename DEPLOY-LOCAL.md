# Deploy — Morning Market Monitor (Mac-local)

The brief runs **entirely on Kaan's Mac**. One layer, no cloud: no GitHub Actions
pull, no GitHub Pages, no server. The trading data bank supplies prices; the
monitor computes the brief, renders a static site to `./site/`, and opens it in
the browser.

```
trading data bank          morning-market-monitor                  browser
(~/data/tradingbank)  ──▶  python -m morning_monitor.main  ──▶  site/index.html
  08:00 launchd ingest      08:15 launchd  OR  double-click       (open ./site/)
  (SEP EOD prices)          scripts/compute.command               site/archive/<date>.html
                            └─ breadth: data-bank-first,           site/archive/index.html
                               NDL gap-fill only for missing tail
```

---

## The two triggers

### 1. "Calculate" — double-click (on demand)

Open Finder → `morning-market-monitor/scripts/` → double-click **`compute.command`**.

It runs the pipeline against the local data bank + FRED, renders `./site/`, and
opens `site/index.html`. Logs go to `logs/compute_<YYYYMMDD>.log`.

> First time only: macOS Gatekeeper may block a downloaded/`.command` file. If so,
> right-click → **Open** once (or `xattr -d com.apple.quarantine scripts/compute.command`).

### 2. launchd — automatic, every morning at 08:15 local

`deploy/com.morningmonitor.daily.plist` runs the same `compute.command` at **08:15
local**, just after the data bank's 08:00 ingest, so breadth computes from the
bank with no NDL gap-fill. Install once:

```bash
mkdir -p logs   # StandardOutPath parent must exist (logs/ is gitignored)
cp deploy/com.morningmonitor.daily.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.morningmonitor.daily.plist

# verify / test / uninstall
launchctl print    gui/$(id -u)/com.morningmonitor.daily
launchctl kickstart -k gui/$(id -u)/com.morningmonitor.daily   # run now
launchctl bootout  gui/$(id -u)/com.morningmonitor.daily       # remove
```

Edit the time in the plist's `StartCalendarInterval` block. Logs:
`logs/launchd.out.log` / `logs/launchd.err.log`.

---

## Browsing past days

The render is a fully static site under `site/` — open it directly, no server:

- `site/index.html` — today's full brief (all tiles incl. breadth).
- `site/archive/index.html` — browseable list of every past day.
- `site/archive/<YYYY-MM-DD>.html` — a specific past day.

Per-day briefs persist as `data/<YYYY-MM-DD>.json`; the archive index is rebuilt
from them on every run, so history accumulates automatically.

---

## Setup (one-time)

```bash
# 1. Python env (3.13)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. (optional) API keys — export from ~/.zshenv so launchd inherits them
#    FRED_API_KEY               rates/credit/plumbing tiles + FRED calendar
#    NASDAQ_DATA_LINK_API_KEY   breadth gap-fill ONLY (latest missing day)
#    Both are OPTIONAL: the run degrades gracefully (stale/missing tiles), and
#    breadth reads the local data bank first — NDL is only a latest-day gap-fill.

# 3. Smoke-test offline (no keys, bundled fixture)
.venv/bin/python -m morning_monitor.main --config config.yaml \
  --fixture tests/fixtures/sample_run.json --date 2026-06-24
open site/index.html
```

The trading data bank (`~/data/tradingbank`) is the breadth price source. It is
maintained separately (its own `com.tradingbank.daily-ingest` launchd job). This
monitor only **reads** it — it never writes into the data bank.

---

## Why local (not cloud)

- **License-clean by construction.** Raw per-security prices stay on the Mac in
  the local data bank; only derived `%` aggregates (`data/breadth/*.csv`) are ever
  written to a committed/published file.
- **No full-universe NDL pull.** The cloud path re-fetched the entire SEP universe
  each run (24M rows, minutes, GBs). Local reads the data bank that already holds
  that history; NDL is touched only to fill the latest 1–3 missing days, ticker-filtered.
- **No moving parts.** No Actions runner, no Pages publish race, no secrets in CI.

The old cloud workflow (`.github/workflows/morning.yml`) is kept **dormant** for
reversibility — see the banner at the top of that file and `DEPLOY.md` (archived).
