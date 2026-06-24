"""Static HTML renderer — BUILD TARGET 4.

Consumes a models.Brief (or a per-day JSON on disk); emits static HTML via Jinja2:
  site/index.html              -> latest day
  site/archive/<date>.html     -> each past day
  site/archive/index.html      -> browseable history list

Sparklines are INLINE SVG computed here at render time from each tile's history[]
(no JS, no chart lib). Host = GitHub Pages; output is a fully static site.
"""

from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .config import Config
from .models import Brief, Composite, HistoryPoint

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# Human axis labels for the orientation grid section headers. Mirrors config.yaml
# `axes`, kept here as a render-layer fallback so the page is self-describing even
# if a brief is rendered without the live config's axis map.
AXIS_LABELS: dict[int, str] = {
    0: "Composite anchor",
    1: "Risk appetite",
    2: "Rates — front",
    3: "Rates — long",
    4: "Curve",
    5: "Credit",
    6: "Dollar / funding",
    7: "Commodities",
    8: "Equity vol",
    9: "Rates vol",
    10: "Breadth / concentration",
    11: "Funding plumbing",
    12: "Crypto",
}

# Composite display order + labels for the anchors row.
_COMPOSITE_ORDER: list[tuple[str, str]] = [
    ("ofr_fsi", "OFR FSI"),
    ("nfci", "NFCI"),
    ("anfci", "ANFCI"),
    ("stlfsi4", "STLFSI4"),
]


# ---------------------------------------------------------------------------
# Jinja filters / environment
# ---------------------------------------------------------------------------
def _fmt_num(value: object, digits: int = 2) -> str:
    """Format a number compactly; '—' for None/non-numeric. Large magnitudes
    collapse to k/M/B so a $249.5e9 stablecoin cap stays one cell wide."""
    if value is None or isinstance(value, bool):
        return "—"
    # Narrow to types float() accepts so the static checker is satisfied; the
    # try/except still catches a non-numeric str at runtime.
    if not isinstance(value, (int, float, str)):
        return str(value)
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 1e4:
        return f"{v / 1e3:.1f}k"
    return f"{v:.{digits}f}"


def _fmt_signed(value: object, digits: int = 2) -> str:
    """Signed number with explicit '+' for non-negative; '—' for None."""
    if value is None or isinstance(value, bool):
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    body = _fmt_num(v, digits)
    return body if body.startswith("-") or body == "—" else f"+{body}"


def fmt_change_by_transform(change: object, transform: object) -> str:
    """Render a tile's `change` legibly given its transform — the one place that
    decides display magnitude/units (used by BOTH the HTML grid tile and the
    brief card metric string, so they never diverge).

    A log_return-transform tile's `change` is a one-day LOG RETURN; rounding it to
    2 decimals turns a real +0.84% broad-USD move into a meaningless "+0.01". So:

      log_return -> percent move (exp(change)-1), e.g. +0.84%  [≈ change for small moves]
      ratio      -> percent change of the ratio,  e.g. -1.20%
      first_diff -> the level change itself (bp-ish), with enough precision
      sign / level / other -> signed level change

    Returns '—' for a None/non-numeric change.
    """
    if change is None or isinstance(change, bool):
        return "—"
    try:
        v = float(change)
    except (TypeError, ValueError):
        return "—"

    t = str(transform) if transform is not None else ""
    if t in ("log_return", "ratio"):
        import math
        # log_return: exact pct = exp(r)-1. ratio change is already a fractional
        # first-difference of the ratio level -> treat as a fractional move too.
        pct = (math.exp(v) - 1.0) * 100.0 if t == "log_return" else v * 100.0
        return f"{pct:+.2f}%"
    # first_diff / level / sign: the change is a level delta. Keep more precision
    # for sub-unit moves (yields/spreads in pct points) without scientific noise.
    a = abs(v)
    digits = 4 if a < 0.1 else (3 if a < 1 else 2)
    return f"{v:+.{digits}f}"


def make_env(template_dir: Path = TEMPLATE_DIR) -> Environment:
    """Build the Jinja2 Environment (FileSystemLoader, autoescape on). Registers
    the `sparkline` filter so templates can call {{ tile.history | sparkline }}."""
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["sparkline"] = lambda hist, **kw: Markup(sparkline_svg(hist, **kw))
    env.filters["num"] = _fmt_num
    env.filters["signed"] = _fmt_signed
    env.filters["change_disp"] = fmt_change_by_transform
    return env


