# SPEC-3 — Cloud Sharadar Breadth + FRED Calendar (v1.1)

**Status:** Approved (Kaan "oturdu" 2026-06-25) — build target
**Supersedes:** the breadth-hybrid (TV-seed + yfinance) portion of Cin handoff `a80e6fb9`. TV-seed approach is CANCELLED.
**Owner:** Usta (architecture) → coder (build)
**Builds on:** SPEC-2 backend (live). Reuses the existing ingest router, anomaly engine, render, graceful-degradation contract.

---

## 1. Decision (ADR-style)

**Context.** v1.1 needs (a) real breadth tiles (the shipped `breadth_200dma` is a dead `vendor:SPXA200R` degraded tile) and (b) an economic calendar (`calendar.provider: off`). The original plan was a TV-seed + yfinance breadth hybrid and FMP calendar. Both were invalidated by evidence:
- FMP calendar is dead for free tier (`/stable` 402, `/api/v3` 403). → FRED release-dates (free, key already set).
- We already license **Sharadar** (Nasdaq Data Link). A GitHub-Actions probe (2026-06-25) confirmed the `NASDAQ_DATA_LINK_API_KEY` secret works **from the cloud**: HTTP 200, **~1s per 10k-row page**, `closeadj` present. The 10–50 min local ingest time is the data-bank's revision-cursor (`lastupdated.gte`) + pagination — NOT inherent NDL latency. A direct `date.gte` query is seconds.

**Decision.** Compute breadth **in the cloud** (GitHub Actions cron) directly from Sharadar SEP via the NDL secret, and publish **only the derived breadth percentages** to the public repo. No Mac role, no local data-bank dependency, no wake mechanism, no TV-seed download. Calendar switches to **FRED release-dates + a static FOMC schedule**.

**Why cloud, not Mac (rejected alternatives, for the record):**
- *Local Mac compute* — rejected: scheduled wake is unreliable on battery + closed-lid (Apple Silicon throttles background wake; `womp 0` on battery); and a cold local pull is a 10–50 min foreground wait. A slow/variable pull is a dealbreaker as a foreground wait but a non-issue as an unattended cron.
- *Mac computes from git-committed raw closes* — rejected: repo is **PUBLIC**; committing raw Sharadar prices violates the redistribution license.

**License constraint (hard).** The repo is public. **Only derived aggregates** (the breadth `%` series) may be committed/published. Raw Sharadar per-security prices MUST NOT be written to the repo. Publishing "% of S&P 500 above 200DMA" is a derived statistic (as StockCharts/Barchart do publicly) — license-clean.

**Consequences.** Brief is viewable from anywhere again (mobile restored). Breadth is backtest-grade (full PIT universe). One new cloud secret (`NASDAQ_DATA_LINK_API_KEY`, already added). NDL becomes a cloud dependency for breadth (graceful-degrade if unreachable — see §7).

---

## 2. Breadth tile set (6 tiles, axis 10)

| key | label | universe | metric |
|-----|-------|----------|--------|
| `breadth_200dma` | % S&P 500 >200DMA | S&P 500 (Wikipedia) | % of members with `closeadj > SMA200` |
| `breadth_50dma` | % S&P 500 >50DMA | S&P 500 | % with `closeadj > SMA50` |
| `breadth_broad_200dma` | % broad-US >200DMA | broad-US (Sharadar) | % with `closeadj > SMA200` |
| `breadth_broad_50dma` | % broad-US >50DMA | broad-US | % with `closeadj > SMA50` |
| `breadth_nhnl_52w` | 52w New Highs − New Lows (net) | broad-US | `(#52w-highs − #52w-lows)` as % of valid universe |
| `rsp_spy` | RSP/SPY (equal vs cap) | — | **UNCHANGED** — existing yfinance ratio tile, do not touch |

**Methodology (all MA tiles):**
- Price = `closeadj` (split/dividend-adjusted) — prevents false MA crossings on corporate actions.
- `SMA_N` = simple moving average over **N trading days** (200, 50). EMA NOT used.
- A member needs ≥N valid closes to be counted; otherwise excluded from the denominator.
- Value = `count(closeadj_today > SMA_N) / count(valid_members) * 100`.
- `breadth_nhnl_52w`: 52w = rolling **252 trading days**; a name is a new high if `closeadj_today == max(closeadj, last 252d)`, new low if `== min`. Net = `(highs − lows) / valid * 100` (signed).

