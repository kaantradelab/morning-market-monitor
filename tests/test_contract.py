"""Contract tests — these run GREEN today against the skeleton (no NotImplementedError
paths exercised). They lock the spine so the 5 build agents cannot drift the contract.
"""

from __future__ import annotations

import json

import pytest

from morning_monitor.models import (
    Brief,
    CalendarEvent,
    Card,
    Composite,
    Composites,
    Meta,
    RawSeries,
    Staleness,
    Tile,
    WhyNow,
)


def test_schema_is_valid_json(brief_schema):
    assert brief_schema["title"] == "MorningBrief"
    assert brief_schema["properties"]["schema_version"]["const"] == "1.0.0"
    # The spine's top-level required keys.
    required = set(brief_schema["required"])
    assert {
        "meta", "composites", "tiles", "cards", "calendar",
        "plumbing_flags", "corr_breaks", "dog_didnt_bark",
    } <= required


def test_fixture_loads_as_rawseries(sample_run):
    """Every fixture series round-trips through RawSeries.from_dict/to_dict."""
    assert sample_run["date"] == "2026-06-24"
    series = sample_run["series"]
    assert "ofr_fsi" in series and "hy_oas" in series and "sofr_iorb" in series
    for key, raw in series.items():
        rs = RawSeries.from_dict(raw)
        assert rs.key == key
        assert rs.to_dict()["source"] == raw["source"]
    # Fixture includes a degraded tile for graceful-degradation tests.
    degraded = [k for k, v in series.items() if not v["ok"]]
    assert degraded, "fixture should include at least one ok=False tile"


def test_fixture_calendar_loads(sample_run):
    events = [CalendarEvent.from_dict(e) for e in sample_run["calendar"]]
    assert any(e.high_impact for e in events)
    cpi = next(e for e in events if e.event.startswith("US CPI"))
    assert cpi.consensus == 0.2


def _minimal_brief() -> Brief:
    stale = Staleness(asof="2026-06-23", lag_desc="EOD delayed", is_stale=False)
    tile = Tile(
        key="vix", axis=1, label="VIX", source="yfinance:^VIX", value=18.6,
        change=4.2, transform="first_diff", ewma_z=3.1, pct_1y=99.0, pct_3y=98.0,
        level_pct_756=72.0, robust_z=3.4, color="red", staleness=stale,
        history=[], is_front_screen=True, note=None,
    )
    comp = Composite(value=-0.40, level_pct=60.0, change_score=2.1)
    card = Card(
        title="Vol spike", metric="VIX +4.2 (^VIX)", score_desc="3.1-sigma, 99th pct",
        why_now=WhyNow(percentile_or_z="99th pct / 3.1-sigma", calendar_event="US CPI",
                       cross_asset_confirm_or_contradict="confirmed by HY OAS widening"),
        color="red", is_banner=False, tile_keys=["vix", "hy_oas"],
    )
    return Brief(
        meta=Meta(date="2026-06-24", run_ts_utc="2026-06-24T06:00:00Z", config_hash="abc123def456"),
        composites=Composites(ofr_fsi=comp, nfci=None, anfci=None),
        tiles=[tile], cards=[card], calendar=[], plumbing_flags=[],
        corr_breaks=[], dog_didnt_bark=[],
    )


def test_brief_roundtrips_to_dict_and_back():
    brief = _minimal_brief()
    d = brief.to_dict()
    assert d["schema_version"] == "1.0.0"
    again = Brief.from_dict(d)
    assert again.to_dict() == d
    # JSON-serializable.
    assert json.loads(json.dumps(d))["meta"]["date"] == "2026-06-24"


def test_card_why_now_is_mandatory_shape():
    """Every card carries the three why_now fields (decorative-noise suppression rule)."""
    brief = _minimal_brief()
    why = brief.cards[0].to_dict()["why_now"]
    assert set(why) == {"percentile_or_z", "calendar_event", "cross_asset_confirm_or_contradict"}


@pytest.mark.parametrize("color", ["green", "amber", "red", "gray"])
def test_color_enum_values(color):
    assert color in {"green", "amber", "red", "gray"}
