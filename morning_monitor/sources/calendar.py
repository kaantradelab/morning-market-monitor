"""Economic-calendar fetcher — BUILD TARGET 1 (ingestion).

PRIMARY source = FMP (Financial Modeling Prep) economic calendar (Kaan's choice).
Optional SECONDARY fallback = Finnhub, used only if FMP yields nothing AND a
Finnhub key exists.
SPEC-3 addition: FRED release-dates provider (provider: fred).

    fetch_calendar(config, date, *, http) -> list[CalendarEvent]          (legacy)
    fetch_calendar_with_status(config, date, *, http)
        -> (list[CalendarEvent], degraded_reason: str | None)

NO SILENT SWALLOW (post-first-live-run fix): an HTTP/auth/access error (missing
key, 401/403, non-2xx, network/parse failure) returns a DEGRADED reason like
'calendar:no FMP key' / 'calendar:FMP 403' so meta.degraded_sources records it.
A genuine empty 200 (no scheduled releases that day) is NOT degraded — the brief
must never again show a FAILED source as a "calm / no events" morning.

Ranks events by cross-asset transmission power (reference section 3), flags
high_impact via config.calendar.high_impact_events or the provider impact field.
"""

from __future__ import annotations

import time
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from ..config import Config
from ..models import CalendarEvent
from ._retry import _RETRY_ATTEMPTS, _RETRY_BASE_DELAY, _RETRY_FACTOR

_FRED_RELEASES_BASE = "https://api.stlouisfed.org/fred"

# US macro releases land at fixed US-Eastern WALL-CLOCK times. We resolve that ET
# wall-clock to UTC DST-aware (ZoneInfo on the event's release DATE: summer EDT=
# UTC-4, winter EST=UTC-5), then Istanbul = UTC + 3 (no DST). Keys are lowercase
# substrings matched against the event/release title; the list is ordered MOST-
# SPECIFIC FIRST so e.g. "consumer confidence" (10:00) is not shadowed by
# "consumer price" (08:30). A title matching nothing keeps time=None (no guess).
_NY_TZ = ZoneInfo("America/New_York")

_ET_RELEASE_TIMES: list[tuple[str, tuple[int, int]]] = [
    # --- 08:30 ET ---
    ("consumer price", (8, 30)),                # CPI
    ("cpi", (8, 30)),
    ("employment situation", (8, 30)),          # NFP
    ("nonfarm", (8, 30)),
    ("nfp", (8, 30)),
    ("payroll", (8, 30)),
    ("gross domestic product", (8, 30)),        # GDP
    ("gdp", (8, 30)),
    ("personal income", (8, 30)),               # Personal Income & Outlays (PCE)
    ("pce", (8, 30)),
    ("producer price", (8, 30)),                # PPI
    ("ppi", (8, 30)),
    ("retail sales", (8, 30)),
    ("jobless claims", (8, 30)),                # Initial / Continuing Claims
    ("durable goods", (8, 30)),
    ("international trade", (8, 30)),            # Trade Balance
    ("trade balance", (8, 30)),
    # --- 10:00 ET ---
    ("jolts", (10, 0)),
    ("job openings", (10, 0)),
    ("ism", (10, 0)),                           # ISM Mfg / Services / PMI
    ("pmi", (10, 0)),
    ("surveys of consumers", (10, 0)),          # UMich sentiment
    ("umich", (10, 0)),
    ("consumer sentiment", (10, 0)),
    ("consumer confidence", (10, 0)),
    ("factory orders", (10, 0)),
    ("home sales", (10, 0)),                    # New / Existing Home Sales
    ("construction spending", (10, 0)),
    ("wholesale inventories", (10, 0)),
    # --- 13:00 ET — Treasury auctions (10Y/30Y note/bond) ---
    ("auction", (13, 0)),
    ("treasury note", (13, 0)),
    ("treasury bond", (13, 0)),
    # --- 14:00 ET — FOMC statement ---
    ("fomc", (14, 0)),
]

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

# Countries kept: US plus globally market-moving sovereigns/blocs. Empty/unknown
# country is kept (FMP sometimes omits it for global releases); the transmission
# rank then decides relevance.
_KEEP_COUNTRIES = {
    "us", "usa", "united states",
    "eu", "ea", "euro area", "eurozone", "european union",
    "jp", "japan", "gb", "uk", "united kingdom", "cn", "china", "de", "germany",
}