**Config entries** (replace the dead `breadth_200dma`, add the rest; keep `rsp_spy`):
```yaml
- key: breadth_200dma
  axis: 10
  label: "% S&P >200DMA"
  source: sharadar:sp500_above_200dma
  transform: first_diff
  front_screen: true
- key: breadth_50dma
  axis: 10
  label: "% S&P >50DMA"
  source: sharadar:sp500_above_50dma
  transform: first_diff
- key: breadth_broad_200dma
  axis: 10
  label: "% broad-US >200DMA"
  source: sharadar:broad_above_200dma
  transform: first_diff
  front_screen: true
- key: breadth_broad_50dma
  axis: 10
  label: "% broad-US >50DMA"
  source: sharadar:broad_above_50dma
  transform: first_diff
- key: breadth_nhnl_52w
  axis: 10
  label: "52w NH−NL (broad)"
  source: sharadar:nhnl_52w
  transform: level        # already a signed net %, no diff
```
> `transform` choices: %>MA series use `first_diff` (daily change of the % is the z-input, matching the existing dead tile). NH-NL net is already a bounded signed level → `level`. Confirm against anomaly engine expectations during build.

**OUT of scope (do NOT build):** cap-segmented breadth (redundant — broad %>MA is already a de-facto small-cap participation read, ~0.9 corr with small-cap-only; large-vs-broad pair already spans the cap-divergence axis), NDTH (verified = NDX mega-cap, redundant with S&P), Advance/Decline, McClellan. **v1.2 queue:** compact GICS sector-breadth strip (Wikipedia table already carries `GICS Sector` — free input when we get there).

---

## 3. Universes

### 3.1 S&P 500 (for `breadth_*` non-broad)
- **Source:** `https://en.wikipedia.org/wiki/List_of_S%26P_500_companies`, first wikitable, `Symbol` column. **Verified** 2026-06-25: HTTP 200, 0.6s, 503 tickers, only 2 dotted (`BRK.B`, `BF.B`).
- **Fetch:** `requests` (or httpx) with a real `User-Agent` (default UA → 403) → `pandas.read_html`.
- **Normalize:** map Wikipedia dotted class shares to Sharadar's ticker format. Verify exact mapping against the actual SEP ticker set at build (do not assume `.`→`-`). Unmatched tickers are logged + dropped from the denominator.
- **Validate:** accept only if parse yields ~480–520 plausible tickers; else REJECT and fall back to cache (no silent broken-parse).
- **Cache + fallback:** persist resolved list to `data/universe/sp500.csv` (dated). Each run: fetch fresh → on success update cache → on failure use committed cache. Membership changes ~quarterly; a 1-day-stale list is harmless. **Seed the cache at build time** (commit an initial `sp500.csv` from a successful fetch) so the first cloud run never starts cache-empty.

