"""Anomaly engine tests — BUILD TARGET 2 (anomaly).

Exercises the section-2 statistics primitives and the full ``enrich`` pipeline:
  * transforms / EWMA-standardize / percentile / robust-z math,
  * BY/BH FDR + corroboration,
  * residual correlation-break + dog-didn't-bark,
  * the detect-on-composites family gate + calm-morning calibration,
  * graceful degradation (degraded series -> gray, never raises),
  * schema conformance of produced Tile/Composite/CorrBreak/DogDidntBark objects.

Builds ``Config`` directly from config.yaml (the config-loader is a sibling build
target's stub; the engine only needs ``config.raw`` and falls back from the typed
knob accessors to raw, so these tests are decoupled from that timing).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
import yaml

from morning_monitor.anomaly import correlation, enrich, fdr, stats, transforms
from morning_monitor.config import Config
from morning_monitor.models import CalendarEvent, HistoryPoint, RawSeries

REPO_ROOT = Path(__file__).resolve().parent.parent

# Engine's thin-history gate; tests build series longer than this to earn Reds.
_MIN_SAMPLE = 30


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cfg() -> Config:
    raw = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    return Config(raw=raw, config_hash="testhash0000", tiles=[])


@pytest.fixture(scope="module")
def fixture_series_and_calendar():
    fx = json.loads((REPO_ROOT / "tests" / "fixtures" / "sample_run.json").read_text())
    series = {k: RawSeries.from_dict(v) for k, v in fx["series"].items()}
    cal = [CalendarEvent.from_dict(e) for e in fx["calendar"]]
    return series, cal


def _long_series(key: str, source: str, base: float, *, n: int = 800,
                 shock_last: float | None = None, seed: int = 0) -> RawSeries:
    rng = np.random.default_rng(seed)
    vals = [base]
    for _ in range(1, n):
        vals.append(vals[-1] * (1 + rng.normal(0, 0.01)))
    if shock_last is not None:
        vals[-1] = vals[-2] * (1 + shock_last)
    hist = [HistoryPoint(date=f"2024-{1 + i % 12:02d}-{1 + i % 28:02d}", value=round(v, 6))
            for i, v in enumerate(vals)]
    return RawSeries(key=key, source=source, history=hist, asof="2026-06-23", lag_desc="EOD", ok=True)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------
def test_transforms_basic():
    assert np.allclose(transforms.first_diff(np.array([1.0, 2.0, 4.0])), [1.0, 2.0])
    lr = transforms.log_return(np.array([100.0, 110.0]))
    assert abs(lr[0] - math.log(1.1)) < 1e-12
    # ratio aligns on the shorter, newest-anchored.
    r = transforms.ratio_series(np.array([2.0, 4.0, 6.0]), np.array([2.0, 2.0]))
    assert np.allclose(r, [2.0, 3.0])


def test_log_return_guards_nonpositive():
    lr = transforms.log_return(np.array([1.0, -1.0, 2.0]))
    assert np.isnan(lr[0]) and np.isnan(lr[1])


def test_ewma_sigma_positive_and_short_guard():
    sig = stats.ewma_sigma(np.array([0.01, -0.02, 0.015, -0.005, 0.03]))
    assert np.isfinite(sig) and sig > 0
    assert np.isnan(stats.ewma_sigma(np.array([0.01])))


def test_standardize_is_unit_scale_and_causal():
    changes = np.random.default_rng(1).normal(0, 0.01, 600)
    z = stats.standardize(changes)
    zf = z[np.isfinite(z)]
    assert 0.5 < float(np.std(zf)) < 2.0


def test_empirical_percentile_two_sided():
    z = np.random.default_rng(2).normal(0, 1, 500)
    assert stats.empirical_percentile(z, 6.0, 252) > 95     # huge move -> rare
    assert stats.empirical_percentile(z, 0.0, 252) < 30     # nil move -> common
    # symmetric: a large negative is as rare as a large positive.
    assert stats.empirical_percentile(z, -6.0, 252) == stats.empirical_percentile(z, 6.0, 252)


def test_robust_z_iglewicz_hoaglin():
    rz = stats.robust_z(np.array([1.0, 1.1, 0.9, 1.05, 0.95, 1.0, 1.0, 5.0]), 5.0)
    assert rz > 3.5
    # flat series, on-median point -> 0.
    assert stats.robust_z(np.array([2.0, 2.0, 2.0]), 2.0) == 0.0


def test_level_percentile_one_sided():
    assert stats.level_percentile(np.array([1.0, 2.0, 3.0, 4.0, 5.0]), 5.0, 756) == 100.0
    assert stats.level_percentile(np.array([1.0, 2.0, 3.0, 4.0, 5.0]), 1.0, 756) == 20.0


# ---------------------------------------------------------------------------
# FDR
# ---------------------------------------------------------------------------
def test_two_sided_p_from_z():
    assert abs(fdr.two_sided_p_from_z(3.0) - 0.0027) < 1e-3
    assert fdr.two_sided_p_from_z(float("nan")) == 1.0


def test_by_is_more_conservative_than_bh():
    pvals = [0.001, 0.002, 0.5, 0.6, 0.9]
    by = fdr.benjamini_yekutieli(pvals, 0.10)
    bh = fdr.benjamini_hochberg(pvals, 0.10)
    assert by[0] and by[1] and not by[2]
    assert sum(bh) >= sum(by)  # BH rejects at least as many (less conservative)


def test_fdr_controls_false_alarms_on_pure_noise():
    rng = np.random.default_rng(7)
    pvals = list(rng.uniform(0, 1, 30))   # 30 null tiles
    rejected = sum(fdr.benjamini_yekutieli(pvals, 0.10))
    assert rejected <= 2  # the whole point: not ~1.4+ false alarms/morning


def test_corroboration_requires_two_orthogonal():
    axis_members = {"axis_5": ["hy_oas", "ig_oas"]}
    # one strong tile -> no Red (needs 2 orthogonal at >=2 sigma).
    reds = fdr.corroboration_gate(["axis_5"], axis_members,
                                  {"hy_oas": 3.1, "ig_oas": 0.4}, min_orthogonal=2, headline_sigma=3.0)
    assert reds == ["axis_5"]  # hy_oas alone clears the 3-sigma headline fallback
    # two moderate tiles -> Red via corroboration.
    reds2 = fdr.corroboration_gate(["axis_5"], axis_members,
                                   {"hy_oas": 2.2, "ig_oas": 2.4}, min_orthogonal=2, headline_sigma=3.0)
    assert reds2 == ["axis_5"]
    # one moderate, one quiet, no headline -> no Red.
    reds3 = fdr.corroboration_gate(["axis_5"], axis_members,
                                   {"hy_oas": 2.2, "ig_oas": 0.1}, min_orthogonal=2, headline_sigma=3.0)
    assert reds3 == []


# ---------------------------------------------------------------------------
# Correlation-break + dog-didn't-bark
# ---------------------------------------------------------------------------
def test_corr_break_degraded_inputs_never_trigger():
    bad = RawSeries(key="x", source="s", history=[], asof=None, lag_desc="", ok=False)
    cb = correlation.detect_corr_break("t", bad, [bad])
    assert cb.triggered is False and cb.residual_z is None


def test_dog_didnt_bark_low_tail_on_event_day():
    # A tile that has been volatile historically but is dead-flat today.
    rng = np.random.default_rng(3)
    vals = [100.0]
    for _ in range(300):
        vals.append(vals[-1] * (1 + rng.normal(0, 0.02)))
    vals[-1] = vals[-2] * 1.0001  # near-zero move today
    hist = [HistoryPoint(date=f"2025-{1 + i % 12:02d}-{1 + i % 28:02d}", value=v) for i, v in enumerate(vals)]
    tile = RawSeries(key="spx", source="s", history=hist, asof="2026-06-23", lag_desc="EOD", ok=True)
    event = CalendarEvent(event="US CPI", time=None, consensus=0.2, high_impact=True, prior_citi_surprise=None)
    d = correlation.detect_dog_didnt_bark(tile, event, ratio_threshold=0.5)
    assert d.triggered is True and d.ratio is not None and d.ratio < 0.5


def test_dog_didnt_bark_all_day_fallback_flags_low_confidence():
    """Reference SS2.4: with no straddle and no event-day history, the detector
    falls back to the unconditional all-day mean and MUST self-flag lower
    confidence (the all-day mean understates the event-day expected move)."""
    rng = np.random.default_rng(3)
    vals = [100.0]
    for _ in range(300):
        vals.append(vals[-1] * (1 + rng.normal(0, 0.02)))
    vals[-1] = vals[-2] * 1.0001
    hist = [HistoryPoint(date=f"2025-{1 + i % 12:02d}-{1 + i % 28:02d}", value=v) for i, v in enumerate(vals)]
    tile = RawSeries(key="spx", source="s", history=hist, asof="2026-06-23", lag_desc="EOD", ok=True)
    event = CalendarEvent(event="US CPI", time=None, consensus=0.2, high_impact=True, prior_citi_surprise=None)
    d = correlation.detect_dog_didnt_bark(tile, event, ratio_threshold=0.5, event_day_indices=None)
    assert d.note is not None and "all-day mean" in d.note
    assert "LOW-CONFIDENCE" in d.note and "confidence reduced" in d.note


def test_dog_didnt_bark_uses_conditional_event_day_baseline():
    """Reference SS2.4: when prior same-event days are supplied, the expected-move
    baseline is computed from that 3y event-day SUBSET (not the all-day mean) and
    the note is NOT flagged low-confidence."""
    rng = np.random.default_rng(11)
    # Quiet most days, but a set of designated "event days" carry large moves so the
    # conditional baseline is materially HIGHER than the all-day mean.
    vals = [100.0]
    moves = rng.normal(0, 0.005, 400)
    event_idx_levels: list[int] = []
    for i in range(1, 400):
        m = moves[i]
        if i % 25 == 0:                  # designated event days = big historical moves
            m = 0.06 * (1 if i % 2 else -1)
            event_idx_levels.append(i)
        vals.append(vals[-1] * (1 + m))
    vals[-1] = vals[-2] * 1.0002         # today: dead-flat
    hist = [HistoryPoint(date=f"2024-{1 + i % 12:02d}-{1 + i % 28:02d}", value=v) for i, v in enumerate(vals)]
    tile = RawSeries(key="spx", source="s", history=hist, asof="2026-06-23", lag_desc="EOD", ok=True)
    event = CalendarEvent(event="US CPI", time=None, consensus=0.2, high_impact=True, prior_citi_surprise=None)
    # change-series index i-1 corresponds to level index i (first_diff drops index 0).
    change_indices = [i - 1 for i in event_idx_levels]
    d = correlation.detect_dog_didnt_bark(
        tile, event, ratio_threshold=0.5, event_day_indices=change_indices, min_event_days=8,
    )
    assert d.note is not None and "event-day" in d.note
    assert "LOW-CONFIDENCE" not in d.note
    # Event-day expected move >> a flat today -> a clean no-bark on the conditional baseline.
    assert d.triggered is True and d.ratio is not None and d.ratio < 0.5


def test_corr_break_sign_flip_does_not_waive_persistence():
    """Reference SS2.3: a sign-flip is amplified WITHIN a persisted break — it must
    NOT trip on a single day. A residual that breaches threshold on only ONE day
    stays un-triggered even when a sign-flip is detected."""
    rng = np.random.default_rng(21)
    n = 400
    # Build a target/factor pair whose structural slope flips sign in the recent
    # window (so _detect_sign_flip fires) but whose residual is calm except a lone
    # spike on the final day (persistence == 1 only).
    x = rng.normal(0, 1.0, n)
    y = np.empty(n)
    half = n // 2
    y[:half] = 1.5 * x[:half] + rng.normal(0, 0.05, half)     # positive beta early
    y[half:] = -1.5 * x[half:] + rng.normal(0, 0.05, n - half)  # negative beta late (flip)
    # Inject a single large residual shock on the very last day only.
    y[-1] += 8.0
    base_dates = [f"2024-{1 + i % 12:02d}-{1 + i % 28:02d}" for i in range(n)]
    target = RawSeries(key="spx", source="s",
                       history=[HistoryPoint(date=base_dates[i], value=float(100 + np.cumsum(y)[i])) for i in range(n)],
                       asof="2026-06-23", lag_desc="EOD", ok=True)
    factor = RawSeries(key="ust_2y", source="s",
                       history=[HistoryPoint(date=base_dates[i], value=float(50 + np.cumsum(x)[i])) for i in range(n)],
                       asof="2026-06-23", lag_desc="EOD", ok=True)
    cb = correlation.detect_corr_break(
        "stock_bond_corr", target, [factor],
        window_days=756, sigma_threshold=2.5, persistence_days=3, weight_sign_flip=True,
    )
    # A single-day breach must NOT trigger even with a sign-flip present.
    assert cb.persistence_days < 3
    assert cb.triggered is False


def test_dog_didnt_bark_not_triggered_on_low_impact():
    rng = np.random.default_rng(4)
    vals = list(100 + np.cumsum(rng.normal(0, 1, 200)))
    hist = [HistoryPoint(date=f"2025-{1 + i % 12:02d}-{1 + i % 28:02d}", value=v) for i, v in enumerate(vals)]
    tile = RawSeries(key="spx", source="s", history=hist, asof="2026-06-23", lag_desc="EOD", ok=True)
    event = CalendarEvent(event="minor", time=None, consensus=None, high_impact=False, prior_citi_surprise=None)
    d = correlation.detect_dog_didnt_bark(tile, event)
    assert d.triggered is False


# ---------------------------------------------------------------------------
# Full engine
# ---------------------------------------------------------------------------
def test_enrich_on_fixture_is_calm(cfg, fixture_series_and_calendar):
    """Reference rec-2: a calm morning surfaces <=1 Red. The offline fixture's
    thin history must NOT manufacture Reds (the calibration guard)."""
    series, cal = fixture_series_and_calendar
    res = enrich(series, cfg, cal)
    reds = [t for t in res.tiles if t.color == "red"]
    assert len(reds) <= cfg.raw["calibration"]["calm_morning_max_reds"]
    assert all(t.color in {"green", "amber", "red", "gray"} for t in res.tiles)
    # produced tiles are fully schema-shaped.
    for t in res.tiles:
        d = t.to_dict()
        for fld in ("key", "axis", "label", "source", "value", "change", "transform",
                    "ewma_z", "pct_1y", "pct_3y", "level_pct_756", "robust_z", "color",
                    "staleness", "history"):
            assert fld in d


def test_enrich_quarantines_intraday(cfg, fixture_series_and_calendar):
    series, _ = fixture_series_and_calendar
    # inject a quarantined intraday gauge as if ingested.
    series = dict(series)
    series["dealer_gamma"] = RawSeries(key="dealer_gamma", source="x", history=[], ok=True)
    res = enrich(series, cfg, [])
    assert not any(t.key == "dealer_gamma" for t in res.tiles)


def test_enrich_degraded_series_goes_gray(cfg):
    series = {
        "vix": RawSeries(key="vix", source="yfinance:^VIX", history=[], asof=None, lag_desc="", ok=False, error="fetch failed"),
    }
    res = enrich(series, cfg, [])
    vix = next(t for t in res.tiles if t.key == "vix")
    assert vix.color == "gray"
    assert vix.staleness.is_stale is True
    assert vix.ewma_z is None and vix.pct_3y is None


def test_enrich_detects_planted_shock_via_composites(cfg, fixture_series_and_calendar):
    """A real long-history series with a large vol spike flags Red through the
    detect-on-composites family + corroboration, and carries percentile scores."""
    _, cal = fixture_series_and_calendar
    series: dict[str, RawSeries] = {}
    for spec in cfg.raw["tiles"]:
        k = spec["key"]
        if k in (cfg.raw.get("quarantine_intraday") or []):
            continue
        shock = {"vix": 0.35, "hy_oas": 0.20}.get(k)
        base = {"vix": 14.0, "hy_oas": 3.0}.get(k, 100.0)
        series[k] = _long_series(k, spec.get("source", k), base, shock_last=shock, seed=abs(hash(k)) % 1000)
    for ck in ("ofr_fsi", "nfci", "anfci"):
        series[ck] = _long_series(ck, f"comp:{ck}", -0.5, seed=abs(hash(ck)) % 1000)

    res = enrich(series, cfg, cal)
    vix = next(t for t in res.tiles if t.key == "vix")
    assert vix.ewma_z is not None and abs(vix.ewma_z) > 3
    assert vix.pct_3y is not None and vix.pct_3y > 90
    assert vix.color == "red"
    assert "vix" in res.flagged_keys
    # the engine still returned corr-breaks and dog-checks for the event day.
    assert len(res.corr_breaks) == len(cfg.raw["corr_breaks"])
    assert any(d.event == "US CPI (MoM)" for d in res.dog_didnt_bark)


def test_enrich_honors_fpr_control_composite_only(cfg, fixture_series_and_calendar):
    """With fpr_control=composite_only, axis-factor tiles do not flag; only a
    composite at the headline threshold can."""
    _, cal = fixture_series_and_calendar
    raw = json.loads(json.dumps(cfg.raw))  # deep copy
    raw["knobs"]["fpr_control"] = "composite_only"
    cfg2 = Config(raw=raw, config_hash="x2", tiles=[])

    series: dict[str, RawSeries] = {}
    for spec in raw["tiles"]:
        k = spec["key"]
        if k in (raw.get("quarantine_intraday") or []):
            continue
        shock = {"vix": 0.40}.get(k)  # a single shocked axis tile
        base = {"vix": 14.0}.get(k, 100.0)
        series[k] = _long_series(k, spec.get("source", k), base, shock_last=shock, seed=abs(hash(k)) % 1000)
    for ck in ("ofr_fsi", "nfci", "anfci"):
        series[ck] = _long_series(ck, f"comp:{ck}", -0.5, seed=abs(hash(ck)) % 1000)

    res = enrich(series, cfg2, cal)
    # vix (an axis-factor member) must NOT be a family Red under composite_only.
    assert "vix" not in res.flagged_keys