class _CalendarHTTPError(Exception):
    """Raised inside a provider fetch to carry a degraded reason upward."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def fetch_calendar(
    config: Config, date: str, *, http: Optional[httpx.Client] = None
) -> list[CalendarEvent]:
    """Legacy shim: events only (drops the degraded reason). Prefer
    fetch_calendar_with_status so an auth/HTTP failure is recorded, not hidden."""
    events, _reason = fetch_calendar_with_status(config, date, http=http)
    return events


def fetch_calendar_with_status(
    config: Config, date: str, *, http: Optional[httpx.Client] = None
) -> tuple[list[CalendarEvent], Optional[str]]:
    """Fetch (events, degraded_reason) for `date` (YYYY-MM-DD, Istanbul).

    FMP primary, Finnhub secondary. degraded_reason is None on success (including a
    genuine empty calendar); a 'calendar:<reason>' string on auth/HTTP/access
    failure of the configured provider. NEVER raises.
    """
    owns_client = http is None
    client = http or httpx.Client()
    try:
        raw = getattr(config, "raw", {}) or {}
        cal_cfg = raw.get("calendar", {}) or {}
        provider = str(cal_cfg.get("provider", "fmp")).strip().lower()

        # Calendar OFF: TradingView / SPEC-1 (Pine) owns the economic calendar (TV
        # has a native one). Return immediately — no HTTP call, no degraded reason.
        # The FMP/Finnhub fetchers below stay as dormant, selectable code-paths.
        if provider in {"off", "none", "disabled", ""}:
            return [], None

        high_impact_events = [str(e).lower() for e in cal_cfg.get("high_impact_events", [])]

        primary_reason: Optional[str] = None
        events: list[CalendarEvent] = []

        # --- PRIMARY ---
        try:
            if provider == "fred":
                events = _fetch_fred(config, date, client, high_impact_events)
            elif provider == "finnhub":
                events = _fetch_finnhub(config, date, client, high_impact_events)
            else:
                events = _fetch_fmp(config, date, client, high_impact_events)
        except _CalendarHTTPError as exc:
            primary_reason = exc.reason
        except Exception as exc:  # noqa: BLE001 — unexpected error is still a DEGRADE, not a silent empty
            primary_reason = f"calendar:{provider} error {type(exc).__name__}"

        # --- SECONDARY fallback: only if primary yielded NOTHING and a Finnhub key exists ---
        # (FRED is self-sufficient; skip secondary for fred provider)
        if not events and provider not in {"finnhub", "fred"}:
            fh_key = _resolve_key(config, "finnhub_api_key")
            if fh_key:
                try:
                    fallback = _fetch_finnhub(config, date, client, high_impact_events)
                    if fallback:
                        events = fallback
                        primary_reason = None  # secondary saved us
                except _CalendarHTTPError:
                    pass  # keep the primary degraded reason (or empty)
                except Exception:  # noqa: BLE001
                    pass

        return _sort_events(events), primary_reason
    finally:
        if owns_client:
            client.close()


# ---------------------------------------------------------------------------
# FRED (SPEC-3 primary — release-dates API + static FOMC schedule)
# ---------------------------------------------------------------------------
def _fetch_fred(
    config: Config, date: str, http: httpx.Client, high_impact_events: list[str]
) -> list[CalendarEvent]:
    """Fetch upcoming economic releases from FRED release/dates API + static FOMC.

    Window: `date` through `date + 6 days` ("today / this week").
    Queries the PER-RELEASE endpoint once per configured release_id
    (SPEC-3 §10: `/fred/release/dates?release_id=N` with
    `include_release_dates_with_no_data=true`, `realtime_start=<today>`,
    `realtime_end=9999-12-31`, `sort_order=asc` — the global `releases/dates`
    endpoint with a bounded realtime window returns only PAST dates, producing a
    perpetually-empty calendar). Returned dates are filtered to the window
    client-side. Injects static FOMC dates from config.calendar.fomc_dates.
    A genuine empty window is NOT degraded; HTTP/auth failures raise _CalendarHTTPError.
    """
    raw = getattr(config, "raw", {}) or {}
    cal_cfg = raw.get("calendar", {}) or {}

    # Parse configured release map: {release_id (int): display_name (str)}
    release_map: dict[int, str] = {}
    for entry in (cal_cfg.get("fred_releases") or []):
        if isinstance(entry, dict):
            rid = entry.get("id")
            name = entry.get("name", "")
            if rid is not None:
                release_map[int(rid)] = str(name)

    # Date window: brief date through +6 days
    from_date = date_cls.fromisoformat(date)
    to_date = from_date + timedelta(days=6)
    date_from = from_date.isoformat()

    # Effective ET release-time map (config overrides + code defaults).
    release_times = _build_release_times(cal_cfg)

    events: list[CalendarEvent] = []

    # FRED release/dates — one call PER configured release_id (SPEC-3 §10)
    if release_map:
        fred_key = _resolve_key(config, "fred_api_key")
        if not fred_key:
            raise _CalendarHTTPError("calendar:no FRED key")
        url = f"{_FRED_RELEASES_BASE}/release/dates"

        for rid, title in release_map.items():
            params = {
                "api_key": fred_key,
                "file_type": "json",
                "release_id": rid,
                "realtime_start": date_from,
                "realtime_end": "9999-12-31",  # MANDATORY for FUTURE scheduled dates
                "include_release_dates_with_no_data": "true",
                "sort_order": "asc",
                "limit": 50,  # <= 1000; the next handful of dates is plenty for a 7-day window
            }
            # Bounded retry: transient transport errors and 5xx are retried.
            # 401/403/other-4xx are NOT transient — they pass through to the
            # status checks below without retry. Non-transport exceptions
            # (unexpected errors) raise immediately, preserving existing
            # behaviour. Sleeps are via the module-level `time` reference so
            # tests can monkeypatch `calendar.time.sleep` to a no-op.
            _last_t_exc: Optional[Exception] = None
            _last_5xx: Optional[int] = None
            resp = None
            for _attempt in range(_RETRY_ATTEMPTS):
                try:
                    resp = http.get(url, params=params, timeout=30.0)
                except httpx.TransportError as _exc:
                    _last_t_exc = _exc
                    if _attempt < _RETRY_ATTEMPTS - 1:
                        time.sleep(_RETRY_BASE_DELAY * (_RETRY_FACTOR ** _attempt))
                    continue
                except Exception as exc:  # noqa: BLE001 — non-transport: raise immediately
                    raise _CalendarHTTPError(
                        f"calendar:FRED network {type(exc).__name__}"
                    ) from exc
                if resp.status_code >= 500:
                    _last_5xx = resp.status_code
                    if _attempt < _RETRY_ATTEMPTS - 1:
                        time.sleep(_RETRY_BASE_DELAY * (_RETRY_FACTOR ** _attempt))
                    continue
                break  # 2xx or 4xx — handle below
            else:
                # All attempts exhausted without a usable response.
                if _last_t_exc is not None:
                    raise _CalendarHTTPError(
                        f"calendar:FRED network {type(_last_t_exc).__name__}"
                    ) from _last_t_exc
                raise _CalendarHTTPError(f"calendar:FRED {_last_5xx}")

            if resp.status_code in (401, 403):
                raise _CalendarHTTPError(f"calendar:FRED {resp.status_code} (auth)")
            if resp.status_code >= 400:
                raise _CalendarHTTPError(f"calendar:FRED {resp.status_code}")

            try:
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                raise _CalendarHTTPError("calendar:FRED bad JSON") from exc

            for rd in payload.get("release_dates", []):
                rd_raw = rd.get("date")
                if not rd_raw:
                    continue
                try:
                    rd_date = date_cls.fromisoformat(str(rd_raw)[:10])
                except ValueError:
                    continue
                # Keep only dates inside the [today, today+6] window
                if not (from_date <= rd_date <= to_date):
                    continue
                rank = _rank_for(title)
                title_low = title.lower()
                hi = any(kw in title_low for kw in high_impact_events) or (rank is not None and rank <= 3)
                events.append(CalendarEvent(
                    event=title,
                    time=_resolve_event_time(title, rd_date, release_times),
                    consensus=None,
                    high_impact=hi,
                    prior_citi_surprise=None,
                    rank=rank,
                ))

    # Static FOMC dates: inject any that fall within the window
    fomc_dates: list[str] = [str(d) for d in (cal_cfg.get("fomc_dates") or [])]
    for fd in fomc_dates:
        try:
            fd_date = date_cls.fromisoformat(fd[:10])
        except ValueError:
            continue
        if from_date <= fd_date <= to_date:
            events.append(CalendarEvent(
                event="FOMC Meeting (Statement)",
                # 14:00 ET statement, resolved DST-aware on the meeting date.
                time=_resolve_event_time("FOMC", fd_date, release_times),
                consensus=None,
                high_impact=True,
                prior_citi_surprise=None,
                rank=1,
            ))

    return events


# ---------------------------------------------------------------------------
# FMP (primary)
# ---------------------------------------------------------------------------
def _fetch_fmp(
    config: Config, date: str, http: httpx.Client, high_impact_events: list[str]
) -> list[CalendarEvent]:
    raw = getattr(config, "raw", {}) or {}
    sources = raw.get("sources", {}) or {}
    fmp_cfg = sources.get("fmp", {}) or {}
    base_url = fmp_cfg.get("base_url", "https://financialmodelingprep.com/api/v3")

    api_key = _resolve_key(config, "fmp_api_key")
    if not api_key:
        raise _CalendarHTTPError("calendar:no FMP key")

    url = f"{base_url.rstrip('/')}/economic_calendar"
    params = {"from": date, "to": date, "apikey": str(api_key)}
    try:
        resp = http.get(url, params=params, timeout=30.0)
    except Exception as exc:  # noqa: BLE001 — network failure -> degraded, not empty
        raise _CalendarHTTPError(f"calendar:FMP network {type(exc).__name__}") from exc

    if resp.status_code in (401, 403):
        raise _CalendarHTTPError(f"calendar:FMP {resp.status_code} (auth/paid-tier)")
    if resp.status_code >= 400:
        raise _CalendarHTTPError(f"calendar:FMP {resp.status_code}")

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise _CalendarHTTPError("calendar:FMP bad JSON") from exc

    # FMP returns a JSON list of event dicts. An object with an "Error Message"
    # means the request was rejected (e.g. invalid/paid key) -> degraded.
    if isinstance(payload, dict):
        msg = payload.get("Error Message") or payload.get("error")
        raise _CalendarHTTPError(f"calendar:FMP rejected ({str(msg)[:60]})" if msg
                                 else "calendar:FMP unexpected object response")
    if not isinstance(payload, list):
        raise _CalendarHTTPError("calendar:FMP unexpected response shape")

    events: list[CalendarEvent] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        if not _fmp_keep(row):
            continue
        events.append(_fmp_to_event(row, high_impact_events))
    return events


def _fmp_keep(row: dict) -> bool:
    """US + globally market-moving filter. Unknown country kept (rank decides)."""
    country = str(row.get("country") or row.get("currency") or "").strip().lower()
    if not country:
        return True
    return country in _KEEP_COUNTRIES


def _fmp_to_event(row: dict, high_impact_events: list[str]) -> CalendarEvent:
    title = str(row.get("event") or row.get("name") or "").strip()
    time_str = _str_or_none(row.get("date") or row.get("datetime"))

    consensus = _coerce_consensus(row.get("estimate", row.get("consensus")))
    rank = _rank_for(title)

    title_low = title.lower()
    by_config = any(kw in title_low for kw in high_impact_events)
    impact_field = str(row.get("impact", "")).strip().lower()
    by_provider = _IMPACT_MAP.get(impact_field, False)
    high_impact = bool(by_config or by_provider)

    return CalendarEvent(
        event=title,
        time=time_str,
        consensus=consensus,
        high_impact=high_impact,
        prior_citi_surprise=_coerce_float(row.get("previous")),
        rank=rank,
    )


# ---------------------------------------------------------------------------
# Finnhub (secondary fallback)
# ---------------------------------------------------------------------------
def _fetch_finnhub(
    config: Config, date: str, http: httpx.Client, high_impact_events: list[str]
) -> list[CalendarEvent]:
    raw = getattr(config, "raw", {}) or {}
    sources = raw.get("sources", {}) or {}
    finnhub_cfg = sources.get("finnhub", {}) or {}
    base_url = finnhub_cfg.get("base_url", "https://finnhub.io/api/v1")

    api_key = _resolve_key(config, "finnhub_api_key")
    if not api_key:
        raise _CalendarHTTPError("calendar:no Finnhub key")

    url = f"{base_url.rstrip('/')}/calendar/economic"
    params = {"from": date, "to": date, "token": str(api_key)}
    try:
        resp = http.get(url, params=params, timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        raise _CalendarHTTPError(f"calendar:Finnhub network {type(exc).__name__}") from exc

    if resp.status_code in (401, 403):
        raise _CalendarHTTPError(f"calendar:Finnhub {resp.status_code} (auth/premium-gate)")
    if resp.status_code >= 400:
        raise _CalendarHTTPError(f"calendar:Finnhub {resp.status_code}")

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise _CalendarHTTPError("calendar:Finnhub bad JSON") from exc

    rows = payload.get("economicCalendar", payload) if isinstance(payload, dict) else payload
    if isinstance(rows, dict):
        rows = rows.get("result", [])
    if not isinstance(rows, list):
        raise _CalendarHTTPError("calendar:Finnhub unexpected response shape")

    events: list[CalendarEvent] = []
    for row in rows:
        if isinstance(row, dict):
            events.append(_to_event(row, high_impact_events))
    return events


def _to_event(row: dict, high_impact_events: list[str]) -> CalendarEvent:
    title = str(row.get("event") or row.get("name") or "").strip()
    time_str = _str_or_none(row.get("time") or row.get("date") or row.get("datetime"))

    consensus = _coerce_consensus(
        row.get("estimate") if row.get("estimate") is not None else row.get("consensus")
    )
    rank = _rank_for(title)

    title_low = title.lower()
    by_config = any(kw in title_low for kw in high_impact_events)
    impact_field = str(row.get("impact", "")).strip().lower()
    by_provider = _IMPACT_MAP.get(impact_field, False)
    high_impact = bool(by_config or by_provider)

    return CalendarEvent(
        event=title,
        time=time_str,
        consensus=consensus,
        high_impact=high_impact,
        prior_citi_surprise=_coerce_float(row.get("prior_citi_surprise")),
        rank=rank,
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _sort_events(events: list[CalendarEvent]) -> list[CalendarEvent]:
    """Rank ascending (strongest transmission first); None rank sinks to the end."""
    events.sort(key=lambda e: (e.rank if e.rank is not None else 999, e.event))
    return events


def _resolve_key(config: Config, getter_name: str) -> Optional[str]:
    getter = getattr(config, getter_name, None)
    if callable(getter):
        try:
            return getter()
        except Exception:  # noqa: BLE001
            return None
    return None


def _rank_for(title: str) -> Optional[int]:
    low = title.lower()
    for needle, rank in _TRANSMISSION_RANK:
        if needle in low:
            return rank
    return None


# ---------------------------------------------------------------------------
# Release-time resolution (ET wall-clock -> UTC DST-aware -> Istanbul UTC+3)
# ---------------------------------------------------------------------------
def _parse_hhmm(value) -> Optional[tuple[int, int]]:
    """Parse a 'HH:MM' string into (hour, minute); None if malformed/out-of-range."""
    try:
        h_str, m_str = str(value).strip().split(":")
        h, m = int(h_str), int(m_str)
    except (ValueError, AttributeError):
        return None
    if 0 <= h < 24 and 0 <= m < 60:
        return (h, m)
    return None


def _build_release_times(cal_cfg: dict) -> list[tuple[str, tuple[int, int]]]:
    """Effective ET release-time map: config calendar.release_times overrides FIRST
    (checked before the code defaults, since resolution stops on first match), then
    the built-in _ET_RELEASE_TIMES defaults. config.release_times is an optional
    mapping {title-substring: 'HH:MM' (ET wall-clock)}."""
    overrides: list[tuple[str, tuple[int, int]]] = []
    rt = cal_cfg.get("release_times")
    if isinstance(rt, dict):
        for key, val in rt.items():
            hm = _parse_hhmm(val)
            if hm is not None:
                overrides.append((str(key).strip().lower(), hm))
    return overrides + _ET_RELEASE_TIMES


def _format_et_time(event_date: date_cls, hour: int, minute: int) -> str:
    """ET wall-clock (hour:minute) on event_date -> 'HH:MM UTC · HH:MM İst'.

    DST-aware: ZoneInfo('America/New_York') resolves EDT (UTC-4, summer) vs EST
    (UTC-5, winter) from the release DATE. Istanbul = UTC + 3 (no DST)."""
    et_dt = datetime(event_date.year, event_date.month, event_date.day, hour, minute, tzinfo=_NY_TZ)
    utc_dt = et_dt.astimezone(timezone.utc)
    ist_dt = utc_dt + timedelta(hours=3)
    return f"{utc_dt:%H:%M} UTC · {ist_dt:%H:%M} İst"


def _resolve_event_time(
    title: str, event_date: date_cls, release_times: list[tuple[str, tuple[int, int]]]
) -> Optional[str]:
    """Resolve an event's display time string from its title substring + release
    date. Unmatched title -> None (do not guess a time)."""
    low = title.lower()
    for needle, (hour, minute) in release_times:
        if needle in low:
            return _format_et_time(event_date, hour, minute)
    return None


def _str_or_none(value) -> Optional[str]:
    return str(value) if value is not None else None


def _coerce_consensus(value) -> Optional[float | str]:
    if value is None or value == "":
        return None
    f = _coerce_float(value)
    if f is not None:
        return f
    return str(value)


def _coerce_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
