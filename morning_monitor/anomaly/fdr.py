"""Multiple-comparisons control — BUILD TARGET 2 (anomaly).

Reference section 2.2 — the part naive dashboards get catastrophically wrong.
Scanning ~30 tiles at >2sigma => ~64-75% chance of >=1 false alarm EVERY morning.

The fix, all three layers, selected by the fpr_control knob:
  - DETECT ON COMPOSITES (point 5): the test family is composites + ~5-8 axis-factors
    (config.detect_on), NOT 30 raw tiles. Shrinks family 30->~6, builds corroboration in.
  - FDR: Benjamini-Yekutieli (dependency-robust; tiles cross-correlated) at q=0.10,
    or Benjamini-Hochberg. Controls the PROPORTION of false flags.
  - CORROBORATION: >=2 orthogonal tiles in the same narrative cluster before a Red.
  - 3-sigma headline as the simple Bonferroni-flavored fallback.
"""

from __future__ import annotations

import math
from typing import Literal

import numpy as np
from scipy.stats import norm


def two_sided_p_from_z(z: float) -> float:
    """Two-sided normal p-value from a standardized score. p = 2*(1 - Phi(|z|)).

    NaN/None -> 1.0 (no evidence). Clamped to [0, 1].
    """
    if z is None or not np.isfinite(z):
        return 1.0
    p = 2.0 * float(norm.sf(abs(z)))
    if not np.isfinite(p):
        return 1.0
    return min(1.0, max(0.0, p))


def _step_up(pvalues: list[float], q: float, c_m: float) -> list[bool]:
    """Generic FDR step-up. ``c_m`` is the harmonic-correction factor:
    1.0 -> Benjamini-Hochberg; sum(1/i) -> Benjamini-Yekutieli.

    Find the largest k such that p_(k) <= (k / (m * c_m)) * q; reject all with
    rank <= k. Returns a reject-mask aligned to the ORIGINAL pvalue order.
    """
    m = len(pvalues)
    if m == 0:
        return []
    p = np.asarray(pvalues, dtype=float)
    p = np.where(np.isfinite(p), p, 1.0)
    order = np.argsort(p, kind="mergesort")          # ascending, stable
    ranked = p[order]
    thresh = (np.arange(1, m + 1) / (m * c_m)) * q
    passed = ranked <= thresh
    mask_sorted = np.zeros(m, dtype=bool)
    if passed.any():
        k = int(np.max(np.nonzero(passed)[0]))       # largest passing rank (0-based)
        mask_sorted[: k + 1] = True
    # Map the sorted mask back to original positions.
    out = np.zeros(m, dtype=bool)
    out[order] = mask_sorted
    return out.tolist()


def benjamini_yekutieli(pvalues: list[float], q: float = 0.10) -> list[bool]:
    """BY step-up procedure. Returns a reject-mask aligned to pvalues.

    BY (not BH) because the test family is cross-correlated. Uses the
    dependency-robust harmonic correction c(m) = sum_{i=1..m} 1/i. q default 0.10.
    """
    m = len(pvalues)
    if m == 0:
        return []
    c_m = float(sum(1.0 / i for i in range(1, m + 1)))
    return _step_up(pvalues, q, c_m)


def benjamini_hochberg(pvalues: list[float], q: float = 0.10) -> list[bool]:
    """BH step-up (independent/PRDS assumption). Selected when fdr_method='bh'."""
    return _step_up(pvalues, q, 1.0)


def build_test_family(
    composite_scores: dict[str, float],
    axis_factor_scores: dict[str, float],
) -> dict[str, float]:
    """Merge composite change-scores + axis-aggregate scores into the ~6-member family
    the FDR runs on (detect-on-composites). Returns {family_key -> standardized_score}.

    This is the load-bearing family-shrink: 30 raw tiles -> ~6 family members
    (3 composites + ~6 axis factors), keyed so the engine can map a flagged
    family member back to its member tiles for the corroboration gate.
    """
    family: dict[str, float] = {}
    for k, v in composite_scores.items():
        if v is not None and np.isfinite(v):
            family[k] = float(v)
    for k, v in axis_factor_scores.items():
        if v is not None and np.isfinite(v):
            family[k] = float(v)
    return family


