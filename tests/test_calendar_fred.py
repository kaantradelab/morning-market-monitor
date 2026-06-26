"""SPEC-3 §8 — FRED calendar provider tests.

All offline: mock HTTP responses, no live FRED API calls.
Covers:
  - FRED releases mapped to CalendarEvent and ranked by transmission power
  - FOMC static dates injected correctly when in window
  - FOMC static dates outside window not injected
  - FRED-down (network, 403) → degraded reason, not silent empty
  - Genuine empty window → empty list, not degraded (no releases scheduled)
  - Provider routing: provider='fred' calls FRED, not FMP/Finnhub
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from morning_monitor.sources.calendar import fetch_calendar_with_status, _sort_events
from morning_monitor.models import CalendarEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_config(provider: str = "fred", fred_key: str = "test-fred-key") -> MagicMock:
    """Build a minimal mock Config for calendar testing."""
    cfg = MagicMock()
    cfg.fred_api_key.return_value = fred_key
    cfg.fmp_api_key.return_value = None
    cfg.finnhub_api_key.return_value = None
    cfg.raw = {
        "calendar": {
            "provider": provider,
            "high_impact_events": ["FOMC", "CPI", "NFP", "Employment Situation"],
            "fred_releases": [
                {"id": 10, "name": "Consumer Price Index (CPI)"},
                {"id": 50, "name": "Employment Situation (NFP)"},
                {"id": 54, "name": "Personal Income and Outlays (PCE)"},
                {"id": 46, "name": "Producer Price Index (PPI)"},
                {"id": 180, "name": "Initial Jobless Claims"},
            ],
            "fomc_dates": [
                "2026-06-18",   # past (before our test window)
                "2026-07-30",   # in window
                "2026-09-17",   # future (outside 7-day window from 2026-07-28)
            ],
        }
    }
    return cfg


def _make_fred_releases_response(release_dates: list[dict]) -> dict:
    """Build a FRED release/dates API response (per-release endpoint shape).

    Each item is {release_id, release_name, date}. The SPEC-3 §10 calendar
    queries the PER-RELEASE endpoint (`/fred/release/dates?release_id=N`) once per
    configured release_id; the helper below routes by the requested release_id so
    each call only sees its own release's dates (mirrors the live API).
    """
    return {
        "realtime_start": "2026-07-28",
        "realtime_end": "9999-12-31",
        "order_by": "release_date",
        "sort_order": "asc",
        "count": len(release_dates),
        "offset": 0,
        "limit": 50,
        "release_dates": release_dates,
    }


def _make_http(json_body: dict, status_code: int = 200) -> MagicMock:
    """HTTP mock that routes a per-release (`release_id=N`) GET to only that
    release's dates, drawn from json_body['release_dates']. Releases with no
    scheduled date in json_body return an empty list (200), as the live FRED
    per-release endpoint does with include_release_dates_with_no_data."""
    all_dates = list(json_body.get("release_dates", []))

    def _route(*args, **kwargs):
        resp = MagicMock()
        resp.status_code = status_code
        if status_code != 200:
            resp.json.return_value = {}
            return resp
        params = kwargs.get("params", {}) or {}
        rid = params.get("release_id")
        if rid is None:
            # No release_id filter (defensive): return everything.
            scoped = all_dates
        else:
            scoped = [rd for rd in all_dates if rd.get("release_id") == rid]
        resp.json.return_value = _make_fred_releases_response(scoped)
        return resp

    http = MagicMock()
    http.get.side_effect = _route
    return http


# ---------------------------------------------------------------------------
# 1. FRED releases mapped to CalendarEvent
# ---------------------------------------------------------------------------
class TestFredReleasesMapping:
    """Configured release IDs are mapped to named CalendarEvent objects."""

    def test_cpi_release_mapped_and_ranked(self):
        """Release ID 10 (CPI) → CalendarEvent with rank=2, high_impact=True."""
        rd = [{"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-07-28"}]
        http = _make_http(_make_fred_releases_response(rd))
        cfg = _make_config()

        events, reason = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        assert reason is None, f"No degraded reason expected; got: {reason}"
        assert any(e.event == "Consumer Price Index (CPI)" for e in events), \
            "CPI release should appear with configured display name"
        cpi = next(e for e in events if "CPI" in e.event)
        assert cpi.high_impact is True
        assert cpi.rank is not None and cpi.rank <= 3  # CPI rank ≤ 3 in _TRANSMISSION_RANK

    def test_nfp_release_mapped(self):
        """Release ID 50 (Employment Situation/NFP) → CalendarEvent."""
        rd = [{"release_id": 50, "release_name": "Employment Situation", "date": "2026-08-07"}]
        http = _make_http(_make_fred_releases_response(rd))
        cfg = _make_config()

        events, reason = fetch_calendar_with_status(cfg, "2026-08-05", http=http)

        assert reason is None
        assert any("Employment Situation" in e.event for e in events)
        nfp = next(e for e in events if "Employment Situation" in e.event)
        assert nfp.high_impact is True

    def test_unknown_release_id_filtered_out(self):
        """A release ID not in fred_releases is not included in events."""
        rd = [
            {"release_id": 999, "release_name": "Unknown Release", "date": "2026-07-28"},
            {"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-07-28"},
        ]
        http = _make_http(_make_fred_releases_response(rd))
        cfg = _make_config()

        events, _ = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        titles = [e.event for e in events]
        assert not any("Unknown" in t for t in titles), "Unknown release IDs must be filtered"
        assert any("CPI" in t for t in titles), "Known release IDs must be kept"

    def test_multiple_releases_on_same_day(self):
        """Multiple releases on the same day all appear, sorted by rank."""
        rd = [
            {"release_id": 46, "release_name": "Producer Price Index", "date": "2026-07-10"},
            {"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-07-10"},
            {"release_id": 180, "release_name": "Initial Jobless Claims", "date": "2026-07-10"},
        ]
        http = _make_http(_make_fred_releases_response(rd))
        cfg = _make_config()

        events, reason = fetch_calendar_with_status(cfg, "2026-07-10", http=http)

        assert reason is None
        assert len(events) >= 3
        # Sorted by rank ascending (strongest transmission first)
        ranks = [e.rank for e in events if e.rank is not None]
        assert ranks == sorted(ranks), "Events should be sorted by rank (strongest first)"

    def test_no_releases_in_window_is_not_degraded(self):
        """Empty release list (no releases this week) → [], reason=None (NOT degraded)."""
        rd = []  # no releases
        http = _make_http(_make_fred_releases_response(rd))
        cfg = _make_config()

        events, reason = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        # But FOMC on 2026-07-30 IS in the window — it should be injected from static list
        assert reason is None, "Empty FRED releases window must NOT be degraded"
        fomc_events = [e for e in events if "FOMC" in e.event]
        assert len(fomc_events) >= 1, "FOMC static date should still appear"


# ---------------------------------------------------------------------------
# 2. FOMC static injection
# ---------------------------------------------------------------------------
class TestFomcStaticDates:
    """Static FOMC dates are injected when they fall within the 7-day window."""

    def test_fomc_date_in_window_injected(self):
        """2026-07-30 FOMC is within window of 2026-07-28 → injected."""
        http = _make_http(_make_fred_releases_response([]))
        cfg = _make_config()

        events, reason = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        fomc = [e for e in events if "FOMC" in e.event]
        assert len(fomc) == 1, f"Expected 1 FOMC event; got {len(fomc)}"
        assert fomc[0].high_impact is True
        assert fomc[0].rank == 1  # FOMC is rank 1 (highest priority)
        # 2026-07-30 is summer (EDT, UTC-4): 14:00 ET -> 18:00 UTC -> 21:00 İst
        assert fomc[0].time == "18:00 UTC · 21:00 İst"

    def test_fomc_date_outside_window_not_injected(self):
        """2026-09-17 FOMC is outside 7-day window of 2026-07-28 → NOT injected."""
        http = _make_http(_make_fred_releases_response([]))
        cfg = _make_config()

        events, _ = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        # 2026-09-17 is > 7 days from 2026-07-28 → must not appear
        titles = [e.event for e in events]
        # Only 2026-07-30 FOMC is in window (within 6 days)
        assert len([e for e in events if "FOMC" in e.event]) == 1

    def test_fomc_today_injected(self):
        """FOMC on the brief date itself → injected (edge case: from_date == fd_date)."""
        cfg = MagicMock()
        cfg.fred_api_key.return_value = "key"
        cfg.raw = {
            "calendar": {
                "provider": "fred",
                "high_impact_events": [],
                "fred_releases": [],
                "fomc_dates": ["2026-09-17"],
            }
        }
        http = _make_http(_make_fred_releases_response([]))

        events, reason = fetch_calendar_with_status(cfg, "2026-09-17", http=http)

        fomc = [e for e in events if "FOMC" in e.event]
        assert len(fomc) == 1, "FOMC on the brief date itself should be included"

    def test_no_fomc_dates_config_no_injection(self):
        """If fomc_dates is empty in config → no FOMC events injected."""
        cfg = MagicMock()
        cfg.fred_api_key.return_value = "key"
        cfg.raw = {
            "calendar": {
                "provider": "fred",
                "high_impact_events": [],
                "fred_releases": [],
                "fomc_dates": [],
            }
        }
        http = _make_http(_make_fred_releases_response([]))

        events, _ = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        assert not any("FOMC" in e.event for e in events)


# ---------------------------------------------------------------------------
# 3. FRED provider down → degraded reason (no silent empty)
# ---------------------------------------------------------------------------
class TestFredDegradation:
    """HTTP errors from FRED → degraded reason, never a silent empty calendar."""

    def test_fred_network_failure_returns_degraded_reason(self):
        """Network error → degraded_reason 'calendar:FRED network ...' not None."""
        http = MagicMock()
        http.get.side_effect = ConnectionError("timeout")
        cfg = _make_config()

        events, reason = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        assert reason is not None, "Network failure must produce a degraded reason"
        assert "calendar" in reason.lower() or "FRED" in reason or "network" in reason.lower()
        assert events == []

    def test_fred_403_returns_degraded_reason(self):
        """HTTP 403 from FRED → degraded reason, not empty list."""
        http = _make_http({}, status_code=403)
        cfg = _make_config()

        events, reason = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        assert reason is not None
        assert "403" in reason
        assert events == []

    def test_fred_non_200_returns_degraded_reason(self):
        """HTTP 500 from FRED → degraded reason."""
        http = _make_http({}, status_code=500)
        cfg = _make_config()

        events, reason = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        assert reason is not None
        assert events == []

    def test_no_fred_key_returns_degraded_reason(self):
        """Missing FRED API key → degraded reason 'calendar:no FRED key'."""
        cfg = _make_config(fred_key=None)
        cfg.fred_api_key.return_value = None
        http = MagicMock()  # should not be called

        events, reason = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        assert reason is not None
        assert "FRED" in reason or "key" in reason.lower()
        assert events == []

    def test_fred_bad_json_returns_degraded_reason(self):
        """Malformed JSON from FRED → degraded reason, not a crash."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("invalid JSON")
        http = MagicMock()
        http.get.return_value = resp
        cfg = _make_config()

        events, reason = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        assert reason is not None
        assert events == []


