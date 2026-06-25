"""Config loader + resolver for the morning monitor.

Loads config.yaml, resolves env-keyed secrets (FRED_API_KEY, FINNHUB_API_KEY)
from the environment, and computes a stable config_hash recorded in each brief's
meta. The disputed knobs (vol_model, percentile_window, fpr_control,
net_liquidity, cot_positioning) live here, NEVER hard-coded elsewhere.

Stub: build agent implements the bodies. Signatures + return shapes are the contract.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

# The methodology subset hashed into config_hash: two runs with identical
# methodology share a hash even if comments / source-lists change. Source lists
# (fred_series, tiles, calendar events) are NOT in this subset — only the knobs
# and thresholds that change what an anomaly *means*.
_HASH_KEYS = ("schema_version", "knobs", "anomaly", "calibration", "output")


@dataclass
class TileSpec:
    """One CORE tile definition from config.yaml `tiles`."""
    key: str
    axis: int
    label: str
    source: str
    transform: str
    front_screen: bool = False
    note: str | None = None


@dataclass
class Config:
    """Resolved runtime config. `raw` is the parsed YAML; the rest are typed views."""
    raw: dict[str, Any]
    config_hash: str
    tiles: list[TileSpec] = field(default_factory=list)

    # --- typed convenience accessors ---
    def _knob(self, name: str, default: str) -> str:
        knobs = self.raw.get("knobs", {}) if isinstance(self.raw, dict) else {}
        val = knobs.get(name, default)
        return str(val) if val is not None else default

    @property
    def vol_model(self) -> str:
        """knobs.vol_model -> 'ewma' | 'garch'."""
        return self._knob("vol_model", "ewma")

    @property
    def percentile_window(self) -> str:
        """knobs.percentile_window -> '1y' | '3y' | 'full'."""
        return self._knob("percentile_window", "3y")

    @property
    def fpr_control(self) -> str:
        """knobs.fpr_control -> 'fdr' | 'corroboration' | 'composite_only'."""
        return self._knob("fpr_control", "fdr")

    def _secret(self, source_name: str, default_env: str) -> str | None:
        """Resolve sources.<name>.api_key_env from the environment. None if unset."""
        sources = self.raw.get("sources", {}) if isinstance(self.raw, dict) else {}
        src = sources.get(source_name, {}) or {}
        env_var = src.get("api_key_env", default_env)
        val = os.environ.get(env_var)
        return val or None

    def fred_api_key(self) -> str | None:
        """Resolve sources.fred.api_key_env from the environment. None if unset."""
        return self._secret("fred", "FRED_API_KEY")

    def finnhub_api_key(self) -> str | None:
        """Resolve sources.finnhub.api_key_env from the environment. None if unset."""
        return self._secret("finnhub", "FINNHUB_API_KEY")

    def fmp_api_key(self) -> str | None:
        """Resolve sources.fmp.api_key_env from the environment. None if unset.

        Mirrors finnhub_api_key — FMP is the PRIMARY economic-calendar source.
        """
        return self._secret("fmp", "FMP_API_KEY")

    def nasdaq_data_link_api_key(self) -> str | None:
        """Resolve sources.nasdaq_data_link.api_key_env from the environment.

        Used by the Sharadar SEP breadth computation (SPEC-3). Mirrors fred_api_key().
        """
        return self._secret("nasdaq_data_link", "NASDAQ_DATA_LINK_API_KEY")


def _build_tiles(raw: dict[str, Any]) -> list[TileSpec]:
    """Build the typed TileSpec list from the raw `tiles` list."""
    out: list[TileSpec] = []
    for t in raw.get("tiles", []) or []:
        if not isinstance(t, dict) or "key" not in t:
            continue
        out.append(
            TileSpec(
                key=t["key"],
                axis=int(t.get("axis", 0)),
                label=t.get("label", t["key"]),
                source=t.get("source", ""),
                transform=t.get("transform", "level"),
                front_screen=bool(t.get("front_screen", False)),
                note=t.get("note"),
            )
        )
    return out


def _hash_subset(raw: dict[str, Any]) -> dict[str, Any]:
    """The methodology subset hashed into config_hash."""
    return {k: raw[k] for k in _HASH_KEYS if k in raw}


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """Parse config.yaml, build TileSpec list, compute config_hash.

    config_hash = sha256 of the canonical-JSON of the *knob+threshold* subset
    (schema_version, knobs, anomaly, calibration, output) truncated to 12 hex
    chars — so two runs with identical methodology share a hash even if
    comments/source-lists change.

    Raises FileNotFoundError if the file is missing. Does NOT read secrets here.
    """
    import yaml  # local import: keep the module importable without PyYAML for tests

    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"config not found: {cfg_path}")

    raw = yaml.safe_load(cfg_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config.yaml did not parse to a mapping: {cfg_path}")

    config_hash = compute_config_hash(_hash_subset(raw))
    tiles = _build_tiles(raw)
    return Config(raw=raw, config_hash=config_hash, tiles=tiles)


def compute_config_hash(resolved: dict[str, Any]) -> str:
    """sha256(canonical_json(resolved))[:12]. Stable across key ordering."""
    blob = json.dumps(resolved, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]
