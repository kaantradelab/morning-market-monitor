"""Shared retry constants for bounded exponential back-off.

Imported by ingest._safe() and calendar._fetch_fred(). Each consumer calls
time.sleep directly so tests can independently monkeypatch the sleep
(e.g. patch("morning_monitor.sources.ingest.time.sleep")).

Defaults give sleeps of 1 s then 2 s between attempts, so a full 3-attempt
failure adds ≤ ~3 s of latency before the degraded result is returned.
"""
from __future__ import annotations

_RETRY_ATTEMPTS: int = 3        # total attempts including the first
_RETRY_BASE_DELAY: float = 1.0  # sleep before attempt[1]: base * factor**0 = 1 s
_RETRY_FACTOR: float = 2.0      # exponential multiplier: sleep[i] = base * factor**i
