"""Regression tests for the post-first-live-run hardening pass (2026-06-24).

Locks the five fixes so the live-run bugs cannot silently return:

  FIX 1  display    — a log_return tile renders its move as a PERCENT, not a
                      raw log-return rounded to 2 decimals ("+0.84%" not "+0.01").
  FIX 2  calendar   — a calendar HTTP/auth error -> a 'calendar:<reason>' degraded
                      entry, NEVER a silent empty calendar masquerading as calm.
  FIX 3  freshness  — a fetched-OK, in-window tile (is_stale=False) is NOT in
                      meta.degraded_sources; DTWEXBGS gets an H.10 multi-day window.
  FIX 4  srf        — the NY Fed SRF endpoint path is the {operationType}/{method}
                      form and the parse keeps Repo (SRF), excludes Reverse Repo.
"""

from __future__ import annotations

import copy
from pathlib import Path

import httpx
import pytest

from morning_monitor.config import Config, load_config
from morning_monitor.models import CalendarEvent, RawSeries, Staleness, Tile
from morning_monitor.render import fmt_change_by_transform

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"


# ===========================================================================
# FIX 1 — display by transform
# ===========================================================================
def test_log_return_change_renders_as_percent_not_rounded_logret():
    # The live usd_broad +0.84% move: raw log-return 0.008413 must NOT collapse to "+0.01".
    out = fmt_change_by_transform(0.008413, "log_return")
    assert out == "+0.84%"
    assert out != "+0.01"


def test_ratio_change_renders_as_percent():
    assert fmt_change_by_transform(0.012, "ratio") == "+1.20%"


def test_first_diff_change_keeps_precision_for_small_moves():
    # A yield/spread first-difference of 0.05 pct-pts must stay legible, not "+0.05"
    # truncated — sub-0.1 magnitudes get 4 decimals.
    assert fmt_change_by_transform(0.05, "first_diff") == "+0.0500"
    assert fmt_change_by_transform(0.5, "first_diff") == "+0.500"


def test_change_disp_handles_none_and_nonnumeric():
    assert fmt_change_by_transform(None, "log_return") == "—"
    assert fmt_change_by_transform("x", "level") == "—"


def test_card_metric_uses_percent_for_log_return_tile():
    """The brief card metric string (not just the HTML grid) must show the percent."""
    from morning_monitor.anomaly.engine import AnomalyResult
    from morning_monitor.brief import build_cards
    from morning_monitor.models import Composites

    tile = Tile(
        key="usd_broad", axis=6, label="Fed Broad USD (DTWEXBGS)",
        source="fred:DTWEXBGS", value=120.4, change=0.008413, transform="log_return",
        ewma_z=3.37, pct_1y=99.6, pct_3y=99.07, level_pct_756=99.0, robust_z=2.0,
        color="red", staleness=Staleness(asof="2026-06-18", lag_desc="Fed H.10 broad $", is_stale=False),
    )
    result = AnomalyResult(
        composites=Composites(ofr_fsi=None, nfci=None, anfci=None),
        tiles=[tile], corr_breaks=[], dog_didnt_bark=[], flagged_keys=["usd_broad"],
    )
    cfg = load_config(CONFIG_PATH)
    cards = build_cards(result, [], cfg)
    assert cards, "a flagged red tile with a rarity should produce a card"
    assert "+0.84%" in cards[0].metric
    assert "+0.01 " not in cards[0].metric  # the old meaningless render must be gone


# ===========================================================================
# FIX 2 — calendar: HTTP/auth error -> degraded, genuine empty -> not degraded
# ===========================================================================
class _StubResp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]


class _StubClient:
    def __init__(self, status_code: int, payload):
        self._status, self._payload = status_code, payload

    def get(self, *a, **k):
        return _StubResp(self._status, self._payload)

    def close(self):
        pass


def _cfg_with_fmp_key(key: str | None) -> Config:
    cfg = load_config(CONFIG_PATH)
    raw = copy.deepcopy(cfg.raw)
    raw.setdefault("calendar", {})["provider"] = "fmp"
    cfg2 = Config(raw=raw, config_hash=cfg.config_hash, tiles=cfg.tiles)
    cfg2._test_fmp_key = key  # type: ignore[attr-defined]

    def _fmp():
        return key
    cfg2.fmp_api_key = _fmp  # type: ignore[assignment,method-assign]

    def _none():
        return None
    cfg2.finnhub_api_key = _none  # type: ignore[assignment,method-assign]
    return cfg2


def _cfg_with_provider(provider: str) -> Config:
    cfg = load_config(CONFIG_PATH)
    raw = copy.deepcopy(cfg.raw)
    raw.setdefault("calendar", {})["provider"] = provider
    return Config(raw=raw, config_hash=cfg.config_hash, tiles=cfg.tiles)


