#!/usr/bin/env bash
# Keep the Memecoin Desk running on your Mac: starts at login, restarts if it crashes, and stops the Mac
# from idle-sleeping while it runs. Usage: scripts/install-desk-service.sh [paper|live|turbo]
#   turbo = fake money, no coin checks, about a coin a minute (config/turbo.yaml); it runs next to paper, not instead of it.
#   Remove with: launchctl unload ~/Library/LaunchAgents/com.phantom.desk.plist
#   (turbo:      launchctl unload ~/Library/LaunchAgents/com.phantom.desk.turbo.plist)
set -euo pipefail
MODE="${1:-paper}"
if [ "$MODE" != "paper" ] && [ "$MODE" != "live" ] && [ "$MODE" != "turbo" ]; then
  echo "Usage: scripts/install-desk-service.sh [paper|live|turbo]"
  exit 1
fi
DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$MODE" = "turbo" ]; then
  LABEL="com.phantom.desk.turbo"; PORT=8003; LOG="desk-turbo-service"
else
  LABEL="com.phantom.desk"; PORT=8002; LOG="desk-service"
fi
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$DIR/logs" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>/bin/bash</string><string>$DIR/desk.sh</string><string>$MODE</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ExitTimeOut</key><integer>150</integer>
  <key>StandardOutPath</key><string>$DIR/logs/$LOG.out</string>
  <key>StandardErrorPath</key><string>$DIR/logs/$LOG.err</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed. The Memecoin Desk runs in $MODE mode now and after every login."
echo "Dashboard: http://localhost:$PORT    Logs: $DIR/logs/"
echo "Keep the Mac plugged in. Closing the lid still sleeps it unless an external display is connected."
