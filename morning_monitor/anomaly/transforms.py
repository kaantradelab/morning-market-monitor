"""Series transforms — BUILD TARGET 2 (anomaly).

Reference section 2.1: changes, not levels. Prices -> log-returns; yields/spreads
-> first-differences. 'level'/'sign' transforms produce no z (handled upstream).

All functions are pure, NaN-tolerant, and never raise on degenerate input — the
engine relies on graceful degradation (a bad series -> gray tile, never a crash).
"""

from __future__ import annotations

import numpy as np


def _clean(values: np.ndarray) -> np.ndarray:
    """Coerce to float ndarray. Non-array / None -> empty."""
    if values is None:
        return np.asarray([], dtype=float)
    arr = np.asarray(values, dtype=float)
    return arr.reshape(-1)


def log_return(values: np.ndarray) -> np.ndarray:
    """diff(log(values)). Length n-1. Guards non-positive values.

    A non-positive (<=0) or NaN value makes its adjacent return NaN rather than
    raising — the EWMA/percentile machinery downstream is NaN-tolerant.
    """
    arr = _clean(values)
    if arr.size < 2:
        return np.asarray([], dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        logs = np.where(arr > 0, np.log(arr), np.nan)
    return np.diff(logs)


def first_diff(values: np.ndarray) -> np.ndarray:
    """values[1:] - values[:-1]. Length n-1."""
    arr = _clean(values)
    if arr.size < 2:
        return np.asarray([], dtype=float)
    return np.diff(arr)


def ratio_series(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    """num/den element-wise (already date-aligned). For copper_gold, rsp_spy, vix_term.

    Aligns on the shorter length (tail-aligned, newest-anchored). den==0 -> NaN.
    """
    a = _clean(num)
    b = _clean(den)
    if a.size == 0 or b.size == 0:
        return np.asarray([], dtype=float)
    n = min(a.size, b.size)
    a = a[-n:]
    b = b[-n:]
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(b != 0, a / b, np.nan)
    return out
