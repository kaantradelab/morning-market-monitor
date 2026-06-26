"""Shared pytest fixtures for the morning-market-monitor test suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "brief.schema.json"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "sample_run.json"
CONFIG_PATH = REPO_ROOT / "config.yaml"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def brief_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture(scope="session")
def sample_run() -> dict:
    """The offline raw-ingestion fixture (no API keys needed)."""
    return json.loads(FIXTURE_PATH.read_text())


@pytest.fixture(scope="session")
def config_path() -> Path:
    return CONFIG_PATH


@pytest.fixture(autouse=True)
def _isolate_breadth_cache(tmp_path_factory, monkeypatch):
    """Root-cause guard: redirect the breadth cache dir to a per-test tmp dir.

    ``morning_monitor.sources.breadth._update_cache`` writes
    ``_BREADTH_CACHE_DIR/<key>.csv``. Any test that exercises
    ``fetch_breadth_series`` (directly or via the pipeline) without patching
    ``_BREADTH_CACHE_DIR`` would otherwise write synthetic rows into the REAL
    committed ``data/breadth/*.csv`` working-tree files. This autouse fixture
    makes that impossible for EVERY test: writes land in a throwaway tmp dir.

    Tests that already patch ``_BREADTH_CACHE_DIR`` (e.g. TestCacheManagement,
    TestLicenseGuard) compose cleanly — their inner patch overrides this one and
    restores back to the tmp dir on exit. The license-guard tests inspect the
    CSV they themselves write into their own tmp_path, so they are unaffected.
    """
    from morning_monitor.sources import breadth as _breadth_mod

    cache_dir = tmp_path_factory.mktemp("breadth_cache")
    monkeypatch.setattr(_breadth_mod, "_BREADTH_CACHE_DIR", cache_dir)
    yield
