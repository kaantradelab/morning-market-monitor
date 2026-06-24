"""Derived-tile builders — BUILD TARGET 1 (ingestion).

Tiles that are computed from other fetched series rather than fetched directly:

  net_liquidity = WALCL - WTREGEN(TGA) - RRPONTSYD     (axis 11, CONSTRUCT)
  sofr_iorb     = SOFR - IORB                          (axis 11, plumbing)
  copper_gold   = HG=F / GC=F                          (axis 7, clean growth read)
  rsp_spy       = RSP / SPY                            (axis 10, equal vs cap weight)
  move_proxy    = 20d realized vol of DGS10 first-diffs (axis 9, free MOVE proxy)

Each takes already-fetched RawSeries inputs and returns a derived RawSeries.
Date-align inputs on common dates; propagate ok=False if any required input failed.
"""

from __future__ import annotations

from ..models import HistoryPoint, RawSeries


def _series_map(series: RawSeries) -> dict[str, float]:
    """{date: value} for the non-null points of a series."""
    return {h.date: h.value for h in series.history if h.value is not None}


def _stalest_lag(*inputs: RawSeries) -> str:
    """Inherit the lag descriptor of the input with the latest (stalest) asof."""
    candidates = [s for s in inputs if s.asof]
    if not candidates:
        return inputs[0].lag_desc if inputs else ""
    stalest = min(candidates, key=lambda s: s.asof or "9999-99-99")
    return stalest.lag_desc


def _fail(tile_key: str, source: str, lag_desc: str, reason: str) -> RawSeries:
    return RawSeries(
        key=tile_key, source=source, history=[], asof=None,
        lag_desc=lag_desc, ok=False, error=reason,
    )


def _combine(
    inputs: list[RawSeries],
    fn,
    *,
    tile_key: str,
    source: str,
) -> RawSeries:
    """Date-align all inputs on the intersection of dates and apply fn(values...)."""
    lag_desc = _stalest_lag(*inputs)

    for s in inputs:
        if not s.ok:
            return _fail(tile_key, source, lag_desc, f"input '{s.key}' degraded: {s.error}")

    maps = [_series_map(s) for s in inputs]
    if any(not m for m in maps):
        return _fail(tile_key, source, lag_desc, "input has no usable datapoints")

    common = set(maps[0])
    for m in maps[1:]:
        common &= set(m)
    if not common:
        return _fail(tile_key, source, lag_desc, "no overlapping dates across inputs")

    history: list[HistoryPoint] = []
    for d in sorted(common):
        try:
            value = fn(*(m[d] for m in maps))
        except (ZeroDivisionError, TypeError, ValueError):
            continue
        if value is None or value != value:  # NaN guard
            continue
        history.append(HistoryPoint(date=d, value=value))

    if not history:
        return _fail(tile_key, source, lag_desc, "no datapoints survived combination")

    return RawSeries(
        key=tile_key, source=source, history=history,
        asof=history[-1].date, lag_desc=lag_desc, ok=True, error=None,
    )


def build_net_liquidity(walcl: RawSeries, tga: RawSeries, rrp: RawSeries, *, tile_key: str = "net_liquidity") -> RawSeries:
    """WALCL - TGA - RRP, date-aligned. lag_desc inherits the stalest input."""
    return _combine(
        [walcl, tga, rrp],
        lambda w, t, r: w - t - r,
        tile_key=tile_key,
        source="derived:fred_walcl_wtregen_rrpontsyd",
    )


def build_sofr_iorb(sofr: RawSeries, iorb: RawSeries, *, tile_key: str = "sofr_iorb") -> RawSeries:
    """SOFR - IORB in percentage points, date-aligned."""
    return _combine(
        [sofr, iorb],
        lambda s, i: s - i,
        tile_key=tile_key,
        source="derived:fred_sofr_iorb",
    )


def build_ratio(numerator: RawSeries, denominator: RawSeries, *, tile_key: str) -> RawSeries:
    """Generic A/B ratio (copper_gold, rsp_spy), date-aligned."""
    src = f"derived:ratio:{numerator.key}/{denominator.key}"
    return _combine(
        [numerator, denominator],
        lambda a, b: (a / b) if b not in (0, 0.0) else None,
        tile_key=tile_key,
        source=src,
    )


def build_realized_vol(series: RawSeries, *, window: int = 20, tile_key: str = "move_proxy") -> RawSeries:
    """Rolling realized vol of first-differences (free MOVE proxy from DGS10).

    realized_vol_t = stddev(first_diff over the trailing `window` observations).
    Population stddev; emits a point once `window` diffs are available.
    """
    source = "derived:realized_vol_dgs10_20d"
    lag_desc = series.lag_desc

    if not series.ok:
        return _fail(tile_key, source, lag_desc, f"input '{series.key}' degraded: {series.error}")

    points = [(h.date, h.value) for h in series.history if h.value is not None]
    points.sort(key=lambda p: p[0])
    if len(points) < window + 1:
        return _fail(tile_key, source, lag_desc, f"need >{window} obs, have {len(points)}")

    diffs: list[tuple[str, float]] = []
    for i in range(1, len(points)):
        diffs.append((points[i][0], points[i][1] - points[i - 1][1]))

    history: list[HistoryPoint] = []
    for i in range(window - 1, len(diffs)):
        win = [d[1] for d in diffs[i - window + 1 : i + 1]]
        n = len(win)
        mean = sum(win) / n
        var = sum((x - mean) ** 2 for x in win) / n
        rv = var ** 0.5
        history.append(HistoryPoint(date=diffs[i][0], value=rv))

    if not history:
        return _fail(tile_key, source, lag_desc, "no realized-vol points produced")

    return RawSeries(
        key=tile_key, source=source, history=history,
        asof=history[-1].date, lag_desc=lag_desc, ok=True, error=None,
    )
