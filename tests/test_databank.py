"""Tests for morning_monitor.sources.databank — the local data-bank breadth reader.

Two layers:
  - Hermetic unit tests for the SQL builders / fallbacks (no I/O).
  - Real-data-bank integration tests against ~/data/tradingbank (READ-ONLY),
    skipped automatically when the data bank is not present (e.g. CI). These prove
    read_sep_window dedups overlapping revisions across the mixed flat/nested
    append layout and returns sane rows. They NEVER write into the data bank.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from morning_monitor.sources import databank as db

# Skip the real-data-bank tests when the lake is not on disk.
_HAS_DATABANK = db._SEP_BASELINE.exists()
_HAS_CATALOG = db._CATALOG_DB.exists()
_HAS_TICKERS = db._TICKERS_PARQUET.exists()

databank_required = pytest.mark.skipif(
    not _HAS_DATABANK, reason="local data bank (~/data/tradingbank) not present"
)
catalog_required = pytest.mark.skipif(
    not _HAS_CATALOG, reason="data-bank catalog.sqlite not present"
)
tickers_required = pytest.mark.skipif(
    not _HAS_TICKERS, reason="data-bank TICKERS.parquet not present"
)


# ---------------------------------------------------------------------------
# Hermetic unit tests (no I/O)
# ---------------------------------------------------------------------------
class TestSqlBuilders:
    def test_sql_str_list_quotes_and_escapes(self):
        assert db._sql_str_list(["/a/b.parquet"]) == "['/a/b.parquet']"
        assert db._sql_str_list(["/a/b.parquet", "/c/d.parquet"]) == (
            "['/a/b.parquet','/c/d.parquet']"
        )
        # single quotes in a path are doubled (defensive)
        assert db._sql_str_list(["/a'b.parquet"]) == "['/a''b.parquet']"

    def test_union_cte_includes_baseline_and_dedup_column(self):
        sql = db._union_cte(with_lastupdated=True)
        assert "read_parquet(" in sql
        assert "CAST(date AS DATE) AS d" in sql
        assert "CAST(lastupdated AS DATE) AS lu" in sql

    def test_read_sep_window_empty_ticker_filter_short_circuits(self):
        """An explicit-but-empty universe yields zero rows without any query."""
        out = db.read_sep_window("2026-06-01", "2026-06-25", tickers=[])
        assert list(out.columns) == ["ticker", "date", "closeadj"]
        assert out.empty


# ---------------------------------------------------------------------------
# Real-data-bank integration (READ-ONLY; skipped if the lake is absent)
# ---------------------------------------------------------------------------
@databank_required
class TestReadSepWindow:
    def test_window_columns_and_dtypes(self):
        df = db.read_sep_window("2026-06-10", "2026-06-25", tickers=["AAPL", "MSFT"])
        assert list(df.columns) == ["ticker", "date", "closeadj"]
        assert not df.empty
        # date is a 'YYYY-MM-DD' string (matches the existing pipeline contract)
        assert isinstance(df["date"].iloc[0], str)
        assert len(df["date"].iloc[0]) == 10
        assert df["closeadj"].dtype.kind == "f"
        assert (df["closeadj"] > 0).all()

    def test_dedup_one_row_per_ticker_date(self):
        """Overlapping revisions across the mixed flat/nested append layout collapse
        to exactly one row per (ticker, date) — the latest lastupdated wins.

        SATA/APH/CINF have 9-10 revisions for 2026-06-18 in the append lake; the
        QUALIFY dedup must leave a single row each.
        """
        df = db.read_sep_window(
            "2026-06-15", "2026-06-25", tickers=["SATA", "APH", "CINF", "AAPL"]
        )
        assert not df.empty
        counts = df.groupby(["ticker", "date"]).size()
        assert counts.max() == 1, "dedup must leave at most one row per (ticker, date)"

    def test_ticker_filter_restricts_universe(self):
        df = db.read_sep_window("2026-06-20", "2026-06-25", tickers=["AAPL"])
        assert not df.empty
        assert set(df["ticker"].unique()) == {"AAPL"}

    def test_window_spans_baseline_to_append_boundary(self):
        """A window crossing the baseline (<=2026-06-12) / append (>=2026-06-15)
        seam returns rows from BOTH sources for the same ticker."""
        df = db.read_sep_window("2026-06-10", "2026-06-25", tickers=["AAPL"])
        dates = set(df["date"])
        assert any(d <= "2026-06-12" for d in dates), "baseline rows expected"
        assert any(d >= "2026-06-15" for d in dates), "append rows expected"


@databank_required
class TestMaxSepDate:
    def test_max_date_is_a_plausible_iso_date(self):
        mx = db.databank_max_sep_date()
        assert isinstance(mx, str) and len(mx) == 10
        # baseline ends 2026-06-12; appends extend it forward
        assert mx >= "2026-06-12"


@catalog_required
class TestLastTradingDay:
    def test_known_friday_returns_itself(self):
        # 2026-06-26 is a Friday and a NYSE session.
        assert db.last_trading_day("2026-06-26") == "2026-06-26"

    def test_sunday_returns_prior_friday(self):
        # 2026-06-28 is a Sunday → last session on/before is Fri 2026-06-26.
        assert db.last_trading_day("2026-06-28") == "2026-06-26"

    def test_returns_iso_date_string(self):
        out = db.last_trading_day("2026-06-26")
        assert isinstance(out, str) and len(out) == 10


@databank_required
@catalog_required
class TestSepComplete:
    def test_complete_for_a_covered_day(self):
        # The latest bar date is covered by definition.
        mx = db.databank_max_sep_date()
        assert mx is not None  # data bank present (databank_required) → always set
        assert db.databank_sep_complete(mx) is True

    def test_incomplete_for_far_future_day(self):
        assert db.databank_sep_complete("2099-12-31") is False


@tickers_required
class TestBroadUniverse:
    def test_universe_is_large_and_filtered(self):
        bu = db.broad_universe()
        assert isinstance(bu, set)
        assert len(bu) > 1000, "broad US common-stock universe should be a few thousand names"
        assert "AAPL" in bu
        # ETFs and ADRs are excluded by category.
        assert "SPY" not in bu, "ETF must be excluded"
        assert "BABA" not in bu, "ADR must be excluded"

    def test_mirrors_breadth_constants(self):
        """databank.broad_universe filters with breadth.py's constants (no drift)."""
        from morning_monitor.sources.breadth import _BROAD_CATEGORIES, _BROAD_EXCHANGES

        assert "domestic common stock" in _BROAD_CATEGORIES
        assert _BROAD_EXCHANGES == {"NYSE", "NASDAQ", "NYSEMKT"}


