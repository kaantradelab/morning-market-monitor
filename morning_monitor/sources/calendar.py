"""Economic-calendar fetcher — BUILD TARGET 1 (ingestion).

Pulls today's scheduled releases + consensus from Finnhub (or FMP). Ranks events
by cross-asset transmission power (reference section 3), flags high_impact via
config.calendar.high_impact_events, and optionally attaches prior Citi surprise.

    fetch_calendar(config, date, *, http) -> list[CalendarEvent]

Returns [] on failure (graceful degradation — an empty calendar strip, not a crash).
"""

from __future__ import annotations

from typing import Optional

import httpx

from ..config import Config
from ..models import CalendarEvent

# Cross-asset transmission-power ranking (reference section 3). Lower rank = stronger.
# Keys are lowercase substrings matched against the event title.
_TRANSMISSION_RANK: list[tuple[str, int]] = [
    ("fomc", 1), ("fed interest rate", 1), ("federal funds", 1),
    ("cpi", 2), ("consumer price", 2),
    ("nonfarm", 3), ("nfp", 3), ("payroll", 3), ("unemployment rate", 3), ("average hourly", 3),
    ("ecb", 4), ("boj", 4), ("bank of japan", 4), ("boe", 4), ("bank of england", 4),
    ("core pce", 5), ("pce price", 5), ("gdp", 5),
    ("refunding", 6), ("auction", 6),
    ("ism", 7), ("pmi", 7), ("jolts", 7), ("retail sales", 7), ("ppi", 7), ("producer price", 7),
    ("h.4.1", 8), ("balance sheet", 8),
    ("china", 9), ("eurozone cpi", 9), ("flash pmi", 9),
]

_IMPACT_MAP = {"high": True, "3": True, "medium": False, "2": False, "low": False, "1": False}


def fetch_calendar(config: Config, date: str, *, http: Optional[httpx.Client] = None) -> list[CalendarEvent]:
    """Fetch CalendarEvent list for `date` (YYYY-MM-DD, Istanbul).

    Finnhub: GET {base}/calendar/economic?from=DATE&to=DATE&token=KEY .
    Sets high_impact from config.calendar.high_impact_events (or Finnhub's own
    impact field), and rank from the transmission-power ordering. On any error
    return []. Never raises.
    """
    owns_client = http is None
    client = http or httpx.Client()
    try:
        return _fetch_finnhub(config, date, client)
    except Exception:  # noqa: BLE001 — graceful degradation, empty strip not a crash
        return []
    finally:
        if owns_client:
            client.close()


def _fetch_finnhub(config: Config, date: str, http: httpx.Client) -> list[CalendarEvent]:
    raw = getattr(config, "raw", {}) or {}
    sources = raw.get("sources", {})
    cal_cfg = raw.get("calendar", {}) or {}
    high_impact_events = [str(e).lower() for e in cal_cfg.get("high_impact_events", [])]

    finnhub_cfg = sources.get("finnhub", {}) or {}
    base_url = finnhub_cfg.get("base_url", "https://finnhub.io/api/v1")

    api_key = None
    getter = getattr(config, "finnhub_api_key", None)
    if callable(getter):
        try:
            api_key = getter()
        except Exception:  # noqa: BLE001
            api_key = None
    if not api_key:
        return []

    url = f"{base_url.rstrip('/')}/calendar/economic"
    params: dict[str, str] = {"from": date, "to": date, "token": str(api_key)}
    resp = http.get(url, params=params, timeout=30.0)
    resp.raise_for_status()
    payload = resp.json()

    rows = payload.get("economicCalendar", payload) if isinstance(payload, dict) else payload
    if isinstance(rows, dict):
        rows = rows.get("result", [])
    if not isinstance(rows, list):
        return []

    events: list[CalendarEvent] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        events.append(_to_event(row, high_impact_events))

    # Rank ascending (strongest transmission first); None rank sinks to the end.
    events.sort(key=lambda e: (e.rank if e.rank is not None else 999, e.event))
    return events


def _to_event(row: dict, high_impact_events: list[str]) -> CalendarEvent:
    title = str(row.get("event") or row.get("name") or "").strip()
    time_val = row.get("time") or row.get("date") or row.get("datetime")
    time_str = str(time_val) if time_val is not None else None

    consensus = row.get("estimate")
    if consensus is None:
        consensus = row.get("consensus")
    consensus = _coerce_consensus(consensus)

    rank = _rank_for(title)

    # high_impact: config keyword match OR provider impact field.
    title_low = title.lower()
    by_config = any(kw in title_low for kw in high_impact_events)
    impact_field = str(row.get("impact", "")).strip().lower()
    by_provider = _IMPACT_MAP.get(impact_field, False)
    high_impact = bool(by_config or by_provider)

    prior = row.get("prior_citi_surprise")
    prior = _coerce_float(prior)

    return CalendarEvent(
        event=title,
        time=time_str,
        consensus=consensus,
        high_impact=high_impact,
        prior_citi_surprise=prior,
        rank=rank,
    )


def _rank_for(title: str) -> Optional[int]:
    low = title.lower()
    for needle, rank in _TRANSMISSION_RANK:
        if needle in low:
            return rank
    return None


def _coerce_consensus(value) -> Optional[float | str]:
    if value is None or value == "":
        return None
    f = _coerce_float(value)
    if f is not None:
        return f
    return str(value)


def _coerce_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
