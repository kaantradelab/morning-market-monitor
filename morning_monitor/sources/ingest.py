"""Ingestion orchestrator — BUILD TARGET 1.

Top-level entry the pipeline calls. Fans out to the per-source fetchers, assembles
one RawSeries per CORE tile keyed by tile.key, applies the staleness rule, and
collects degraded-source keys. NEVER raises: a failed fetcher yields ok=False.

    ingest(config, *, http=None) -> tuple[dict[str, RawSeries], list[str]]
                                    (series_by_key, degraded_source_keys)

Routing is driven by each tile's `source` string in config.yaml:

    fred:<ID>                      -> fetch_fred_series
    yfinance:<TICKER>              -> fetch_yf_series (single ticker)
    yfinance:<A>,<B>               -> ratio of two yfinance tickers (copper_gold, rsp_spy, vix_term)
    ofr:fsi                        -> fetch_ofr_fsi
    defillama:stablecoins          -> fetch_stablecoin_cap
    nyfed:srf                      -> fetch_srf_takeup
    vendor:<X>                     -> no free source -> degraded tile (graceful)
    derived:realized_vol_*         -> build_realized_vol over a FRED dependency (DGS10)
    derived:fred_walcl_*           -> build_net_liquidity (WALCL,WTREGEN,RRPONTSYD)
    derived:fred_sofr_iorb         -> build_sofr_iorb (SOFR,IORB)
"""

from __future__ import annotations

from datetime import date as date_cls
from typing import Optional

import httpx

from ..config import Config
from ..models import RawSeries
from . import calendar as calendar_mod  # noqa: F401 — re-export convenience
from .composites import fetch_ofr_fsi
from .crypto import fetch_stablecoin_cap
from .derived import build_net_liquidity, build_ratio, build_realized_vol, build_sofr_iorb
from .fred import fetch_fred_series
from .market import fetch_yf_series
from .nyfed import fetch_srf_takeup

# Re-export the calendar fetcher so callers can do `from ...sources.ingest import fetch_calendar`.
from .calendar import fetch_calendar  # noqa: F401,E402

# Per-tile freshness windows (expected_max_age_days). A tile older than its window
# is flagged is_stale. Keyed by tile.key; falls back to source-family defaults.
# Windows derived from each source's PUBLISH cadence (reference SPEC-2 line 31:
# "OFR 2-bd lag, NFCI/STLFSI weekly, copper monthly") so the freshness badge
# matches the stated per-source lag and a healthy series does not false-stale.
_FRESHNESS_BY_KEY: dict[str, int] = {
    "ofr_fsi": 7,            # daily index, 2-bd lag; 7 absorbs a holiday weekend
    "nfci": 14,             # weekly (Wed obs, ~1wk publish lag): freshest obs can be 8-13d old
    "anfci": 14,            # weekly, same cadence as NFCI
    "stlfsi4": 14,          # weekly
    "net_liquidity": 10,    # H.4.1 weekly (Thu)
    "breadth_200dma": 5,    # EOD vendor
    "srf_takeup": 5,        # NY Fed daily ops (skips non-op days)
    "copper_gold": 5,       # daily HG=F/GC=F ratio supersedes the monthly-copper label (see note below)
    "brent": 5,
    "move_proxy": 5,
}
_FRESHNESS_BY_FAMILY: dict[str, int] = {
    "fred": 5,              # most FRED daily series carry a 1-2 bd lag
    "yfinance": 5,          # delayed EOD; weekends/holidays widen the gap
    "defillama": 5,
    "derived": 7,
    "ofr": 5,
    "nyfed": 5,
    "vendor": 5,
}
_DEFAULT_FRESHNESS = 5


