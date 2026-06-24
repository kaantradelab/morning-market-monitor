"""Validate that a models.Brief.to_dict() conforms to schema/brief.schema.json.

Skipped if jsonschema is unavailable (it is a test-only dep). Build agents rely on
this to confirm every brief they emit is schema-valid before it is committed.
"""

from __future__ import annotations

import pytest

jsonschema = pytest.importorskip("jsonschema")

from morning_monitor.models import (  # noqa: E402
    Brief,
    Card,
    Composite,
    Composites,
    Meta,
    Staleness,
    Tile,
    WhyNow,
)


def _full_brief() -> Brief:
    stale = Staleness(asof="2026-06-23", lag_desc="EOD delayed", is_stale=False)
    tile = Tile(
        key="hy_oas", axis=5, label="HY OAS", source="fred:BAMLH0A0HYM2", value=3.40,
        change=0.28, transform="first_diff", ewma_z=3.2, pct_1y=98.0, pct_3y=97.0,
        level_pct_756=64.0, robust_z=3.6, color="red", staleness=stale, history=[],
        is_front_screen=True, note="OAS not ETF",
    )
    comp = Composite(value=-0.40, level_pct=60.0, change_score=2.1, color="amber",
                     staleness=stale, history=[])
    card = Card(
        title="HY credit widening", metric="HY OAS +28bp", score_desc="3.2-sigma, 97th pct",
        why_now=WhyNow(percentile_or_z="97th pct / 3.2-sigma", calendar_event="US CPI",
                       cross_asset_confirm_or_contradict="confirmed by VIX backwardation"),
        color="red", is_banner=False, tile_keys=["hy_oas", "vix"],
    )
    return Brief(
        meta=Meta(date="2026-06-24", run_ts_utc="2026-06-24T06:00:00Z",
                  config_hash="abc123def456", vol_model="ewma", percentile_window="3y",
                  fpr_control="fdr", calm_morning=True, degraded_sources=["move_proxy"]),
        composites=Composites(ofr_fsi=comp, nfci=None, anfci=None),
        tiles=[tile], cards=[card], calendar=[], plumbing_flags=[],
        corr_breaks=[], dog_didnt_bark=[],
    )


def test_full_brief_validates(brief_schema):
    jsonschema.validate(instance=_full_brief().to_dict(), schema=brief_schema)


def test_card_without_why_now_fails(brief_schema):
    bad = _full_brief().to_dict()
    del bad["cards"][0]["why_now"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=brief_schema)


def test_bad_color_fails(brief_schema):
    bad = _full_brief().to_dict()
    bad["tiles"][0]["color"] = "purple"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=brief_schema)
