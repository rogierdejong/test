#!/bin/bash
# Eén controle van de voorraad tegen je filter, met melding bij een nieuwe hit.
# Bedoeld om door launchd aangeroepen te worden (zie install-watch.sh), maar
# werkt net zo goed met de hand: ./watch.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# launchd start met een kale PATH — uv staat daar niet in.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

mkdir -p results
LOG="$PROJECT_DIR/results/watch.log"

# Houd het logbestand klein.
if [[ -f "$LOG" && $(wc -c <"$LOG") -gt 1000000 ]]; then
  tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

{
  echo
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="
  uv run python -m tesla_mcp.watch
} >> "$LOG" 2>&1
