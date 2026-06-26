"""Local trading-data-bank reader — PRIMARY breadth price source (Mac-local arch).

Reads the PIT Sharadar SEP parquet lake under ``~/data/tradingbank`` (READ-ONLY).
The breadth module reads its trailing price window from here (fast, free, no NDL);
NDL is used ONLY to gap-fill the most-recent missing day(s) when the data bank
lags the last trading day.

NEVER writes into the data bank. Returns in-memory price windows only; the breadth
module aggregates them to derived % series before anything is persisted
(LICENSE: no raw per-security prices ever hit a committed/published file).

Data-bank layout (all under ~/data/tradingbank):
  raw/sharadar/_baseline_2026-06-13/SEP.parquet   baseline 1997 -> 2026-06-12
                                                  (date, lastupdated = DATE)
  raw/sharadar/append/**/SEP.parquet              daily revision appends — MIXED
                                                  layout: flat append/<date>/SEP.parquet
                                                  AND nested append/<date>/<tsZ>/SEP.parquet
                                                  (date, lastupdated = VARCHAR; +knowledge_date)
  raw/sharadar/_baseline_2026-06-13/TICKERS.parquet  broad-universe metadata
  db/catalog.sqlite                               ingestion_log + calendar_master

A (ticker, date) pair can have MANY revisions across append files; we keep the
latest ``lastupdated`` per (ticker, date) via a QUALIFY window dedup.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date as date_cls, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

log = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Paths (all via Path.home() — READ-ONLY; never written to)
# -----------------------------------------------------------------------
_DATABANK_ROOT = Path.home() / "data" / "tradingbank"
_SHARADAR_ROOT = _DATABANK_ROOT / "raw" / "sharadar"
_SEP_BASELINE = _SHARADAR_ROOT / "_baseline_2026-06-13" / "SEP.parquet"
_APPEND_ROOT = _SHARADAR_ROOT / "append"
_TICKERS_PARQUET = _SHARADAR_ROOT / "_baseline_2026-06-13" / "TICKERS.parquet"
_CATALOG_DB = _DATABANK_ROOT / "db" / "catalog.sqlite"


# -----------------------------------------------------------------------
# Internal SQL builders
# -----------------------------------------------------------------------
def _append_sep_files() -> list[str]:
    """Every append SEP.parquet under both flat and nested layouts.

    ``Path.glob('**/SEP.parquet')`` catches BOTH ``append/<date>/SEP.parquet`` and
    ``append/<date>/<timestampZ>/SEP.parquet`` in one pass.
    """
    if not _APPEND_ROOT.exists():
        return []
    return sorted(p.as_posix() for p in _APPEND_ROOT.glob("**/SEP.parquet"))


def _sql_str_list(paths: list[str]) -> str:
    """Render a python path list as a DuckDB string-array literal."""
    return "[" + ",".join("'" + p.replace("'", "''") + "'" for p in paths) + "]"


def _union_cte(*, with_lastupdated: bool) -> str:
    """SELECT body that UNION ALLs baseline + append SEP into a common schema.

    Casts ``date``/``lastupdated`` to DATE so the baseline (DATE) and append
    (VARCHAR) schemas line up. ``union_by_name=true`` lets the append file list
    tolerate the extra ``knowledge_date`` column / mixed layouts.
    """
    base = _SEP_BASELINE.as_posix()
    cols = "ticker, CAST(date AS DATE) AS d, closeadj"
    if with_lastupdated:
        cols += ", CAST(lastupdated AS DATE) AS lu"
    parts = [f"SELECT {cols} FROM read_parquet('{base}')"]
    append_files = _append_sep_files()
    if append_files:
        parts.append(
            f"SELECT {cols} FROM read_parquet({_sql_str_list(append_files)}, union_by_name=true)"
        )
    return " UNION ALL ".join(parts)


def _connect_duck():
    """Open an in-memory DuckDB connection (lazy import keeps the module importable
    without duckdb installed until a data-bank read is actually attempted)."""
    import duckdb  # local import — duckdb is only needed on the local data-bank path

    return duckdb.connect()


# -----------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------
def read_sep_window(
    date_from: str,
    date_to: str,
    tickers: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """Read a trailing closeadj window from the data bank (baseline + appends).

    Returns ``DataFrame[ticker, date (str 'YYYY-MM-DD'), closeadj (float)]`` with
    one row per (ticker, date) — the latest ``lastupdated`` revision wins. Rows
    are filtered to ``date_from <= date <= date_to``, ``closeadj IS NOT NULL`` and
    (optionally) ``ticker IN tickers``.

    Raises FileNotFoundError if the baseline parquet is missing (unless the caller
    passed an explicit-but-empty universe, which short-circuits to an empty frame
    WITHOUT touching disk — no query, no baseline read).
    """
    where = ["d BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)", "closeadj IS NOT NULL"]
    params: list[object] = [str(date_from), str(date_to)]

    ticker_list: Optional[list[str]] = None
    if tickers is not None:
        ticker_list = sorted({str(t) for t in tickers if t})
        if not ticker_list:
            # Caller passed an explicit-but-empty universe → no rows possible.
            # Short-circuit BEFORE any disk access (no baseline read, no query).
            return pd.DataFrame(columns=["ticker", "date", "closeadj"])
        placeholders = ",".join("?" for _ in ticker_list)
        where.append(f"ticker IN ({placeholders})")
        params.extend(ticker_list)

    if not _SEP_BASELINE.exists():
        raise FileNotFoundError(f"SEP baseline parquet not found: {_SEP_BASELINE}")

    q = f"""
    WITH u AS ({_union_cte(with_lastupdated=True)})
    SELECT ticker, strftime(d, '%Y-%m-%d') AS date, CAST(closeadj AS DOUBLE) AS closeadj
    FROM u
    WHERE {' AND '.join(where)}
    QUALIFY row_number() OVER (PARTITION BY ticker, d ORDER BY lu DESC NULLS LAST) = 1
    ORDER BY ticker, date
    """

    con = _connect_duck()
    try:
        df = con.execute(q, params).fetchdf()
    finally:
        con.close()

    if df.empty:
        return pd.DataFrame(columns=["ticker", "date", "closeadj"])
    df["closeadj"] = pd.to_numeric(df["closeadj"], errors="coerce")
    df = df.dropna(subset=["closeadj"])
    out = df.loc[:, ["ticker", "date", "closeadj"]].reset_index(drop=True)
    return pd.DataFrame(out)


def databank_max_sep_date() -> Optional[str]:
    """Latest price-bar date across baseline + appends ('YYYY-MM-DD'), or None.

    This is the actual latest SEP *bar* — NOT ingestion_log.knowledge_date (which
    is a revision cursor, not a price date).
    """
    if not _SEP_BASELINE.exists():
        return None
    base = _SEP_BASELINE.as_posix()
    parts = [f"SELECT max(CAST(date AS DATE)) AS m FROM read_parquet('{base}')"]
    append_files = _append_sep_files()
    if append_files:
        parts.append(
            f"SELECT max(CAST(date AS DATE)) AS m FROM read_parquet({_sql_str_list(append_files)}, union_by_name=true)"
        )
    q = "SELECT strftime(max(m), '%Y-%m-%d') FROM (" + " UNION ALL ".join(parts) + ")"
    con = _connect_duck()
    try:
        row = con.execute(q).fetchone()
    finally:
        con.close()
    return row[0] if row and row[0] else None


def last_trading_day(today: Optional[str] = None) -> str:
    """Last NYSE trading day on/before ``today`` (default: today, UTC date).

    Reads ``calendar_master`` from the data-bank catalog. Falls back to ``today``
    if the catalog is unavailable (the gap-fill path then degrades gracefully).
    """
    today_str = today or date_cls.today().isoformat()
    if not _CATALOG_DB.exists():
        log.warning("catalog.sqlite not found at %s; last_trading_day -> today", _CATALOG_DB)
        return today_str
    try:
        con = sqlite3.connect(f"file:{_CATALOG_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:  # pragma: no cover — env-specific
        log.warning("catalog.sqlite open failed (%s); last_trading_day -> today", exc)
        return today_str
    try:
        row = con.execute(
            "SELECT max(session_date) FROM calendar_master "
            "WHERE exchange='NYSE' AND is_trading_day=1 AND session_date <= ?",
            (today_str,),
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return today_str
    return str(row[0])


def last_completed_session(now: Optional[datetime] = None) -> Optional[str]:
    """Last NYSE session whose close (UTC) is at/before ``now`` (default: UTC now).

    This is the freshness/read-window TARGET for breadth: the data bank's latest
    bar is the last COMPLETED session, because today's EOD does not exist until
    after the US close (~20:00-21:00 UTC). Using ``last_trading_day`` (= today on a
    trading day) as the target would make a weekday-MORNING run always look
    incomplete and fire empty NDL gap-fill calls. ``last_completed_session`` returns
    yesterday on a morning run (bank is complete → no NDL) and today only AFTER the
    close (the one genuinely-missing session the gap-fill should pull).

    Reads ``calendar_master.session_close`` (ISO UTC ts, e.g. '...T20:00:00+00:00').
    Both ``now`` and ``session_close`` carry the '+00:00' offset, so the string
    ``<=`` comparison is a correct UTC ordering. Returns None if the catalog is
    unavailable (caller degrades gracefully).
    """
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_iso = now_dt.astimezone(timezone.utc).isoformat()

    if not _CATALOG_DB.exists():
        log.warning(
            "catalog.sqlite not found at %s; last_completed_session -> None", _CATALOG_DB
        )
        return None
    try:
        con = sqlite3.connect(f"file:{_CATALOG_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:  # pragma: no cover — env-specific
        log.warning("catalog.sqlite open failed (%s); last_completed_session -> None", exc)
        return None
    try:
        row = con.execute(
            "SELECT max(session_date) FROM calendar_master "
            "WHERE exchange='NYSE' AND is_trading_day=1 AND session_close <= ?",
            (now_iso,),
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return None
    return str(row[0])


def nyse_trading_days(date_from: str, date_to: str) -> set[str]:
    """Set of NYSE trading-day session_dates in [date_from, date_to] (inclusive).

    Used as a write-time prune gate so only genuine NYSE sessions survive in the
    published % cache (drops legacy/holiday rows). Read-only; returns an empty set
    if the catalog is unavailable so the caller can SKIP the prune (never drop rows
    on a missing-catalog machine).
    """
    if not _CATALOG_DB.exists():
        return set()
    try:
        con = sqlite3.connect(f"file:{_CATALOG_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError:  # pragma: no cover — env-specific
        return set()
    try:
        rows = con.execute(
            "SELECT session_date FROM calendar_master "
            "WHERE exchange='NYSE' AND is_trading_day=1 "
            "AND session_date BETWEEN ? AND ?",
            (str(date_from), str(date_to)),
        ).fetchall()
    finally:
        con.close()
    return {str(r[0]) for r in rows if r and r[0]}


def _has_sep_ingestion_row() -> bool:
    """True iff ingestion_log has a current sharadar_sep pull (is_latest=1)."""
    if not _CATALOG_DB.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{_CATALOG_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError:  # pragma: no cover — env-specific
        return False
    try:
        row = con.execute(
            "SELECT count(*) FROM ingestion_log WHERE source='sharadar_sep' AND is_latest=1"
        ).fetchone()
    finally:
        con.close()
    return bool(row and row[0])


def databank_sep_complete(last_td: str) -> bool:
    """True iff the data bank already covers ``last_td``.

    Requires BOTH a current ``sharadar_sep`` ingestion row AND a latest SEP bar
    date >= ``last_td``. When False, the breadth path gap-fills the missing tail
    from NDL.
    """
    if not _has_sep_ingestion_row():
        return False
    mx = databank_max_sep_date()
    return mx is not None and mx >= str(last_td)


def broad_universe() -> set[str]:
    """Broad-US active common-stock universe from TICKERS.parquet.

    Mirrors breadth.py's ``_BROAD_CATEGORIES`` / ``_BROAD_EXCHANGES`` exactly
    (imported lazily to avoid a circular import). Keeps domestic common stock on
    NYSE/NASDAQ/NYSEMKT with isdelisted='N'. NO market-cap floor (Kaan-confirmed:
    small-cap mass is the point). Returns an empty set if TICKERS.parquet is absent.
    """
    # Lazy import: breadth imports databank at module load, so importing breadth
    # at databank's top level would be circular. The constants are the source of
    # truth in breadth.py.
    from .breadth import _BROAD_CATEGORIES, _BROAD_EXCHANGES

    if not _TICKERS_PARQUET.exists():
        return set()
    exchanges = sorted(_BROAD_EXCHANGES)
    categories = sorted(_BROAD_CATEGORIES)  # already lowercase in breadth.py
    ex_ph = ",".join("?" for _ in exchanges)
    cat_ph = ",".join("?" for _ in categories)
    q = f"""
    SELECT DISTINCT ticker
    FROM read_parquet('{_TICKERS_PARQUET.as_posix()}')
    WHERE upper(isdelisted) = 'N'
      AND exchange IN ({ex_ph})
      AND lower(category) IN ({cat_ph})
    """
    con = _connect_duck()
    try:
        rows = con.execute(q, exchanges + categories).fetchall()
    finally:
        con.close()
    return {r[0] for r in rows if r and r[0]}
