"""Core statistics — BUILD TARGET 2 (anomaly).

Reference section 2.1. The load-bearing window construction:
  STANDARDIZE BY SHORT EWMA-VOL FIRST -> TAKE LONG PERCENTILE SECOND.
  Short denominator removes the vol-regime (~stationary); long numerator gives
  tail resolution. Resolves stationarity-vs-tail tension.

Every routine is NaN-tolerant and returns ``float('nan')`` (NOT raises) on
insufficient/degenerate input. The engine maps NaN -> None on the tile.
"""

from __future__ import annotations

import numpy as np

# Iglewicz-Hoaglin constant: 0.6745 = Phi^{-1}(0.75), makes MAD a consistent
# estimator of sigma for a normal distribution.
_MAD_CONST = 0.6745


def _finite(arr: np.ndarray) -> np.ndarray:
    """Drop NaN/inf; return a 1-D float array."""
    a = np.asarray(arr, dtype=float).reshape(-1)
    return a[np.isfinite(a)]


def ewma_sigma(changes: np.ndarray, lam: float = 0.94) -> float:
    """EWMA volatility estimate (RiskMetrics, lambda=0.94 daily, half-life ~11d).

    Returns the latest sigma-hat over the change series. Recursion:
        var_t = lam*var_{t-1} + (1-lam)*x_{t-1}^2 ;  sigma_hat = sqrt(var_T).

    Seeded with the sample variance of the (finite) series so the estimate is
    well-conditioned even on short history. Returns NaN if < 2 finite points.
    """
    x = _finite(changes)
    if x.size < 2:
        return float("nan")
    # Seed with the unconditional sample variance (centered at 0 since these are
    # already de-trended changes/returns).
    var = float(np.mean(x**2))
    if not np.isfinite(var) or var <= 0:
        var = float(np.var(x))
    if not np.isfinite(var) or var <= 0:
        return float("nan")
    # Walk the recursion forward; the last-but-one squared change updates to T.
    for prev in x[:-1]:
        var = lam * var + (1.0 - lam) * (prev * prev)
    sigma = float(np.sqrt(var))
    return sigma if np.isfinite(sigma) and sigma > 0 else float("nan")


def garch_sigma(changes: np.ndarray) -> float:
    """GARCH(1,1)-t sigma-hat (vol_model=garch). EWMA is the robust default.

    OPTIONAL PATH — clearly marked. A lightweight scipy MLE fit of the symmetric
    GARCH(1,1) variance recursion var_t = omega + alpha*x_{t-1}^2 + beta*var_{t-1}.
    Falls back to ``ewma_sigma`` if the optimizer fails or history is too short
    (graceful degradation — never raises). The contract is "implement ewma fully;
    garch may be a clearly-marked optional path."
    """
    x = _finite(changes)
    if x.size < 60:  # too little data for a stable GARCH fit -> EWMA fallback
        return ewma_sigma(x)
    try:
        from scipy.optimize import minimize  # local import: optional path only

        uncond = float(np.var(x))
        if not np.isfinite(uncond) or uncond <= 0:
            return ewma_sigma(x)

        def neg_loglik(theta: np.ndarray) -> float:
            omega, alpha, beta = theta
            if omega <= 0 or alpha < 0 or beta < 0 or (alpha + beta) >= 0.999:
                return 1e12
            var = uncond
            ll = 0.0
            for xt in x:
                var = omega + alpha * (xt * xt) + beta * var
                if var <= 0 or not np.isfinite(var):
                    return 1e12
                ll += np.log(var) + (xt * xt) / var
            return 0.5 * ll

        # Sensible RiskMetrics-flavored start: alpha small, beta high.
        x0 = np.array([uncond * 0.05, 0.06, 0.90])
        res = minimize(neg_loglik, x0, method="Nelder-Mead",
                       options={"maxiter": 2000, "xatol": 1e-8, "fatol": 1e-8})
        omega, alpha, beta = res.x
        if not res.success or omega <= 0 or (alpha + beta) >= 0.999:
            return ewma_sigma(x)
        # Roll the fitted recursion forward to the terminal conditional variance.
        var = uncond
        for xt in x:
            var = omega + alpha * (xt * xt) + beta * var
        sigma = float(np.sqrt(var))
        return sigma if np.isfinite(sigma) and sigma > 0 else ewma_sigma(x)
    except Exception:
        return ewma_sigma(x)


