#!/bin/bash
# Zet de wachter als terugkerende taak op je Mac (launchd).
#
#   ./install-watch.sh              elke 3 uur
#   ./install-watch.sh 3600         elk uur
#   ./install-watch.sh --uninstall  weer weghalen
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.teslawebscrape.watch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "FOUT: dit installatiescript is voor macOS (launchd)." >&2
  exit 1
fi

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Wachter verwijderd."
  exit 0
fi

INTERVAL="${1:-10800}"   # standaard 3 uur

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$PROJECT_DIR/watch.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>StartInterval</key>
    <integer>$INTERVAL</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/results/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/results/launchd.log</string>
</dict>
</plist>
PLIST_EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Wachter geïnstalleerd: elke $((INTERVAL / 60)) minuten."
echo "Plist   : $PLIST"
echo "Log     : $PROJECT_DIR/results/watch.log"
echo "Stoppen : ./install-watch.sh --uninstall"

if ! grep -qs '^NTFY_TOPIC=..' "$PROJECT_DIR/.env" 2>/dev/null; then
  SUGGESTION="tesla-nl-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 10)"
  echo
  echo "LET OP: NTFY_TOPIC staat nog niet in .env, dus je krijgt alleen een"
  echo "macOS-melding en geen push op je telefoon. Zet er bijvoorbeeld dit in:"
  echo "  NTFY_TOPIC=$SUGGESTION"
  echo "en abonneer je in de ntfy-app op datzelfde topic."
fi
