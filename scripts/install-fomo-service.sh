#!/usr/bin/env bash
# Keep the FOMO copy bot running on your Mac: starts at login, restarts if it crashes, and stops the Mac
# from idle-sleeping while it runs. Usage: scripts/install-fomo-service.sh [paper|live] [fomo|grok]
#   grok = the Grok bot (./grok.sh, config/grok.yaml, dashboard on port 8005); it runs next to the FOMO bot, not instead.
#   Remove with: launchctl unload ~/Library/LaunchAgents/com.phantom.fomo.plist
#   (Grok bot:  launchctl unload ~/Library/LaunchAgents/com.phantom.grok.plist)
set -euo pipefail
MODE="${1:-paper}"
BOT="${2:-fomo}"
if { [ "$MODE" != "paper" ] && [ "$MODE" != "live" ]; } || { [ "$BOT" != "fomo" ] && [ "$BOT" != "grok" ]; }; then
  echo "Usage: scripts/install-fomo-service.sh [paper|live] [fomo|grok]"
  exit 1
fi
DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$BOT" = "grok" ]; then PORT=8005; NAME="Grok bot"; else PORT=8001; NAME="FOMO copy bot"; fi
PLIST="$HOME/Library/LaunchAgents/com.phantom.$BOT.plist"
mkdir -p "$DIR/logs" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.phantom.$BOT</string>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>/bin/bash</string><string>$DIR/$BOT.sh</string><string>$MODE</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$DIR/logs/$BOT-service.out</string>
  <key>StandardErrorPath</key><string>$DIR/logs/$BOT-service.err</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed. The $NAME runs in $MODE mode now and after every login."
echo "Dashboard: http://localhost:$PORT    Logs: $DIR/logs/"
echo "Keep the Mac plugged in. Closing the lid still sleeps it unless an external display is connected."
