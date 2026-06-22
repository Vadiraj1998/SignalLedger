#!/bin/bash
# push_to_github.sh
# Called after morning scan and EOD update to commit + push data to GitHub
# Usage: bash scanner/push_to_github.sh "Morning scan 2026-06-22"

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

MSG="${1:-Auto update $(date -u '+%Y-%m-%d %H:%M UTC')}"

echo "[push_to_github] Staging changes..."
git add data/ aggregator/filter_performance.json

# Only commit if there's something to commit
if git diff --cached --quiet; then
    echo "[push_to_github] Nothing to commit. Exiting."
    exit 0
fi

git commit -m "$MSG"
git push origin main

echo "[push_to_github] Pushed successfully."
