"""Typed in-code representation of the per-day brief JSON contract.

These dataclasses mirror schema/brief.schema.json 1:1. The JSON file on disk is
the durable spine; these are the typed objects modules pass to each other:

    ingestion  -> dict[str, RawSeries]          (raw, pre-stats)
    anomaly    -> list[Tile] + Composites        (enriched)
    brief      -> Brief                          (assembled)
    render     -> reads Brief                    (HTML)

Every dataclass provides `to_dict()` (JSON-serializable) and `from_dict()` so the
brief can round-trip through schema/brief.schema.json. Keep field names identical
to the schema property names.

Python 3.13. Stdlib only (dataclasses, typing). No external deps in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal, Optional

SCHEMA_VERSION = "1.0.0"

Color = Literal["green", "amber", "red", "gray"]
Transform = Literal["log_return", "first_diff", "level", "ratio", "sign"]


# ---------------------------------------------------------------------------
# Raw ingestion type (pre-stats) — what ingestion returns, anomaly consumes
# ---------------------------------------------------------------------------
@dataclass
class HistoryPoint:
    """One (date, value) datapoint for a series / sparkline."""
    date: str                     # YYYY-MM-DD
    value: Optional[float]

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "value": self.value}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HistoryPoint":
        return cls(date=d["date"], value=d.get("value"))


@dataclass
class RawSeries:
    """Raw fetched series for one tile, BEFORE any stats. Produced by ingestion.

    `history` is the full pulled series (>=3y, oldest->newest) used by the anomaly
    engine to compute EWMA-z / percentiles. `staleness` is set at fetch time.
    A failed fetch yields ok=False with empty history (-> tile flagged gray/stale).
    """
    key: str                                   # tile key, matches config
    source: str                                # e.g. 'fred:DGS2'
    history: list[HistoryPoint] = field(default_factory=list)
    asof: Optional[str] = None                 # YYYY-MM-DD of latest datapoint
    lag_desc: str = ""                         # human freshness descriptor
    ok: bool = True                            # False if fetch failed (degraded)
    error: Optional[str] = None                # failure reason for the degraded log

    @property
    def latest(self) -> Optional[float]:
        return self.history[-1].value if self.history else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source": self.source,
            "history": [h.to_dict() for h in self.history],
            "asof": self.asof,
            "lag_desc": self.lag_desc,
            "ok": self.ok,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RawSeries":
        return cls(
            key=d["key"],
            source=d["source"],
            history=[HistoryPoint.from_dict(h) for h in d.get("history", [])],
            asof=d.get("asof"),
            lag_desc=d.get("lag_desc", ""),
            ok=d.get("ok", True),
            error=d.get("error"),
        )


# ---------------------------------------------------------------------------
# Brief sub-objects (mirror schema $defs)
# ---------------------------------------------------------------------------
@dataclass
class Staleness:
    asof: Optional[str]           # YYYY-MM-DD
    lag_desc: str
    is_stale: bool

    def to_dict(self) -> dict[str, Any]:
        return {"asof": self.asof, "lag_desc": self.lag_desc, "is_stale": self.is_stale}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Staleness":
        return cls(asof=d.get("asof"), lag_desc=d["lag_desc"], is_stale=d["is_stale"])


@dataclass
class Composite:
    value: Optional[float]
    level_pct: Optional[float]
    change_score: Optional[float]
    color: Optional[Color] = None
    staleness: Optional[Staleness] = None
    history: list[HistoryPoint] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "level_pct": self.level_pct,
            "change_score": self.change_score,
            "color": self.color,
            "staleness": self.staleness.to_dict() if self.staleness else None,
            "history": [h.to_dict() for h in self.history],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Composite":
        return cls(
            value=d.get("value"),
            level_pct=d.get("level_pct"),
            change_score=d.get("change_score"),
            color=d.get("color"),
            staleness=Staleness.from_dict(d["staleness"]) if d.get("staleness") else None,
            history=[HistoryPoint.from_dict(h) for h in d.get("history", [])],
        )


@dataclass
class Composites:
    ofr_fsi: Optional[Composite]
    nfci: Optional[Composite]
    anfci: Optional[Composite]
    stlfsi4: Optional[Composite] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ofr_fsi": self.ofr_fsi.to_dict() if self.ofr_fsi else None,
            "nfci": self.nfci.to_dict() if self.nfci else None,
            "anfci": self.anfci.to_dict() if self.anfci else None,
        }
        if self.stlfsi4 is not None:
            out["stlfsi4"] = self.stlfsi4.to_dict()
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Composites":
        def opt(k: str) -> Optional[Composite]:
            return Composite.from_dict(d[k]) if d.get(k) else None
        return cls(ofr_fsi=opt("ofr_fsi"), nfci=opt("nfci"), anfci=opt("anfci"), stlfsi4=opt("stlfsi4"))


@dataclass
class Tile:
    key: str
    axis: int
    label: str
    source: str
    value: Optional[float]
    change: Optional[float]
    transform: Transform
    ewma_z: Optional[float]
    pct_1y: Optional[float]
    pct_3y: Optional[float]
    level_pct_756: Optional[float]
    robust_z: Optional[float]
    color: Color
    staleness: Staleness
    history: list[HistoryPoint] = field(default_factory=list)
    is_front_screen: bool = False
    note: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "axis": self.axis,
            "label": self.label,
            "source": self.source,
            "value": self.value,
            "change": self.change,
            "transform": self.transform,
            "ewma_z": self.ewma_z,
            "pct_1y": self.pct_1y,
            "pct_3y": self.pct_3y,
            "level_pct_756": self.level_pct_756,
            "robust_z": self.robust_z,
            "color": self.color,
            "staleness": self.staleness.to_dict(),
            "history": [h.to_dict() for h in self.history],
            "is_front_screen": self.is_front_screen,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Tile":
        return cls(
            key=d["key"], axis=d["axis"], label=d["label"], source=d["source"],
            value=d.get("value"), change=d.get("change"), transform=d["transform"],
            ewma_z=d.get("ewma_z"), pct_1y=d.get("pct_1y"), pct_3y=d.get("pct_3y"),
            level_pct_756=d.get("level_pct_756"), robust_z=d.get("robust_z"),
            color=d["color"], staleness=Staleness.from_dict(d["staleness"]),
            history=[HistoryPoint.from_dict(h) for h in d.get("history", [])],
            is_front_screen=d.get("is_front_screen", False), note=d.get("note"),
        )


@dataclass
class WhyNow:
    """MANDATORY on every card. A card without a populated why_now is suppressed."""
    percentile_or_z: str
    calendar_event: Optional[str]
    cross_asset_confirm_or_contradict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "percentile_or_z": self.percentile_or_z,
            "calendar_event": self.calendar_event,
            "cross_asset_confirm_or_contradict": self.cross_asset_confirm_or_contradict,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WhyNow":
        return cls(
            percentile_or_z=d["percentile_or_z"],
            calendar_event=d.get("calendar_event"),
            cross_asset_confirm_or_contradict=d["cross_asset_confirm_or_contradict"],
        )


@dataclass
class Card:
    title: str
    metric: str
    score_desc: str
    why_now: WhyNow
    color: Optional[Color] = None
    is_banner: bool = False
    tile_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "metric": self.metric,
            "score_desc": self.score_desc,
            "why_now": self.why_now.to_dict(),
            "color": self.color,
            "is_banner": self.is_banner,
            "tile_keys": self.tile_keys,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Card":
        return cls(
            title=d["title"], metric=d["metric"], score_desc=d["score_desc"],
            why_now=WhyNow.from_dict(d["why_now"]), color=d.get("color"),
            is_banner=d.get("is_banner", False), tile_keys=d.get("tile_keys", []),
        )


@dataclass
class CalendarEvent:
    event: str
    time: Optional[str]
    consensus: Optional[float | str]
    high_impact: bool
    prior_citi_surprise: Optional[float]
    rank: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event, "time": self.time, "consensus": self.consensus,
            "high_impact": self.high_impact, "prior_citi_surprise": self.prior_citi_surprise,
            "rank": self.rank,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CalendarEvent":
        return cls(
            event=d["event"], time=d.get("time"), consensus=d.get("consensus"),
            high_impact=d["high_impact"], prior_citi_surprise=d.get("prior_citi_surprise"),
            rank=d.get("rank"),
        )


@dataclass
class PlumbingFlag:
    name: str
    value: Optional[float]
    triggered: bool
    note: str
    staleness: Optional[Staleness] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "value": self.value, "triggered": self.triggered,
            "note": self.note,
            "staleness": self.staleness.to_dict() if self.staleness else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlumbingFlag":
        return cls(
            name=d["name"], value=d.get("value"), triggered=d["triggered"], note=d["note"],
            staleness=Staleness.from_dict(d["staleness"]) if d.get("staleness") else None,
        )


@dataclass
class CorrBreak:
    name: str
    residual_z: Optional[float]
    persistence_days: int
    triggered: bool
    note: str
    sign_flip: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "residual_z": self.residual_z,
            "persistence_days": self.persistence_days, "triggered": self.triggered,
            "sign_flip": self.sign_flip, "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CorrBreak":
        return cls(
            name=d["name"], residual_z=d.get("residual_z"),
            persistence_days=d["persistence_days"], triggered=d["triggered"],
            note=d["note"], sign_flip=d.get("sign_flip", False),
        )


@dataclass
class DogDidntBark:
    tile_key: str
    event: str
    expected_move: Optional[float]
    realized_move: Optional[float]
    ratio: Optional[float]
    triggered: bool
    note: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tile_key": self.tile_key, "event": self.event,
            "expected_move": self.expected_move, "realized_move": self.realized_move,
            "ratio": self.ratio, "triggered": self.triggered, "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DogDidntBark":
        return cls(
            tile_key=d["tile_key"], event=d["event"], expected_move=d.get("expected_move"),
            realized_move=d.get("realized_move"), ratio=d.get("ratio"),
            triggered=d["triggered"], note=d.get("note"),
        )


@dataclass
class Meta:
    date: str                     # YYYY-MM-DD (Istanbul logical morning)
    run_ts_utc: str               # ISO 8601 UTC
    config_hash: str
    vol_model: Optional[str] = None
    percentile_window: Optional[str] = None
    fpr_control: Optional[str] = None
    calm_morning: Optional[bool] = None
    degraded_sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Meta":
        return cls(
            date=d["date"], run_ts_utc=d["run_ts_utc"], config_hash=d["config_hash"],
            vol_model=d.get("vol_model"), percentile_window=d.get("percentile_window"),
            fpr_control=d.get("fpr_control"), calm_morning=d.get("calm_morning"),
            degraded_sources=d.get("degraded_sources", []),
        )


# ---------------------------------------------------------------------------
# The root brief — the spine
# ---------------------------------------------------------------------------
@dataclass
class Brief:
    meta: Meta
    composites: Composites
    tiles: list[Tile]
    cards: list[Card]
    calendar: list[CalendarEvent]
    plumbing_flags: list[PlumbingFlag]
    corr_breaks: list[CorrBreak]
    dog_didnt_bark: list[DogDidntBark]
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "meta": self.meta.to_dict(),
            "composites": self.composites.to_dict(),
            "tiles": [t.to_dict() for t in self.tiles],
            "cards": [c.to_dict() for c in self.cards],
            "calendar": [e.to_dict() for e in self.calendar],
            "plumbing_flags": [f.to_dict() for f in self.plumbing_flags],
            "corr_breaks": [cb.to_dict() for cb in self.corr_breaks],
            "dog_didnt_bark": [d.to_dict() for d in self.dog_didnt_bark],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Brief":
        return cls(
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            meta=Meta.from_dict(d["meta"]),
            composites=Composites.from_dict(d["composites"]),
            tiles=[Tile.from_dict(t) for t in d.get("tiles", [])],
            cards=[Card.from_dict(c) for c in d.get("cards", [])],
            calendar=[CalendarEvent.from_dict(e) for e in d.get("calendar", [])],
            plumbing_flags=[PlumbingFlag.from_dict(f) for f in d.get("plumbing_flags", [])],
            corr_breaks=[CorrBreak.from_dict(cb) for cb in d.get("corr_breaks", [])],
            dog_didnt_bark=[DogDidntBark.from_dict(x) for x in d.get("dog_didnt_bark", [])],
        )
