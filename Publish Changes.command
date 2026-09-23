#!/bin/bash
# Double-click this in Finder to send your DataHub changes live.
# Netlify and the Cloudflare worker update in about a minute; the Pi within 5 minutes.
cd "$(dirname "$0")" || exit 1
echo "=== Harvest Watch: publish changes ==="
echo
git pull --ff-only -q 2>/dev/null
if [ -z "$(git status --porcelain)" ]; then
  echo "Nothing has changed - nothing to publish."
else
  echo "These files changed:"
  git status --short
  echo
  read -r -p "Type a short note on what changed (or just press Return): " msg
  if git add -A && git commit -q -m "${msg:-Update}" && git push -q; then
    echo
    echo "PUBLISHED. Website + cloud watcher: ~1 minute. Pi: within 5 minutes (you'll get an ntfy note)."
  else
    echo
    echo "Something went wrong. Copy this window and send it to Claude."
  fi
fi
echo
read -r -n 1 -p "Press any key to close this window."
