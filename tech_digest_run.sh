#!/usr/bin/env bash
# tech_digest_run.sh — daily runner for the tech digest cron job.
#
# Runs the fetcher first, then the summarizer. If the fetcher fails,
# continue to summarizer anyway (the summarizer uses the data file,
# which may still be useful from a previous run if the fetcher died
# mid-flight).
set -e

SCRIPTS=/data/.hermes/scripts
PYTHON=/opt/hermes/.venv/bin/python

echo "=== tech_digest_run.sh $(date -u) ==="
echo
echo "--- Stage 1: fetch ---"
$PYTHON "$SCRIPTS/tech_digest.py" || echo "(fetcher returned non-zero; continuing)"

echo
echo "--- Stage 1b: trending ---"
$PYTHON "$SCRIPTS/trending.py" || echo "(trending returned non-zero; continuing)"

echo
echo "--- Stage 2: summarize + send ---"
exec $PYTHON "$SCRIPTS/tech_digest_summarize.py"
