"""DefiLlama crypto fetcher — BUILD TARGET 1 (ingestion).

Pulls the stablecoin aggregate market-cap history (crypto "dry powder") from the
DefiLlama stablecoins API. BTC itself comes via market.py (yfinance BTC-USD).
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from ..models import HistoryPoint, RawSeries


def fetch_stablecoin_cap(*, base_url: str, http: httpx.Client, tile_key: str = "stablecoin_cap") -> RawSeries:
    """Fetch aggregate stablecoin market cap history as a RawSeries.

    DefiLlama: GET {base}/stablecoincharts/all -> list of
    {date(unix-seconds str), totalCirculatingUSD:{peggedUSD: float}}.
    On error return RawSeries(ok=False, error=...). lag_desc = 'DefiLlama daily'.
    """
    source = "defillama:stablecoins"
    lag_desc = "DefiLlama daily"
    url = f"{base_url.rstrip('/')}/stablecoincharts/all"

    try:
        resp = http.get(url, timeout=30.0)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — graceful degradation
        return RawSeries(
            key=tile_key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error=f"fetch failed: {exc!r}",
        )

    if not isinstance(payload, list):
        return RawSeries(
            key=tile_key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error="unexpected payload shape",
        )

    history: list[HistoryPoint] = []
    for point in payload:
        if not isinstance(point, dict):
            continue
        ts = point.get("date")
        cap = point.get("totalCirculatingUSD")
        # totalCirculatingUSD may be a dict {peggedUSD: ...} or a scalar
        if isinstance(cap, dict):
            cap = cap.get("peggedUSD")
        if ts is None or cap is None:
            continue
        try:
            obs_date = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
            value = float(cap)
        except (TypeError, ValueError, OSError):
            continue
        history.append(HistoryPoint(date=obs_date, value=value))

    history.sort(key=lambda h: h.date)

    if not history:
        return RawSeries(
            key=tile_key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error="no valid points returned",
        )

    return RawSeries(
        key=tile_key, source=source, history=history,
        asof=history[-1].date, lag_desc=lag_desc, ok=True, error=None,
    )