### 3.2 broad-US (for `breadth_broad_*`, `breadth_nhnl_52w`)
Filter the Sharadar universe via the `TICKERS` metadata:
- **IN:** domestic common stock (primary + secondary class); `isdelisted = N`; exchange ∈ {NYSE, NASDAQ, NYSEMKT}; traded in the last few sessions; `closeadj > $1`; ≥N trading days of history.
- **OUT:** ETF/ETN/fund, preferred, warrant, ADR/foreign, OTC/pink, sub-$1 pennies, delisted.
- **No market-cap floor** (Kaan-confirmed) — the small/mid/micro mass is the point (it's what makes broad a small-cap-participation read). All filters are config knobs (`breadth.broad_universe.*`), incl. an optional `min_marketcap` defaulting to null.
- Expected ~3,000–4,000 names after filtering.
- This is the **live** active universe (no delisted) — correct for a state monitor. PIT/backtest membership is a separate concern the monitor does not use.

---

## 4. Sharadar / NDL integration

- **Endpoint:** `https://data.nasdaq.com/api/v3/datatables/SHARADAR/SEP.json?date.gte=<YYYY-MM-DD>&qopts.per_page=10000&api_key=<key>` — paginate via `meta.next_cursor_id`. Columns include `ticker, date, closeadj`. Key from env `NASDAQ_DATA_LINK_API_KEY` (cloud secret).
- **Single pull per run:** ALL breadth tiles share ONE SEP fetch per run (memoize like the FRED dependency cache in `ingest.py`). Do NOT pull SEP once per tile.
- **History strategy (anomaly engine needs ≥3y = `history_depth_years: 3`, `level_pct_window_days: 756`):**
  - The breadth `%` series must carry ≥756 trading days of history for the 1y/3y percentile baselines + sparkline.
  - **Cache the computed `%` series** (derived → public-OK) under `data/breadth/<key>.csv` (date,value).
  - **First run (backfill):** pull `date.gte = today − ~3.3y` (≈ 756 + 200 SMA warmup trading days), compute the full historical `%` series per tile, write the cache. One-time, est. ~6–7 min for broad (~3.5M rows) — fine for an unattended cron.
  - **Subsequent runs (incremental):** pull only a trailing window (`date.gte = today − ~300 calendar days`, enough for the latest SMA200), compute the latest session's value(s), **append** to the cached series. Bounded daily pull (~1M rows broad, ~2 min).
  - Never commit raw closes — only the derived `%` cache.
- **Determinism / dedup:** if a run computes a value for a date already in the cache, overwrite (idempotent re-run safe).

---

## 5. Calendar — FRED release-dates + static FOMC

Switch `calendar.provider: off → fred`. Add a `fred` path in `sources/calendar.py` (FMP/Finnhub paths stay dormant, NOT deleted).
- **Source:** FRED `releases/dates` API (`https://api.stlouisfed.org/fred/releases/dates`, `FRED_API_KEY` already set) → upcoming release dates. Map to a **curated** set of high-impact releases (~10–15): CPI, Employment Situation/NFP, PCE, GDP, Retail Sales, ISM PMI, PPI, Jobless Claims, JOLTS, UMich sentiment. The release→FRED-release-ID map lives in config (`calendar.fred_releases`).
- **Static FOMC schedule:** FRED release-dates do not cleanly give FOMC meeting dates → add a small static `calendar.fomc_dates` list in config (Fed publishes annually).
- **Output:** fill the brief's `calendar_event` / `CalendarEvent` list from this source (currently always empty). Reuse the existing `_TRANSMISSION_RANK` + `high_impact_events` ranking.
- **Window:** "today / this week" upcoming releases relative to the logical brief date.
- **Graceful degradation:** FRED unreachable → `calendar:fred <reason>` degraded reason (per the existing no-silent-swallow contract); `calendar_event` null, run continues. A genuine empty (no releases that day) is NOT degraded.
- **Do NOT use FMP** (dead) and do NOT add any paid vendor.

---

## 6. Routing + workflow wiring

- **`sources/ingest.py`:** add a `sharadar:` kind in `_fetch_tile`. Route the 5 breadth identifiers to a new `sources/breadth.py` (or `sharadar.py`) with a per-run memoized batch computation (one SEP pull → all tiles). Add freshness windows for the new keys to `_FRESHNESS_BY_KEY` (EOD ~5d). Preserve the never-raise contract — any failure → degraded RawSeries, run continues.
- **`sources/breadth.py` (new):** SEP pull + pagination, Wikipedia harness (§3.1), broad-US filter (§3.2), SMA/NH-NL compute (§2), `%`-series cache (§4), returns one `RawSeries` per breadth key (with history).
- **`config.yaml`:** §2 tile entries; `calendar.provider: fred` + `calendar.fred_releases` + `calendar.fomc_dates`; `sources.nasdaq_data_link: {base_url: https://data.nasdaq.com/api/v3, api_key_env: NASDAQ_DATA_LINK_API_KEY}`; `breadth:` config block (universe knobs, MA windows, cache paths).
- **`config.py`:** add a `nasdaq_data_link_api_key()` accessor (mirror `fred_api_key()`).
- **`.github/workflows/morning.yml`:** add `NASDAQ_DATA_LINK_API_KEY: ${{ secrets.NASDAQ_DATA_LINK_API_KEY }}` to the run step env. **Cron tuning:** current `37 3 * * *` (03:37 UTC) is likely too early for the prior US session's Sharadar EOD. The data-bank pulls successfully at 05:00 UTC. **Verify** Sharadar EOD availability time, then set cron in the **05:30–06:30 UTC** window on an **off-round minute** (e.g. `17 6 * * *` = 09:17 İst) to guarantee the freshest session. Keep it an İst-morning brief.
- **`.env.example` / `DEPLOY.md`:** document the new secret.

---

## 7. Graceful degradation (non-negotiable — matches existing contract)
- NDL unreachable / non-200 / key missing → breadth tiles degrade (`ok=False`, honest reason), run continues; the `rsp_spy` tile still covers axis 10. NEVER raise out of ingest.
- Wikipedia fetch fails → use cached `sp500.csv`; if both fail → S&P breadth tiles degrade (broad tiles unaffected, they need no list).
- Cache exists but NDL gives no new session → serve last cached value, flag staleness per the freshness window.
- Publish-license guard: assert no raw per-security price array is ever written to `data/` (only `%` aggregates + the constituent list).

---

## 8. Tests
- Universe filter (broad-US): ETF/ADR/preferred/penny/delisted excluded; common stock kept.
- Wikipedia parse: 503-row fixture → ~500 tickers, BRK.B/BF.B normalized; broken-HTML fixture → rejected → cache fallback.
- SMA/NH-NL math: known small fixture → exact `%` and NH-NL counts; <N-history names excluded from denominator.
- closeadj used (not raw close) — split fixture proves no false crossing.
- Pagination: multi-page cursor fixture assembled correctly; single SEP pull shared across all 5 tiles.
- Cache: backfill writes ≥756 points; incremental appends one day; idempotent re-run overwrites same date.
- Calendar-FRED: releases mapped + ranked; FOMC static injected; FRED-down → degraded reason, not silent empty.
- Graceful degradation: NDL 403 / Wikipedia 500 → degraded tiles, pipeline still produces a valid brief JSON (schema-valid).
- License guard test: no raw price arrays in emitted `data/*.json` or `data/breadth/*`.
- Full suite green (existing 72 + new); `python -m morning_monitor.main` produces a schema-valid brief with the 5 breadth values populated.

## 9. Acceptance criteria
- [ ] 5 breadth tiles populate with real values; `rsp_spy` untouched.
- [ ] Both S&P + broad universes resolve; broad ~3–4k after filter; S&P from Wikipedia w/ cache+fallback (seeded).
- [ ] One SEP pull per run; `%`-series cache backfills ≥756d then increments.
- [ ] Only derived `%` (and the constituent list) committed — zero raw Sharadar prices in the repo.
- [ ] Calendar-FRED populates `calendar_event`; FOMC dates present; degrades honestly.
- [ ] Cron verified against Sharadar EOD availability; secret wired; live cloud run produces a valid brief and self-verifies.
- [ ] All tests green; graceful degradation paths covered.

---

## 10. VERIFIED FACTS — overrides/refines §3–§6 (fact-verify workflow, 2026-06-25, all high-confidence, live-sourced)

**Calendar / FRED (§5):**
- Release-ID map (verified live against api.stlouisfed.org):
  ```yaml
  fred_releases:
    Consumer Price Index: 10                              # CPI
    Employment Situation: 50                              # NFP
    Personal Income and Outlays: 54                       # PCE price index lives here
    Gross Domestic Product: 53                            # GDP
    Advance Monthly Sales for Retail and Food Services: 9 # Advance Retail Sales (NOT 436/494)
    Producer Price Index: 46                              # PPI
    Unemployment Insurance Weekly Claims Report: 180      # initial jobless claims (weekly)
    Job Openings and Labor Turnover Survey: 192           # JOLTS
    Surveys of Consumers: 91                              # UMich sentiment (lists prelim+final)
  ```
- **ISM Manufacturing PMI is NOT in FRED (proprietary, 0 series) → DROP it from the curated list. Do NOT fabricate an ID.** ISM stays TradingView/SPEC-1's to show.
- Upcoming dates (per release): `GET https://api.stlouisfed.org/fred/release/dates?release_id=N&include_release_dates_with_no_data=true&realtime_start=<TODAY>&realtime_end=9999-12-31&sort_order=asc&limit=10&file_type=json&api_key=$FRED_API_KEY`. **`include_release_dates_with_no_data=true` is MANDATORY for FUTURE dates** (else only past). `limit` ≤ 1000. Item shape: `{release_id, release_name, date}`. PCE label derives from release 54's day.

**S&P ticker normalization (§3.1): PASS-THROUGH — keep the dot.** Verified in SEP: `BRK.B→BRK.B`, `BF.B→BF.B`, plain symbols unchanged. **No `.`→`-` transform.** (Defensive only: if a source ever yields dash-class `BRK-B`, convert dash→dot before lookup; Sharadar's dash form `TICKER-PA` is preferred shares, unrelated.)

**NDL pull (§4):** account is **PREMIUM** (1M-unit budget, 1 unit/page → backfill+daily negligible). Full 10k-row page ≈ **2.28s**; single latest session ≈ 1.45s (1 page, ~6246 rows, no cursor). **Backfill ~3.5–5.9M rows = 350–594 pages ≈ 13–23 min** (one-time). **Daily ~300-cal-day trailing ≈ 130 pages ≈ ~5 min.** Raw SEP universe ≈ **6246 names/day** incl. delisted (the §3.2 broad filter reduces this). Build sequential (no concurrency). `closeadj` is correct for a LIVE state read (no look-ahead); the data-bank's backtest convention (closeunadj + query-time split factor) is NOT required here — keep `closeadj`.

**Cron (§6): use `cron: '7 5 * * *'` (05:07 UTC = 08:07 İst).** +9h after the 20:00 UTC NYSE close; inside the validated 05:00 UTC availability floor (4/4 scheduled local pulls landed the prior session); finishes before the 06:00 UTC (09:00 İst) brief deadline; off-round minute. **Replaces the current `37 3 * * *` (03:37 UTC = too early, only +7.5h, unvalidated) — NOT the 05:30–06:30 guess.** DST caveat: in EST winter the NYSE close shifts to 21:00 UTC → re-confirm 05:07 still clears 06:00 UTC. Cloud self-fetches from Sharadar (not the local lake).

**FOMC static schedule (§5):** 2026 statement (day-2) dates — `2026-01-28, 2026-03-18*, 2026-04-29, 2026-06-17*, 2026-07-29, 2026-09-16*, 2026-10-28, 2026-12-09*` (`*` = SEP/quarterly meeting; all 8 have a press conference). 2027 tentative dates available if a forward buffer is wanted.
