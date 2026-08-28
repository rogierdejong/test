#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$PROJECT_DIR/.claude/skills/tesla"

# Load environment variables (optional — defaults target the Dutch market)
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  source "$PROJECT_DIR/.env"
  set +a
fi

# Storage mode: local, postgres, or both (default: local)
STORAGE_MODE="${1:-local}"

cd "$PROJECT_DIR"

# De scraper logt voortgang en fouten naar stderr. Die mag niet door de
# JSON-stream heen lopen (format_output.py leest stdout), dus gaat hij naar een
# logbestand — volg live met: tail -f results/scrape.log
mkdir -p "$PROJECT_DIR/results"
LOG_FILE="$PROJECT_DIR/results/scrape.log"
: > "$LOG_FILE"
echo "Voortgang en fouten: $LOG_FILE  (volg live met: tail -f results/scrape.log)"

INSTRUCTION="Execute the Tesla inventory scraping workflow above. Market: ${TESLA_REGION:-NL}. Storage mode: $STORAGE_MODE. Do NOT ask the user anything — just run the full workflow with this storage mode and report results at the end."

# Pipe skill docs via stdin to avoid shell expansion of $, ` characters
{
  cat "$SKILL_DIR/SKILL.md"
  echo ""
  echo "---"
  echo "$INSTRUCTION"
} | claude -p - \
  --verbose \
  --allowedTools \
    "mcp__tesla-inventory__region_info,mcp__tesla-inventory__acquire_cookies,mcp__tesla-inventory__search_inventory,mcp__tesla-inventory__search_top_n,mcp__tesla-inventory__merge_results,mcp__tesla-inventory__save_to_postgres,mcp__tesla-inventory__save_results,Bash,Read" \
  --output-format stream-json \
  2>"$LOG_FILE" \
  | python3 "$PROJECT_DIR/format_output.py"