def corroboration_gate(
    flagged_family_keys: list[str],
    axis_members: dict[str, list[str]],
    tile_scores: dict[str, float],
    *,
    min_orthogonal: int = 2,
    headline_sigma: float = 3.0,
) -> list[str]:
    """Apply the corroboration gate to FDR-passing family members.

    A family key elevates to Red only if >=min_orthogonal orthogonal member tiles
    independently exceed the cluster threshold, OR a single tile exceeds the
    headline_sigma fallback. Returns the final list of Red tile/family keys.

    Cluster threshold for "a member tile counts as corroborating" is 2-sigma
    (|z|>=2): the corroboration is what guards against the 2-sigma false-alarm,
    so individual members only need to be individually notable, not headline-rare.
    Composites (no member list) corroborate against themselves at headline_sigma.
    """
    member_threshold = 2.0
    reds: list[str] = []
    for fam_key in flagged_family_keys:
        members = axis_members.get(fam_key)
        if members:
            strong = [
                m for m in members
                if m in tile_scores
                and np.isfinite(tile_scores[m])
                and abs(tile_scores[m]) >= member_threshold
            ]
            if len(strong) >= min_orthogonal:
                reds.append(fam_key)
            else:
                # Single-tile headline fallback inside the cluster.
                headline = [
                    m for m in members
                    if m in tile_scores
                    and np.isfinite(tile_scores[m])
                    and abs(tile_scores[m]) >= headline_sigma
                ]
                if headline:
                    reds.append(fam_key)
        else:
            # A composite stands on its own only at the headline threshold.
            score = tile_scores.get(fam_key)
            if score is not None and np.isfinite(score) and abs(score) >= headline_sigma:
                reds.append(fam_key)
    return reds


def select_reds(
    family_scores: dict[str, float],
    config_fpr_control: Literal["fdr", "corroboration", "composite_only"],
    *,
    q: float,
    method: str,
    headline_sigma: float,
    corroboration_min: int,
    axis_members: dict[str, list[str]],
    tile_scores: dict[str, float],
) -> list[str]:
    """Top-level Red selection honoring the fpr_control knob.

    'fdr'           -> BY/BH on the family, then corroboration gate.
    'corroboration' -> corroboration gate only (>=2 orthogonal).
    'composite_only'-> a composite alone may flag; no axis-factor expansion.

    Returns the list of Red FAMILY keys (composite ids or axis-factor ids).
    """
    if not family_scores:
        return []

    keys = list(family_scores.keys())

    if config_fpr_control == "composite_only":
        # Only composites (members empty / not in axis_members) may flag, and only
        # at the headline threshold. No axis-factor expansion, no FDR.
        reds: list[str] = []
        for k in keys:
            if not axis_members.get(k):  # treat as composite-like
                s = family_scores[k]
                if s is not None and np.isfinite(s) and abs(s) >= headline_sigma:
                    reds.append(k)
        return reds

    if config_fpr_control == "corroboration":
        # No FDR pre-filter — every family member is a corroboration candidate.
        return corroboration_gate(
            keys, axis_members, tile_scores,
            min_orthogonal=corroboration_min, headline_sigma=headline_sigma,
        )

    # Default: 'fdr' -> p-values from family scores, BY/BH step-up, then gate.
    pvals = [two_sided_p_from_z(family_scores[k]) for k in keys]
    if method == "bh":
        mask = benjamini_hochberg(pvals, q=q)
    else:
        mask = benjamini_yekutieli(pvals, q=q)
    fdr_passed = [k for k, passed in zip(keys, mask) if passed]

    # 3-sigma headline fallback: any family member at >=headline_sigma flags even
    # if FDR did not (simple Bonferroni-flavored safety net).
    for k in keys:
        s = family_scores[k]
        if k not in fdr_passed and s is not None and np.isfinite(s) and abs(s) >= headline_sigma:
            fdr_passed.append(k)

    return corroboration_gate(
        fdr_passed, axis_members, tile_scores,
        min_orthogonal=corroboration_min, headline_sigma=headline_sigma,
    )