def ingest(config: Config, *, http: Optional[httpx.Client] = None) -> tuple[dict[str, RawSeries], list[str]]:
    """Fetch every CORE tile defined in config.tiles.

    Returns (series_by_key, degraded). `series_by_key[tile.key]` is a RawSeries
    (ok=False on failure). `degraded` lists keys whose fetch failed OR came back
    stale. Pass an httpx.Client for connection reuse / test injection; if None,
    one is created and closed internally.

    Graceful degradation contract: this function MUST NOT propagate exceptions
    from individual fetchers. Every fetch is wrapped; on error -> ok=False RawSeries.
    """
    owns_client = http is None
    client = http or httpx.Client()
    today = date_cls.today().isoformat()

    try:
        fred_cfg, fred_key = _fred_settings(config)
        # FRED dependency cache so derived tiles reuse already-fetched series.
        fred_cache: dict[str, RawSeries] = {}

        def fred(series_id: str, *, lag_desc: str = "FRED daily", years: int = 3) -> RawSeries:
            if series_id not in fred_cache:
                fred_cache[series_id] = _safe(
                    lambda: fetch_fred_series(
                        series_id, api_key=fred_key or "", base_url=fred_cfg["base_url"],
                        http=client, years=years, lag_desc=lag_desc,
                        tile_key=f"_dep:fred:{series_id}",
                    ),
                    key=f"_dep:fred:{series_id}", source=f"fred:{series_id}",
                    lag_desc=lag_desc,
                )
            return fred_cache[series_id]

        series_by_key: dict[str, RawSeries] = {}
        for tile in _tiles(config):
            series_by_key[tile.key] = _fetch_tile(
                tile, config, client, fred, fred_cfg, fred_key,
            )

        # Apply staleness + assemble degraded list (failed OR stale).
        degraded: list[str] = []
        for key, series in series_by_key.items():
            window = _freshness_window(key, series.source)
            stale = compute_staleness(series.asof, series.lag_desc, window, today=today)
            if not series.ok or stale:
                degraded.append(key)

        # A detect-on-composites anchor declared in config but never fetched (no
        # tile entry -> never in series_by_key) would otherwise vanish with ZERO
        # signal: no gray tile, no degraded badge, the test family silently
        # shrinks. Surface it explicitly so a missing composite anchor is loud.
        raw = getattr(config, "raw", {}) or {}
        composite_anchors = (raw.get("detect_on", {}) or {}).get("composites", []) or []
        for anchor in composite_anchors:
            if anchor not in series_by_key and anchor not in degraded:
                degraded.append(f"{anchor}:missing-composite-anchor (no tile entry)")

        return series_by_key, degraded
    finally:
        if owns_client:
            client.close()


def compute_staleness(
    asof: Optional[str],
    lag_desc: str,
    expected_max_age_days: int,
    *,
    today: Optional[str] = None,
) -> bool:
    """is_stale = (today - asof) > expected_max_age_days. True if asof is None.

    expected_max_age_days encodes the per-tile freshness window (OFR 2-bd,
    NFCI weekly ~7-10, copper monthly ~31, EOD ~1-5).
    """
    if not asof:
        return True
    today_str = today or date_cls.today().isoformat()
    try:
        asof_d = date_cls.fromisoformat(asof[:10])
        today_d = date_cls.fromisoformat(today_str[:10])
    except ValueError:
        return True
    age = (today_d - asof_d).days
    return age > expected_max_age_days


# ---------------------------------------------------------------------------
# Internal routing helpers
# ---------------------------------------------------------------------------
def _fetch_tile(tile, config: Config, client: httpx.Client, fred, fred_cfg, fred_key) -> RawSeries:
    """Route one TileSpec to its fetcher. Always returns a RawSeries (never raises)."""
    source = tile.source
    key = tile.key

    try:
        kind, _, rest = source.partition(":")

        if kind == "fred":
            lag = "NFCI weekly" if rest in ("NFCI", "ANFCI", "STLFSI4") else "FRED daily"
            return _safe(
                lambda: fetch_fred_series(
                    rest, api_key=fred_key or "", base_url=fred_cfg["base_url"],
                    http=client, lag_desc=lag, tile_key=key,
                ),
                key=key, source=source, lag_desc=lag,
            )

        if kind == "ofr":
            return _safe(lambda: fetch_ofr_fsi(http=client, tile_key=key),
                         key=key, source=source, lag_desc="OFR 2-bd lag")

        if kind == "defillama":
            llama = (config.raw.get("sources", {}) or {}).get("defillama", {}) or {}
            base = llama.get("base_url", "https://stablecoins.llama.fi")
            return _safe(lambda: fetch_stablecoin_cap(base_url=base, http=client, tile_key=key),
                         key=key, source=source, lag_desc="DefiLlama daily")

        if kind == "nyfed":
            return _safe(lambda: fetch_srf_takeup(http=client, tile_key=key),
                         key=key, source=source, lag_desc="NY Fed daily ops")

        if kind == "yfinance":
            tickers = [t.strip() for t in rest.split(",") if t.strip()]
            if len(tickers) == 1:
                return _safe(lambda: fetch_yf_series(tickers[0], tile_key=key),
                             key=key, source=source, lag_desc="EOD delayed")
            if len(tickers) == 2:
                num = _safe(lambda: fetch_yf_series(tickers[0]),
                            key=key, source=source, lag_desc="EOD delayed")
                den = _safe(lambda: fetch_yf_series(tickers[1]),
                            key=key, source=source, lag_desc="EOD delayed")
                return _safe(lambda: build_ratio(num, den, tile_key=key),
                             key=key, source=source, lag_desc="EOD delayed")
            return _degraded(key, source, "EOD delayed", "no yfinance tickers in source")

        if kind == "derived":
            return _fetch_derived(key, rest, source, fred)

        if kind == "vendor":
            # No reliable FREE source for vendor tiles (e.g. StockCharts $SPXA200R).
            # Degrade gracefully: tile flagged missing/stale, run continues.
            return _degraded(key, source, "EOD vendor",
                             "no free vendor source configured (graceful degradation)")

        return _degraded(key, source, "", f"unknown source kind '{kind}'")
    except Exception as exc:  # noqa: BLE001 — absolute belt-and-braces; ingest never raises
        return _degraded(key, source, "", f"router error: {exc!r}")


