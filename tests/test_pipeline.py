"""Integration + math tests for the wired pipeline (INTEGRATOR target).

Two layers:
  1. Anomaly-math unit tests on KNOWN inputs — EWMA-z, percentile ordering,
     robust-z, BY-FDR — locking the numeric contracts the engine depends on.
  2. Full-pipeline smoke test — config.load_config + main.run on the offline
     fixture, asserting a schema-valid Brief JSON on disk and non-empty HTML.

All offline: no API keys, no network (the fixture path bypasses ingestion).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import jsonschema
import numpy as np
import pytest

from morning_monitor import main as main_mod
from morning_monitor.anomaly import fdr, stats, transforms
from morning_monitor.config import Config, compute_config_hash, load_config
from morning_monitor.models import Brief

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "sample_run.json"
SCHEMA_PATH = REPO_ROOT / "schema" / "brief.schema.json"


# ===========================================================================
# 1. Anomaly math on KNOWN inputs
# ===========================================================================
def test_ewma_z_known_constant_volatility():
    """On an i.i.d. unit-variance change series, the EWMA-standardized series has
    ~unit scale and the latest z of a +5-sigma shock is large and positive."""
    rng = np.random.default_rng(11)
    changes = rng.normal(0.0, 1.0, 500)
    z = stats.standardize(changes, lam=0.94)
    zf = z[np.isfinite(z)]
    # Standardizing unit-variance changes by their own EWMA-vol -> ~unit scale.
    assert 0.7 < float(np.std(zf)) < 1.4
    # A planted shock at the end standardizes to a large z.
    shocked = np.append(changes, 5.0)
    z2 = stats.standardize(shocked, lam=0.94)
    assert z2[-1] > 3.0


def test_ewma_sigma_matches_hand_computed_recursion():
    """ewma_sigma must follow var_t = lam*var_{t-1} + (1-lam)*x_{t-1}^2 exactly."""
    x = np.array([0.02, -0.01, 0.015, -0.03])
    lam = 0.94
    var = float(np.mean(x**2))  # the implementation's seed
    for prev in x[:-1]:
        var = lam * var + (1.0 - lam) * (prev * prev)
    expected = math.sqrt(var)
    assert abs(stats.ewma_sigma(x, lam=lam) - expected) < 1e-12


def test_percentile_ordering_is_monotone_in_magnitude():
    """Empirical percentile is monotone non-decreasing in |latest| (two-sided)."""
    z = np.random.default_rng(12).normal(0, 1, 1000)
    p_small = stats.empirical_percentile(z, 0.2, 252)
    p_mid = stats.empirical_percentile(z, 1.5, 252)
    p_big = stats.empirical_percentile(z, 4.0, 252)
    assert p_small <= p_mid <= p_big
    assert p_big > 95.0           # a 4-sigma move is rare
    # Two-sided: sign does not matter, only magnitude.
    assert stats.empirical_percentile(z, -3.0, 252) == stats.empirical_percentile(z, 3.0, 252)


def test_robust_z_flags_known_outlier():
    """A single 5.0 in a cluster around 1.0 -> robust-z well past the 3.5 flag."""
    series = np.array([1.0, 1.1, 0.9, 1.05, 0.95, 1.0, 0.98, 1.02, 5.0])
    rz = stats.robust_z(series, 5.0)
    assert rz > 3.5
    # On-median point in a flat series -> exactly 0.0 (no scale).
    assert stats.robust_z(np.array([2.0, 2.0, 2.0, 2.0]), 2.0) == 0.0


def test_by_fdr_controls_false_alarms_and_finds_true_signal():
    """BY-FDR: pure-noise p-values -> almost no rejections; two tiny p-values among
    noise -> exactly those two rejected. BH is no more conservative than BY."""
    # 30 uniform null p-values: the multiple-comparisons trap. BY must not flag ~1.4.
    rng = np.random.default_rng(99)
    null_p = list(rng.uniform(0, 1, 30))
    assert sum(fdr.benjamini_yekutieli(null_p, 0.10)) <= 2

    # Two genuine signals buried in noise.
    pvals = [0.0001, 0.0008] + list(rng.uniform(0.2, 1.0, 8))
    by = fdr.benjamini_yekutieli(pvals, 0.10)
    assert by[0] and by[1]
    assert not any(by[2:])  # the noise tail is not rejected
    # BH rejects at least as many as BY (BY is the conservative one).
    bh = fdr.benjamini_hochberg(pvals, 0.10)
    assert sum(bh) >= sum(by)


def test_two_sided_p_from_z_known_values():
    assert abs(fdr.two_sided_p_from_z(0.0) - 1.0) < 1e-9
    assert abs(fdr.two_sided_p_from_z(1.96) - 0.05) < 1e-2
    assert abs(fdr.two_sided_p_from_z(3.0) - 0.0027) < 1e-3
    assert fdr.two_sided_p_from_z(float("nan")) == 1.0


def test_transforms_known_inputs():
    assert np.allclose(transforms.first_diff(np.array([1.0, 3.0, 6.0])), [2.0, 3.0])
    lr = transforms.log_return(np.array([100.0, 110.0]))
    assert abs(lr[0] - math.log(1.1)) < 1e-12


# ===========================================================================
# 2. Config loader
# ===========================================================================
def test_load_config_builds_tiles_and_stable_hash():
    cfg = load_config(CONFIG_PATH)
    assert isinstance(cfg, Config)
    assert cfg.config_hash and len(cfg.config_hash) == 12
    # 29 CORE tiles: original 25 + 4 new breadth tiles (SPEC-3: breadth_50dma,
    # breadth_broad_200dma, breadth_broad_50dma, breadth_nhnl_52w).
    assert len(cfg.tiles) == 29
    assert {t.key for t in cfg.tiles} >= {
        "ofr_fsi", "vix", "hy_oas", "sofr_iorb",
        # New SPEC-3 breadth tiles
        "breadth_200dma", "breadth_50dma", "breadth_broad_200dma",
        "breadth_broad_50dma", "breadth_nhnl_52w", "rsp_spy",
    }
    # Typed knob accessors resolve.
    assert cfg.vol_model == "ewma"
    assert cfg.percentile_window == "3y"
    assert cfg.fpr_control == "fdr"
    # Hash is deterministic across reloads.
    assert load_config(CONFIG_PATH).config_hash == cfg.config_hash


def test_config_hash_is_key_order_stable():
    a = compute_config_hash({"x": 1, "y": {"b": 2, "a": 1}})
    b = compute_config_hash({"y": {"a": 1, "b": 2}, "x": 1})
    assert a == b


def test_load_config_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_config(REPO_ROOT / "does-not-exist.yaml")


def test_secret_accessors_read_env(monkeypatch):
    cfg = load_config(CONFIG_PATH)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    assert cfg.fred_api_key() is None
    monkeypatch.setenv("FRED_API_KEY", "secret123")
    assert cfg.fred_api_key() == "secret123"


def test_nasdaq_data_link_api_key_accessor(monkeypatch):
    """nasdaq_data_link_api_key() reads NASDAQ_DATA_LINK_API_KEY from env (SPEC-3)."""
    cfg = load_config(CONFIG_PATH)
    monkeypatch.delenv("NASDAQ_DATA_LINK_API_KEY", raising=False)
    assert cfg.nasdaq_data_link_api_key() is None
    monkeypatch.setenv("NASDAQ_DATA_LINK_API_KEY", "ndl-key-xyz")
    assert cfg.nasdaq_data_link_api_key() == "ndl-key-xyz"


def test_breadth_tiles_in_config():
    """All 5 SPEC-3 breadth tiles are present in config with correct sharadar: sources."""
    cfg = load_config(CONFIG_PATH)
    tile_map = {t.key: t for t in cfg.tiles}

    # Verify all 5 breadth tiles exist with correct sharadar: source routing
    assert tile_map["breadth_200dma"].source == "sharadar:sp500_above_200dma"
    assert tile_map["breadth_50dma"].source == "sharadar:sp500_above_50dma"
    assert tile_map["breadth_broad_200dma"].source == "sharadar:broad_above_200dma"
    assert tile_map["breadth_broad_50dma"].source == "sharadar:broad_above_50dma"
    assert tile_map["breadth_nhnl_52w"].source == "sharadar:nhnl_52w"

    # rsp_spy must be UNCHANGED (SPEC-3 hard guardrail)
    assert tile_map["rsp_spy"].source == "yfinance:RSP,SPY"
    assert tile_map["rsp_spy"].transform == "ratio"

    # SPEC-3 transform rules: MA tiles use first_diff, nhnl uses level
    assert tile_map["breadth_200dma"].transform == "first_diff"
    assert tile_map["breadth_50dma"].transform == "first_diff"
    assert tile_map["breadth_nhnl_52w"].transform == "level"


def test_calendar_provider_is_fred():
    """Config must have calendar.provider='fred' after SPEC-3 switch from 'off'."""
    cfg = load_config(CONFIG_PATH)
    cal_cfg = (cfg.raw.get("calendar") or {})
    assert cal_cfg.get("provider") == "fred", \
        "calendar.provider must be 'fred' (SPEC-3 §5)"
    assert "fred_releases" in cal_cfg, "fred_releases must be in calendar config"
    assert "fomc_dates" in cal_cfg, "fomc_dates must be in calendar config"
    assert len(cal_cfg["fomc_dates"]) >= 4, "At least 4 FOMC dates expected for 2026"


# ===========================================================================
# 2b. Ingestion: detect-on-composites coverage (ANFCI fetched; missing anchor loud)
# ===========================================================================
class _FailingClient:
    """httpx.Client stand-in whose every request raises — drives all fetchers
    down their graceful-degradation path so ingest() returns ok=False RawSeries
    WITHOUT touching the network. Routing still iterates every config tile, so a
    series_by_key entry per tile proves the tile is actually fetched."""

    def get(self, *args, **kwargs):
        raise RuntimeError("network disabled in test")

    def close(self):
        pass


def _offline_ingest(monkeypatch):
    """Neutralise every library-based fetcher (yfinance) so ingest() never touches
    the network; httpx-based fetchers are starved via _FailingClient. Returns the
    ingest module for the caller to invoke."""
    import importlib

    from morning_monitor.models import RawSeries

    # NB: morning_monitor.sources re-exports the `ingest` FUNCTION, shadowing the
    # submodule attribute; load the module object directly to monkeypatch it.
    ingest_mod = importlib.import_module("morning_monitor.sources.ingest")

    def _dead_yf(ticker, *a, **k):
        key = k.get("tile_key") or f"yfinance:{ticker}"
        return RawSeries(key=key, source=f"yfinance:{ticker}", history=[],
                         asof=None, lag_desc="EOD delayed", ok=False, error="offline")

    monkeypatch.setattr(ingest_mod, "fetch_yf_series", _dead_yf)

    # Neutralise the LOCAL data bank too: breadth is now data-bank-first, so an
    # offline ingest must not read ~/data/tradingbank (external dep, slow disk).
    # read_sep_window raises -> empty frame; 'complete' True skips any gap-fill ->
    # breadth tiles degrade gracefully like every other offline fetcher.
    from morning_monitor.sources import databank as _db

    def _no_databank(*a, **k):
        raise RuntimeError("data bank disabled in test")

    monkeypatch.setattr(_db, "read_sep_window", _no_databank)
    monkeypatch.setattr(_db, "databank_sep_complete", lambda *a, **k: True)
    monkeypatch.setattr(_db, "last_trading_day", lambda *a, **k: "2026-06-26")
    monkeypatch.setattr(_db, "broad_universe", lambda: set())
    return ingest_mod


def test_ingest_fetches_anfci_into_detect_on_composites_family(monkeypatch):
    """ANFCI is a detect_on.composites anchor; it must have a tile entry so the
    ingest loop fetches it and it enters series_by_key (else the test family
    silently shrinks). Regression for the dropped-ANFCI finding."""
    ingest_mod = _offline_ingest(monkeypatch)

    cfg = load_config(CONFIG_PATH)
    composites = cfg.raw["detect_on"]["composites"]
    assert "anfci" in composites  # config contract

    series, degraded = ingest_mod.ingest(cfg, http=_FailingClient())

    # Every detect-on composite anchor has a real tile -> appears in series_by_key.
    for anchor in composites:
        assert anchor in series, f"{anchor} missing from series_by_key (no tile entry)"
    # ANFCI specifically is fetched + staleness-checked + surfaced (degraded here
    # because the network is disabled, but PRESENT — never silently dropped).
    assert "anfci" in series
    assert any(d == "anfci" or d.startswith("anfci") for d in degraded)


def test_ingest_emits_degraded_for_missing_composite_anchor(monkeypatch):
    """If a detect_on.composites key has NO tile entry it must surface as a loud
    'missing-composite-anchor' degraded entry — never vanish with zero signal."""
    import copy

    ingest_mod = _offline_ingest(monkeypatch)

    cfg = load_config(CONFIG_PATH)
    raw = copy.deepcopy(cfg.raw)
    # Drop ANFCI's tile entry but KEEP it in detect_on.composites -> orphaned anchor.
    raw["tiles"] = [t for t in raw["tiles"] if t.get("key") != "anfci"]
    assert "anfci" in raw["detect_on"]["composites"]
    cfg2 = Config(raw=raw, config_hash="orphan-anchor", tiles=[])

    series, degraded = ingest_mod.ingest(cfg2, http=_FailingClient())

    assert "anfci" not in series  # no tile -> never fetched
    # ...but it is surfaced as a missing-composite-anchor, not silently dropped.
    assert any("anfci" in d and "missing-composite-anchor" in d for d in degraded)


# ===========================================================================
# 3. Full-pipeline smoke test (offline fixture)
# ===========================================================================
@pytest.fixture(scope="module")
def brief_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def test_resolve_date_override_and_default():
    assert main_mod.resolve_date(override="2026-06-24") == "2026-06-24"
    today = main_mod.resolve_date()
    assert len(today) == 10 and today[4] == "-" and today[7] == "-"


def test_load_fixture_returns_rawseries():
    series = main_mod.load_fixture(FIXTURE_PATH)
    assert "vix" in series and "ofr_fsi" in series
    assert series["vix"].latest == 18.6
    # The degraded demo tile round-trips as ok=False.
    assert series["move_delayed_example_degraded"].ok is False


def test_full_pipeline_on_fixture_writes_valid_json_and_html(tmp_path, brief_schema):
    """End-to-end: load_config -> run(fixture) -> schema-valid JSON + non-empty HTML.

    Outputs are redirected into tmp_path via an out-dir override on the config so
    the test never touches the repo's data/ or site/.
    """
    cfg = load_config(CONFIG_PATH)
    # Redirect durable + rendered outputs into the test sandbox.
    cfg.raw["output"]["data_dir"] = str(tmp_path / "data")
    cfg.raw["output"]["site_dir"] = str(tmp_path / "site")

    brief = main_mod.run(cfg, date="2026-06-24", fixture=FIXTURE_PATH, do_render=True)

    # --- Brief object sanity ---
    assert isinstance(brief, Brief)
    assert brief.schema_version == "1.0.0"
    assert brief.meta.date == "2026-06-24"
    assert brief.meta.config_hash == cfg.config_hash
    assert len(brief.tiles) == 29                      # one per CORE tile (degraded demo excluded)
    # Thin fixture history -> calm morning, no manufactured Reds (calibration guard).
    assert brief.meta.calm_morning is True
    assert sum(1 for t in brief.tiles if t.color == "red") <= 1
    # The fixture's degraded tile is reported.
    assert "move_delayed_example_degraded" in brief.meta.degraded_sources

    # --- Durable JSON written + schema-valid ---
    json_path = tmp_path / "data" / "2026-06-24.json"
    assert json_path.exists()
    payload = json.loads(json_path.read_text())
    jsonschema.validate(instance=payload, schema=brief_schema)   # raises on contract drift
    # Round-trips back through the model.
    assert Brief.from_dict(payload).to_dict() == brief.to_dict()

    # --- Non-empty, well-formed HTML written ---
    index_html = tmp_path / "site" / "index.html"
    archive_html = tmp_path / "site" / "archive" / "2026-06-24.html"
    arch_index = tmp_path / "site" / "archive" / "index.html"
    for p in (index_html, archive_html, arch_index):
        assert p.exists(), f"missing render output: {p}"
    html = index_html.read_text()
    assert len(html) > 2000
    assert html.strip().endswith("</html>")
    assert "sparkline" in html          # inline-SVG sparklines present
    assert "VIX" in html                # real tile content rendered


def test_pipeline_degrades_not_crashes_on_empty_series(tmp_path):
    """A run with NO series still produces a schema-valid Brief (graceful degradation)."""
    cfg = load_config(CONFIG_PATH)
    cfg.raw["output"]["data_dir"] = str(tmp_path / "data")
    cfg.raw["output"]["site_dir"] = str(tmp_path / "site")

    empty_fixture = tmp_path / "empty.json"
    empty_fixture.write_text(json.dumps({"date": "2026-06-24", "series": {}, "calendar": []}))

    brief = main_mod.run(cfg, date="2026-06-24", fixture=empty_fixture, do_render=True)
    payload = brief.to_dict()
    jsonschema.validate(
        instance=payload, schema=json.loads(SCHEMA_PATH.read_text())
    )
    # Every config tile still appears (as a gray, no-data tile) — never a crash.
    # 29 tiles: original 25 + 4 new SPEC-3 breadth tiles.
    assert len(brief.tiles) == 29
    assert all(t.color == "gray" for t in brief.tiles)


def test_main_cli_exit_zero_on_fixture(tmp_path, capsys):
    """`main(['--fixture', ...])` returns 0 even though the fixture has a degraded tile.

    A sandboxed config (output dirs redirected into tmp_path) keeps the repo clean."""
    import yaml

    raw = yaml.safe_load(CONFIG_PATH.read_text())
    raw["output"]["data_dir"] = str(tmp_path / "data")
    raw["output"]["site_dir"] = str(tmp_path / "site")
    sandbox_cfg = tmp_path / "config.yaml"
    sandbox_cfg.write_text(yaml.safe_dump(raw))

    rc = main_mod.main(
        ["--config", str(sandbox_cfg), "--fixture", str(FIXTURE_PATH),
         "--date", "2026-06-24", "--no-render"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "2026-06-24" in out
    assert "degraded" in out
    # JSON written under the sandbox, not the repo.
    assert (tmp_path / "data" / "2026-06-24.json").exists()


def test_main_cli_fatal_on_missing_config(capsys):
    rc = main_mod.main(["--config", "/no/such/config.yaml"])
    assert rc == 2
    assert "FATAL" in capsys.readouterr().err