# ---------------------------------------------------------------------------
# last_completed_session — hermetic (temp catalog, no real data bank needed)
# ---------------------------------------------------------------------------
def _make_temp_catalog(path: str, sessions: list[tuple[str, str]]) -> None:
    """Build a minimal calendar_master with (session_date, session_close_utc) rows."""
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE calendar_master ("
            "exchange TEXT, session_date TEXT, is_trading_day INTEGER, session_close TEXT)"
        )
        con.executemany(
            "INSERT INTO calendar_master VALUES ('NYSE', ?, 1, ?)", sessions
        )
        con.commit()
    finally:
        con.close()


class TestLastCompletedSession:
    """Freshness TARGET = last NYSE session whose close (UTC) is at/before now.

    These drive the two cases the breadth gap-fill hinges on:
      - MORNING run (now before today's 20:00 UTC close): target = the PRIOR
        session → the data bank (holding yesterday) is complete → ZERO NDL.
      - POST-CLOSE run (now after today's close, bank still at yesterday): target =
        today → the gap-fill pulls ONLY that one session. (NDL consequences are
        asserted in test_breadth.py TestGapFill.)
    """

    def _catalog(self, tmp_path):
        p = tmp_path / "catalog.sqlite"
        _make_temp_catalog(
            str(p),
            [
                ("2026-06-25", "2026-06-25T20:00:00+00:00"),
                ("2026-06-26", "2026-06-26T20:00:00+00:00"),
            ],
        )
        return p

    def test_morning_before_close_returns_prior_session(self, tmp_path):
        p = self._catalog(tmp_path)
        # 2026-06-26 08:00 UTC — a weekday morning, BEFORE today's 20:00 close.
        now = datetime(2026, 6, 26, 8, 0, tzinfo=timezone.utc)
        with patch.object(db, "_CATALOG_DB", p):
            assert db.last_completed_session(now) == "2026-06-25"

    def test_post_close_returns_today(self, tmp_path):
        p = self._catalog(tmp_path)
        # 2026-06-26 21:00 UTC — AFTER today's 20:00 close.
        now = datetime(2026, 6, 26, 21, 0, tzinfo=timezone.utc)
        with patch.object(db, "_CATALOG_DB", p):
            assert db.last_completed_session(now) == "2026-06-26"

    def test_exactly_at_close_counts_as_completed(self, tmp_path):
        p = self._catalog(tmp_path)
        # now == the close instant → that session is completed (<= comparison).
        now = datetime(2026, 6, 26, 20, 0, tzinfo=timezone.utc)
        with patch.object(db, "_CATALOG_DB", p):
            assert db.last_completed_session(now) == "2026-06-26"

    def test_naive_now_is_treated_as_utc(self, tmp_path):
        p = self._catalog(tmp_path)
        now = datetime(2026, 6, 26, 8, 0)  # tz-naive → coerced to UTC
        with patch.object(db, "_CATALOG_DB", p):
            assert db.last_completed_session(now) == "2026-06-25"

    def test_missing_catalog_returns_none(self, tmp_path):
        with patch.object(db, "_CATALOG_DB", tmp_path / "nope.sqlite"):
            now = datetime(2026, 6, 26, 8, 0, tzinfo=timezone.utc)
            assert db.last_completed_session(now) is None


class TestNyseTradingDays:
    """nyse_trading_days returns the NYSE session set in [from, to]; empty if no catalog."""

    def test_returns_sessions_in_range(self, tmp_path):
        p = tmp_path / "catalog.sqlite"
        _make_temp_catalog(
            str(p),
            [
                ("2026-06-18", "2026-06-18T20:00:00+00:00"),
                ("2026-06-22", "2026-06-22T20:00:00+00:00"),
            ],
        )
        with patch.object(db, "_CATALOG_DB", p):
            out = db.nyse_trading_days("2026-06-18", "2026-06-22")
        assert out == {"2026-06-18", "2026-06-22"}

    def test_missing_catalog_returns_empty(self, tmp_path):
        with patch.object(db, "_CATALOG_DB", tmp_path / "nope.sqlite"):
            assert db.nyse_trading_days("2026-06-18", "2026-06-22") == set()
