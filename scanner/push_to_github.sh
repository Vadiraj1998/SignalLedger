#!/bin/bash
# push_to_github.sh
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

MSG="${1:-Auto update $(date -u '+%Y-%m-%d %H:%M UTC')}"

echo "[push_to_github] Staging changes..."
git add data/ aggregator/filter_performance.json 2>/dev/null || true

if git diff --cached --quiet; then
    echo "[push_to_github] Nothing to commit. Exiting."
    exit 0
fi

git commit -m "$MSG"

# Pull before push to avoid rejection
git pull --rebase origin main 2>/dev/null || true

git push origin main
echo "[push_to_github] Pushed successfully."
