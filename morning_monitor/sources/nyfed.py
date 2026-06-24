"""NY Fed Standing Repo Facility (SRF) take-up fetcher — BUILD TARGET 1 (ingestion).

SRF take-up rising off ~0 is an early funding-stress tell (axis 11 plumbing). The
NY Fed publishes repo-operation results via the markets data API:

    GET https://markets.newyorkfed.org/api/rp/all/results/last/{n}.json

Each result carries an operation date and a total accepted amount. We sum the
accepted amount per date across SRF/repo operations to get a daily take-up series.
On any error or empty response we degrade gracefully (ok=False) — a missing SRF
tile, never a crashed run.
"""

from __future__ import annotations

from datetime import date as date_cls

import httpx

from ..models import HistoryPoint, RawSeries

NYFED_REPO_URL = "https://markets.newyorkfed.org/api/rp/all/results/last/{n}.json"


def fetch_srf_takeup(*, http: httpx.Client, years: int = 3, tile_key: str = "srf_takeup") -> RawSeries:
    """Fetch NY Fed repo (SRF) take-up history (USD billions) as a RawSeries.

    Sums accepted amounts per operation date. lag_desc = 'NY Fed daily ops'.
    """
    source = "nyfed:srf"
    lag_desc = "NY Fed daily ops"

    # ~252 ops/yr; pull generously then sort. Cap to keep the response sane.
    n = min(max(years, 1) * 300, 1000)
    url = NYFED_REPO_URL.format(n=n)

    try:
        resp = http.get(url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — graceful degradation
        return RawSeries(
            key=tile_key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error=f"fetch failed: {exc!r}",
        )

    operations = []
    if isinstance(payload, dict):
        repo = payload.get("repo", {})
        if isinstance(repo, dict):
            operations = repo.get("operations", [])
    if not isinstance(operations, list) or not operations:
        return RawSeries(
            key=tile_key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error="no repo operations in response",
        )

    per_date: dict[str, float] = {}
    for op in operations:
        if not isinstance(op, dict):
            continue
        op_date = op.get("operationDate") or op.get("operationDateTime")
        if not op_date:
            continue
        op_date = str(op_date)[:10]
        accepted = op.get("totalAmtAccepted")
        if accepted is None:
            # fall back to summing per-security accepted amounts
            details = op.get("details", [])
            if isinstance(details, list):
                accepted = 0.0
                for d in details:
                    if isinstance(d, dict) and d.get("amtAccepted") is not None:
                        try:
                            accepted += float(d["amtAccepted"])
                        except (TypeError, ValueError):
                            pass
        try:
            amt = float(accepted) if accepted is not None else 0.0
        except (TypeError, ValueError):
            continue
        # NY Fed reports in USD; express in billions for tile readability.
        per_date[op_date] = per_date.get(op_date, 0.0) + amt / 1e9

    if not per_date:
        return RawSeries(
            key=tile_key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error="no dated operations parsed",
        )

    history = [HistoryPoint(date=d, value=v) for d, v in sorted(per_date.items())]

    # Date-floor / baseline-depth guard: `last/{n}` returns the most-recent n ops
    # regardless of date, so in a busy regime n records can span < `years`. The
    # srf_takeup percentile baseline assumes >=3y of depth; if the oldest parsed
    # operationDate does not reach ~today-`years`, flag insufficient-baseline-depth
    # so the percentile scores are not over-trusted (mirrors the engine's
    # _MIN_SAMPLE_FOR_TILE_RED gating). Data is still returned (ok=True).
    error: str | None = None
    try:
        oldest = date_cls.fromisoformat(history[0].date[:10])
        floor = date_cls.today().replace(year=date_cls.today().year - max(years, 1))
        if oldest > floor:
            error = (
                f"insufficient-baseline-depth: oldest op {oldest.isoformat()} does not "
                f"reach ~{years}y floor {floor.isoformat()} (cap n={n} too low for this regime) "
                f"— srf_takeup percentile baseline is short"
            )
    except (ValueError, IndexError):
        pass

    return RawSeries(
        key=tile_key, source=source, history=history,
        asof=history[-1].date, lag_desc=lag_desc, ok=True, error=error,
    )