# ---------------------------------------------------------------------------
# 4. Provider routing — fred provider uses FRED, not FMP/Finnhub
# ---------------------------------------------------------------------------
class TestProviderRouting:
    """provider='fred' routes to FRED fetch; dormant FMP/Finnhub paths not called."""

    def test_fred_provider_calls_fred_url(self):
        """With provider='fred', HTTP calls go to api.stlouisfed.org, not FMP."""
        rd = [{"release_id": 10, "release_name": "CPI", "date": "2026-07-28"}]
        http = _make_http(_make_fred_releases_response(rd))
        cfg = _make_config(provider="fred")

        fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        called_urls = [str(c.args[0]) for c in http.get.call_args_list if c.args]
        assert any("stlouisfed" in url for url in called_urls), \
            "FRED provider must call api.stlouisfed.org"
        assert not any("financialmodelingprep" in url for url in called_urls), \
            "FRED provider must NOT call FMP"

    def test_off_provider_returns_empty_no_call(self):
        """provider='off' → empty list, no HTTP call, no degraded reason."""
        http = MagicMock()
        cfg = MagicMock()
        cfg.raw = {"calendar": {"provider": "off", "high_impact_events": []}}

        events, reason = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        assert events == []
        assert reason is None
        http.get.assert_not_called()

    def test_fred_no_release_map_no_fred_call(self):
        """If fred_releases is empty → no FRED HTTP call for releases (only FOMC static)."""
        cfg = MagicMock()
        cfg.fred_api_key.return_value = "key"
        cfg.raw = {
            "calendar": {
                "provider": "fred",
                "high_impact_events": [],
                "fred_releases": [],       # empty map
                "fomc_dates": ["2026-07-30"],
            }
        }
        http = MagicMock()

        events, reason = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        # FOMC should be injected from static list; no HTTP call needed for empty release map
        fomc = [e for e in events if "FOMC" in e.event]
        assert len(fomc) == 1
        http.get.assert_not_called(), "Empty release_map → no HTTP call to FRED"