@pytest.mark.parametrize("provider", ["off", "none", "disabled", ""])
def test_calendar_off_yields_empty_and_no_degraded(provider):
    """Calendar OFF (TradingView/SPEC-1 owns it): immediate ([], None) — no HTTP,
    no degraded entry. The daily 'calendar:FMP 403' degrade must not appear."""
    from morning_monitor.sources.calendar import fetch_calendar_with_status

    cfg = _cfg_with_provider(provider)

    class _BoomClient:
        def get(self, *a, **k):
            raise AssertionError("calendar OFF must make NO HTTP call")

        def close(self):
            pass

    events, reason = fetch_calendar_with_status(cfg, "2026-06-24", http=_BoomClient())
    assert events == []
    assert reason is None


def test_calendar_default_config_is_fred():
    """SPEC-3: the shipped config.yaml uses provider='fred' for the economic calendar.

    FRED network errors degrade gracefully (no exception; reason is set).
    The provider is no longer 'off' — calendar is fully wired to FRED release-dates.
    """
    from morning_monitor.sources.calendar import fetch_calendar_with_status

    cfg = load_config(CONFIG_PATH)
    provider = str(cfg.raw.get("calendar", {}).get("provider", "")).strip().lower()
    assert provider == "fred", \
        f"SPEC-3 requires calendar.provider='fred', got '{provider}'"

    class _BoomClient:
        """Simulates FRED being unreachable — must degrade, not crash."""
        def get(self, *a, **k):
            raise ConnectionError("FRED unreachable (test isolation)")

        def close(self):
            pass

    # With FRED unreachable, calendar degrades honestly — empty list + degraded reason.
    events, reason = fetch_calendar_with_status(cfg, "2026-06-24", http=_BoomClient())
    assert events == [], "FRED network failure must yield empty events"
    assert reason is not None, "FRED network failure must set a degraded reason (no silent swallow)"
    assert "calendar" in reason.lower() or "FRED" in reason


def test_calendar_no_key_is_degraded_not_silent_empty():
    from morning_monitor.sources.calendar import fetch_calendar_with_status

    cfg = _cfg_with_fmp_key(None)
    events, reason = fetch_calendar_with_status(cfg, "2026-06-24", http=_StubClient(200, []))
    assert events == []
    assert reason is not None and "calendar:" in reason and "FMP key" in reason


def test_calendar_403_is_degraded_with_reason():
    from morning_monitor.sources.calendar import fetch_calendar_with_status

    cfg = _cfg_with_fmp_key("present")
    events, reason = fetch_calendar_with_status(cfg, "2026-06-24", http=_StubClient(403, {}))
    assert events == []
    assert reason is not None and "FMP 403" in reason


def test_calendar_genuine_empty_200_is_not_degraded():
    """A real empty calendar (no releases today) is NOT degraded — calm, not failed."""
    from morning_monitor.sources.calendar import fetch_calendar_with_status

    cfg = _cfg_with_fmp_key("present")
    events, reason = fetch_calendar_with_status(cfg, "2026-06-24", http=_StubClient(200, []))
    assert events == []
    assert reason is None


def test_calendar_parses_fmp_fields_and_ranks():
    from morning_monitor.sources.calendar import fetch_calendar_with_status

    cfg = _cfg_with_fmp_key("present")
    payload = [
        {"event": "CPI YoY", "date": "2026-06-24 12:30:00", "country": "US",
         "actual": None, "estimate": 3.1, "impact": "High", "previous": 3.0},
        {"event": "Some minor PMI", "date": "2026-06-24", "country": "DE",
         "estimate": 50.0, "impact": "Low", "previous": 49.5},
    ]
    events, reason = fetch_calendar_with_status(cfg, "2026-06-24", http=_StubClient(200, payload))
    assert reason is None
    assert len(events) == 2
    # CPI (rank 2, high impact) sorts ahead of the minor PMI.
    assert events[0].event == "CPI YoY"
    assert events[0].high_impact is True
    assert events[0].consensus == 3.1


def test_calendar_fmp_error_object_is_degraded():
    """FMP returns {'Error Message': ...} on a rejected/paid key -> degraded, not empty-calm."""
    from morning_monitor.sources.calendar import fetch_calendar_with_status

    cfg = _cfg_with_fmp_key("badkey")
    payload = {"Error Message": "Invalid API KEY."}
    events, reason = fetch_calendar_with_status(cfg, "2026-06-24", http=_StubClient(200, payload))
    assert events == []
    assert reason is not None and "FMP" in reason


