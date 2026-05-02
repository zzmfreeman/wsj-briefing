#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Run WSJ Briefing in Docker (SKIP_GIT_PUSH=1)
docker run --rm \
  -v "$SCRIPT_DIR/cn_wsj_cookies.txt:/data/cookies/cn_wsj_cookies.txt:ro" \
  -v "$SCRIPT_DIR/archive:/data/archive" \
  -v "/Users/zzm/.openclaw/workspace/openclaw_macmini_ICnews/docs:/data/web" \
  -v "/Users/zzm/.openclaw/openclaw.json:/app/openclaw.json:ro" \
  -e OPENCLAW_CONFIG=/app/openclaw.json \
  -e COOKIE_FILE=/data/cookies/cn_wsj_cookies.txt \
  -e ARCHIVE_DIR=/data/archive \
  -e WEB_DIR=/data/web \
  -e SKIP_GIT_PUSH=1 \
  wsj-briefing 2>&1

# Git push on host (after container exits with generated files)
echo "[$(date '+%H:%M:%S')] Pushing to GitHub..."
cd /Users/zzm/.openclaw/workspace/openclaw_macmini_ICnews
git add -A 2>/dev/null
git diff --cached --quiet || git commit -m "wsj-briefing update $(date +%Y-%m-%d)" && git push 2>/dev/null
echo "[$(date '+%H:%M:%S')] Done."