# ---------------------------------------------------------------------------
# Inline-SVG sparkline (hand-rolled, no chart lib, no JS)
# ---------------------------------------------------------------------------
def sparkline_svg(history: list[HistoryPoint], *, width: int = 120, height: int = 28) -> str:
    """Render an inline <svg> polyline from history values (oldest->newest).

    Pure-Python min/max normalization to the viewbox; no external deps. Returns an
    HTML-safe SVG string. Empty/degraded history -> a flat 'no data' placeholder.
    """
    pad = 2.0
    # Accept HistoryPoint objects or plain dicts (rendering straight from JSON).
    vals: list[float] = []
    for h in history or []:
        v = getattr(h, "value", None) if not isinstance(h, dict) else h.get("value")
        if v is not None:
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass

    if len(vals) < 2:
        # Flat placeholder — a faint baseline so the cell keeps its height.
        mid = height / 2.0
        return (
            f'<svg class="sparkline" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="no data" '
            f'preserveAspectRatio="none">'
            f'<line x1="{pad}" y1="{mid:.1f}" x2="{width - pad}" y2="{mid:.1f}" '
            f'stroke="#444b58" stroke-width="1" stroke-dasharray="2 2"/></svg>'
        )

    vmin, vmax = min(vals), max(vals)
    span = vmax - vmin
    n = len(vals)
    plot_w = width - 2 * pad
    plot_h = height - 2 * pad

    def x(i: int) -> float:
        return pad + (plot_w * i / (n - 1))

    def y(v: float) -> float:
        if span == 0:
            return height / 2.0
        # Invert: higher value -> higher on screen (smaller y).
        return pad + plot_h * (1.0 - (v - vmin) / span)

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))

    # Color by net direction (last vs first): up = green, down = red, flat = gray.
    delta = vals[-1] - vals[0]
    stroke = "#2e7d32" if delta > 0 else ("#c62828" if delta < 0 else "#9e9e9e")
    lx, ly = x(n - 1), y(vals[-1])

    return (
        f'<svg class="sparkline" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="trend {vals[0]:.4g} to {vals[-1]:.4g}" '
        f'preserveAspectRatio="none">'
        f'<polyline fill="none" stroke="{stroke}" stroke-width="1.5" '
        f'stroke-linejoin="round" stroke-linecap="round" points="{pts}"/>'
        f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="1.8" fill="{stroke}"/></svg>'
    )


# ---------------------------------------------------------------------------
# View-model helpers (Brief -> template context)
# ---------------------------------------------------------------------------
def _axis_label(axis: int, config: Config | None) -> str:
    """Prefer the live config's axis map; fall back to the render-layer table."""
    if config is not None:
        try:
            raw_axes = (config.raw or {}).get("axes") or {}
            if axis in raw_axes:
                return str(raw_axes[axis])
            if str(axis) in raw_axes:
                return str(raw_axes[str(axis)])
        except Exception:
            pass
    return AXIS_LABELS.get(axis, f"Axis {axis}")


def _axis_groups(brief: Brief, config: Config | None) -> list[dict]:
    """Group tiles by axis (ascending), preserving tile order within each axis."""
    by_axis: dict[int, list] = {}
    for tile in brief.tiles:
        by_axis.setdefault(tile.axis, []).append(tile)
    groups: list[dict] = []
    for axis in sorted(by_axis):
        groups.append({
            "axis": axis,
            "label": _axis_label(axis, config),
            "tiles": by_axis[axis],
        })
    return groups


def _composites_list(brief: Brief) -> list[dict]:
    """Flatten Composites into an ordered name+composite list, skipping nulls."""
    out: list[dict] = []
    for key, label in _COMPOSITE_ORDER:
        comp: Composite | None = getattr(brief.composites, key, None)
        if comp is None:
            continue
        out.append({
            "key": key,
            "name": label,
            "value": comp.value,
            "level_pct": comp.level_pct,
            "change_score": comp.change_score,
            "color": comp.color,
            "staleness": comp.staleness,
        })
    return out


def _temperature(brief: Brief) -> dict:
    """System temperature for the page header. Worst tile/composite color wins;
    calm_morning self-check downshifts the label. Pure presentation — the brief's
    own colors/flags already carry the analytic verdict."""
    sev = {"green": 0, "amber": 1, "red": 2, "gray": 0}
    worst = 0
    n_red = 0
    for t in brief.tiles:
        worst = max(worst, sev.get(t.color, 0))
        if t.color == "red":
            n_red += 1
    for c in _composites_list(brief):
        if c["color"]:
            worst = max(worst, sev.get(c["color"], 0))
            if c["color"] == "red":
                n_red += 1

    if worst >= 2:
        color, label = "red", f"Elevated — {n_red} red"
    elif worst == 1:
        color, label = "amber", "Watch"
    else:
        color, label = "green", "Calm"

    if brief.meta.calm_morning and color == "amber":
        label = "Calm (within band)"
    return {"color": color, "label": label, "n_reds": n_red}


def _day_context(brief: Brief, config: Config | None, *, day_href_prefix: str,
                 archive_index_href: str, archive_dates: list[str]) -> dict:
    return {
        "brief": brief,
        "temperature": _temperature(brief),
        "composites_list": _composites_list(brief),
        "axis_groups": _axis_groups(brief, config),
        "day_href_prefix": day_href_prefix,
        "archive_index_href": archive_index_href,
        "archive_dates": archive_dates,
    }


