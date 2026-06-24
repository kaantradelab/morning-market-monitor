# SPEC-2 — Backend / snapshot side

**Build owner:** Usta (code) · **Type:** BUILD request.
**Reads:** the vetted reference `cin/reports/2026-06-24-morning-monitor-design-reference.md` (§1 tiles, §2 stats, §3 calendar, §4 blind-spots, §6 disputes).
**Posture:** SNAPSHOT — runs once a morning on free EOD/snapshot data. NOT a streaming terminal.

## Goal

The thin **intelligence layer** TV lacks: pull free EOD/snapshot data → compute the anomaly layer (the heavy
stats that don't fit in Pine) → emit a **09:00 İstanbul morning brief** (≤3 attention cards + calendar +
plumbing flags) via push/Telegram/email → and receive TV event-alert webhooks to relay. The backend owns the
morning clock (TV can't schedule) and the cross-tile FDR (impractical in Pine).

---

## 1. Data ingestion (free-first)

| Group | Source | Series / fields |
|---|---|---|
| Rates | **FRED API** | `DGS2`,`DGS10`,`DFII10`(10y real),`T10YIE`(10y BE),`T10Y2Y`,`T10Y3M` |
| Credit | FRED | `BAMLH0A0HYM2`(HY OAS),`BAMLC0A0CM`(IG OAS),`BAMLC0A4CBBB`(BBB) |
| Dollar | FRED | `DTWEXBGS`(broad); DXY via yfinance |
| Plumbing ⭐ | FRED + NY Fed | `WALCL`,`WTREGEN`(TGA),`RRPONTSYD`(ON RRP),`SOFR`,`IORB`,`EFFR`,`DFEDTARU/L`; **SRF take-up** = NY Fed Repo Operations page (scrape/API) |
| Composites | FRED + OFR | `NFCI`,`ANFCI`,`STLFSI4`; **OFR FSI** = financialresearch.gov (daily, 2-bd lag) |
| Equities/FX/cmdty/vol | **yfinance** (or EODHD/Twelve Data) | `^GSPC`,`ES=F`,`^NDX`,`^VIX`,`^VIX3M`,`^VVIX`,`^MOVE`(delayed),`DX-Y.NYB`,`EURUSD=X`,`JPY=X`,`CNH=X`,`CL=F`,`BZ=F`(Brent),`GC=F`,`HG=F` |
| Breadth | StockCharts / vendor | `$SPXA200R`(% >200DMA), RSP/SPY ratio |
| Crypto | Binance API + DefiLlama | BTC, ETH; **stablecoin aggregate cap** (DefiLlama) |
| Calendar | **Finnhub** or FMP free-tier | econ calendar + consensus; **Citi Surprise Index** via MacroMicro if scrapable |

History depth: pull ≥3y daily per series (for the 1y+3y percentile baselines). Label each tile's **freshness**
(OFR 2-bd lag, NFCI/STLFSI weekly, copper monthly) — staleness badge is a hard requirement (reference §4 #12).

## 2. Anomaly engine (the heavy stats — reference §2)

1. **Transform:** log-returns (prices), first-differences (yields/spreads). Never raw z on a level.
2. **Magnitude:** EWMA σ (λ=0.94 daily), `z = x/σ̂`. (Config-switchable to GARCH(1,1)-t — see §5.)
3. **Rarity:** empirical percentile of standardized move over **1y AND 3y** windows. **Standardize-by-short-EWMA
   FIRST, then long-percentile** (reference §2.1). Robust z (median/MAD) for spiky series.
4. **Level percentile** (756d) shown alongside change-score (state vs shock).
5. ⭐ **Detect on COMPOSITES + ~5-8 axis-factors, not 30 raw tiles** — run the deviation test on
   OFR-FSI/NFCI/CISS + axis-aggregates first; raw tiles are drill-down. (Shrinks the test family 30→~6,
   builds corroboration in, kills the fake-corroboration trap.)
6. **Multiple-comparisons control:** **FDR (Benjamini–Yekutieli, q=0.10)** across the morning's tile/family set
   (BY not BH because tiles are cross-correlated). Plus **≥2-orthogonal-tile corroboration** before a Red.
   3σ headline as the simple fallback.
7. **Correlation-break = residual, not rolling-corr:** fit `A ≈ f(B)` over a long window (e.g. HY-OAS-Δ on
   ACWI-ret + VIX-Δ over 252d); flag when today's residual is a ±2.5-EWMA-σ / percentile outlier. Require 3-day
   persistence for a corr-regime flag; weight sign-flips (stock-bond corr) heavily.
8. **Dog-didn't-bark:** on scheduled-event days, compare realized standardized move to the expected event-day
   move (straddle-implied or 3y event-day historical); flag the LOW tail (<~0.5× expected) = priced-in/coiled-spring.
9. **Quarantine intraday gauges** (dealer-gamma/0DTE/intraday-repo) — never a same-morning trigger; regime-label only.

## 3. Morning brief generator (cron 09:00 İstanbul)

Output ≤3 attention cards (a crisis banner may add more). Each card carries the mandatory **"why now"**:
`abnormal metric · its percentile/z · related calendar event · cross-asset confirmation-or-contradiction`.
Plus: the **calendar strip** (today's releases · time · consensus · high-impact flag · prior Citi surprise) and
the **plumbing flags** (SRF rising off ~0, SOFR−IORB >25bp, EFFR top-of-range, net-liq 5d drop). Staleness
badges on every tile. Delivery: push / Telegram / email (Kaan's choice). A card without "why now" must be
suppressed (decorative noise, reference §4 #6/§2.2).

**Calibration benchmark:** on a typical calm morning the system should surface **≤1 Red**. If it surfaces more,
the thresholds/FDR are too loose → raise the headline to 3σ and tighten corroboration (reference §rec-2).

## 4. Webhook receiver

HTTPS endpoint (port 443, <3s response) that ingests TV `alert()` JSON payloads (SPEC-1 B.5 conditions) and
relays them to the same push channel as the morning brief. This is the "reach me intraday when X breaks" path;
provide the endpoint URL back to SPEC-1.

## 5. Expose the disputes as CONFIG, do not hard-code (reference §6)

These are genuinely unsettled in the literature — make them switchable so we can A/B, not bake one in:
`vol_model: ewma|garch` · `percentile_window: 1y|3y|full` · `fpr_control: fdr|corroboration|composite_only` ·
`net_liquidity: context_only` (flagged non-Fed-metric, frequency-mismatched — never an oracle) ·
`cot_positioning: modifier_only` (delayed/category-ambiguous).

## Acceptance criteria
- [ ] Free-first ingestion of all §1 series with ≥3y history + per-tile freshness label.
- [ ] Anomaly engine: EWMA-z + 1y/3y percentile + robust-z + detect-on-composites + BY-FDR(q=0.10) + ≥2-tile corroboration + residual-based correlation-break + dog-didn't-bark.
- [ ] Cron 09:00 İstanbul emits ≤3 "why-now" cards + calendar strip + plumbing flags via push; ≤1 Red on a calm day.
- [ ] HTTPS webhook receiver relays TV event-alerts; endpoint URL handed to SPEC-1.
- [ ] Disputed knobs are config, not hard-coded; intraday gauges quarantined.

## Open / to-confirm with Kaan
- Delivery channel (Telegram vs email vs push).
- Licensed feeds? (Bloomberg/ICE) → upgrade MOVE/CDX/x-ccy-basis/dealer-gamma from proxies to real.
- Calendar vendor (Finnhub vs FMP) + whether Citi Surprise is scrapable for free.
- Hosting for the cron + webhook endpoint.
