"""Brief assembler — BUILD TARGET 3.

Consumes the AnomalyResult + calendar + config; produces the final models.Brief
(cards, calendar strip, plumbing flags) and writes the durable per-day JSON.

Card discipline (reference §2.2 / §4 #6): <=3 cards (a crisis banner may add a
4th flagged is_banner). EVERY card MUST carry a populated why_now
(percentile/z · related calendar event · cross-asset confirm-or-contradict).
A card without why_now is SUPPRESSED (decorative noise).

Calibration discipline (reference §rec-2): on a calm morning surface <=1 Red.
The FDR + corroboration gate lives in the anomaly engine (it controls how many
keys land in AnomalyResult.flagged_keys). The brief is the downstream consumer:
it derives `calm_morning = (#reds <= calibration.calm_morning_max_reds)` as a
provenance self-check so a loose-threshold morning is visible in meta, and it
caps the attention cards at config.output.max_cards regardless.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .anomaly.engine import AnomalyResult
from .config import Config
from .models import (
    Brief,
    CalendarEvent,
    Card,
    Composites,
    CorrBreak,
    DogDidntBark,
    Meta,
    PlumbingFlag,
    RawSeries,
    Staleness,
    Tile,
    WhyNow,
)

# ---------------------------------------------------------------------------
# Narrative clustering — which orthogonal tiles corroborate a given flagged key.
# Used to populate why_now.cross_asset_confirm_or_contradict: a card is stronger
# when an ORTHOGONAL tile (different axis) moves the same risk-on/risk-off way.
# ---------------------------------------------------------------------------

# A risk-OFF (stress) move for each tile is in this sign direction (sign of the
# tile's `change` that means "stress rising"). +1 = a rise is stress; -1 = a
# fall is stress. Tiles whose direction is ambiguous/level-only are omitted.
_STRESS_SIGN: dict[str, int] = {
    "ofr_fsi": +1,
    "nfci": +1,
    "anfci": +1,
    "spx": -1,          # equities falling = stress
    "vix": +1,          # vol rising = stress
    "ust_2y": -1,       # front yields falling = flight-to-quality (stress)
    "real_10y": -1,
    "breakeven_10y": -1,
    "hy_oas": +1,       # credit spreads widening = stress
    "ig_oas": +1,
    "usd_broad": +1,    # USD bid = stress / flight
    "usdjpy": -1,       # JPY bid (USDJPY down) = risk-off
    "brent": -1,
    "copper_gold": -1,  # copper/gold falling = growth scare
    "vix_term": +1,     # backwardation rising = acute stress
    "move_proxy": +1,
    "breadth_200dma": -1,
    "rsp_spy": -1,
    "net_liquidity": -1,
    "sofr_iorb": +1,
    "srf_takeup": +1,
    "btc": -1,
    "stablecoin_cap": -1,
}


def _abs_or_none(x: Optional[float]) -> float:
    return abs(x) if x is not None else 0.0


def _severity(tile: Tile) -> float:
    """Rank score for a flagged tile: bigger |z| / higher percentile = more urgent."""
    z = max(_abs_or_none(tile.ewma_z), _abs_or_none(tile.robust_z))
    # percentile distance from 50 (extremeness), scaled into a z-comparable range.
    pct = tile.pct_3y if tile.pct_3y is not None else tile.pct_1y
    pct_extreme = (abs(pct - 50.0) / 50.0) * 3.0 if pct is not None else 0.0
    return max(z, pct_extreme)


def _percentile_or_z(tile: Tile) -> Optional[str]:
    """Human rarity statement. Returns None if neither a z nor a percentile exists."""
    parts: list[str] = []
    pct = tile.pct_3y if tile.pct_3y is not None else tile.pct_1y
    if pct is not None:
        parts.append(f"{pct:.0f}th pct")
    z = tile.ewma_z if tile.ewma_z is not None else tile.robust_z
    if z is not None:
        parts.append(f"{z:+.1f}-sigma")
    if not parts:
        return None
    return " / ".join(parts)


def _matching_calendar_event(tile: Tile, calendar: list[CalendarEvent]) -> Optional[str]:
    """Pick the most relevant scheduled event today (highest-impact / best rank).

    A card's calendar_event is nullable (an unscheduled shock has none) — but a
    high-impact release on the same morning is the strongest 'why now'. Prefer
    high_impact, then lowest rank.
    """
    if not calendar:
        return None
    ranked = sorted(
        calendar,
        key=lambda e: (not e.high_impact, e.rank if e.rank is not None else 999),
    )
    top = ranked[0]
    return top.event


def _cross_asset_corroboration(
    tile: Tile,
    tiles_by_key: dict[str, Tile],
    flagged_keys: set[str],
) -> str:
    """Confirm-or-contradict line from ORTHOGONAL tiles.

    Looks at every other notable tile on a DIFFERENT axis: if it moved the same
    risk-on/off direction it confirms; opposite direction it contradicts. >=2
    orthogonal confirmers is the corroboration bar (reference §2.2 #6).
    """
    sign = _STRESS_SIGN.get(tile.key)
    if sign is None or tile.change is None:
        # No directional read on this tile — report co-flagged peers if any.
        peers = sorted(flagged_keys - {tile.key})
        if peers:
            return "co-flagged with " + ", ".join(peers)
        return "isolated move — no orthogonal corroboration"

    tile_stress = sign * (1 if tile.change >= 0 else -1)

    confirmers: list[str] = []
    contradictors: list[str] = []
    for other in tiles_by_key.values():
        if other.key == tile.key or other.axis == tile.axis:
            continue
        osign = _STRESS_SIGN.get(other.key)
        if osign is None or other.change is None or other.change == 0:
            continue
        # Only count meaningful peer moves (elevated/red, or notable z).
        notable = (
            other.color in ("red", "amber")
            or _abs_or_none(other.ewma_z) >= 1.5
        )
        if not notable:
            continue
        other_stress = osign * (1 if other.change >= 0 else -1)
        if other_stress == tile_stress:
            confirmers.append(other.label)
        else:
            contradictors.append(other.label)

    if confirmers:
        head = "confirmed by " + " + ".join(confirmers[:3])
        if contradictors:
            head += " (but contradicted by " + ", ".join(contradictors[:2]) + ")"
        return head
    if contradictors:
        return "contradicted by " + ", ".join(contradictors[:3]) + " (calm cross-asset)"
    return "isolated move — no orthogonal corroboration"


def build_cards(
    result: AnomalyResult,
    calendar: list[CalendarEvent],
    config: Config,
) -> list[Card]:
    """Turn flagged_keys into <=max_cards attention Cards, each with a why_now.

    Rank flagged keys by severity, build a why_now (percentile_or_z from the tile;
    calendar_event from a matching today's release; cross_asset_confirm_or_contradict
    from corroborating/contradicting orthogonal tiles). DROP any candidate whose
    why_now cannot be populated. Cap at config.output.max_cards; a crisis banner
    (is_banner=True) may exceed the cap.
    """
    tiles_by_key = {t.key: t for t in result.tiles}
    flagged = set(result.flagged_keys)
    max_cards = int(config.raw.get("output", {}).get("max_cards", 3))

    # Candidate tiles = flagged keys that resolve to a real (non-stale) tile.
    candidates: list[Tile] = []
    for key in result.flagged_keys:
        tile = tiles_by_key.get(key)
        if tile is None:
            continue
        if tile.color == "gray" or tile.staleness.is_stale:
            # Degraded tiles never become attention cards (reference: gray = noise).
            continue
        candidates.append(tile)

    candidates.sort(key=_severity, reverse=True)

    cards: list[Card] = []
    for tile in candidates:
        # --- mandatory why_now; DROP the card if it cannot be populated ---
        rarity = _percentile_or_z(tile)
        if rarity is None:
            continue  # no statistical rarity -> decorative noise -> suppress
        cross = _cross_asset_corroboration(tile, tiles_by_key, flagged)
        cal_event = _matching_calendar_event(tile, calendar)
        why = WhyNow(
            percentile_or_z=rarity,
            calendar_event=cal_event,
            cross_asset_confirm_or_contradict=cross,
        )

        change_txt = f"{tile.change:+.2f}" if tile.change is not None else "n/a"
        metric = f"{tile.label} {change_txt} ({tile.source})"
        score_desc = rarity
        if tile.level_pct_756 is not None:
            score_desc = f"{rarity}; level {tile.level_pct_756:.0f}th pct of 3y"

        tile_keys = [tile.key]
        # Attach an orthogonal corroborating peer key for >=2-tile provenance.
        for other in result.tiles:
            if other.key == tile.key or other.axis == tile.axis:
                continue
            if other.color in ("red", "amber") and other.key not in tile_keys:
                tile_keys.append(other.key)
                break

        cards.append(
            Card(
                title=f"{tile.label} anomaly",
                metric=metric,
                score_desc=score_desc,
                why_now=why,
                color=tile.color,
                is_banner=False,
                tile_keys=tile_keys,
            )
        )

    # Cap at max_cards. Reds keep priority (already severity-sorted).
    capped = cards[:max_cards]

    # Crisis banner: if a composite breached hard (red composite), allow it as a
    # banner ON TOP of the cap (reference: a crisis banner may add a 4th).
    banner = _crisis_banner(result, calendar)
    if banner is not None:
        # Don't duplicate a card already shown for the same tile.
        shown = {k for c in capped for k in c.tile_keys}
        if not (set(banner.tile_keys) & shown):
            capped = [banner] + capped
            # Schema allows maxItems 4 (3 cards + 1 banner).
            capped = capped[: max_cards + 1]

    return capped


def _crisis_banner(result: AnomalyResult, calendar: list[CalendarEvent]) -> Optional[Card]:
    """A red composite (system-temperature spike) -> a banner card."""
    comp_map = {
        "OFR FSI": result.composites.ofr_fsi,
        "Chicago Fed NFCI": result.composites.nfci,
        "ANFCI": result.composites.anfci,
    }
    for label, comp in comp_map.items():
        if comp is None or comp.color != "red":
            continue
        rarity_parts: list[str] = []
        if comp.level_pct is not None:
            rarity_parts.append(f"{comp.level_pct:.0f}th pct level")
        if comp.change_score is not None:
            rarity_parts.append(f"{comp.change_score:+.1f}-sigma change")
        rarity = " / ".join(rarity_parts) if rarity_parts else "elevated"
        cal_event = _matching_calendar_event(_dummy_tile(label), calendar)
        return Card(
            title=f"SYSTEM STRESS: {label} red",
            metric=f"{label} = {comp.value:.2f}" if comp.value is not None else f"{label} red",
            score_desc=rarity,
            why_now=WhyNow(
                percentile_or_z=rarity,
                calendar_event=cal_event,
                cross_asset_confirm_or_contradict="composite-level spike — see drill-down tiles",
            ),
            color="red",
            is_banner=True,
            tile_keys=[],
        )
    return None


def _dummy_tile(label: str) -> Tile:
    """Minimal Tile for calendar-matching on a composite banner."""
    return Tile(
        key="__composite__", axis=0, label=label, source="composite", value=None,
        change=None, transform="level", ewma_z=None, pct_1y=None, pct_3y=None,
        level_pct_756=None, robust_z=None, color="red",
        staleness=Staleness(asof=None, lag_desc="", is_stale=False),
    )


# ---------------------------------------------------------------------------
# Plumbing flags
# ---------------------------------------------------------------------------
def _staleness_from(series: Optional[RawSeries], expected_max_age_days: int = 5) -> Optional[Staleness]:
    if series is None:
        return None
    return Staleness(asof=series.asof, lag_desc=series.lag_desc, is_stale=not series.ok)


def _series_5d_change(series: RawSeries) -> Optional[float]:
    vals = [h.value for h in series.history if h.value is not None]
    if len(vals) < 2:
        return None
    window = vals[-6:] if len(vals) >= 6 else vals
    return window[-1] - window[0]


def build_plumbing_flags(
    series_by_key: dict[str, RawSeries],
    config: Config,
) -> list[PlumbingFlag]:
    """Evaluate config.plumbing_flags rules against fetched plumbing series.

    SRF rising off ~0 · SOFR-IORB>25bp · EFFR top-of-range · net-liq 5d drop.
    Each -> PlumbingFlag(triggered, value, note, staleness). net-liq flag carries
    the 'context only' note (net_liquidity knob).
    """
    flags: list[PlumbingFlag] = []

    def get(key: str) -> Optional[RawSeries]:
        rs = series_by_key.get(key)
        return rs if (rs is not None and rs.ok) else (series_by_key.get(key))

    # --- srf_takeup: value > 0 (rising off ~0) ---
    srf = series_by_key.get("srf_takeup")
    if srf is not None:
        val = srf.latest
        triggered = bool(srf.ok and val is not None and val > 0)
        flags.append(PlumbingFlag(
            name="srf_takeup",
            value=val,
            triggered=triggered,
            note=(f"SRF take-up {val:.1f}bn off zero = funding stress"
                  if triggered and val is not None
                  else "SRF take-up at/near zero"),
            staleness=_staleness_from(srf),
        ))

    # --- sofr_minus_iorb: value > 0.25 (>25bp, in percentage points) ---
    sofr_iorb = series_by_key.get("sofr_iorb")
    if sofr_iorb is not None:
        val = sofr_iorb.latest
        triggered = bool(sofr_iorb.ok and val is not None and val > 0.25)
        flags.append(PlumbingFlag(
            name="sofr_minus_iorb",
            value=val,
            triggered=triggered,
            note=(f"SOFR-IORB {val * 100:.0f}bp > 25bp threshold = repo stress"
                  if val is not None
                  else "SOFR-IORB unavailable"),
            staleness=_staleness_from(sofr_iorb),
        ))

    # --- effr_top_of_range: effr >= dfedtaru - 0.01 (within 1bp of top) ---
    effr = series_by_key.get("effr")
    dfedtaru = series_by_key.get("dfedtaru")
    if effr is not None and dfedtaru is not None:
        e_val = effr.latest
        top = dfedtaru.latest
        triggered = bool(
            effr.ok and dfedtaru.ok and e_val is not None and top is not None
            and e_val >= top - 0.01
        )
        flags.append(PlumbingFlag(
            name="effr_top_of_range",
            value=e_val,
            triggered=triggered,
            note=(f"EFFR {e_val:.2f} within 1bp of range top {top:.2f} = reserves tightening"
                  if (e_val is not None and top is not None)
                  else "EFFR/target-top unavailable"),
            staleness=_staleness_from(effr),
        ))

    # --- net_liq_5d_drop: 5d_change < 0 (context_only) ---
    net_liq = series_by_key.get("net_liquidity")
    if net_liq is not None:
        chg5 = _series_5d_change(net_liq) if net_liq.ok else None
        triggered = bool(net_liq.ok and chg5 is not None and chg5 < 0)
        flags.append(PlumbingFlag(
            name="net_liq_5d_drop",
            value=chg5,
            triggered=triggered,
            note=(f"Net liquidity 5d change {chg5:+.0f} — CONTEXT only, not an oracle "
                  "(knobs.net_liquidity=context_only)"
                  if chg5 is not None
                  else "Net liquidity 5d change unavailable — CONTEXT only"),
            staleness=_staleness_from(net_liq, expected_max_age_days=10),
        ))

    return flags


# ---------------------------------------------------------------------------
# Meta + assembly
# ---------------------------------------------------------------------------
def _count_reds(result: AnomalyResult) -> int:
    reds = sum(1 for t in result.tiles if t.color == "red")
    for comp in (result.composites.ofr_fsi, result.composites.nfci,
                 result.composites.anfci, result.composites.stlfsi4):
        if comp is not None and comp.color == "red":
            reds += 1
    return reds


def _knob(config: Config, name: str, default: Optional[str]) -> Optional[str]:
    return config.raw.get("knobs", {}).get(name, default)


def assemble_brief(
    *,
    date: str,
    run_ts_utc: str,
    config: Config,
    result: AnomalyResult,
    series_by_key: dict[str, RawSeries],
    calendar: list[CalendarEvent],
    degraded: list[str],
) -> Brief:
    """Compose the full models.Brief.

    Sets meta (date, run_ts_utc, config_hash, knob echoes, degraded_sources,
    calm_morning = (#reds <= calibration.calm_morning_max_reds)). Assembles
    composites/tiles from `result`, cards from build_cards, plumbing from
    build_plumbing_flags, calendar/corr_breaks/dog_didnt_bark passthrough.
    """
    max_reds = int(
        config.raw.get("calibration", {}).get("calm_morning_max_reds", 1)
    )
    reds = _count_reds(result)
    calm = reds <= max_reds

    meta = Meta(
        date=date,
        run_ts_utc=run_ts_utc,
        config_hash=config.config_hash,
        vol_model=_knob(config, "vol_model", None),
        percentile_window=_knob(config, "percentile_window", None),
        fpr_control=_knob(config, "fpr_control", None),
        calm_morning=calm,
        degraded_sources=list(degraded),
    )

    cards = build_cards(result, calendar, config)
    plumbing = build_plumbing_flags(series_by_key, config)

    return Brief(
        meta=meta,
        composites=result.composites,
        tiles=result.tiles,
        cards=cards,
        calendar=list(calendar),
        plumbing_flags=plumbing,
        corr_breaks=list(result.corr_breaks),
        dog_didnt_bark=list(result.dog_didnt_bark),
    )


# ---------------------------------------------------------------------------
# Durable JSON record
# ---------------------------------------------------------------------------
def _validate_against_schema(payload: dict[str, Any]) -> None:
    """Best-effort schema validation. Silent no-op if jsonschema/schema missing."""
    try:
        import jsonschema  # type: ignore
    except Exception:
        return
    schema_path = Path(__file__).resolve().parent.parent / "schema" / "brief.schema.json"
    if not schema_path.exists():
        return
    schema = json.loads(schema_path.read_text())
    jsonschema.validate(instance=payload, schema=schema)


def write_brief_json(brief: Brief, config: Config, *, out_dir: Path | None = None) -> Path:
    """Write brief.to_dict() to {data_dir}/{date}.json (the durable record).

    Also maintains {data_dir}/index.json — the sorted list of all archived days
    (so render/archive can enumerate history without a directory scan).
    out_dir overrides config.output.data_dir (used in tests). Returns the path.
    Validates against schema/brief.schema.json if a validator is available.
    """
    if out_dir is not None:
        data_dir = Path(out_dir)
    else:
        configured = config.raw.get("output", {}).get("data_dir", "data")
        data_dir = Path(configured)
        if not data_dir.is_absolute():
            data_dir = Path(__file__).resolve().parent.parent / configured

    data_dir.mkdir(parents=True, exist_ok=True)

    payload = brief.to_dict()
    _validate_against_schema(payload)

    out_path = data_dir / f"{brief.meta.date}.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")

    _update_index(data_dir, brief)
    return out_path


def _update_index(data_dir: Path, brief: Brief) -> Path:
    """Append/update this day in {data_dir}/index.json (newest-first list)."""
    index_path = data_dir / "index.json"
    entries: list[dict[str, Any]] = []
    if index_path.exists():
        try:
            loaded = json.loads(index_path.read_text())
            if isinstance(loaded, dict):
                entries = loaded.get("days", [])
            elif isinstance(loaded, list):
                entries = loaded
        except (json.JSONDecodeError, OSError):
            entries = []

    reds = sum(1 for t in brief.tiles if t.color == "red")
    entry = {
        "date": brief.meta.date,
        "run_ts_utc": brief.meta.run_ts_utc,
        "calm_morning": brief.meta.calm_morning,
        "red_count": reds,
        "card_count": len(brief.cards),
        "degraded_sources": brief.meta.degraded_sources,
        "file": f"{brief.meta.date}.json",
    }

    # Upsert on date, then sort newest-first.
    by_date = {e.get("date"): e for e in entries if isinstance(e, dict)}
    by_date[brief.meta.date] = entry
    days = sorted(by_date.values(), key=lambda e: e.get("date", ""), reverse=True)

    index_path.write_text(
        json.dumps({"schema_version": brief.schema_version, "days": days}, indent=2) + "\n"
    )
    return index_path
