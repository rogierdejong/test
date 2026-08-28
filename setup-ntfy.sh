#!/bin/bash
# Zet ntfy-push aan voor de wachter.
#
#   ./setup-ntfy.sh mijn-topic-naam   zet NTFY_TOPIC in .env en stuurt een test
#   ./setup-ntfy.sh --find            zoekt een topic dat je elders al gebruikt
#   ./setup-ntfy.sh                   stelt een nieuwe, onraadbare naam voor
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
cd "$PROJECT_DIR"

if [[ "${1:-}" == "--find" ]]; then
  echo "Zoeken naar een NTFY_TOPIC dat je elders al gebruikt..."
  echo "(doorzoekt je home-map, dit duurt een halve minuut)"
  echo

  candidates=$(mktemp)
  trap 'rm -f "$candidates"' EXIT

  # Zware mappen overslaan, anders duurt het eeuwig.
  find "$HOME" \
       \( -name node_modules -o -name .git -o -name Caches -o -name .venv \
          -o -name "*.app" -o -name Photos\ Library.photoslibrary \) -prune -o \
       -type f \( -name ".env" -o -name "*.env" -o -name ".envrc" \
          -o -name "*.conf" -o -name "*.yml" -o -name "*.yaml" -o -name "*.json" \
          -o -name "*.sh" \) -print 2>/dev/null \
    | head -20000 > "$candidates" || true

  found=0
  while IFS= read -r file; do
    line=$(grep -i -h -m1 -E "NTFY_TOPIC[=:]|ntfy\.sh/" "$file" 2>/dev/null || true)
    if [[ -n "$line" ]]; then
      echo "  $file"
      echo "      ${line#"${line%%[![:space:]]*}"}"
      found=$((found + 1))
    fi
  done < "$candidates"

  echo
  if [[ $found -eq 0 ]]; then
    echo "Geen bestaand topic gevonden in je bestanden."
    echo "Kijk in de ntfy-app op je telefoon: daar staan de topics waarop je"
    echo "geabonneerd bent. Of maak een nieuw topic met: ./setup-ntfy.sh"
  else
    echo "$found bestand(en) met een ntfy-verwijzing gevonden."
    echo "Zet het gewenste topic met: ./setup-ntfy.sh <topicnaam>"
  fi
  exit 0
fi

TOPIC="${1:-}"

if [[ -z "$TOPIC" ]]; then
  SUGGESTION="tesla-nl-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 12)"
  echo "Geef een topicnaam mee. Een nieuwe, onraadbare naam zou zijn:"
  echo
  echo "  ./setup-ntfy.sh $SUGGESTION"
  echo
  echo "Hergebruik je een bestaand topic, dan komen deze meldingen bij je andere"
  echo "meldingen op dezelfde plek binnen. Bestaand topic kwijt? ./setup-ntfy.sh --find"
  exit 1
fi

if [[ ${#TOPIC} -lt 8 ]]; then
  echo "LET OP: '$TOPIC' is kort en dus makkelijk te raden. Iedereen die het topic"
  echo "kent, kan je meldingen meelezen — VIN, prijs en link."
  echo
fi

touch "$ENV_FILE"
if grep -q '^NTFY_TOPIC=' "$ENV_FILE" 2>/dev/null; then
  OLD=$(grep -m1 '^NTFY_TOPIC=' "$ENV_FILE" | cut -d= -f2-)
  echo "Bestaande NTFY_TOPIC ($OLD) wordt vervangen door $TOPIC."
  grep -v '^NTFY_TOPIC=' "$ENV_FILE" > "$ENV_FILE.tmp"
  mv "$ENV_FILE.tmp" "$ENV_FILE"
fi
echo "NTFY_TOPIC=$TOPIC" >> "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo "NTFY_TOPIC=$TOPIC opgeslagen in .env"
echo
echo "Abonneer je in de ntfy-app op het topic '$TOPIC' en druk op Enter voor een test."
read -r _ 2>/dev/null || true

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
uv run python -m tesla_mcp.watch --test-notify
