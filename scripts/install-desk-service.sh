#!/usr/bin/env bash
# Keep the Memecoin Desk running on your Mac: starts at login, restarts if it crashes, and stops the Mac
# from idle-sleeping while it runs. Usage: scripts/install-desk-service.sh [paper|live]
#   Remove with: launchctl unload ~/Library/LaunchAgents/com.phantom.desk.plist
set -euo pipefail
MODE="${1:-paper}"
if [ "$MODE" != "paper" ] && [ "$MODE" != "live" ]; then
  echo "Usage: scripts/install-desk-service.sh [paper|live]"
  exit 1
fi
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.phantom.desk.plist"
mkdir -p "$DIR/logs" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.phantom.desk</string>
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
  <key>StandardOutPath</key><string>$DIR/logs/desk-service.out</string>
  <key>StandardErrorPath</key><string>$DIR/logs/desk-service.err</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed. The Memecoin Desk runs in $MODE mode now and after every login."
echo "Dashboard: http://localhost:8002    Logs: $DIR/logs/"
echo "Keep the Mac plugged in. Closing the lid still sleeps it unless an external display is connected."
