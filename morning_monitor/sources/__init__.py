"""Ingestion package — per-source fetchers + the ingest() orchestrator.

BUILD TARGET 1 (ingestion). Contract:

    ingest(config) -> dict[str, RawSeries]      # one entry per CORE tile key

Each fetcher pulls >=3y daily history, sets staleness (asof + lag_desc), and on
failure returns a RawSeries with ok=False (graceful degradation — NEVER raise out
of ingest(); a dead source = a stale/missing tile, not a crashed run).

Fetchers by source family:
    fred.py       -> FRED API series (rates, credit, dollar, plumbing, composites)
    market.py     -> yfinance delayed EOD (equities, vol, fx, commodity, BTC)
    crypto.py     -> DefiLlama stablecoin aggregate cap
    derived.py    -> computed tiles (net_liquidity, sofr_iorb, copper_gold, move_proxy, rsp_spy)
    composites.py -> OFR FSI (financialresearch.gov), FRED NFCI/ANFCI/STLFSI4
    calendar.py   -> Finnhub/FMP econ calendar + consensus (-> CalendarEvent list)
"""

from .ingest import ingest  # noqa: F401