# ---------------------------------------------------------------------------
# 5. Integration with existing transmission ranking
# ---------------------------------------------------------------------------
class TestTransmissionRanking:
    """FRED-sourced events use the same _TRANSMISSION_RANK as FMP events."""

    def test_fomc_is_rank_1(self):
        """FOMC static injection always gets rank=1 (hardcoded in _fetch_fred)."""
        cfg = MagicMock()
        cfg.fred_api_key.return_value = "key"
        cfg.raw = {
            "calendar": {
                "provider": "fred",
                "high_impact_events": [],
                "fred_releases": [],
                "fomc_dates": ["2026-07-30"],
            }
        }
        http = MagicMock()

        events, _ = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        fomc = next(e for e in events if "FOMC" in e.event)
        assert fomc.rank == 1

    def test_cpi_event_ranked_above_ppi(self):
        """CPI (rank 2) appears before PPI (rank 7) in the sorted list."""
        cfg = _make_config()
        rd = [
            {"release_id": 46, "release_name": "PPI", "date": "2026-07-28"},
            {"release_id": 10, "release_name": "CPI", "date": "2026-07-28"},
        ]
        http = _make_http(_make_fred_releases_response(rd))

        events, _ = fetch_calendar_with_status(cfg, "2026-07-28", http=http)

        titles = [e.event for e in events if e.event]
        cpi_idx = next(i for i, t in enumerate(titles) if "CPI" in t)
        ppi_idx = next(i for i, t in enumerate(titles) if "PPI" in t)
        assert cpi_idx < ppi_idx, "CPI (rank 2) should appear before PPI (rank 7)"


