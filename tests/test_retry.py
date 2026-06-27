"""Bounded retry-with-back-off tests for _safe (ingest) and _fetch_fred (calendar).

All tests monkeypatch time.sleep to a no-op so the suite stays fast (no real
seconds). Tests assert sleep call counts to verify the back-off cadence.

Test matrix
-----------
1. _safe retries on exception        — raises once then ok=True → ok=True; sleep ×1
2. _safe retries on ok=False         — ok=False once then ok=True → ok=True; sleep ×1
3. _safe exhausts attempts           — always-raising fetcher → ok=False; sleep ×(N-1)
4. _safe retry=False                 — failing fetcher called exactly once; no sleep
5. calendar: transient ReadError     — fails then succeeds → events, no degraded reason
6. calendar: persistent ReadError    — all attempts fail → degraded reason, call count=N
"""
from __future__ import annotations

import importlib
from typing import Optional
from unittest.mock import MagicMock, patch

import httpx

from morning_monitor.models import RawSeries
from morning_monitor.sources import _retry

# Load ingest as a module object so we can monkeypatch module-level names.
_ingest = importlib.import_module("morning_monitor.sources.ingest")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ok_series(key: str = "test") -> RawSeries:
    return RawSeries(
        key=key, source="test:src", history=[], asof=None, lag_desc="test", ok=True
    )


def _bad_series(key: str = "test") -> RawSeries:
    return RawSeries(
        key=key, source="test:src", history=[], asof=None, lag_desc="test",
        ok=False, error="transient miss",
    )


def _fred_resp(status_code: int = 200, dates: Optional[list] = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"release_dates": dates or []}
    return resp


def _calendar_config(fred_key: str = "test-key") -> MagicMock:
    cfg = MagicMock()
    cfg.fred_api_key.return_value = fred_key
    cfg.fmp_api_key.return_value = None
    cfg.finnhub_api_key.return_value = None
    cfg.raw = {
        "calendar": {
            "provider": "fred",
            "high_impact_events": [],
            "fred_releases": [{"id": 10, "name": "Consumer Price Index"}],
            "fomc_dates": [],
        }
    }
    return cfg


# ---------------------------------------------------------------------------
# 1. _safe retries on exception then succeeds
# ---------------------------------------------------------------------------
def test_safe_retries_on_exception_then_succeeds(monkeypatch):
    """Fetcher raises on attempt 0, returns ok=True on attempt 1.
    Final result is ok=True; sleep called exactly once (between attempts).
    """
    monkeypatch.setattr(_ingest, "_RETRY_ATTEMPTS", 3)

    call_count = 0

    def _fetcher():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient network error")
        return _ok_series()

    with patch("morning_monitor.sources.ingest.time.sleep") as mock_sleep:
        result = _ingest._safe(_fetcher, key="test", source="test:src", lag_desc="test")

    assert result.ok is True, "second attempt (ok=True) must be returned"
    assert call_count == 2, "fetcher must be called twice (1 raise + 1 success)"
    mock_sleep.assert_called_once()


# ---------------------------------------------------------------------------
# 2. _safe retries on ok=False result then succeeds
# ---------------------------------------------------------------------------
def test_safe_retries_on_ok_false_then_succeeds(monkeypatch):
    """Fetcher returns ok=False on attempt 0, returns ok=True on attempt 1.
    Final result is ok=True; sleep called exactly once.
    """
    monkeypatch.setattr(_ingest, "_RETRY_ATTEMPTS", 3)

    call_count = 0

    def _fetcher():
        nonlocal call_count
        call_count += 1
        return _bad_series() if call_count == 1 else _ok_series()

    with patch("morning_monitor.sources.ingest.time.sleep") as mock_sleep:
        result = _ingest._safe(_fetcher, key="test", source="test:src", lag_desc="test")

    assert result.ok is True
    assert call_count == 2
    mock_sleep.assert_called_once()


