"""FRED API fetcher — BUILD TARGET 1 (ingestion).

Pulls daily observations (>=3y) for a FRED series id via the FRED API. Used for
rates, credit, dollar, plumbing, and composite (NFCI/ANFCI/STLFSI4) series.

API: GET {base}/series/observations?series_id=ID&api_key=KEY&file_type=json
     &observation_start=YYYY-MM-DD . FRED uses "." for missing observations.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from ..models import HistoryPoint, RawSeries


def fetch_fred_series(
    series_id: str,
    *,
    api_key: str,
    base_url: str,
    http: httpx.Client,
    years: int = 3,
    lag_desc: str = "FRED",
    tile_key: str | None = None,
) -> RawSeries:
    """Fetch one FRED series as a RawSeries (oldest->newest).

    tile_key defaults to f'fred:{series_id}' if None. Drop "." (missing) rows.
    On HTTP/parse error return RawSeries(ok=False, error=...). asof = last real obs date.
    """
    key = tile_key if tile_key is not None else f"fred:{series_id}"
    source = f"fred:{series_id}"

    if not api_key:
        return RawSeries(
            key=key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error="FRED_API_KEY not set",
        )

    # >=3y of daily history for the 1y/3y percentile baselines.
    start = (date.today() - timedelta(days=int(365.25 * years) + 30)).isoformat()
    url = f"{base_url.rstrip('/')}/series/observations"
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start,
    }

    try:
        resp = http.get(url, params=params, timeout=30.0)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — graceful degradation, never raise
        return RawSeries(
            key=key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error=f"fetch failed: {exc!r}",
        )

    observations = payload.get("observations")
    if not isinstance(observations, list):
        return RawSeries(
            key=key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error="no observations in response",
        )

    history: list[HistoryPoint] = []
    for obs in observations:
        raw_val = obs.get("value")
        obs_date = obs.get("date")
        if raw_val is None or raw_val == "." or obs_date is None:
            continue  # FRED missing marker
        try:
            value = float(raw_val)
        except (TypeError, ValueError):
            continue
        history.append(HistoryPoint(date=obs_date, value=value))

    history.sort(key=lambda h: h.date)  # oldest -> newest

    if not history:
        return RawSeries(
            key=key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error="no valid observations returned",
        )

    return RawSeries(
        key=key, source=source, history=history,
        asof=history[-1].date, lag_desc=lag_desc, ok=True, error=None,
    )
