#!/bin/bash
# Eén commando om de Tesla-scraper (markt NL) startklaar te maken en te draaien.
#
#   ./run.sh            controleer, installeer en scrape → CSV in results/
#   ./run.sh both       ook wegschrijven naar PostgreSQL (local | postgres | both)
#   ./run.sh --check    alleen controleren en installeren, niet scrapen
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

MODE="${1:-local}"
CHECK_ONLY=false
if [[ "$MODE" == "--check" ]]; then
  CHECK_ONLY=true
  MODE="local"
fi

fail() { echo "FOUT: $*" >&2; exit 1; }

# ── Vereisten ────────────────────────────────────────────────────────
command -v uv >/dev/null || fail "uv ontbreekt — installeer met: curl -LsSf https://astral.sh/uv/install.sh | sh"

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

# nodriver heeft een echte Chrome nodig; TESLA_CHROME_PATH overschrijft de zoektocht.
if [[ -z "${TESLA_CHROME_PATH:-}" ]]; then
  for candidate in \
    "$(command -v google-chrome || true)" \
    "$(command -v google-chrome-stable || true)" \
    "$(command -v chromium || true)" \
    "$(command -v chromium-browser || true)" \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      export TESLA_CHROME_PATH="$candidate"
      break
    fi
  done
fi

if [[ -n "${TESLA_CHROME_PATH:-}" ]]; then
  echo "Chrome            : $TESLA_CHROME_PATH"
else
  echo "Chrome            : niet gevonden — zet TESLA_CHROME_PATH in .env als het ophalen van cookies faalt" >&2
fi

# ── Installeren en configuratie controleren ──────────────────────────
echo "Dependencies      : uv sync"
uv sync --quiet

echo
uv run python -m tesla_mcp.selfcheck | grep -vE '^(Sample request:$|https://www\.tesla\.com/inventory/api)'

if [[ "$CHECK_ONLY" == true ]]; then
  echo
  echo "Klaar om te draaien. Start de scrape met: ./run.sh $MODE"
  exit 0
fi

# ── Scrapen ──────────────────────────────────────────────────────────
command -v claude >/dev/null || fail "Claude Code ontbreekt — zie https://docs.anthropic.com/en/docs/claude-code, of open deze map met 'claude' en typ /tesla"

echo
echo "Scrapen (opslag: $MODE). Er opent een Chrome-venster — laat dat staan tot het klaar is."
exec "$PROJECT_DIR/start.sh" "$MODE"
