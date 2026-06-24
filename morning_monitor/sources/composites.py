"""Composite stress-index fetchers — BUILD TARGET 1 (ingestion).

The 'one-number' anchors the anomaly engine detects on FIRST:
  OFR FSI  — financialresearch.gov (daily, 2-bd lag); download CSV.
  NFCI / ANFCI / STLFSI4 — FRED weekly series (use fred.fetch_fred_series).

Returns RawSeries keyed 'ofr_fsi' / 'nfci' / 'anfci' / 'stlfsi4'. The brief module
maps these into models.Composites with level_pct + change_score from the anomaly engine.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence

import httpx

from ..models import HistoryPoint, RawSeries

# OFR publishes the Financial Stress Index as a downloadable CSV.
OFR_FSI_CSV_URL = "https://www.financialresearch.gov/financial-stress-index/data/fsi.csv"


def fetch_ofr_fsi(*, http: httpx.Client, tile_key: str = "ofr_fsi") -> RawSeries:
    """Fetch the OFR Financial Stress Index daily history.

    financialresearch.gov publishes a downloadable CSV; parse date,value.
    The OFR FSI CSV has a 'Date' column and an 'OFR FSI' (total index) column.
    lag_desc = 'OFR 2-bd lag'. On error return RawSeries(ok=False).
    """
    source = "ofr:fsi"
    lag_desc = "OFR 2-bd lag"

    try:
        resp = http.get(OFR_FSI_CSV_URL, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        text = resp.text
    except Exception as exc:  # noqa: BLE001 — graceful degradation
        return RawSeries(
            key=tile_key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error=f"fetch failed: {exc!r}",
        )

    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames or []
        date_col = _pick_column(fieldnames, ("date",))
        value_col = _pick_column(fieldnames, ("ofr fsi", "fsi", "total", "value"))
        if date_col is None or value_col is None:
            return RawSeries(
                key=tile_key, source=source, history=[], asof=None,
                lag_desc=lag_desc, ok=False,
                error=f"unexpected CSV columns: {fieldnames}",
            )

        history: list[HistoryPoint] = []
        for row in reader:
            obs_date = (row.get(date_col) or "").strip()
            raw_val = (row.get(value_col) or "").strip()
            if not obs_date or raw_val in ("", "."):
                continue
            try:
                value = float(raw_val)
            except ValueError:
                continue
            history.append(HistoryPoint(date=_normalize_date(obs_date), value=value))
    except Exception as exc:  # noqa: BLE001
        return RawSeries(
            key=tile_key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error=f"parse failed: {exc!r}",
        )

    history.sort(key=lambda h: h.date)

    if not history:
        return RawSeries(
            key=tile_key, source=source, history=[], asof=None,
            lag_desc=lag_desc, ok=False, error="no valid rows in CSV",
        )

    return RawSeries(
        key=tile_key, source=source, history=history,
        asof=history[-1].date, lag_desc=lag_desc, ok=True, error=None,
    )


def _pick_column(fieldnames: Sequence[str], wants: tuple[str, ...]) -> str | None:
    """Case-insensitive substring match for a column name."""
    lowered = {name.lower().strip(): name for name in fieldnames if name}
    # exact-ish then substring
    for want in wants:
        for low, original in lowered.items():
            if low == want:
                return original
    for want in wants:
        for low, original in lowered.items():
            if want in low:
                return original
    return None


def _normalize_date(value: str) -> str:
    """Best-effort normalise common CSV date forms to YYYY-MM-DD."""
    value = value.strip()
    # Already ISO-ish
    if len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return value[:10]
    # MM/DD/YYYY
    if "/" in value:
        parts = value.split("/")
        if len(parts) == 3:
            m, d, y = parts
            if len(y) == 2:
                y = "20" + y
            try:
                return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
            except ValueError:
                return value
    return value
