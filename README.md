# Morning Cross-Asset Market-Monitor — Project

**What:** A once-a-morning, single-screen cross-asset monitor for Kaan (macro-aware operator). Two jobs:
**ORIENT** ("where is the world right now?") + **ATTENTION** ("what is genuinely abnormal today?").
**Snapshot use-case** — looked at once in the morning, NOT a real-time trading terminal → delayed/EOD data is fine.

**This is NOT a trading edge / signal.** It describes market *state*, not what to trade. No verdicts.

## Architecture — hybrid (rent the display, build the brain)

TradingView (Kaan's Premium already paid) is the rich DISPLAY + event-sentinel; a thin custom backend is the
scheduled BRAIN + delivery. We do NOT rebuild a terminal — TV already is one; the licensing-expensive part
(real-time streaming) is rented, the valuable part (anomaly detection + morning brief) is built cheaply on
free EOD/snapshot data.

```
                ┌─ SPEC-1  TV / Pine  (build owner: PINE) ─────────────┐
                │  16-panel layout (orientation) + Pine anomaly-overlay │
                │  table + 5-10 never-expire event-alerts → webhook     │
   morning ───► │                                                       │
   monitor      └─ SPEC-2  backend / snapshot (build owner: USTA) ──────┘
                   FRED+yfinance+calendar pull → anomaly engine (EWMA+
                   percentile+FDR on composites) → 09:00 brief (push) +
                   webhook receiver for TV event-alerts
```

**Why split:** TV alerts fire at **bar-close** (daily = İstanbul night, ~23:00) — so the *scheduled morning
brief* MUST come from the backend cron, not TV. TV owns the eyeball display + intraday event-alerts; the
backend owns the 09:00 clock + the heavy stats (FDR across tiles is impractical in Pine).

## v1 sequencing (Premium already in hand)

1. **v1 = SPEC-1 (TV/Pine)** — near-zero infra, leans on Kaan's Pine strength; "open it and see" + "ping me intraday."
2. **v2 = SPEC-2 (backend cron)** — the "reach me at 09:00 with a brief before I even open it" piece.

## Files

| File | Build owner | Channel |
|---|---|---|
| `specs/SPEC-1-tv-pine.md` | **Pine** (TradingView authoring) | a BUILD/authoring request — NOT an edge measurement, no hypothesis-card, no verdict |
| `specs/SPEC-2-backend-snapshot.md` | **Usta** (code) | a BUILD request |

## Source of truth (the WHY behind every choice)

The full design rationale — 8-arm deep-research synthesis (4 MOS scouts + 4 web-DR), CORE/nice tile
justification, sound anomaly statistics, blind-spots, and the open methodological disputes — lives in:
**`~/myos/cin/reports/2026-06-24-morning-monitor-design-reference.md`** (the VETTED reference).
Raw run: `~/myos/cin/research/2026-06-24-morning-market-monitor/`.

Both specs draw from that reference; read §1 (tiles), §2 (anomaly), §4 (blind-spots), §6 (open disputes) before building.

## Data posture

**Free-first.** Every CORE tile is free (FRED / exchange-delayed / public index methodologies). The 4 paywalled
gauges (MOVE, CDX, cross-currency basis, dealer-gamma) ship as **free proxies** unless Kaan confirms a licensed
(Bloomberg/ICE) feed — then those 4 upgrade to the real series.

## Owner

Discover/spec: Cin. Build: Pine (SPEC-1) + Usta (SPEC-2). Handoffs gated on Kaan greenlight.