# ---------------------------------------------------------------------------
# 3. _safe exhausts _RETRY_ATTEMPTS and returns degraded
# ---------------------------------------------------------------------------
def test_safe_exhausts_attempts_returns_degraded(monkeypatch):
    """Always-raising fetcher: called exactly _RETRY_ATTEMPTS times.
    sleep called _RETRY_ATTEMPTS-1 times (no sleep after the final attempt).
    Result is ok=False (degraded).
    """
    n = 3
    monkeypatch.setattr(_ingest, "_RETRY_ATTEMPTS", n)

    call_count = 0

    def _always_raises():
        nonlocal call_count
        call_count += 1
        raise RuntimeError("persistent failure")

    with patch("morning_monitor.sources.ingest.time.sleep") as mock_sleep:
        result = _ingest._safe(
            _always_raises, key="test", source="test:src", lag_desc="test"
        )

    assert result.ok is False
    assert call_count == n, f"expected {n} calls, got {call_count}"
    assert mock_sleep.call_count == n - 1, (
        f"sleep must be called {n - 1} times (not after final attempt)"
    )


# ---------------------------------------------------------------------------
# 4. retry=False — pure-compute wrapper is called exactly once
# ---------------------------------------------------------------------------
def test_safe_retry_false_calls_fetcher_once(monkeypatch):
    """With retry=False, a failing fetcher is called exactly once.
    No sleep regardless of _RETRY_ATTEMPTS.
    """
    monkeypatch.setattr(_ingest, "_RETRY_ATTEMPTS", 3)

    call_count = 0

    def _always_raises():
        nonlocal call_count
        call_count += 1
        raise RuntimeError("compute failure")

    with patch("morning_monitor.sources.ingest.time.sleep") as mock_sleep:
        result = _ingest._safe(
            _always_raises, key="test", source="test:src", lag_desc="test",
            retry=False,
        )

    assert result.ok is False
    assert call_count == 1, "retry=False must call the fetcher exactly once"
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# 5a. Calendar: transient ReadError on first call, success on second
# ---------------------------------------------------------------------------
def test_calendar_fred_retries_read_error_then_succeeds():
    """http.get raises ReadError on attempt 0, returns 200 (empty) on attempt 1.
    fetch_calendar_with_status returns (events, None) — not degraded.
    sleep called once between the two attempts.
    """
    from morning_monitor.sources.calendar import fetch_calendar_with_status

    http = MagicMock()
    http.get.side_effect = [
        httpx.ReadError("connection reset"),
        _fred_resp(200, []),
    ]

    with patch("morning_monitor.sources.calendar.time.sleep") as mock_sleep:
        events, reason = fetch_calendar_with_status(
            _calendar_config(), "2026-06-27", http=http
        )

    assert reason is None, f"expected no degraded reason, got: {reason!r}"
    assert isinstance(events, list)
    mock_sleep.assert_called_once()


# ---------------------------------------------------------------------------
# 5b. Calendar: persistent ReadError exhausts all attempts → degraded reason
# ---------------------------------------------------------------------------
def test_calendar_fred_persistent_read_error_degrades():
    """Persistent httpx.ReadError across all _RETRY_ATTEMPTS calls.
    fetch_calendar_with_status returns a non-None degraded reason (not raises).
    http.get is called exactly _RETRY_ATTEMPTS times.
    """
    from morning_monitor.sources.calendar import fetch_calendar_with_status

    n = _retry._RETRY_ATTEMPTS

    http = MagicMock()
    http.get.side_effect = httpx.ReadError("persistent reset")

    with patch("morning_monitor.sources.calendar.time.sleep"):
        events, reason = fetch_calendar_with_status(
            _calendar_config(), "2026-06-27", http=http
        )

    assert reason is not None, "persistent failure must produce a degraded reason"
    assert "FRED" in reason, f"reason should reference FRED, got: {reason!r}"
    assert events == [], "persistent failure should yield an empty events list"
    assert http.get.call_count == n, (
        f"expected {n} http.get calls, got {http.get.call_count}"
    )