def standardize(changes: np.ndarray, lam: float = 0.94) -> np.ndarray:
    """z_t = change_t / ewma_sigma_up_to_t. The short-EWMA-standardized series.

    This is the series whose LONG-window empirical percentile gives rarity
    (reference 2.1 — standardize by short EWMA FIRST). Each point is divided by
    the EWMA sigma estimated from the changes STRICTLY BEFORE it (causal, no
    look-ahead). Points lacking enough prior history -> NaN.

    Returns an array the same length as the finite-filtered changes.
    """
    x = np.asarray(changes, dtype=float).reshape(-1)
    n = x.size
    if n == 0:
        return np.asarray([], dtype=float)
    out = np.full(n, np.nan, dtype=float)
    # Causal EWMA variance: var seeded from the first ~min(20,n) finite points so
    # early entries are not divided by garbage; updated point-by-point.
    finite0 = _finite(x[: min(20, n)])
    if finite0.size >= 2:
        var = float(np.mean(finite0**2))
    else:
        var = float("nan")
    for t in range(n):
        sigma = np.sqrt(var) if (np.isfinite(var) and var > 0) else np.nan
        xt = x[t]
        if np.isfinite(sigma) and sigma > 0 and np.isfinite(xt):
            out[t] = xt / sigma
        # Update variance with this observation for the NEXT point (causal).
        if np.isfinite(xt):
            if np.isfinite(var):
                var = lam * var + (1.0 - lam) * (xt * xt)
            else:
                var = xt * xt
    return out


def empirical_percentile(standardized: np.ndarray, latest: float, window_days: int) -> float:
    """Percentile (0-100) of ``latest`` within the last ``window_days`` of ``standardized``.

    Distribution-free, comparable across tiles with different kurtosis (ECB CISS
    rank-transform idea). window_days: 252 (1y) / 756 (3y) / len (full) per the
    percentile_window knob. Two-sided rarity: ranks by MAGNITUDE so a large
    negative move scores as 'rare' as a large positive one (a market shock is a
    shock in either direction). Returns NaN on empty/degenerate input.
    """
    if latest is None or not np.isfinite(latest):
        return float("nan")
    arr = np.asarray(standardized, dtype=float).reshape(-1)
    if window_days and window_days > 0:
        arr = arr[-window_days:]
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return float("nan")
    # Two-sided magnitude rank: P(|X| <= |latest|).
    mag = np.abs(arr)
    target = abs(latest)
    pct = 100.0 * float(np.mean(mag <= target))
    return pct


def robust_z(values: np.ndarray, latest: float) -> float:
    """0.6745*(latest - median)/MAD (Iglewicz-Hoaglin). For spiky series; flag |z|>3.5.

    MAD = median(|x - median(x)|). If MAD is 0 (flat series) returns 0.0 for an
    on-median point, else NaN (no scale to measure against). Returns NaN on empty.
    """
    if latest is None or not np.isfinite(latest):
        return float("nan")
    x = _finite(values)
    if x.size == 0:
        return float("nan")
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med)))
    if mad <= 0:
        return 0.0 if latest == med else float("nan")
    return _MAD_CONST * (latest - med) / mad


def level_percentile(values: np.ndarray, latest: float, window_days: int = 756) -> float:
    """Percentile (0-100) of the raw LEVEL over ``window_days`` — state context.

    e.g. 'HY OAS at 95th pct of 3y' is informative even on a zero-change day.
    One-sided (high level = high percentile), since 'where in its range' is the
    state question. Returns NaN on empty/degenerate input.
    """
    if latest is None or not np.isfinite(latest):
        return float("nan")
    arr = np.asarray(values, dtype=float).reshape(-1)
    if window_days and window_days > 0:
        arr = arr[-window_days:]
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return float("nan")
    return 100.0 * float(np.mean(arr <= latest))
