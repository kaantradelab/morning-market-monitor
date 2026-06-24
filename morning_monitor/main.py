"""Orchestrator — BUILD TARGET 5.

The single entrypoint the cron / workflow_dispatch invokes. Composes the four
build targets end-to-end and is the sole owner of graceful degradation at the
run level: ANY single source/stage failure degrades a tile, it NEVER crashes the run.

    python -m morning_monitor.main [--config config.yaml] [--date YYYY-MM-DD]
                                   [--fixture tests/fixtures/sample_run.json] [--no-render]

--fixture loads RawSeries from an offline JSON instead of hitting live APIs (dry-run
without FRED_API_KEY/FINNHUB_API_KEY). --date overrides the Istanbul logical date.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import anomaly, brief, render
from .config import Config, load_config
from .models import Brief, CalendarEvent, RawSeries
from .sources.ingest import fetch_calendar_with_status, ingest

# Turkey is permanently UTC+3 with NO daylight saving (since 2016).
_ISTANBUL = timezone(timedelta(hours=3))


def resolve_date(tz: str = "Europe/Istanbul", override: str | None = None) -> str:
    """Logical morning date (YYYY-MM-DD) in Istanbul (UTC+3, no DST). override wins.

    `tz` is accepted for signature compatibility but the offset is fixed at +3
    (no DST) per the locked tech decision — we do not depend on a tz database.
    """
    if override:
        # Validate it parses as a date; raise a clear error otherwise.
        datetime.strptime(override[:10], "%Y-%m-%d")
        return override[:10]
    return datetime.now(_ISTANBUL).date().isoformat()


def _load_fixture_payload(path: Path) -> dict:
    """Parse the offline fixture JSON into its raw {date, series, calendar} payload."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_fixture(path: Path) -> dict[str, RawSeries]:
    """Load offline RawSeries dict from a fixture JSON (tests/fixtures/sample_run.json).

    Expected shape: {"date":..., "series": {key: RawSeries-dict}, "calendar": [...]}.
    Lets the whole pipeline dry-run with no API keys. Returns just the series map;
    use ``_fixture_calendar`` for the calendar strip.
    """
    payload = _load_fixture_payload(path)
    series = payload.get("series", {}) or {}
    return {key: RawSeries.from_dict(raw) for key, raw in series.items()}


def _fixture_calendar(path: Path) -> list[CalendarEvent]:
    """Load the calendar events from the same fixture JSON."""
    payload = _load_fixture_payload(path)
    return [CalendarEvent.from_dict(e) for e in (payload.get("calendar", []) or [])]


def run(config: Config, *, date: str, fixture: Path | None = None, do_render: bool = True) -> Brief:
    """End-to-end single morning run.

      1. ingest (or load_fixture)        -> series_by_key, degraded
      2. fetch_calendar (or fixture)     -> calendar
      3. anomaly.enrich                  -> AnomalyResult
      4. brief.assemble_brief            -> Brief ; brief.write_brief_json
      5. render.render_site (if do_render)

    Each stage is wrapped so a failure degrades gracefully and is logged into
    meta.degraded_sources rather than aborting the run. Returns the Brief.
    """
    run_ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    degraded: list[str] = []

    # ---- 1 + 2. Ingestion (or offline fixture) + calendar ----
    series_by_key: dict[str, RawSeries] = {}
    calendar: list[CalendarEvent] = []

    if fixture is not None:
        # Offline path: no API keys, no network. Fixture is the source of truth.
        try:
            series_by_key = load_fixture(fixture)
            calendar = _fixture_calendar(fixture)
        except Exception as exc:  # noqa: BLE001 — a broken fixture must not crash the run
            degraded.append(f"fixture:{exc!r}")
        # Degraded keys = any ok=False series in the fixture.
        degraded.extend(k for k, rs in series_by_key.items() if not rs.ok)
    else:
        # Live path: ingest fans out to all fetchers; it never raises.
        try:
            series_by_key, ingest_degraded = ingest(config)
            degraded.extend(ingest_degraded)
        except Exception as exc:  # noqa: BLE001 — belt-and-braces; ingest contract says it won't
            degraded.append(f"ingest:{exc!r}")
        try:
            # NO SILENT SWALLOW: a calendar source that FAILS (auth/HTTP/access)
            # records a degraded reason; a genuine empty 200 (no releases) does not.
            calendar, cal_degraded = fetch_calendar_with_status(config, date)
            if cal_degraded:
                degraded.append(cal_degraded)
        except Exception as exc:  # noqa: BLE001 — belt-and-braces; fetcher contract says it won't raise
            degraded.append(f"calendar:{exc!r}")

    # ---- 3. Anomaly enrichment ----
    try:
        result = anomaly.enrich(series_by_key, config, calendar)
    except Exception as exc:  # noqa: BLE001 — never abort; produce an empty-but-valid result
        degraded.append(f"anomaly:{exc!r}")
        result = anomaly.AnomalyResult(
            composites=anomaly_empty_composites(),
            tiles=[],
            corr_breaks=[],
            dog_didnt_bark=[],
            flagged_keys=[],
        )

    # ---- 4. Assemble + write durable JSON ----
    the_brief = brief.assemble_brief(
        date=date,
        run_ts_utc=run_ts_utc,
        config=config,
        result=result,
        series_by_key=series_by_key,
        calendar=calendar,
        degraded=_dedup(degraded),
    )
    try:
        brief.write_brief_json(the_brief, config)
    except Exception as exc:  # noqa: BLE001 — a write failure should not lose the Brief object
        the_brief.meta.degraded_sources = _dedup(
            list(the_brief.meta.degraded_sources) + [f"write_json:{exc!r}"]
        )

    # ---- 5. Render the static site ----
    if do_render:
        try:
            render.render_site(the_brief, config)
        except Exception as exc:  # noqa: BLE001 — render failure is non-fatal to the run
            the_brief.meta.degraded_sources = _dedup(
                list(the_brief.meta.degraded_sources) + [f"render:{exc!r}"]
            )

    return the_brief


