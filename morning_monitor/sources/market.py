"""yfinance delayed-EOD fetcher — BUILD TARGET 1 (ingestion).

Pulls delayed/EOD daily history for equities, vol, FX, commodity, and BTC tickers
via yfinance. Snapshot-valid (delayed data is acceptable per project posture).

yfinance is imported lazily so the package still imports (and the rest of the
pipeline still runs against a fixture) on hosts where yfinance is not installed —
a missing library simply degrades the affected tiles, it never crashes the run.
"""

from __future__ import annotations

from ..models import HistoryPoint, RawSeries


def fetch_yf_series(
    ticker: str,
    *,
    years: int = 3,
    field: str = "Close",
    lag_desc: str = "EOD delayed",
    tile_key: str | None = None,
) -> RawSeries:
    """Fetch one yfinance ticker as a RawSeries (oldest->newest close).

    tile_key defaults to f'yfinance:{ticker}'. On any yfinance error/empty frame
    return RawSeries(ok=False, error=...). asof = last bar date.
    """
    key = tile_key if tile_key is not None else f"yfinance:{ticker}"
    source = f"yfinance:{ticker}"

    try:
        import yfinance as yf  # lazy: optional dependency
    except Exception as exc:  # noqa: BLE001
        return RawSeries(
            key=key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error=f"yfinance unavailable: {exc!r}",
        )

    period = f"{max(years, 1)}y"
    try:
        frame = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
    except Exception as exc:  # noqa: BLE001 — graceful degradation
        return RawSeries(
            key=key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error=f"fetch failed: {exc!r}",
        )

    if frame is None or getattr(frame, "empty", True) or field not in frame.columns:
        return RawSeries(
            key=key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error="fetch failed: no data returned",
        )

    history: list[HistoryPoint] = []
    for idx, raw_val in frame[field].items():
        try:
            value = float(raw_val)
        except (TypeError, ValueError):
            continue
        if value != value:  # NaN guard
            continue
        # idx is a pandas Timestamp; normalise to YYYY-MM-DD
        try:
            obs_date = idx.date().isoformat()
        except Exception:  # noqa: BLE001
            obs_date = str(idx)[:10]
        history.append(HistoryPoint(date=obs_date, value=value))

    history.sort(key=lambda h: h.date)

    if not history:
        return RawSeries(
            key=key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error="fetch failed: empty frame",
        )

    return RawSeries(
        key=key, source=source, history=history,
        asof=history[-1].date, lag_desc=lag_desc, ok=True, error=None,
    )