def _fetch_derived(key: str, rest: str, source: str, fred) -> RawSeries:
    """Build a derived tile from FRED dependencies."""
    if rest.startswith("realized_vol_dgs10"):
        dgs10 = fred("DGS10", lag_desc="FRED daily")
        return _safe(lambda: build_realized_vol(dgs10, window=20, tile_key=key),
                     key=key, source=source, lag_desc="derived (DGS10 20d rv)")

    if rest.startswith("fred_walcl"):
        walcl = fred("WALCL", lag_desc="H.4.1 weekly Thu")
        tga = fred("WTREGEN", lag_desc="H.4.1 weekly Thu")
        rrp = fred("RRPONTSYD", lag_desc="FRED daily")
        return _safe(lambda: build_net_liquidity(walcl, tga, rrp, tile_key=key),
                     key=key, source=source, lag_desc="derived (H.4.1 weekly Thu)")

    if rest.startswith("fred_sofr_iorb"):
        sofr = fred("SOFR", lag_desc="FRED daily")
        iorb = fred("IORB", lag_desc="FRED daily")
        return _safe(lambda: build_sofr_iorb(sofr, iorb, tile_key=key),
                     key=key, source=source, lag_desc="derived (FRED daily)")

    return _degraded(key, source, "derived", f"unknown derived recipe '{rest}'")


def _safe(fn, *, key: str, source: str, lag_desc: str) -> RawSeries:
    """Run a fetcher; convert any exception into a degraded RawSeries."""
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 — graceful degradation
        return _degraded(key, source, lag_desc, f"fetch raised: {exc!r}")
    if not isinstance(result, RawSeries):
        return _degraded(key, source, lag_desc, "fetcher returned non-RawSeries")
    return result


def _degraded(key: str, source: str, lag_desc: str, reason: str) -> RawSeries:
    return RawSeries(
        key=key, source=source, history=[], asof=None,
        lag_desc=lag_desc, ok=False, error=reason,
    )


def _freshness_window(key: str, source: str) -> int:
    if key in _FRESHNESS_BY_KEY:
        return _FRESHNESS_BY_KEY[key]
    family = source.split(":", 1)[0] if source else ""
    return _FRESHNESS_BY_FAMILY.get(family, _DEFAULT_FRESHNESS)


def _tiles(config: Config) -> list:
    """TileSpec list from config; tolerate a config whose .tiles is empty by
    falling back to the raw YAML tile dicts (so ingestion works even if the
    config module's TileSpec builder is still a stub)."""
    tiles = getattr(config, "tiles", None)
    if tiles:
        return tiles
    raw = getattr(config, "raw", {}) or {}
    out = []
    for t in raw.get("tiles", []):
        out.append(_RawTile(
            key=t["key"], axis=t.get("axis", 0), label=t.get("label", t["key"]),
            source=t["source"], transform=t.get("transform", "level"),
            front_screen=t.get("front_screen", False), note=t.get("note"),
        ))
    return out


def _fred_settings(config: Config) -> tuple[dict, Optional[str]]:
    raw = getattr(config, "raw", {}) or {}
    fred_cfg = (raw.get("sources", {}) or {}).get("fred", {}) or {}
    fred_cfg.setdefault("base_url", "https://api.stlouisfed.org/fred")
    key = None
    getter = getattr(config, "fred_api_key", None)
    if callable(getter):
        try:
            key = getter()
        except Exception:  # noqa: BLE001
            key = None
    return fred_cfg, key


class _RawTile:
    """Lightweight TileSpec stand-in built from raw YAML (fallback only)."""

    __slots__ = ("key", "axis", "label", "source", "transform", "front_screen", "note")

    def __init__(self, *, key, axis, label, source, transform, front_screen, note):
        self.key = key
        self.axis = axis
        self.label = label
        self.source = source
        self.transform = transform
        self.front_screen = front_screen
        self.note = note
