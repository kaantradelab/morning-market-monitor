"""Sharadar SEP breadth computation — SPEC-3 cloud breadth.

Single shared SEP pull per run (via session dict). Returns one RawSeries per breadth key.

Breadth keys (sharadar:<rest>):
  sp500_above_200dma  ->  % of S&P 500 members with closeadj > SMA200
  sp500_above_50dma   ->  % of S&P 500 members with closeadj > SMA50
  broad_above_200dma  ->  % of broad-US with closeadj > SMA200
  broad_above_50dma   ->  % of broad-US with closeadj > SMA50
  nhnl_52w            ->  (52w new-highs − new-lows) / valid * 100  (broad-US)

LICENSE HARD CONSTRAINT: NEVER write raw per-security prices. Only derived %
aggregates + the constituent list (sp500.csv) may be committed/published.

SEP pull strategy:
  - First run (cache missing or <756 rows): pull ~3.8y of history (backfill).
  - Incremental run: pull trailing ~350 calendar days (SMA200 warmup + new session).
  - All 5 breadth tiles share ONE SEP pull per run (via the session dict).
  - Cache: data/breadth/<key>.csv (columns: date,value). Idempotent re-run safe.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date as date_cls, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
import pandas as pd

from ..models import HistoryPoint, RawSeries

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

# Calendar-day look-backs for SEP pulls.
# Sized for the WIDEST warmup tile = NH-NL 52w (252 trading days), NOT just SMA200.
# 252 td ≈ 365 calendar days; backfill must also clear the 756 td baseline on top.
#   backfill:    756 td baseline + 252 td NH-NL warmup ≈ 1008 td ≈ ~1470 cal → 1600 w/ buffer
#   incremental: must exceed 252 td (NH-NL) so nhnl_52w does not degrade every run → 420 cal
_BACKFILL_CALENDAR_DAYS = 1600  # ~4.4 years — 756 td baseline + 252 td NH-NL warmup + buffer
_INCREMENTAL_CALENDAR_DAYS = 420  # >252 td after holidays — covers SMA200 AND the 252 td NH-NL window

# Minimum cached rows before we treat it as "backfill done"
_BACKFILL_MIN_ROWS = 750

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
    """Populate session with SEP DataFrame + universe sets if not already done."""
    if "sep_initialized" in session:
        return

    session["sep_initialized"] = True  # mark even if we fail below — avoid repeated failures

    # Cache-window knobs are config-driven (breadth.cache.*) with the module
    # constants as fallbacks, so an operator can retune the pull windows in
    # config.yaml without a code change.
    backfill_days, incremental_days, backfill_min_rows = _cache_window_knobs(config)

    # Determine pull window (backfill vs incremental) by checking any key's cache size.
    # Use any key since all tiles share the same backfill threshold.
    key0 = _rest_to_key(rest)
    cache_rows = _count_cache_rows(key0)
    is_backfill = cache_rows < backfill_min_rows

    today = date_cls.today()
    if is_backfill:
        date_from = (today - timedelta(days=backfill_days)).isoformat()
        log.info("breadth: BACKFILL pull from %s (~%d cal days)", date_from, backfill_days)
    else:
        date_from = (today - timedelta(days=incremental_days)).isoformat()
        log.info("breadth: INCREMENTAL pull from %s (~%d cal days)", date_from, incremental_days)

    # Resolve API key
    ndl_key = _ndl_api_key(config)
    if not ndl_key:
        session["sep_df"] = None
        session["sep_error"] = "NASDAQ_DATA_LINK_API_KEY not set"
        return

    # Pull SEP (paginated)
    try:
        sep_df = _fetch_sep(date_from=date_from, api_key=ndl_key, http=http)
        session["sep_df"] = sep_df
    except Exception as exc:  # noqa: BLE001
        session["sep_df"] = None
        session["sep_error"] = f"SEP fetch error: {exc!r}"
        return

    # Build universe sets (S&P 500 and broad-US)
    try:
        sp500 = _get_sp500_universe(config=config, http=http, sep_tickers=set(sep_df["ticker"].unique()))
        session["sp500_tickers"] = sp500
    except Exception as exc:  # noqa: BLE001
        log.warning("S&P 500 universe fetch failed: %s", exc)
        session["sp500_tickers"] = set()
        session["sp500_error"] = f"S&P 500 universe error: {exc!r}"

    try:
        broad = _get_broad_universe(api_key=ndl_key, http=http)
        session["broad_tickers"] = broad
    except Exception as exc:  # noqa: BLE001
        log.warning("Broad universe fetch failed: %s", exc)
        session["broad_tickers"] = set()
        session["broad_error"] = f"broad universe error: {exc!r}"


# -----------------------------------------------------------------------
# SEP fetch (paginated NDL datatable)
# -----------------------------------------------------------------------
def _fetch_sep(*, date_from: str, api_key: str, http: httpx.Client) -> pd.DataFrame:
    """Pull SHARADAR/SEP via NDL datatables API, paginating via next_cursor_id.

    Returns a DataFrame with columns: ticker, date (str), closeadj (float).
    """
    params: dict[str, Any] = {
        "date.gte": date_from,
        "qopts.per_page": 10000,
        "qopts.columns": "ticker,date,closeadj",
        "api_key": api_key,
    }
    pages: list[pd.DataFrame] = []
    page_num = 0

    while True:
        resp = http.get(_NDL_SEP_URL, params=params, timeout=120.0)
        if resp.status_code != 200:
            raise RuntimeError(f"NDL SEP HTTP {resp.status_code}")
        payload = resp.json()
        dt = payload.get("datatable", {})
        cols = [c["name"] for c in dt.get("columns", [])]
        rows = dt.get("data", [])

        if rows:
            pages.append(pd.DataFrame(rows, columns=cols))
        page_num += 1

        cursor = (payload.get("meta") or {}).get("next_cursor_id")
        if not cursor:
            break
        params = {"qopts.cursor_id": cursor, "api_key": api_key}

    if not pages:
        return pd.DataFrame(columns=["ticker", "date", "closeadj"])

    df = pd.concat(pages, ignore_index=True)
    # Ensure correct types
    df["closeadj"] = pd.to_numeric(df["closeadj"], errors="coerce")
    df = df.dropna(subset=["closeadj"])
    df = df.sort_values(["ticker", "date"])
    return df[["ticker", "date", "closeadj"]]


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
        resp = http.get(_NDL_TICKERS_URL, params=params, timeout=60.0)
        if resp.status_code != 200:
            raise RuntimeError(f"NDL TICKERS HTTP {resp.status_code}")
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
