"""Sharadar SEP breadth computation — data-bank-first (Mac-local arch).

Single shared SEP read per run (via session dict). Returns one RawSeries per breadth key.

Breadth keys (sharadar:<rest>):
  sp500_above_200dma  ->  % of S&P 500 members with closeadj > SMA200
  sp500_above_50dma   ->  % of S&P 500 members with closeadj > SMA50
  broad_above_200dma  ->  % of broad-US with closeadj > SMA200
  broad_above_50dma   ->  % of broad-US with closeadj > SMA50
  nhnl_52w            ->  (52w new-highs − new-lows) / valid * 100  (broad-US)

LICENSE HARD CONSTRAINT: NEVER write raw per-security prices. Only derived %
aggregates + the constituent list (sp500.csv) may be committed/published.

SEP source strategy (data-bank-first with freshness-aware NDL gap-fill):
  - PRIMARY: read the trailing closeadj window from the LOCAL data bank
    (morning_monitor.sources.databank) — fast, free, no NDL key required.
  - GAP-FILL: if the data bank lags the last trading day (mid-ingest / today's
    bar not yet appended), pull ONLY the missing date range from NDL, ticker-
    filtered to the universe (tiny) and concat onto the data-bank window. If no
    NDL key is available, degrade gracefully (today's bar may be missing) but
    still compute from the data-bank history — never hard-fail.
  - Window: first run (cache cold) reads ~1600 cal days (backfill); incremental
    runs read ~420 cal days (covers the 252-td NH-NL warmup).
  - Universe: S&P 500 from the committed data/universe/sp500.csv; broad-US from
    the data bank's TICKERS.parquet (databank.broad_universe()).
  - All 5 breadth tiles share ONE data-bank read per run (via the session dict).
  - Cache: data/breadth/<key>.csv (columns: date,value). Idempotent re-run safe.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from datetime import date as date_cls, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
import pandas as pd

from ..models import HistoryPoint, RawSeries
from . import databank

log = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------
_NDL_SEP_URL = "https://data.nasdaq.com/api/v3/datatables/SHARADAR/SEP.json"
_NDL_TICKERS_URL = "https://data.nasdaq.com/api/v3/datatables/SHARADAR/TICKERS.json"
_WIKI_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_WIKI_USER_AGENT = (
    "morning-market-monitor/1.0 (github.com/kaantradelab/morning-market-monitor; research use)"
)

# Broad-US universe: domestic common stock categories (no ETF, ADR, preferred, warrant)
_BROAD_CATEGORIES = {
    "domestic common stock",
    "domestic common stock primary class",
    "domestic common stock secondary class",
}
_BROAD_EXCHANGES = {"NYSE", "NASDAQ", "NYSEMKT"}

# MA windows (trading days)
_SMA200 = 200
_SMA50 = 50
_NH_NL_WINDOW = 252  # 52-week = 252 trading days

# SEP fetch chunking + retry configuration.
# Each chunk issues a fresh cursor session bounded to <= _SEP_CHUNK_DAYS calendar
# days so a single transient 5xx can never strand a 2400-page session.
_SEP_CHUNK_DAYS = 180  # max calendar days per cursor session
_SEP_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0, 12.0)  # seconds between retries (3 retries)
# Gap-fill ticker batching: NDL datatables accepts a comma-separated ticker filter.
# ~500 per call keeps each URL bounded while the gap (1-3 days) stays tiny.
_SEP_TICKER_BATCH = 500

# Calendar-day look-backs for SEP pulls.
# Sized for the WIDEST warmup tile = NH-NL 52w (252 trading days), NOT just SMA200.
# 252 td ≈ 365 calendar days; backfill must also clear the 756 td baseline on top.
#   backfill:    756 td baseline + 252 td NH-NL warmup ≈ 1008 td ≈ ~1470 cal → 1600 w/ buffer
#   incremental: must exceed 252 td (NH-NL) so nhnl_52w does not degrade every run → 420 cal
_BACKFILL_CALENDAR_DAYS = 1600  # ~4.4 years — 756 td baseline + 252 td NH-NL warmup + buffer
_INCREMENTAL_CALENDAR_DAYS = 420  # >252 td after holidays — covers SMA200 AND the 252 td NH-NL window

# Minimum cached rows before we treat it as "backfill done"
_BACKFILL_MIN_ROWS = 750

# All 5 breadth cache keys (kept in sync with _rest_to_key's mapping values). The
# backfill gate uses the MINIMUM cached depth across ALL of them so a single thin
# tile (e.g. nhnl_52w) still forces the deep backfill-width read — picking the gate
# off one already-warm key would strand the thin tiles on the incremental window.
_ALL_BREADTH_KEYS: tuple[str, ...] = (
    "breadth_200dma",
    "breadth_50dma",
    "breadth_broad_200dma",
    "breadth_broad_50dma",
    "breadth_nhnl_52w",
)

# Minimum closeadj threshold for broad-US penny-stock filter (per-day)
_MIN_CLOSE = 1.0

# Repo-relative cache paths
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BREADTH_CACHE_DIR = _REPO_ROOT / "data" / "breadth"
_UNIVERSE_DIR = _REPO_ROOT / "data" / "universe"
_SP500_CACHE_PATH = _UNIVERSE_DIR / "sp500.csv"


# -----------------------------------------------------------------------
# Public entry point (called by ingest.py for each sharadar: tile)
# -----------------------------------------------------------------------
def fetch_breadth_series(
    rest: str,
    *,
    config: Any,
    http: httpx.Client,
    session: dict,
) -> RawSeries:
    """Return a RawSeries for the breadth key named by `rest`.

    `session` is a per-run dict (created in ingest.py) that caches the shared
    SEP pull and universe sets so all 5 tiles share one network round-trip.
    NEVER raises — callers wrap in _safe(); any failure returns a degraded series.
    """
    key = _rest_to_key(rest)
    source = f"sharadar:{rest}"

    # --- ensure shared state is populated (one SEP pull per run) ---
    _ensure_session(rest, config=config, http=http, session=session)

    df = session.get("sep_df")
    if df is None or df.empty:
        return _degraded(key, source, session.get("sep_error", "SEP data unavailable"))

    # --- compute % series for this key ---
    try:
        pct_series = _compute_pct_series(rest, df, session)
    except Exception as exc:  # noqa: BLE001
        return _degraded(key, source, f"compute error: {exc!r}")

    if pct_series is None or pct_series.empty:
        return _degraded(key, source, "empty % series after computation")

    # --- update the per-key cache (data/breadth/<key>.csv) ---
    try:
        _update_cache(key, pct_series)
    except Exception as exc:  # noqa: BLE001
        log.warning("breadth cache write failed for %s: %s", key, exc)

    # --- load full cache for history ---
    try:
        history = _load_cache(key)
    except Exception as exc:  # noqa: BLE001
        # Fall back to just the computed window if cache read fails
        log.warning("breadth cache read failed for %s: %s", key, exc)
        history = _series_to_history(pct_series)

    if not history:
        return _degraded(key, source, "no history points after cache load")

    asof = history[-1].date
    return RawSeries(
        key=key,
        source=source,
        history=history,
        asof=asof,
        lag_desc="Sharadar EOD",
        ok=True,
    )


# -----------------------------------------------------------------------
# Session initialisation (called once per run via _ensure_session)
# -----------------------------------------------------------------------
def _ensure_session(rest: str, *, config: Any, http: httpx.Client, session: dict) -> None:
    """Populate session with the SEP DataFrame + universe sets if not already done.

    Data-bank-first: read the trailing window from the LOCAL data bank, then
    gap-fill ONLY the missing tail from NDL when the bank lags the last trading
    day. NEVER raises — any failure leaves a degraded session (sep_df=None).
    """
    if "sep_initialized" in session:
        return

    session["sep_initialized"] = True  # mark even if we fail below — avoid repeated failures

    # Cache-window knobs are config-driven (breadth.cache.*) with the module
    # constants as fallbacks, so an operator can retune the read windows in
    # config.yaml without a code change.
    backfill_days, incremental_days, backfill_min_rows = _cache_window_knobs(config)

    # Determine read window (backfill vs incremental) from the MINIMUM cached depth
    # across ALL 5 breadth keys — not just this tile's. Any under-filled key (e.g.
    # a freshly-seeded nhnl_52w) must force the deep backfill-width read; gating off
    # one already-warm key would leave the thin tiles permanently on the incremental
    # window. The shared SEP read is computed once per run, so the widest needed
    # window wins for everyone.
    min_cache_rows = min(_count_cache_rows(k) for k in _ALL_BREADTH_KEYS)
    is_backfill = min_cache_rows < backfill_min_rows
    window_days = backfill_days if is_backfill else incremental_days

    today = date_cls.today()
    date_from = (today - timedelta(days=window_days)).isoformat()

    # Freshness/read-window TARGET = last COMPLETED NYSE session (close <= now UTC),
    # NOT last_trading_day (= today on a trading day). The data bank's latest bar is
    # the last completed session; on a weekday MORNING run that is yesterday, so the
    # bank is already complete and NO empty NDL gap-fill fires. Only AFTER the US
    # close does the target advance to today — the one session the gap-fill pulls.
    try:
        last_td = databank.last_completed_session()
    except Exception as exc:  # noqa: BLE001
        last_td = None
        log.warning("databank.last_completed_session failed (%s)", exc)
    if not last_td:
        # Catalog unavailable → fall back to last_trading_day (itself today on failure).
        try:
            last_td = databank.last_trading_day()
        except Exception as exc:  # noqa: BLE001
            last_td = today.isoformat()
            log.warning("databank.last_trading_day fallback failed (%s); using today", exc)

    log.info(
        "breadth: %s data-bank read %s -> %s (~%d cal days)",
        "BACKFILL" if is_backfill else "INCREMENTAL", date_from, last_td, window_days,
    )

    # --- universes FIRST (so the data-bank read + any gap pull are ticker-filtered) ---
    sp500 = _load_sp500_cache()
    session["sp500_tickers"] = sp500
    if not sp500:
        session["sp500_error"] = (
            "S&P 500 cache (data/universe/sp500.csv) missing or empty"
        )

    try:
        broad = databank.broad_universe()
    except Exception as exc:  # noqa: BLE001
        broad = set()
        session["broad_error"] = f"broad universe error: {exc!r}"
    session["broad_tickers"] = broad
    if not broad and "broad_error" not in session:
        session["broad_error"] = "broad universe empty (data bank TICKERS.parquet missing?)"

    universe = sp500 | broad  # union; empty only if BOTH sources are unavailable

    # --- PRIMARY: read the trailing window from the LOCAL data bank ---
    # ALWAYS ticker-bounded: pass the universe set (never None). An empty universe
    # short-circuits to an empty frame in read_sep_window — it must NEVER fall back
    # to a full-universe read (that is the 24M-row / OOM footgun this arch avoids).
    try:
        df = databank.read_sep_window(date_from, last_td, tickers=universe)
    except Exception as exc:  # noqa: BLE001
        df = pd.DataFrame(columns=["ticker", "date", "closeadj"])
        session["sep_error"] = f"data-bank read error: {exc!r}"

    # --- GAP-FILL: pull ONLY the missing tail from NDL when the bank lags last_td ---
    # Only meaningful when we have a universe to bound the (tiny) NDL pull. With no
    # universe there is nothing to compute and a None-ticker NDL pull would be the
    # full-universe disaster — so skip gap-fill entirely in that case.
    if universe:
        try:
            complete = databank.databank_sep_complete(last_td)
        except Exception as exc:  # noqa: BLE001
            complete = False
            log.warning("databank.databank_sep_complete failed (%s); assuming gap", exc)

        if not complete:
            df = _gap_fill_from_ndl(
                df, last_td=last_td, universe=universe, config=config, http=http, session=session,
            )

    if df is None or df.empty:
        session["sep_df"] = None
        session.setdefault("sep_error", "no SEP rows from data bank or gap-fill")
        return

    session["sep_df"] = df


def _gap_fill_from_ndl(
    df: pd.DataFrame,
    *,
    last_td: str,
    universe: set[str],
    config: Any,
    http: httpx.Client,
    session: dict,
) -> pd.DataFrame:
    """Concat an NDL pull of ONLY the (databank_max, last_td] tail onto `df`.

    Graceful: if there is no NDL key, or the pull fails/returns nothing, return
    `df` unchanged (the data-bank history) — today's bar may be missing but the
    run never hard-fails.
    """
    try:
        db_max = databank.databank_max_sep_date()
    except Exception as exc:  # noqa: BLE001
        db_max = None
        log.warning("databank.databank_max_sep_date failed (%s)", exc)

    if db_max:
        gap_from = (date_cls.fromisoformat(db_max) + timedelta(days=1)).isoformat()
    else:
        # Data bank empty for SEP — bound the NDL pull to the read window, not all-time.
        gap_from = (date_cls.today() - timedelta(days=_INCREMENTAL_CALENDAR_DAYS)).isoformat()

    if gap_from > str(last_td):
        return df  # nothing to fill

    if not universe:
        # Caller already guards this, but never let a None-ticker NDL pull through
        # (that is the full-universe footgun) — defence in depth.
        return df

    ndl_key = _ndl_api_key(config)
    if not ndl_key:
        log.info(
            "breadth: data bank lags %s but no NDL key; using data-bank history "
            "(today's bar may be missing)", last_td,
        )
        session.setdefault("gap_fill_note", "no NDL key for gap-fill; data-bank history only")
        return df

    try:
        gap_df = _fetch_sep(
            date_from=gap_from,
            api_key=ndl_key,
            http=http,
            date_to=str(last_td),
            tickers=sorted(universe),  # always bounded — never a full-universe pull
        )
    except Exception as exc:  # noqa: BLE001 — gap-fill is best-effort; never hard-fail
        log.warning("breadth gap-fill NDL pull failed (%s); using data-bank history only", exc)
        session.setdefault("gap_fill_note", f"gap-fill failed: {exc!r}")
        return df

    if gap_df is None or gap_df.empty:
        return df

    merged = pd.concat([df, gap_df], ignore_index=True)
    # NDL revision wins on the (rare) boundary overlap — gap is strictly after db_max.
    merged = merged.drop_duplicates(subset=["ticker", "date"], keep="last")
    return merged.sort_values(["ticker", "date"]).reset_index(drop=True)


# -----------------------------------------------------------------------
# HTTP retry helper
# -----------------------------------------------------------------------
def _get_with_retry(
    http: httpx.Client,
    url: str,
    *,
    params: dict[str, Any],
    timeout: float = 120.0,
) -> Any:
    """GET with exponential-backoff retry on transient errors.

    Retry policy (uses module-level _SEP_RETRY_DELAYS):
      - Retries on: httpx network/timeout errors, HTTP 429, HTTP >= 500.
      - Raises immediately on: HTTP 4xx other than 429 (auth/bad-request, no benefit).
      - Raises RuntimeError after all retry attempts are exhausted.
    """
    last_exc: Exception = RuntimeError("no attempts made")
    max_attempts = len(_SEP_RETRY_DELAYS) + 1  # initial + 3 retries = 4 total
    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(_SEP_RETRY_DELAYS[attempt - 1])
        try:
            resp = http.get(url, params=params, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — network / timeout
            last_exc = exc
            continue
        if resp.status_code == 200:
            return resp
        if resp.status_code == 429 or resp.status_code >= 500:
            last_exc = RuntimeError(f"HTTP {resp.status_code}")
            continue
        # 4xx (non-429): client error — no benefit to retrying
        raise RuntimeError(f"HTTP {resp.status_code}")
    raise RuntimeError(
        f"HTTP request failed after {max_attempts} attempts: {last_exc!r}"
    )


# -----------------------------------------------------------------------
# SEP fetch (paginated NDL datatable)
# -----------------------------------------------------------------------
def _fetch_sep(
    *,
    date_from: str,
    api_key: str,
    http: httpx.Client,
    date_to: Optional[str] = None,
    tickers: Optional[list[str]] = None,
) -> pd.DataFrame:
    """Pull SHARADAR/SEP via NDL datatables API with chunked date ranges and per-request retry.

    Used as the GAP-FILL source on the data-bank-first path: the range is normally
    tiny (1-3 trailing days) and ``tickers`` is the breadth universe so each pull
    is small.

    Splits [date_from, date_to] (date_to defaults to today) into sequential chunks
    of <= _SEP_CHUNK_DAYS calendar days. Each chunk issues a FRESH query
    (date.gte=chunk_start, date.lte=chunk_end) and paginates via next_cursor_id
    within that chunk only. This bounds every cursor session to a fraction of the
    total row count, so a transient error on any single page only affects that
    chunk's retry budget.

    When ``tickers`` is given, the universe is split into batches of
    _SEP_TICKER_BATCH and each batch is pulled with a comma-separated ``ticker``
    filter (so the gap pull stays tiny), then concatenated.

    Per-request retry: httpx errors OR HTTP 5xx/429 → up to 3 retries with
    exponential backoff via _get_with_retry. HTTP 4xx (non-429) raises immediately.
    Returns a DataFrame with columns: ticker, date (str), closeadj (float).
    """
    end = date_cls.fromisoformat(date_to) if date_to else date_cls.today()

    frames: list[pd.DataFrame] = []
    if tickers:
        uniq = sorted({str(t) for t in tickers if t})
        for i in range(0, len(uniq), _SEP_TICKER_BATCH):
            batch = uniq[i : i + _SEP_TICKER_BATCH]
            frames.append(
                _fetch_sep_range(
                    date_from=date_from, date_to=end, api_key=api_key, http=http,
                    ticker_csv=",".join(batch),
                )
            )
    else:
        frames.append(
            _fetch_sep_range(
                date_from=date_from, date_to=end, api_key=api_key, http=http, ticker_csv=None,
            )
        )

    all_pages = [f for f in frames if f is not None and not f.empty]
    if not all_pages:
        return pd.DataFrame(columns=["ticker", "date", "closeadj"])

    df = pd.concat(all_pages, ignore_index=True)
    # Ensure correct types (keep existing dtype coercion + dropna + sort)
    df["closeadj"] = pd.to_numeric(df["closeadj"], errors="coerce")
    df = df.dropna(subset=["closeadj"])
    df = df.sort_values(["ticker", "date"])
    return df[["ticker", "date", "closeadj"]]


def _fetch_sep_range(
    *,
    date_from: str,
    date_to: date_cls,
    api_key: str,
    http: httpx.Client,
    ticker_csv: Optional[str],
) -> pd.DataFrame:
    """Pull one [date_from, date_to] range (optionally ticker-filtered) — chunked + paginated.

    Returns the raw concatenated pages (columns: whatever NDL returns; the caller
    coerces dtypes). Raises RuntimeError on a non-retryable HTTP error.
    """
    chunk_start = date_cls.fromisoformat(date_from)
    pages: list[pd.DataFrame] = []

    while chunk_start <= date_to:
        chunk_end = min(chunk_start + timedelta(days=_SEP_CHUNK_DAYS - 1), date_to)

        # Fresh query for this chunk (new cursor session — bounded page count)
        params: dict[str, Any] = {
            "date.gte": chunk_start.isoformat(),
            "date.lte": chunk_end.isoformat(),
            "qopts.per_page": 10000,
            "qopts.columns": "ticker,date,closeadj",
            "api_key": api_key,
        }
        if ticker_csv:
            params["ticker"] = ticker_csv

        # Paginate within this chunk; each request has its own retry budget.
        # The cursor carries the filter context, so ticker= is only on the first page.
        while True:
            resp = _get_with_retry(http, _NDL_SEP_URL, params=params, timeout=120.0)
            payload = resp.json()
            dt = payload.get("datatable", {})
            cols = [c["name"] for c in dt.get("columns", [])]
            rows = dt.get("data", [])
            if rows:
                pages.append(pd.DataFrame(rows, columns=cols))
            cursor = (payload.get("meta") or {}).get("next_cursor_id")
            if not cursor:
                break
            params = {"qopts.cursor_id": cursor, "api_key": api_key}

        chunk_start = chunk_end + timedelta(days=1)

    if not pages:
        return pd.DataFrame(columns=["ticker", "date", "closeadj"])
    return pd.concat(pages, ignore_index=True)


# -----------------------------------------------------------------------
# Universe building
# -----------------------------------------------------------------------
def _get_sp500_universe(
    *, config: Any, http: httpx.Client, sep_tickers: set[str]
) -> set[str]:
    """Fetch S&P 500 tickers from Wikipedia; use cache on failure.

    BRK.B and BF.B are kept as-is — verified to match SEP format exactly.
    """
    # Try a fresh fetch first
    headers = {"User-Agent": _WIKI_USER_AGENT}
    try:
        resp = http.get(_WIKI_SP500_URL, headers=headers, timeout=30.0)
        if resp.status_code != 200:
            raise RuntimeError(f"Wikipedia HTTP {resp.status_code}")
        tables = pd.read_html(io.StringIO(resp.text))
        raw_tickers = list(tables[0]["Symbol"])
        if not (480 <= len(raw_tickers) <= 520):
            raise ValueError(f"Wikipedia parse yielded {len(raw_tickers)} tickers — rejected (expect 480-520)")
        # Keep tickers that appear in SEP; log unmatched ones
        tickers: set[str] = set()
        for t in raw_tickers:
            t = str(t).strip()
            if t in sep_tickers:
                tickers.add(t)
            else:
                log.debug("S&P member %s not found in SEP — excluded", t)
        # Persist to cache
        _UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_SP500_CACHE_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["ticker"])
            for t in sorted(tickers):
                writer.writerow([t])
        return tickers
    except Exception as fetch_exc:  # noqa: BLE001
        log.warning("Wikipedia S&P 500 fetch failed (%s); using cache", fetch_exc)
        return _load_sp500_cache()


def _load_sp500_cache() -> set[str]:
    """Load the committed/cached sp500.csv. Returns empty set if not found."""
    if not _SP500_CACHE_PATH.exists():
        return set()
    with open(_SP500_CACHE_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {row["ticker"].strip() for row in reader if row.get("ticker")}


def _get_broad_universe(*, api_key: str, http: httpx.Client) -> set[str]:
    """Fetch SHARADAR/TICKERS and filter to the broad-US active universe.

    Keeps: domestic common stock categories; NYSE/NASDAQ/NYSEMKT; isdelisted=N.
    Excludes: ETF, ADR, preferred, warrant, Canadian, OTC/pink.
    """
    params: dict[str, Any] = {
        "table": "SEP",
        "qopts.per_page": 10000,
        "qopts.columns": "ticker,exchange,isdelisted,category",
        "api_key": api_key,
    }
    pages: list[pd.DataFrame] = []

    while True:
        resp = _get_with_retry(http, _NDL_TICKERS_URL, params=params, timeout=60.0)
        payload = resp.json()
        dt = payload.get("datatable", {})
        cols = [c["name"] for c in dt.get("columns", [])]
        rows = dt.get("data", [])
        if rows:
            pages.append(pd.DataFrame(rows, columns=cols))
        cursor = (payload.get("meta") or {}).get("next_cursor_id")
        if not cursor:
            break
        params = {"qopts.cursor_id": cursor, "api_key": api_key}

    if not pages:
        return set()

    df = pd.concat(pages, ignore_index=True)
    # Apply universe filters
    mask = (
        df["isdelisted"].str.upper().eq("N")
        & df["exchange"].isin(_BROAD_EXCHANGES)
        & df["category"].str.lower().isin(_BROAD_CATEGORIES)
    )
    return set(df.loc[mask, "ticker"].unique())


# -----------------------------------------------------------------------
# Breadth % series computation
# -----------------------------------------------------------------------
def _compute_pct_series(rest: str, df: pd.DataFrame, session: dict) -> Optional[pd.Series]:
    """Compute the % or net series for the requested breadth key.

    Returns a pandas Series indexed by date string (YYYY-MM-DD), values are floats.
    """
    sp500 = session.get("sp500_tickers", set())
    broad = session.get("broad_tickers", set())

    if rest in ("sp500_above_200dma", "sp500_above_50dma") and not sp500:
        sp500_err = session.get("sp500_error", "S&P 500 universe unavailable")
        raise RuntimeError(sp500_err)

    if rest in ("broad_above_200dma", "broad_above_50dma", "nhnl_52w") and not broad:
        broad_err = session.get("broad_error", "Broad universe unavailable")
        raise RuntimeError(broad_err)

    # Filter dataframe to the relevant universe
    if rest in ("sp500_above_200dma", "sp500_above_50dma"):
        members = sp500
    else:
        members = broad

    sub = df[df["ticker"].isin(members)].copy()
    if sub.empty:
        raise RuntimeError(f"No SEP data for {rest} universe members")

    # Pivot to price matrix: rows=date, cols=ticker
    prices = sub.pivot(index="date", columns="ticker", values="closeadj")
    prices = prices.sort_index()

    # Apply penny stock filter per-day for broad universe
    if rest in ("broad_above_200dma", "broad_above_50dma", "nhnl_52w"):
        prices = prices.where(prices >= _MIN_CLOSE)

    if rest == "sp500_above_200dma":
        return _pct_above_sma(prices, _SMA200)
    if rest == "sp500_above_50dma":
        return _pct_above_sma(prices, _SMA50)
    if rest == "broad_above_200dma":
        return _pct_above_sma(prices, _SMA200)
    if rest == "broad_above_50dma":
        return _pct_above_sma(prices, _SMA50)
    if rest == "nhnl_52w":
        return _net_nhnl(prices, _NH_NL_WINDOW)

    raise RuntimeError(f"Unknown breadth rest: {rest!r}")


def _pct_above_sma(prices: pd.DataFrame, window: int) -> pd.Series:
    """% of members with closeadj > SMA(window) on each date."""
    sma = prices.rolling(window, min_periods=window).mean()
    valid = sma.notna()
    above = (prices > sma) & valid
    n_above = above.sum(axis=1)
    n_valid = valid.sum(axis=1)
    result = (n_above / n_valid * 100).where(n_valid > 0)
    return result.dropna()


def _net_nhnl(prices: pd.DataFrame, window: int) -> pd.Series:
    """Net new-high/new-low as % of valid universe (signed, per 52w window)."""
    rolling_high = prices.rolling(window, min_periods=window).max()
    rolling_low = prices.rolling(window, min_periods=window).min()
    valid = rolling_high.notna() & rolling_low.notna()
    new_highs = ((prices == rolling_high) & valid).sum(axis=1)
    new_lows = ((prices == rolling_low) & valid).sum(axis=1)
    n_valid = valid.sum(axis=1)
    result = ((new_highs - new_lows) / n_valid * 100).where(n_valid > 0)
    return result.dropna()


# -----------------------------------------------------------------------
# Cache management
# -----------------------------------------------------------------------
def _cache_path(key: str) -> Path:
    return _BREADTH_CACHE_DIR / f"{key}.csv"


def _count_cache_rows(key: str) -> int:
    """Number of rows in the cached CSV (0 if missing)."""
    p = _cache_path(key)
    if not p.exists():
        return 0
    try:
        with open(p, newline="", encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)  # subtract header
    except Exception:  # noqa: BLE001
        return 0


def _update_cache(key: str, new_series: pd.Series) -> None:
    """Merge new_series (index=date str, values=float) into the CSV cache.

    Idempotent: existing dates are overwritten; new dates are appended.
    LICENSE: only writes derived % values — no raw prices.
    """
    _BREADTH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = _cache_path(key)

    # Load existing
    existing: dict[str, float] = {}
    if p.exists():
        try:
            with open(p, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    d = row.get("date", "").strip()
                    v = row.get("value", "").strip()
                    if d and v:
                        try:
                            existing[d] = float(v)
                        except ValueError:
                            pass
        except Exception:  # noqa: BLE001
            existing = {}

    # Merge
    for date_str, val in new_series.items():
        if pd.notna(val):
            existing[str(date_str)] = float(val)

    # HOLIDAY-PRUNE: the published % series must contain ONLY NYSE trading days.
    # Drop any cached date that is not an NYSE session — legacy/holiday rows (e.g.
    # the old seed's 100.0 spikes on 2026-04-03 / 05-25 / 06-19) must never survive.
    # The trading-day set comes from the data-bank calendar; if the catalog is absent
    # nyse_trading_days returns empty → we SKIP the prune (never drop rows on a
    # machine without the data bank).
    if existing:
        trading_days = databank.nyse_trading_days(min(existing), max(existing))
        if trading_days:
            existing = {d: v for d, v in existing.items() if d in trading_days}

    # Write sorted
    sorted_items = sorted(existing.items())
    with open(p, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "value"])
        for d, v in sorted_items:
            writer.writerow([d, f"{v:.6f}"])


def _load_cache(key: str) -> list[HistoryPoint]:
    """Load the full CSV cache as a list of HistoryPoint (oldest→newest)."""
    p = _cache_path(key)
    if not p.exists():
        return []
    points: list[HistoryPoint] = []
    with open(p, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = row.get("date", "").strip()
            v = row.get("value", "").strip()
            if d and v:
                try:
                    points.append(HistoryPoint(date=d, value=float(v)))
                except ValueError:
                    pass
    return points


def _series_to_history(series: pd.Series) -> list[HistoryPoint]:
    """Convert a pandas Series (index=date str, values=float) to HistoryPoint list."""
    out: list[HistoryPoint] = []
    for date_str, val in series.items():
        if pd.notna(val):
            out.append(HistoryPoint(date=str(date_str), value=float(val)))
    return out


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
def _rest_to_key(rest: str) -> str:
    """Map sharadar: rest string to the tile key in config."""
    mapping = {
        "sp500_above_200dma": "breadth_200dma",
        "sp500_above_50dma": "breadth_50dma",
        "broad_above_200dma": "breadth_broad_200dma",
        "broad_above_50dma": "breadth_broad_50dma",
        "nhnl_52w": "breadth_nhnl_52w",
    }
    return mapping.get(rest, f"breadth_{rest}")


def _ndl_api_key(config: Any) -> Optional[str]:
    """Resolve NASDAQ_DATA_LINK_API_KEY from config accessor."""
    getter = getattr(config, "nasdaq_data_link_api_key", None)
    if callable(getter):
        try:
            return getter()
        except Exception:  # noqa: BLE001
            return None
    return None


def _cache_window_knobs(config: Any) -> tuple[int, int, int]:
    """Resolve (backfill_days, incremental_days, backfill_min_rows) from config.

    Reads breadth.cache.{backfill_calendar_days, incremental_calendar_days,
    backfill_min_rows} with the module constants as fallbacks, so the pull
    windows are tunable from config.yaml (no longer a dead config block).
    """
    backfill = _BACKFILL_CALENDAR_DAYS
    incremental = _INCREMENTAL_CALENDAR_DAYS
    min_rows = _BACKFILL_MIN_ROWS
    try:
        raw = getattr(config, "raw", {}) or {}
        cache_cfg = ((raw.get("breadth") or {}).get("cache") or {})
        backfill = int(cache_cfg.get("backfill_calendar_days", backfill))
        incremental = int(cache_cfg.get("incremental_calendar_days", incremental))
        min_rows = int(cache_cfg.get("backfill_min_rows", min_rows))
    except Exception:  # noqa: BLE001 — never let a config read break ingest
        return _BACKFILL_CALENDAR_DAYS, _INCREMENTAL_CALENDAR_DAYS, _BACKFILL_MIN_ROWS
    return backfill, incremental, min_rows


def _degraded(key: str, source: str, reason: str) -> RawSeries:
    return RawSeries(
        key=key, source=source, history=[], asof=None,
        lag_desc="Sharadar EOD", ok=False, error=reason,
    )
