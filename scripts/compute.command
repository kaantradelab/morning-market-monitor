#!/bin/zsh
# Morning Market Monitor — "Calculate" trigger (Kaan double-clicks this in Finder).
#
# Mac-local architecture (no cloud): pull/compute the brief from the LOCAL trading
# data bank (breadth) + FRED (calendar/rates), render the static site to ./site/,
# and open today's brief in the default browser.
#
# Breadth reads the local data bank first (free, no key). It only reaches out to
# Nasdaq Data Link to gap-fill the latest missing day(s) — and ONLY if
# NASDAQ_DATA_LINK_API_KEY is exported (zsh sources ~/.zshenv on every invocation,
# interactive or launchd). The run degrades gracefully if a source/key is missing;
# it never crashes — a dead source becomes a stale/missing tile.
#
# This same script is the launchd payload (deploy/com.morningmonitor.daily.plist).

set -e
# pipefail: a non-zero exit anywhere in a `... | tee -a "$LOG"` pipeline must trip
# `set -e`. Without it, tee's exit 0 masks a real python/render failure and the
# script would march on (and `open` a stale site) as if the run had succeeded.
setopt pipefail

REPO="/Users/kaanoztekin/myos/workspace/morning-market-monitor"
PY="$REPO/.venv/bin/python"
LOGDIR="$REPO/logs"

cd "$REPO" || { print -r -- "FATAL: cannot cd $REPO"; exit 1; }
mkdir -p "$LOGDIR"
LOG="$LOGDIR/compute_$(date +%Y%m%d).log"

print -r -- "=== $(date '+%Y-%m-%d %H:%M:%S %z') compute start ===" | tee -a "$LOG"

if [[ ! -x "$PY" ]]; then
  print -r -- "FATAL: venv python not found at $PY — create it: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" | tee -a "$LOG"
  exit 1
fi

# Run the pipeline. It renders site/index.html + site/archive/<date>.html + the
# archive index. Exit 0 even when degraded; non-zero only on an unrecoverable
# config error — so `set -e` here trips only on a real failure.
print -r -- "running: $PY -m morning_monitor.main --config config.yaml" | tee -a "$LOG"
"$PY" -m morning_monitor.main --config config.yaml 2>&1 | tee -a "$LOG"

INDEX="$REPO/site/index.html"
if [[ -f "$INDEX" ]]; then
  print -r -- "opening $INDEX" | tee -a "$LOG"
  open "$INDEX"
else
  print -r -- "WARN: $INDEX not found — render did not produce an index page" | tee -a "$LOG"
  exit 1
fi

print -r -- "=== $(date '+%Y-%m-%d %H:%M:%S %z') compute done ===" | tee -a "$LOG"
