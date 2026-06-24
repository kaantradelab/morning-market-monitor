"""morning_monitor — SPEC-2 backend / snapshot side of the morning-market-monitor.

Pipeline (composes left-to-right, all through models.Brief):

    sources (ingestion)  -> dict[str, RawSeries]
    anomaly              -> Composites + list[Tile] + corr_breaks + dog_didnt_bark
    brief                -> Brief  (assembles cards / calendar / plumbing flags, writes JSON)
    render               -> static HTML (index.html + archive/<date>.html)

Posture: SNAPSHOT. Runs once a morning (cron 06:00 UTC = 09:00 Istanbul) on free
EOD/snapshot data. Graceful degradation: a failing source produces a stale/missing
tile, never crashes the run.
"""

__version__ = "0.1.0"
