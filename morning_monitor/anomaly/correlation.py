"""Correlation-break + dog-didn't-bark — BUILD TARGET 2 (anomaly).

Reference section 2.3 / 2.4. Correlation-break is a RESIDUAL of a structural
relationship, NOT a rolling-corr move (rolling corr rises mechanically in stress =>
detects vol, not regime). Dog-didn't-bark is the OTHER tail of the same conditional
residual object: on event days, realized < ~0.5x expected = priced-in / coiled spring.

Both detectors are graceful: degraded/short inputs -> triggered=False with a note,
never a raise. The target/factor series are first transformed to CHANGES (the
relationship is fit on co-movements, not levels — reference 2.1 + 2.3).
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from ..models import CalendarEvent, CorrBreak, DogDidntBark, RawSeries
from . import stats, transforms


def _series_values(rs: RawSeries) -> np.ndarray:
    """Latest-anchored float array of a RawSeries' history values."""
    if rs is None or not rs.history:
        return np.asarray([], dtype=float)
    return np.asarray([h.value if h.value is not None else np.nan for h in rs.history], dtype=float)


def _changes(rs: RawSeries) -> np.ndarray:
    """First-difference of a series' levels (the co-movement the fit relates).

    First-diff is the generic, scale-stable choice for the structural fit; the
    relationship A~f(B) is between day-over-day changes, matching 2.3's
    HY-OAS-Delta on ACWI-ret + VIX-Delta example.
    """
    return transforms.first_diff(_series_values(rs))


def fit_residual(target: np.ndarray, factors: list[np.ndarray], window_days: int = 252) -> np.ndarray:
    """Fit target ~ f(factors) (OLS, with intercept) over window_days; return the
    residual series.

    e.g. gold ~ -beta*real_yield + gamma*usd ; hy_oas ~ f(acwi_ret, vix). The
    dislocation IS today's residual as a vol-adjusted/percentile outlier.

    Inputs are tail-aligned to the shortest common length, then to the trailing
    ``window_days``. Returns the full residual series (newest last). NaN rows are
    dropped from the FIT but the returned residual array is computed for every
    aligned row (NaN where inputs are NaN). Empty -> empty.
    """
    y = np.asarray(target, dtype=float).reshape(-1)
    fs = [np.asarray(f, dtype=float).reshape(-1) for f in factors]
    if y.size == 0 or any(f.size == 0 for f in fs):
        return np.asarray([], dtype=float)

    n = min([y.size] + [f.size for f in fs])
    if window_days and window_days > 0:
        n = min(n, window_days + 1)
    y = y[-n:]
    fs = [f[-n:] for f in fs]

    X = np.column_stack([np.ones(n)] + fs)            # intercept + factors
    rows_finite = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
    if int(np.sum(rows_finite)) < (X.shape[1] + 2):   # need > params + slack
        return np.asarray([], dtype=float)

    Xf = X[rows_finite]
    yf = y[rows_finite]
    try:
        beta, *_ = np.linalg.lstsq(Xf, yf, rcond=None)
    except np.linalg.LinAlgError:
        return np.asarray([], dtype=float)

    # Residual for EVERY aligned row (so the latest row's residual is included
    # even though degenerate rows were excluded from the fit).
    resid = np.full(n, np.nan, dtype=float)
    valid = np.all(np.isfinite(X), axis=1) & np.isfinite(y)
    resid[valid] = y[valid] - X[valid] @ beta
    return resid


