#!/usr/bin/env bash
# Run AFTER creating an empty public repo at:
#   https://github.com/gowrii23/momentum-trade-desk
set -euo pipefail
cd "$(dirname "$0")"

if [ -d .git ]; then
  echo "Git already initialized in $(pwd)"
else
  git init -b main
fi

git add .
git commit -m "Initial commit: NSE momentum scanner + Nifty OTM CE signals" || true

if git remote get-url origin &>/dev/null; then
  git remote set-url origin "https://github.com/gowrii23/momentum-trade-desk.git"
else
  git remote add origin "https://github.com/gowrii23/momentum-trade-desk.git"
fi

git push -u origin main

echo ""
echo "Next steps:"
echo "  1. Settings → Pages → Source: GitHub Actions"
echo "  2. Actions → Daily Momentum Scan → Run workflow (backfill_days=200 first time)"
