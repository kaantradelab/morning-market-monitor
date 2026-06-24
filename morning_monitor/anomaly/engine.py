"""Anomaly engine orchestrator — BUILD TARGET 2 (anomaly).

Consumes ingestion output + config + calendar; produces enriched composites/tiles
plus corr-breaks and dog-didn't-bark. This is where the section-2 statistics
compose: transform -> EWMA-standardize -> percentile/robust-z ->
detect-on-composites FDR + corroboration -> Red selection -> color assignment.

Design contracts honored here:
  * Standardize by SHORT EWMA-vol FIRST, then take LONG percentile SECOND (2.1).
  * transform in {sign, level} => NO change-z (a 2s10s sign-flip IS the signal).
  * DETECT ON COMPOSITES + ~6 axis-factors, NOT the 30 raw tiles (2.2 point 5).
  * BY-FDR q=0.10 (cross-correlated family) + >=2-orthogonal corroboration +
    3-sigma headline fallback (2.2).
  * Residual correlation-break (2.3) + dog-didn't-bark low tail (2.4).
  * Quarantine intraday gauges — never enriched as triggers (2 / blind-spot 11).
  * Disputed knobs (vol_model / percentile_window / fpr_control) are read from
    config, never hard-coded.
  * Degraded tiles (RawSeries.ok=False) -> gray, scores None, never block.

The engine reads list-shaped config (detect_on, corr_breaks, tiles, knobs) from
``config.raw`` so it is decoupled from the exact typed-accessor shape; scalar
knobs prefer the typed accessor and fall back to raw.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from ..config import Config
from ..models import (
    CalendarEvent,
    Composite,
    Composites,
    CorrBreak,
    DogDidntBark,
    HistoryPoint,
    RawSeries,
    Staleness,
    Tile,
)
from . import correlation, fdr, stats, transforms

# Windows in trading days.
_WINDOW_1Y = 252
_WINDOW_3Y = 756

# Tiles whose transform produces no change-z (the signal is the level/sign itself).
_NO_Z_TRANSFORMS = {"sign", "level"}

# Amber threshold (elevated but not Red): a 2-sigma standardized move, mirroring
# the corroboration member threshold.
_AMBER_SIGMA = 2.0

# Minimum finite sample before a per-tile score is trustworthy enough to drive a
# RED on its own (robust-z / headline fallback). On thin history (e.g. the offline
# fixture's 5-point series) MAD and EWMA-z are noise — letting them flag would blow
# the calm-morning <=1-Red calibration. Real runs carry >=3y (~756) points.
_MIN_SAMPLE_FOR_TILE_RED = 30


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------
@dataclass
class AnomalyResult:
    """Everything the anomaly engine produces, consumed by the brief assembler."""
    composites: Composites
    tiles: list[Tile]
    corr_breaks: list[CorrBreak] = field(default_factory=list)
    dog_didnt_bark: list[DogDidntBark] = field(default_factory=list)
    flagged_keys: list[str] = field(default_factory=list)   # passed FDR + corroboration -> Red


# ---------------------------------------------------------------------------
# Config access helpers (defensive — decouple from config build-agent timing)
# ---------------------------------------------------------------------------
def _raw(config: Config) -> dict[str, Any]:
    return getattr(config, "raw", {}) or {}


def _knob(config: Config, accessor: str, raw_path: tuple[str, ...], default: Any) -> Any:
    """Prefer the typed Config accessor; fall back to raw YAML; then default."""
    try:
        val = getattr(config, accessor)
        if val is not None:
            return val
    except Exception:
        pass
    node: Any = _raw(config)
    for key in raw_path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node if node is not None else default


def _anomaly_cfg(config: Config) -> dict[str, Any]:
    return _raw(config).get("anomaly", {}) or {}


def _window_days(config: Config) -> tuple[int, int]:
    """(short-percentile-window, long-percentile-window) from percentile_window knob.

    The knob selects which window is the PRIMARY rarity window, but the contract's
    Tile carries BOTH pct_1y and pct_3y, so both are always computed. The knob is
    echoed into meta by the brief assembler.
    """
    return _WINDOW_1Y, _WINDOW_3Y


# ---------------------------------------------------------------------------
# Per-tile enrichment
# ---------------------------------------------------------------------------
def _values(rs: RawSeries) -> np.ndarray:
    if rs is None or not rs.history:
        return np.asarray([], dtype=float)
    return np.asarray(
        [h.value if h.value is not None else np.nan for h in rs.history], dtype=float
    )


def _nan_to_none(x: float) -> Optional[float]:
    if x is None:
        return None
    try:
        return None if not np.isfinite(x) else round(float(x), 6)
    except (TypeError, ValueError):
        return None


def _changes_for(values: np.ndarray, transform: str) -> np.ndarray:
    if transform == "log_return":
        return transforms.log_return(values)
    if transform == "first_diff":
        return transforms.first_diff(values)
    # 'ratio' series arrive pre-divided as a level by ingestion -> first-diff it.
    # 'level' / 'sign' -> no change-series (handled by caller, returns empty).
    if transform == "ratio":
        return transforms.first_diff(values)
    return np.asarray([], dtype=float)


def _staleness_from(rs: RawSeries, lam: float) -> Staleness:
    # Prefer ingest's age-derived is_stale (stamped on the RawSeries) so the tile's
    # staleness badge agrees with meta.degraded_sources. Fall back to the ok-only
    # read when ingest hasn't stamped it (e.g. fixture/offline path).
    stamped = getattr(rs, "is_stale", None)
    is_stale = (bool(stamped) if stamped is not None else not getattr(rs, "ok", False))
    return Staleness(
        asof=rs.asof,
        lag_desc=rs.lag_desc or "",
        is_stale=is_stale,
    )


def _history_points(rs: RawSeries, depth: int) -> list[HistoryPoint]:
    if not rs or not rs.history:
        return []
    return [HistoryPoint(date=h.date, value=h.value) for h in rs.history[-depth:]]


def _enrich_tile(spec: dict[str, Any], rs: Optional[RawSeries], config: Config) -> tuple[Tile, int]:
    """Build one enriched Tile from a tile spec + its RawSeries. Never raises.

    Returns (tile, sample_n) where sample_n is the finite-sample size backing the
    tile's scores — the engine uses it to keep thin-history tiles out of the
    detect-on-composites family / corroboration gate."""
    a = _anomaly_cfg(config)
    lam = float(a.get("ewma_lambda", 0.94))
    robust_thr = float(a.get("robust_z_threshold", 3.5))
    level_window = int(a.get("level_pct_window_days", _WINDOW_3Y))
    spark_depth = int(a.get("sparkline_points", 30))
    headline_sigma = float(a.get("headline_sigma", 3.0))
    vol_model = _knob(config, "vol_model", ("knobs", "vol_model"), "ewma")
    w1y, w3y = _window_days(config)

    key = spec["key"]
    transform = spec.get("transform", "level")

    # Degraded / missing series -> gray tile, all scores None.
    if rs is None or not getattr(rs, "ok", False) or not rs.history:
        stale = Staleness(
            asof=getattr(rs, "asof", None) if rs else None,
            lag_desc=(getattr(rs, "lag_desc", "") if rs else "") or "no data",
            is_stale=True,
        )
        return (
            Tile(
                key=key, axis=int(spec.get("axis", -1)), label=spec.get("label", key),
                source=getattr(rs, "source", spec.get("source", "")) if rs else spec.get("source", ""),
                value=None, change=None, transform=transform,
                ewma_z=None, pct_1y=None, pct_3y=None, level_pct_756=None, robust_z=None,
                color="gray", staleness=stale, history=_history_points(rs, spark_depth) if rs else [],
                is_front_screen=bool(spec.get("front_screen", False)),
                note=spec.get("note"),
            ),
            0,
        )

    values = _values(rs)
    finite_vals = values[np.isfinite(values)]
    latest_level = float(finite_vals[-1]) if finite_vals.size else None

    ewma_z: Optional[float] = None
    pct_1y: Optional[float] = None
    pct_3y: Optional[float] = None
    change: Optional[float] = None
    robz: Optional[float] = None
    sample_n: int = 0  # finite-sample size backing the scores (gates per-tile RED)

    # Change-score machinery only for transforms that produce a change-z.
    if transform not in _NO_Z_TRANSFORMS:
        changes = _changes_for(values, transform)
        finite_changes = changes[np.isfinite(changes)]
        sample_n = int(finite_changes.size)
        if finite_changes.size >= 1:
            change = float(finite_changes[-1])
        # Magnitude: standardize by short EWMA-vol FIRST.
        if vol_model == "garch":
            sigma = stats.garch_sigma(changes)
            latest_change = change if change is not None else np.nan
            ewma_z = float(latest_change / sigma) if (sigma and np.isfinite(sigma) and np.isfinite(latest_change)) else None
            # For percentile we still need the full standardized series.
            std_series = stats.standardize(changes, lam=lam)
        else:
            std_series = stats.standardize(changes, lam=lam)
            ewma_z = float(std_series[-1]) if (std_series.size and np.isfinite(std_series[-1])) else None
        # Rarity: LONG empirical percentile of the standardized series SECOND.
        if ewma_z is not None:
            pct_1y = stats.empirical_percentile(std_series, ewma_z, w1y)
            pct_3y = stats.empirical_percentile(std_series, ewma_z, w3y)
        # Robust-z for spiky series (on the change series).
        if finite_changes.size >= 2 and change is not None:
            robz = stats.robust_z(finite_changes, change)
    else:
        # level / sign tiles: change is the simple level delta for display context.
        if transform == "sign" or transform == "level":
            fd = transforms.first_diff(values)
            fd = fd[np.isfinite(fd)]
            if fd.size:
                change = float(fd[-1])
        sample_n = int(finite_vals.size)
        if finite_vals.size >= 2 and latest_level is not None:
            robz = stats.robust_z(finite_vals, latest_level)

    # Level percentile (756d) — state context, always when we have a level.
    level_pct_756 = (
        stats.level_percentile(finite_vals, latest_level, level_window)
        if latest_level is not None else None
    )

    tile = Tile(
        key=key, axis=int(spec.get("axis", -1)), label=spec.get("label", key),
        source=rs.source, value=_nan_to_none(latest_level), change=_nan_to_none(change),
        transform=transform,
        ewma_z=_nan_to_none(ewma_z), pct_1y=_nan_to_none(pct_1y), pct_3y=_nan_to_none(pct_3y),
        level_pct_756=_nan_to_none(level_pct_756), robust_z=_nan_to_none(robz),
        color="green", staleness=_staleness_from(rs, lam),
        history=_history_points(rs, spark_depth),
        is_front_screen=bool(spec.get("front_screen", False)),
        note=spec.get("note"),
    )
    # Provisional color (Red overlaid later from the FDR/corroboration pass).
    tile.color = _provisional_color(tile, robust_thr, headline_sigma, sample_n)
    return tile, sample_n


def _provisional_color(tile: Tile, robust_thr: float, headline_sigma: float, sample_n: int) -> str:
    """Per-tile color BEFORE the family-level Red overlay. Red is reserved for
    the FDR/corroboration winners; an individual tile may still earn Red via the
    spiky-series robust-z guard or the 3-sigma headline fallback — but ONLY when
    the backing sample is large enough that the score is trustworthy (else thin
    history would manufacture false Reds and blow the calm-morning calibration)."""
    if tile.staleness.is_stale:
        return "gray"
    z = tile.ewma_z
    rz = tile.robust_z
    if sample_n >= _MIN_SAMPLE_FOR_TILE_RED:
        if (z is not None and abs(z) >= headline_sigma) or (rz is not None and abs(rz) >= robust_thr):
            return "red"
    if z is not None and abs(z) >= _AMBER_SIGMA:
        return "amber"
    return "green"


# ---------------------------------------------------------------------------
# Composites
# ---------------------------------------------------------------------------
def _enrich_composite(rs: Optional[RawSeries], config: Config) -> Optional[Composite]:
    if rs is None:
        return None
    a = _anomaly_cfg(config)
    lam = float(a.get("ewma_lambda", 0.94))
    level_window = int(a.get("level_pct_window_days", _WINDOW_3Y))
    spark_depth = int(a.get("sparkline_points", 30))

    if not getattr(rs, "ok", False) or not rs.history:
        return Composite(
            value=None, level_pct=None, change_score=None, color="gray",
            staleness=Staleness(asof=getattr(rs, "asof", None),
                                lag_desc=(getattr(rs, "lag_desc", "") or "no data"),
                                is_stale=True),
            history=_history_points(rs, spark_depth),
        )

    values = _values(rs)
    finite_vals = values[np.isfinite(values)]
    latest = float(finite_vals[-1]) if finite_vals.size else None
    # Composites are stationary stress indices -> first-difference for change-score.
    changes = transforms.first_diff(values)
    std = stats.standardize(changes, lam=lam)
    change_score = float(std[-1]) if (std.size and np.isfinite(std[-1])) else None
    level_pct = stats.level_percentile(finite_vals, latest, level_window) if latest is not None else None

    return Composite(
        value=_nan_to_none(latest),
        level_pct=_nan_to_none(level_pct),
        change_score=_nan_to_none(change_score),
        color="green",
        staleness=Staleness(asof=rs.asof, lag_desc=rs.lag_desc or "", is_stale=False),
        history=_history_points(rs, spark_depth),
    )


# ---------------------------------------------------------------------------
# Detect-on-composites family
# ---------------------------------------------------------------------------
def _axis_factor_score(
    members: list[str], tiles_by_key: dict[str, Tile], sample_by_key: dict[str, int]
) -> Optional[float]:
    """Aggregate an axis-factor score from its member tiles' change-z.

    The axis aggregate is the member whose |ewma_z| is largest (the dominant
    deviation in the cluster). Using the max-magnitude member (not the mean) keeps
    a single strong move visible while the corroboration gate separately checks
    breadth. Members whose backing sample is too thin to trust are skipped. None
    if no member has a finite, well-supported z."""
    best: Optional[float] = None
    for m in members:
        t = tiles_by_key.get(m)
        if t is None or t.ewma_z is None:
            continue
        if sample_by_key.get(m, 0) < _MIN_SAMPLE_FOR_TILE_RED:
            continue
        if best is None or abs(t.ewma_z) > abs(best):
            best = t.ewma_z
    return best


def _build_family_and_reds(
    composites: Composites,
    tiles_by_key: dict[str, Tile],
    sample_by_key: dict[str, int],
    config: Config,
) -> list[str]:
    """Build the ~6-member detect-on-composites family and return Red FAMILY keys."""
    a = _anomaly_cfg(config)
    q = float(a.get("fdr_q", 0.10))
    method = str(a.get("fdr_method", "by")).lower()
    headline_sigma = float(a.get("headline_sigma", 3.0))
    corroboration_min = int(a.get("corroboration_min", 2))
    fpr_control = _knob(config, "fpr_control", ("knobs", "fpr_control"), "fdr")

    detect_on = _raw(config).get("detect_on", {}) or {}
    composite_keys = detect_on.get("composites", ["ofr_fsi", "nfci", "anfci"]) or []
    axis_factors = detect_on.get("axis_factors", []) or []

    # Composite change-scores.
    comp_map = {
        "ofr_fsi": composites.ofr_fsi, "nfci": composites.nfci,
        "anfci": composites.anfci, "stlfsi4": composites.stlfsi4,
    }
    composite_scores: dict[str, float] = {}
    for ck in composite_keys:
        comp = comp_map.get(ck)
        if comp is None or comp.change_score is None:
            continue
        # Thin composite history -> change_score is noise; keep it out of the family.
        if len(comp.history) < _MIN_SAMPLE_FOR_TILE_RED:
            continue
        composite_scores[ck] = float(comp.change_score)

    # Axis-factor aggregate scores + the member map for the corroboration gate.
    axis_factor_scores: dict[str, float] = {}
    axis_members: dict[str, list[str]] = {}
    for af in axis_factors:
        axis = af.get("axis")
        members = af.get("members", []) or []
        fam_key = f"axis_{axis}"
        score = _axis_factor_score(members, tiles_by_key, sample_by_key)
        axis_members[fam_key] = members
        if score is not None and np.isfinite(score):
            axis_factor_scores[fam_key] = float(score)

    family = fdr.build_test_family(composite_scores, axis_factor_scores)

    # Per-tile scores for corroboration (member tiles' change-z) — only tiles whose
    # backing sample is large enough that the z is trustworthy enter the gate.
    tile_scores: dict[str, float] = {
        k: t.ewma_z for k, t in tiles_by_key.items()
        if t.ewma_z is not None and sample_by_key.get(k, 0) >= _MIN_SAMPLE_FOR_TILE_RED
    }
    # Composites also need a self-score for the composite_only / headline path.
    for ck, sc in composite_scores.items():
        tile_scores.setdefault(ck, sc)

    return fdr.select_reds(
        family, fpr_control, q=q, method=method, headline_sigma=headline_sigma,
        corroboration_min=corroboration_min, axis_members=axis_members,
        tile_scores=tile_scores,
    )


def _resolve_red_tile_keys(
    red_family_keys: list[str],
    tiles_by_key: dict[str, Tile],
    composites: Composites,
    config: Config,
) -> list[str]:
    """Map Red FAMILY keys -> concrete RED tile keys for color overlay + flagged_keys.

    For an axis-factor family key, the Red tiles are its member tiles whose
    |ewma_z| >= 2 (the corroborating members). For a composite family key, the
    composite itself is colored Red (handled separately by the caller)."""
    a = _anomaly_cfg(config)
    member_thr = 2.0
    detect_on = _raw(config).get("detect_on", {}) or {}
    axis_factors = {f"axis_{af.get('axis')}": (af.get("members", []) or [])
                    for af in (detect_on.get("axis_factors", []) or [])}
    red_tiles: list[str] = []
    for fam_key in red_family_keys:
        members = axis_factors.get(fam_key)
        if members:
            for m in members:
                t = tiles_by_key.get(m)
                if t is not None and t.ewma_z is not None and abs(t.ewma_z) >= member_thr:
                    red_tiles.append(m)
        elif fam_key in tiles_by_key:
            red_tiles.append(fam_key)
    # De-dup, preserve order.
    seen: set[str] = set()
    out = []
    for k in red_tiles:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


# ---------------------------------------------------------------------------
# Correlation-break + dog-didn't-bark drivers
# ---------------------------------------------------------------------------
def _run_corr_breaks(series_by_key: dict[str, RawSeries], config: Config) -> list[CorrBreak]:
    a = _anomaly_cfg(config)
    sigma = float(a.get("corr_break_sigma", 2.5))
    persist = int(a.get("corr_break_persistence_days", 3))
    specs = _raw(config).get("corr_breaks", []) or []
    out: list[CorrBreak] = []
    for spec in specs:
        name = spec.get("name", "?")
        target_key = spec.get("target")
        factor_keys = spec.get("factors", []) or []
        window = int(spec.get("fit_window_days", _WINDOW_3Y))  # default 3y structural baseline
        weight_sign_flip = bool(spec.get("weight_sign_flip", False))
        target = series_by_key.get(target_key)
        factors = [series_by_key.get(fk) for fk in factor_keys]
        if target is None or any(f is None for f in factors):
            out.append(CorrBreak(name=name, residual_z=None, persistence_days=0,
                                 triggered=False, note="missing target/factor series"))
            continue
        out.append(correlation.detect_corr_break(
            name, target, factors, window_days=window, sigma_threshold=sigma,
            persistence_days=persist, weight_sign_flip=weight_sign_flip,
        ))
    return out


def _run_dog_didnt_bark(
    series_by_key: dict[str, RawSeries],
    calendar: list[CalendarEvent],
    config: Config,
) -> list[DogDidntBark]:
    a = _anomaly_cfg(config)
    ratio_thr = float(a.get("dog_didnt_bark_ratio", 0.5))
    out: list[DogDidntBark] = []
    high_impact = [e for e in (calendar or []) if getattr(e, "high_impact", False)]
    if not high_impact:
        return out
    # The dog-didn't-bark watch tiles: the most event-sensitive front-screen tiles.
    # Map by axis intent — risk (spx, vix), rates (ust_2y), credit (hy_oas), fx (usdjpy).
    watch = [k for k in ("spx", "vix", "ust_2y", "hy_oas", "usdjpy") if k in series_by_key]
    for event in high_impact:
        for key in watch:
            rs = series_by_key.get(key)
            if rs is None:
                continue
            # No straddle feed and no historical same-event date index is available
            # at engine time, so the detector falls back to its all-day mean baseline
            # and self-flags LOWER-CONFIDENCE in the note (reference SS2.4). When an
            # event-day history index is wired in later, pass event_day_indices here.
            out.append(correlation.detect_dog_didnt_bark(
                rs, event, expected_move=None, ratio_threshold=ratio_thr,
                event_day_indices=None,
            ))
    return out


# ---------------------------------------------------------------------------
# Quarantine
# ---------------------------------------------------------------------------
def _quarantined_keys(config: Config) -> set[str]:
    return set(_raw(config).get("quarantine_intraday", []) or [])


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def enrich(
    series_by_key: dict[str, RawSeries],
    config: Config,
    calendar: list[CalendarEvent],
) -> AnomalyResult:
    """Run the full section-2 anomaly pipeline. See module docstring for contracts.

    Returns AnomalyResult{composites, tiles, corr_breaks, dog_didnt_bark, flagged_keys}.
    Never raises on degraded input — a failed series becomes a gray, score-None tile.
    """
    series_by_key = series_by_key or {}
    calendar = calendar or []
    quarantine = _quarantined_keys(config)

    # ---- 1. Per-tile enrichment from the config tile specs ----
    tile_specs = _raw(config).get("tiles", []) or []
    composite_family_keys = set(
        (_raw(config).get("detect_on", {}) or {}).get("composites", []) or []
    ) | {"ofr_fsi", "nfci", "anfci", "stlfsi4"}

    tiles: list[Tile] = []
    tiles_by_key: dict[str, Tile] = {}
    sample_by_key: dict[str, int] = {}
    for spec in tile_specs:
        key = spec.get("key")
        if not key or key in quarantine:
            continue  # intraday gauges quarantined — never enriched as triggers
        # Composites are enriched as Composite objects (step 2), not as drill tiles,
        # but if a composite also appears in `tiles`, still surface it as a Tile.
        rs = series_by_key.get(key)
        tile, sample_n = _enrich_tile(spec, rs, config)
        tiles.append(tile)
        tiles_by_key[key] = tile
        sample_by_key[key] = sample_n

    # ---- 2. Composites ----
    composites = Composites(
        ofr_fsi=_enrich_composite(series_by_key.get("ofr_fsi"), config),
        nfci=_enrich_composite(series_by_key.get("nfci"), config),
        anfci=_enrich_composite(series_by_key.get("anfci"), config),
        stlfsi4=_enrich_composite(series_by_key.get("stlfsi4"), config)
        if "stlfsi4" in series_by_key else None,
    )

    # ---- 3. Detect-on-composites: FDR + corroboration -> Red family keys ----
    red_family_keys = _build_family_and_reds(composites, tiles_by_key, sample_by_key, config)
    red_tile_keys = _resolve_red_tile_keys(red_family_keys, tiles_by_key, composites, config)

    # ---- 6 (color overlay). Promote FDR/corroboration winners to Red ----
    flagged: list[str] = []
    for key in red_tile_keys:
        t = tiles_by_key.get(key)
        if t is not None and not t.staleness.is_stale:
            t.color = "red"
            flagged.append(key)
    # Composite-level Reds (composite family keys that survived selection).
    comp_map = {"ofr_fsi": composites.ofr_fsi, "nfci": composites.nfci,
                "anfci": composites.anfci, "stlfsi4": composites.stlfsi4}
    for fam_key in red_family_keys:
        if fam_key in comp_map and comp_map[fam_key] is not None:
            comp = comp_map[fam_key]
            if not (comp.staleness and comp.staleness.is_stale):
                comp.color = "red"
                if fam_key not in flagged:
                    flagged.append(fam_key)

    # Composite color for non-Red composites (amber if elevated change-score).
    for ck, comp in comp_map.items():
        if comp is None or comp.color == "red":
            continue
        if comp.staleness and comp.staleness.is_stale:
            comp.color = "gray"
        elif comp.change_score is not None and abs(comp.change_score) >= _AMBER_SIGMA:
            comp.color = "amber"
        else:
            comp.color = "green"

    # ---- 4. Correlation-break (residual) ----
    corr_breaks = _run_corr_breaks(series_by_key, config)

    # ---- 5. Dog-didn't-bark (conditional low tail on event days) ----
    dog = _run_dog_didnt_bark(series_by_key, calendar, config)

    return AnomalyResult(
        composites=composites,
        tiles=tiles,
        corr_breaks=corr_breaks,
        dog_didnt_bark=dog,
        flagged_keys=flagged,
    )
