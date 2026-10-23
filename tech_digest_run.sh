#!/usr/bin/env bash
# tech_digest_run.sh — daily runner for the tech digest cron job.
#
# Runs the fetcher and trending stages, then the summarizer. A non-zero
# exit from either fetch stage usually means a partial result (some
# sources failed but the file was still written), so we continue. The
# summarizer only uses files dated today: if a stage wrote nothing, its
# section is omitted (trending) or the run fails (fetcher) instead of
# re-sending yesterday's data.
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
