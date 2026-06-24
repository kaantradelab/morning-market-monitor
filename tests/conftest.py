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