def detect_corr_break(
    name: str,
    target: RawSeries,
    factors: list[RawSeries],
    *,
    window_days: int = 756,
    sigma_threshold: float = 2.5,
    persistence_days: int = 3,
    weight_sign_flip: bool = False,
) -> CorrBreak:
    """Flag when today's residual is a >sigma_threshold EWMA-sigma outlier AND has
    persisted persistence_days. Sign-flips (e.g. stock-bond corr) weighted heavily.
    Returns a CorrBreak (triggered False if conditions unmet or inputs degraded).
    """
    # Degraded-input guard: any missing/failed series -> no trigger, just a note.
    if target is None or not getattr(target, "ok", False) or not factors:
        return CorrBreak(name=name, residual_z=None, persistence_days=0,
                         triggered=False, note="target degraded/missing")
    if any((f is None or not getattr(f, "ok", False)) for f in factors):
        return CorrBreak(name=name, residual_z=None, persistence_days=0,
                         triggered=False, note="factor degraded/missing")

    y = _changes(target)
    xs = [_changes(f) for f in factors]
    resid = fit_residual(y, xs, window_days=window_days)
    finite = resid[np.isfinite(resid)]
    if finite.size < 5:
        return CorrBreak(name=name, residual_z=None, persistence_days=0,
                         triggered=False, note="insufficient overlapping history")

    # Standardize the residual series by its own short EWMA vol (causal), then the
    # latest residual's z is the dislocation magnitude.
    z_series = stats.standardize(resid, lam=0.94)
    z_finite = z_series[np.isfinite(z_series)]
    if z_finite.size == 0:
        return CorrBreak(name=name, residual_z=None, persistence_days=0,
                         triggered=False, note="residual vol undefined")
    latest_z = float(z_finite[-1])

    # Sign-flip detection on the structural beta of the FIRST factor: compare the
    # sign of the slope on the trailing short window vs the full fit window.
    sign_flip = False
    if weight_sign_flip and len(xs) >= 1:
        sign_flip = _detect_sign_flip(y, xs, window_days=window_days)

    # Reference SS2.3: a sign-flip is weighted MORE within a persisted break — it
    # does NOT waive the >=3-day persistence gate. We amplify by lowering the
    # effective sigma a flip must clear (more sensitive), while the persistence
    # requirement is identical for both paths.
    effective_sigma = sigma_threshold * 0.8 if sign_flip else sigma_threshold

    # Persistence: count consecutive trailing days (incl. today) over the
    # (possibly amplified) threshold.
    persist = 0
    for zv in z_finite[::-1]:
        if abs(zv) >= effective_sigma:
            persist += 1
        else:
            break

    required = max(1, persistence_days)
    # Persistence is required for ALL paths (sign-flips amplify sensitivity via the
    # lower effective_sigma, they do NOT reduce the persistence requirement).
    triggered = bool(
        (abs(latest_z) >= effective_sigma)
        and persist >= required
    )

    note = (
        f"residual {latest_z:+.2f} EWMA-sigma, persisted {persist}d "
        f"(need {required}d){', SIGN-FLIP (amplified)' if sign_flip else ''}"
    )
    return CorrBreak(
        name=name, residual_z=round(latest_z, 4), persistence_days=int(persist),
        triggered=triggered, note=note, sign_flip=bool(sign_flip),
    )