def anomaly_empty_composites():
    """An all-None Composites for the degraded-anomaly fallback path."""
    from .models import Composites
    return Composites(ofr_fsi=None, nfci=None, anfci=None)


def _dedup(items: list[str]) -> list[str]:
    """Order-preserving de-duplication."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    """CLI: --config, --date, --fixture, --no-render."""
    p = argparse.ArgumentParser(
        prog="morning-monitor",
        description="Once-a-morning cross-asset market monitor (snapshot, free EOD data).",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (defaults to the package's config.yaml).",
    )
    p.add_argument(
        "--date",
        default=None,
        help="Override the Istanbul logical date (YYYY-MM-DD). Defaults to today (UTC+3).",
    )
    p.add_argument(
        "--fixture",
        default=None,
        help="Offline RawSeries fixture JSON — dry-run with no API keys.",
    )
    p.add_argument(
        "--no-render",
        action="store_true",
        help="Skip the static-site render (write the JSON only).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Parse args, load_config, run(). Returns process exit code (0 even on a
    degraded-but-completed run; non-zero only on an unrecoverable config error)."""
    args = build_arg_parser().parse_args(argv)

    # Config load is the one unrecoverable error (no config -> no run).
    try:
        config = load_config(args.config) if args.config else load_config()
    except Exception as exc:  # noqa: BLE001
        print(f"[morning-monitor] FATAL: could not load config: {exc!r}", file=sys.stderr)
        return 2

    try:
        date = resolve_date(config.raw.get("output", {}).get("timezone", "Europe/Istanbul"),
                            override=args.date)
    except Exception as exc:  # noqa: BLE001 — a bad --date is a usage error
        print(f"[morning-monitor] FATAL: invalid --date {args.date!r}: {exc!r}", file=sys.stderr)
        return 2

    fixture = Path(args.fixture) if args.fixture else None
    if fixture is not None and not fixture.exists():
        print(f"[morning-monitor] FATAL: fixture not found: {fixture}", file=sys.stderr)
        return 2

    the_brief = run(config, date=date, fixture=fixture, do_render=not args.no_render)

    degraded = the_brief.meta.degraded_sources
    n_reds = sum(1 for t in the_brief.tiles if t.color == "red")
    print(
        f"[morning-monitor] {date}: {len(the_brief.tiles)} tiles, {n_reds} red, "
        f"{len(the_brief.cards)} cards, {len(degraded)} degraded "
        f"({'calm' if the_brief.meta.calm_morning else 'elevated'})."
    )
    if degraded:
        print(f"[morning-monitor] degraded: {', '.join(degraded)}")
    # A degraded-but-completed run is still a success (exit 0).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
