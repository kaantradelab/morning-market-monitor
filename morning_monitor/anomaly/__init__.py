"""Anomaly engine package — BUILD TARGET 2 (the heavy stats).

Top-level contract the pipeline calls:

    enrich(series_by_key, config, calendar) -> AnomalyResult

where AnomalyResult bundles:
    composites      : models.Composites           (with level_pct + change_score)
    tiles           : list[models.Tile]           (ewma_z, pct_1y/3y, robust_z, color, staleness)
    corr_breaks     : list[models.CorrBreak]
    dog_didnt_bark  : list[models.DogDidntBark]
    flagged_keys    : list[str]                    (keys that passed FDR + corroboration -> Red)

Submodules:
    transforms.py  -> log_return / first_diff / ratio
    stats.py       -> EWMA-sigma, standardize, empirical percentile, robust-z, level-pct
    fdr.py         -> Benjamini-Yekutieli / -Hochberg + corroboration gate (detect-on-composites)
    correlation.py -> structural-residual corr-break + dog-didn't-bark
    engine.py      -> enrich() orchestrator + AnomalyResult
"""

from .engine import AnomalyResult, enrich  # noqa: F401