def _detect_sign_flip(y: np.ndarray, xs: list[np.ndarray], *, window_days: int) -> bool:
    """Sign-flip on the first factor's structural beta: short-window slope sign vs
    long-window slope sign. The 2022 stock-bond regime is exactly this flip.
    """
    n = min([y.size] + [x.size for x in xs])
    if n < 40:
        return False
    short = max(20, n // 6)

    def _slope(yy: np.ndarray, xx: np.ndarray) -> float:
        m = min(yy.size, xx.size)
        yy, xx = yy[-m:], xx[-m:]
        ok = np.isfinite(yy) & np.isfinite(xx)
        if int(np.sum(ok)) < 10:
            return float("nan")
        A = np.column_stack([np.ones(int(np.sum(ok))), xx[ok]])
        try:
            beta, *_ = np.linalg.lstsq(A, yy[ok], rcond=None)
        except np.linalg.LinAlgError:
            return float("nan")
        return float(beta[1])

    x0 = xs[0]
    long_slope = _slope(y[-(window_days + 1):], x0[-(window_days + 1):])
    short_slope = _slope(y[-short:], x0[-short:])
    if not (np.isfinite(long_slope) and np.isfinite(short_slope)):
        return False
    # Require both meaningfully non-zero AND opposite sign.
    eps = 1e-9
    return (abs(long_slope) > eps and abs(short_slope) > eps
            and np.sign(long_slope) != np.sign(short_slope))


def detect_dog_didnt_bark(
    tile: RawSeries,
    event: CalendarEvent,
    *,
    expected_move: Optional[float] = None,
    ratio_threshold: float = 0.5,
    event_day_indices: Optional[list[int]] = None,
    min_event_days: int = 8,
) -> DogDidntBark:
    """On a scheduled-event day, compare realized standardized move to the expected
    EVENT-DAY move (reference SS2.4). ratio<threshold => no-bark triggered.

    Expected-move precedence:
      1. ``expected_move`` (e.g. straddle-implied) if a positive finite value is given.
      2. The mean ABSOLUTE standardized move of this tile **on prior occurrences of
         the SAME event type** — the 3y event-day subset (``event_day_indices``,
         which index this tile's standardized change series; the latest/today index
         is excluded). This is the reference-correct conditional baseline: an
         unconditional all-day mean understates the event-day expected move and
         biases the ratio up.
      3. Fallback — when no straddle and the event-day subset is too thin
         (< ``min_event_days``), use the all-day mean absolute standardized move,
         flagged LOWER-CONFIDENCE in the note (the baseline is unconditional).

    Realized = |today's standardized change|. Realized < ratio_threshold x expected
    on a high-impact day = the dog that didn't bark (priced-in / coiled spring).
    """
    if tile is None or not getattr(tile, "ok", False):
        return DogDidntBark(
            tile_key=getattr(tile, "key", "?") if tile else "?",
            event=event.event, expected_move=expected_move, realized_move=None,
            ratio=None, triggered=False, note="tile degraded/missing",
        )

    vals = _series_values(tile)
    changes = transforms.first_diff(vals)
    z = stats.standardize(changes, lam=0.94)
    # Keep index alignment: the i-th entry of z corresponds to changes[i]; we mask
    # to finite values but remember positions so event-day indices stay meaningful.
    finite_mask = np.isfinite(z)
    z_finite = z[finite_mask]
    if z_finite.size < 3:
        return DogDidntBark(
            tile_key=tile.key, event=event.event, expected_move=expected_move,
            realized_move=None, ratio=None, triggered=False,
            note="insufficient history for expected-move baseline",
        )

    realized = float(abs(z_finite[-1]))
    low_confidence = False

    if expected_move is not None and np.isfinite(expected_move) and expected_move > 0:
        exp = float(expected_move)
        basis = "straddle/implied"
    else:
        # Conditional (event-day) baseline first; restrict to prior same-event days.
        event_baseline: Optional[np.ndarray] = None
        if event_day_indices:
            # Map requested indices onto the standardized series, drop today's
            # (last) index and any out-of-range / non-finite positions.
            last_idx = z.size - 1
            sel = np.zeros(z.size, dtype=bool)
            for idx in event_day_indices:
                if 0 <= idx < z.size and idx != last_idx:
                    sel[idx] = True
            cand = z[sel & finite_mask]
            if cand.size >= min_event_days:
                event_baseline = cand

        if event_baseline is not None and event_baseline.size:
            exp = float(np.mean(np.abs(event_baseline)))
            basis = f"3y event-day ({event_baseline.size} days)"
        else:
            # Fallback: unconditional all-day mean — flag lower confidence.
            baseline = z_finite[:-1] if z_finite.size > 1 else z_finite
            exp = float(np.mean(np.abs(baseline)))
            low_confidence = True
            basis = "all-day mean (LOW-CONFIDENCE: thin event-day history)"
        if not np.isfinite(exp) or exp <= 0:
            exp = 1.0  # standardized series ~ unit scale by construction

    ratio = realized / exp if exp > 0 else float("nan")
    triggered = bool(
        getattr(event, "high_impact", False)
        and np.isfinite(ratio)
        and ratio < ratio_threshold
    )
    note = (
        f"realized {realized:.2f}sigma vs expected {exp:.2f}sigma "
        f"[{basis}] (ratio {ratio:.2f}, threshold {ratio_threshold}) "
        f"{'NO-BARK' if triggered else 'normal'}"
        f"{' — confidence reduced' if low_confidence else ''}"
    )
    return DogDidntBark(
        tile_key=tile.key, event=event.event,
        expected_move=round(exp, 4), realized_move=round(realized, 4),
        ratio=round(ratio, 4) if np.isfinite(ratio) else None,
        triggered=triggered, note=note,
    )