# ---------------------------------------------------------------------------
# 6. Release-time resolution — ET wall-clock -> UTC (DST-aware) + Istanbul (UTC+3)
# ---------------------------------------------------------------------------
class TestReleaseTimeDST:
    """Each mapped release stamps time='HH:MM UTC · HH:MM İst'. DST correctness:
    summer EDT=UTC-4 vs winter EST=UTC-5, resolved from the release DATE. Istanbul
    is always UTC+3 (no DST). Unmapped titles keep time=None."""

    def _cfg(self, releases: list[dict], fomc: Optional[list] = None) -> MagicMock:
        cfg = MagicMock()
        cfg.fred_api_key.return_value = "key"
        cfg.fmp_api_key.return_value = None
        cfg.finnhub_api_key.return_value = None
        cfg.raw = {
            "calendar": {
                "provider": "fred",
                "high_impact_events": [],
                "fred_releases": releases,
                "fomc_dates": fomc or [],
            }
        }
        return cfg

    def test_summer_0830_release_edt(self):
        """SUMMER CPI (08:30 ET bucket): EDT=UTC-4 -> 12:30 UTC -> 15:30 İst."""
        cfg = self._cfg([{"id": 10, "name": "Consumer Price Index (CPI)"}])
        rd = [{"release_id": 10, "release_name": "CPI", "date": "2026-06-10"}]
        http = _make_http(_make_fred_releases_response(rd))

        events, reason = fetch_calendar_with_status(cfg, "2026-06-10", http=http)

        assert reason is None
        cpi = next(e for e in events if "CPI" in e.event)
        assert cpi.time == "12:30 UTC · 15:30 İst"

    def test_winter_0830_release_est(self):
        """WINTER CPI (08:30 ET bucket): EST=UTC-5 -> 13:30 UTC -> 16:30 İst.

        Same wall-clock as summer, ONE HOUR LATER in UTC — proves DST is applied
        from the release date, not hardcoded."""
        cfg = self._cfg([{"id": 10, "name": "Consumer Price Index (CPI)"}])
        rd = [{"release_id": 10, "release_name": "CPI", "date": "2026-01-13"}]
        http = _make_http(_make_fred_releases_response(rd))

        events, reason = fetch_calendar_with_status(cfg, "2026-01-13", http=http)

        assert reason is None
        cpi = next(e for e in events if "CPI" in e.event)
        assert cpi.time == "13:30 UTC · 16:30 İst"

    def test_1000_bucket_umich_summer(self):
        """SUMMER UMich (10:00 ET bucket): 10:00 EDT -> 14:00 UTC -> 17:00 İst."""
        cfg = self._cfg([{"id": 91, "name": "Surveys of Consumers (UMich sentiment)"}])
        rd = [{"release_id": 91, "release_name": "UMich", "date": "2026-06-12"}]
        http = _make_http(_make_fred_releases_response(rd))

        events, _ = fetch_calendar_with_status(cfg, "2026-06-12", http=http)

        umich = next(e for e in events if "Surveys of Consumers" in e.event)
        assert umich.time == "14:00 UTC · 17:00 İst"

    def test_1400_bucket_fomc_winter(self):
        """WINTER FOMC (14:00 ET bucket): 14:00 EST -> 19:00 UTC -> 22:00 İst."""
        cfg = self._cfg([], fomc=["2026-01-28"])
        http = _make_http(_make_fred_releases_response([]))

        events, _ = fetch_calendar_with_status(cfg, "2026-01-28", http=http)

        fomc = next(e for e in events if "FOMC" in e.event)
        assert fomc.time == "19:00 UTC · 22:00 İst"

    def test_1400_bucket_fomc_summer(self):
        """SUMMER FOMC (14:00 ET bucket): 14:00 EDT -> 18:00 UTC -> 21:00 İst."""
        cfg = self._cfg([], fomc=["2026-07-29"])
        http = _make_http(_make_fred_releases_response([]))

        events, _ = fetch_calendar_with_status(cfg, "2026-07-29", http=http)

        fomc = next(e for e in events if "FOMC" in e.event)
        assert fomc.time == "18:00 UTC · 21:00 İst"

    def test_unmapped_event_time_stays_none(self):
        """A configured release whose title matches no time substring -> time None
        (do not guess). The event itself still appears in the calendar."""
        cfg = self._cfg([{"id": 13, "name": "Industrial Production"}])
        rd = [{"release_id": 13, "release_name": "Industrial Production", "date": "2026-06-15"}]
        http = _make_http(_make_fred_releases_response(rd))

        events, _ = fetch_calendar_with_status(cfg, "2026-06-15", http=http)

        ev = next(e for e in events if "Industrial Production" in e.event)
        assert ev.time is None

    def test_config_release_times_override(self):
        """calendar.release_times (ET 'HH:MM') overrides/extends the code map, and
        is resolved with the same DST-aware UTC·İst formatting."""
        cfg = self._cfg([{"id": 13, "name": "Industrial Production"}])
        cfg.raw["calendar"]["release_times"] = {"industrial production": "09:15"}
        rd = [{"release_id": 13, "release_name": "Industrial Production", "date": "2026-06-15"}]
        http = _make_http(_make_fred_releases_response(rd))

        events, _ = fetch_calendar_with_status(cfg, "2026-06-15", http=http)

        ev = next(e for e in events if "Industrial Production" in e.event)
        # 09:15 EDT -> 13:15 UTC -> 16:15 İst
        assert ev.time == "13:15 UTC · 16:15 İst"