# ---------------------------------------------------------------------------
# Render entrypoints
# ---------------------------------------------------------------------------
def render_day(brief: Brief, config: Config, *, env: Environment | None = None,
               at_root: bool = True, archive_dates: list[str] | None = None) -> str:
    """Render one day's HTML page from a Brief (used for index + archive/<date>).

    at_root=True  -> page is site/index.html  (day links live under archive/)
    at_root=False -> page is site/archive/<date>.html (day links are siblings)
    archive_dates: dates (newest first) for the dropdown.
    """
    env = env or make_env()
    if at_root:
        day_href_prefix, archive_index_href = "archive/", "archive/index.html"
    else:
        day_href_prefix, archive_index_href = "", "index.html"
    ctx = _day_context(brief, config, day_href_prefix=day_href_prefix,
                       archive_index_href=archive_index_href,
                       archive_dates=archive_dates or [])
    return env.get_template("day.html").render(**ctx)


def render_archive_index(briefs_meta: list[dict], config: Config,
                         *, env: Environment | None = None) -> str:
    """Render the archive list page from per-day {date, n_cards, n_reds, ...} summaries."""
    env = env or make_env()
    entries = sorted(briefs_meta, key=lambda e: e["date"], reverse=True)
    return env.get_template("archive_index.html").render(entries=entries)


def _summarize(brief: Brief) -> dict:
    """One archive-index row from a Brief."""
    n_reds = sum(1 for t in brief.tiles if t.color == "red")
    n_reds += sum(1 for c in _composites_list(brief) if c["color"] == "red")
    return {
        "date": brief.meta.date,
        "n_cards": len(brief.cards),
        "n_reds": n_reds,
        "calm_morning": bool(brief.meta.calm_morning),
        "degraded": bool(brief.meta.degraded_sources),
    }


def _scan_briefs(data_dir: Path) -> list[Brief]:
    """Load every per-day brief JSON in data_dir (named <YYYY-MM-DD>.json)."""
    briefs: list[Brief] = []
    if not data_dir.is_dir():
        return briefs
    for p in sorted(data_dir.glob("*.json")):
        try:
            with p.open(encoding="utf-8") as fh:
                briefs.append(Brief.from_dict(json.load(fh)))
        except Exception:
            # A malformed archive file must not break the site render.
            continue
    return briefs


def render_site(brief: Brief, config: Config, *, site_dir: Path | None = None) -> list[Path]:
    """Full render: write site/index.html (latest), site/archive/<date>.html, and
    rebuild site/archive/index.html by scanning {data_dir} for all past briefs.

    site_dir overrides config.output.site_dir (tests). Returns written paths.
    Idempotent: re-rendering a date overwrites that date's archive page only.
    """
    out = (config.raw or {}).get("output", {}) if config is not None else {}
    repo_root = TEMPLATE_DIR.parent.parent  # morning-market-monitor/

    site = Path(site_dir) if site_dir is not None else repo_root / out.get("site_dir", "site")
    archive_subdir = out.get("archive_subdir", "archive")
    data_dir = repo_root / out.get("data_dir", "data")

    archive = site / archive_subdir
    archive.mkdir(parents=True, exist_ok=True)

    # GitHub Pages serves the artifact via Jekyll by default, which would skip
    # files/dirs beginning with '_'. .nojekyll disables that. site/ is gitignored
    # wholesale, so this marker is (re)created every run before the artifact upload
    # rather than tracked in git.
    (site / ".nojekyll").write_text("", encoding="utf-8")

    env = make_env()

    # Build the full set of archived briefs: everything on disk + the brief we're
    # rendering now (which may not be persisted yet, or may supersede an old copy).
    archived = {b.meta.date: b for b in _scan_briefs(data_dir)}
    archived[brief.meta.date] = brief
    all_dates = sorted(archived, reverse=True)

    written: list[Path] = []

    # 1. Latest day -> index.html (newest archived date).
    latest = archived[all_dates[0]]
    index_html = render_day(latest, config, env=env,
                            at_root=True, archive_dates=all_dates)
    index_path = site / "index.html"
    index_path.write_text(index_html, encoding="utf-8")
    written.append(index_path)

    # 2. Per-day archive pages (idempotent overwrite per date).
    for date, b in archived.items():
        page = render_day(b, config, env=env,
                          at_root=False, archive_dates=all_dates)
        page_path = archive / f"{date}.html"
        page_path.write_text(page, encoding="utf-8")
        written.append(page_path)

    # 3. Archive index (browse list).
    entries = [_summarize(b) for b in archived.values()]
    arch_html = render_archive_index(entries, config, env=env)
    arch_path = archive / "index.html"
    arch_path.write_text(arch_html, encoding="utf-8")
    written.append(arch_path)

    return written
