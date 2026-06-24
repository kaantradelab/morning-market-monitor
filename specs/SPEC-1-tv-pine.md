# SPEC-1 — TradingView / Pine side

**Build owner:** Pine (TradingView v6 authoring) · **Type:** BUILD/authoring request — NOT an edge measurement.
No hypothesis-card, no `measured`/verdict involved. This is a monitoring tool; it describes market *state*.
**Reads:** the vetted reference `cin/reports/2026-06-24-morning-monitor-design-reference.md` (§1 tiles, §2 stats, §4 blind-spots).
**Tier:** Kaan is on TV **Premium** (already paid) — 16-chart layout, 40 `request.security()`, 800 never-expire alerts, webhooks.

## Goal

Two TV deliverables: (A) a saved **16-panel multi-layout** for 60-second orientation, and (B) a single **Pine
anomaly-overlay indicator** that renders a cross-asset z-score/percentile table AND fires standing
never-expire event-alerts via webhook. The scheduled morning brief is NOT here (backend owns the 09:00 clock —
TV alerts fire at bar-close = İstanbul night).

---

## A. The 16-panel multi-layout (no code — a saved layout)

One TV layout, 16 panels, daily charts, the front-screen orientation set. Suggested panels (verify exact TV
symbols when building — symbol-prefix zoo is real):

| Panel | Symbol (verify) | Axis |
|---|---|---|
| S&P 500 fut | `CME_MINI:ES1!` | risk |
| Nasdaq fut | `CME_MINI:NQ1!` | risk |
| VIX + VIX/VIX3M | `CBOE:VIX`, `CBOE:VIX3M` | equity vol (level+shape) |
| MOVE (delayed) | `TVC:MOVE` (verify) / proxy | rates vol |
| UST 2Y / 10Y | `TVC:US02Y`, `TVC:US10Y` | rates |
| 10Y real / breakeven | `FRED:DFII10`, `FRED:T10YIE` | rates decomposed |
| 2s10s | `FRED:T10Y2Y` | curve |
| HY OAS / IG OAS | `FRED:BAMLH0A0HYM2`, `FRED:BAMLC0A0CM` | credit |
| Broad USD / DXY | `FRED:DTWEXBGS`, `TVC:DXY` | dollar (Broad primary) |
| USDJPY / USDCNH | `FX:USDJPY`, `FX:USDCNH` | FX tells |
| Brent / Copper-Gold | `TVC:UKOIL` (verify), `COMEX:HG1!`/`COMEX:GC1!` | commodities |
| OFR FSI / NFCI | `FRED:NFCI` (+ OFR FSI if symbol exists; else backend-only) | composite anchor |
| SOFR−IORB / SRF | `FRED:SOFR`,`FRED:IORB` (compute spread) | funding plumbing ⭐ |
| Net liquidity | `FRED:WALCL`-`FRED:WTREGEN`-`FRED:RRPONTSYD` (Pine math) | plumbing (label "construct") |
| BTC + stablecoin | `BINANCE:BTCUSDT` (+ stablecoin cap = backend) | crypto |
| Calendar / session strip | TV econ-calendar widget | feed |

Notes: **CME futures delayed = free on TV** (no add-on). `FRED:` prefix works in TV directly. Breadth
(% >200DMA, RSP/SPY) — `RSP/SPY` ratio chart works; `$SPXA200R` is StockCharts, **verify TV equivalent or
push to backend.** OFR FSI may have no TV symbol → if so it lives in the backend brief only.

---

## B. The Pine anomaly-overlay indicator (the custom build)

A single v6 indicator rendering a `table` of the cross-asset tiles with anomaly coloring + standing alerts.

### B.1 Per-tile columns
`symbol · value · overnight %chg · EWMA-z · level-percentile · color`

### B.2 Anomaly math (from reference §2 — implement in Pine)
- **EWMA conditional vol**, λ=0.94 daily: `σ²ₜ = λ·σ²ₜ₋₁ + (1−λ)·xₜ₋₁²`, where `x` = log-return (prices) or
  first-difference (yields/spreads — **never raw z on a level**). Standardized move `z = x/σ̂`.
- **Level percentile** over trailing ~756d (3y) for context (a high-but-stable regime ≠ a fresh shock — show both).
- **Robust z** for spiky series: `0.6745·(x−median)/MAD`, flag `|z|>3.5`.
- **Color rule:** amber `|z|≥2`, red `|z|≥3` (the 3σ headline ≈ the simple multiple-comparisons control —
  see B.4). Term-structure flags are binary (no z): `2s10s<0`; `VIX/VIX3M>1.0` backwardation.

### B.3 The 40-call budget (Premium cap = 40; Pine's earlier consult)
Tuple-pack `[close, open, ...]` per symbol = 1 call each. Budget ~28 of 40 (≈25 tiles + VIX3M + a couple of
companions), leave ~12 reserve. One TF (daily). Draw up the exact allocation with Kaan before coding; if it
ever needs >40 (40+ tiles or dual-TF) → split into two indicators or note Ultimate (64).

### B.4 What Pine does NOT do (division of labor — important)
- **No full cross-tile FDR / Benjamini-Yekutieli** — ranking p-values across 30 tiles is impractical in Pine.
  Pine does **per-tile coloring + the 3σ headline + ≥2-tile corroboration where simple**. The real FDR +
  detect-on-composites lives in the backend (SPEC-2). State this boundary so we don't duplicate/contradict logic.
- **No scheduled morning brief** — TV can't fire "at 09:00 İstanbul" (bar-close timing). Backend owns it.

### B.5 Standing event-alerts (Premium never-expire — wire 5-10, set-and-forget)
Each = an `alert()` with a **JSON payload** (`series string`, built at runtime) → webhook (port 443, <3s
response, ≤~4KB payload — keep it lean: tickers + z only). Conditions (from reference §2/§4):
1. VIX/VIX3M > 1.0 (backwardation onset)
2. Credit divergence: SPX 5d up AND HY OAS 5d-widening > ~95th pct (the canonical "calm equity, widening credit")
3. 2s10s crosses 0 (either direction)
4. Broad-USD or USDCNH daily move z > 2.5
5. SOFR−IORB > 25bp OR EFFR drifting to top of range (if series available on TV; else backend)
6. Any single tile z > 3.0 (single-name outlier)
7. (optional) Net-liquidity 5d drop > threshold

Timing gotcha: gate alerts to fire on confirmed daily bar close; these are EVENT alerts ("X just inverted"),
NOT the scheduled brief.

---

## Acceptance criteria
- [ ] Saved 16-panel daily layout renders the front-screen set (exact symbols verified).
- [ ] One Pine v6 indicator: cross-asset `table`, EWMA-z + level-percentile per tile, amber/red coloring, ≤40 `request.security()` (allocation agreed with Kaan), daily TF.
- [ ] 5-10 never-expire alerts firing JSON webhooks on the B.5 conditions; payload <4KB; fires on confirmed bar close.
- [ ] Indicator labels net-liquidity a "construct" and tags any intraday-only gauge as intraday (per reference §4 #11).
- [ ] README boundary respected: no FDR/cross-tile control, no scheduled brief (those are SPEC-2).

## Open / to-confirm with Kaan
- Exact TV symbols for Brent, MOVE, OFR-FSI, breadth (% >200DMA) — verify or push to backend.
- The webhook endpoint URL (provided by SPEC-2 backend).
- Final tile list + 40-call allocation.