# ===========================================================================
# FIX 3 — freshness / degraded alignment
# ===========================================================================
class _FailingClient:
    def get(self, *a, **k):
        raise RuntimeError("network disabled in test")

    def close(self):
        pass


def test_dtwexbgs_has_lagged_window_and_label():
    from morning_monitor.sources.ingest import (
        _fred_lag_desc,
        _freshness_window,
        compute_staleness,
    )

    assert _fred_lag_desc("DTWEXBGS") != "FRED daily"
    assert "H.10" in _fred_lag_desc("DTWEXBGS")
    win = _freshness_window("usd_broad", "fred:DTWEXBGS")
    assert win >= 7, "DTWEXBGS (H.10 broad, multi-day lag) needs a ~7-8d freshness window"
    # A 6-day-old broad-$ obs is NOT stale under the H.10 window...
    assert compute_staleness("2026-06-18", "Fed H.10 broad $", win, today="2026-06-24") is False
    # ...while a genuinely daily FRED series at 6 days old still flags.
    daily_win = _freshness_window("ust_2y", "fred:DGS2")
    assert compute_staleness("2026-06-18", "FRED daily", daily_win, today="2026-06-24") is True


def test_non_stale_okay_series_not_in_degraded_and_stamped(monkeypatch):
    """A fetched-OK, in-window tile must have is_stale=False AND be absent from the
    degraded list — the two must agree (the live usd_broad disagreement bug)."""
    import importlib

    from morning_monitor.sources import fred as fred_mod

    ingest_mod = importlib.import_module("morning_monitor.sources.ingest")

    # Build a fresh-enough usd_broad RawSeries regardless of the wall clock by using
    # today's date as asof, returned from a stubbed FRED fetcher.
    from datetime import date as date_cls
    today = date_cls.today().isoformat()

    def _fake_fred(series_id, *, api_key, base_url, http, years=3, lag_desc="FRED", tile_key=None):
        from morning_monitor.models import HistoryPoint
        hist = [HistoryPoint(date=today, value=120.0 + i * 0.1) for i in range(5)]
        return RawSeries(key=tile_key or f"fred:{series_id}", source=f"fred:{series_id}",
                         history=hist, asof=today, lag_desc=lag_desc, ok=True, error=None)

    monkeypatch.setattr(ingest_mod, "fetch_fred_series", _fake_fred)

    cfg = load_config(CONFIG_PATH)
    series, degraded = ingest_mod.ingest(cfg, http=_FailingClient())

    usd = series.get("usd_broad")
    assert usd is not None and usd.ok
    assert usd.is_stale is False, "ingest must stamp is_stale onto the RawSeries"
    assert "usd_broad" not in degraded, "a non-stale OK tile must NOT be degraded"

    # And the anomaly engine reflects that stamped flag on the tile's Staleness.
    from morning_monitor.anomaly.engine import _staleness_from
    assert _staleness_from(usd, 0.94).is_stale is False


# ===========================================================================
# FIX 4 — NY Fed SRF endpoint + Repo-only filter
# ===========================================================================
def test_srf_endpoint_uses_operationtype_method_path():
    from morning_monitor.sources import nyfed

    # The fixed path has the {operationType}/{method} double segment; the broken
    # one (/api/rp/all/results/) returned HTTP 400.
    assert "/api/rp/all/all/results/" in nyfed.NYFED_REPO_URL


def test_srf_parse_keeps_repo_excludes_reverse_repo():
    from morning_monitor.sources.nyfed import fetch_srf_takeup

    class _RepoClient:
        def get(self, *a, **k):
            return _StubResp(200, {"repo": {"operations": [
                {"operationType": "Repo", "operationDate": "2026-06-17",
                 "totalAmtAccepted": 2_000_000, "operationMethod": "Full Allotment"},
                {"operationType": "Reverse Repo", "operationDate": "2026-06-17",
                 "totalAmtAccepted": 6_484_000_000, "operationMethod": "Full Allotment"},
                {"operationType": "Repo", "operationDate": "2026-06-18",
                 "totalAmtAccepted": 1_000_000, "operationMethod": "Full Allotment"},
            ]}})

        def close(self):
            pass

    rs = fetch_srf_takeup(http=_RepoClient(), years=3)
    assert rs.ok
    # Reverse Repo ($6.484B) must be EXCLUDED — only the two Repo (SRF) dates remain.
    dates = {h.date for h in rs.history}
    assert dates == {"2026-06-17", "2026-06-18"}
    by_date = {h.date: h.value for h in rs.history}
    # 2M / 1e9 = 0.002bn; 1M / 1e9 = 0.001bn — NOT swamped by the 6.484bn RRP.
    assert by_date["2026-06-17"] == pytest.approx(0.002)
    assert by_date["2026-06-18"] == pytest.approx(0.001)
