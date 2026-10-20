#!/bin/bash
# One-time setup for macOS. Run from inside the tennis-monitor folder:  ./setup.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
echo "1/4 Creating Python environment..."
python3 -m venv .venv
.venv/bin/pip install --quiet playwright pyyaml
echo "2/4 Installing browser (one-time download)..."
.venv/bin/playwright install chromium
echo "3/4 Log in to the LA Rec & Parks site in the window that opens."
.venv/bin/python monitor.py --login
echo "4/4 Scheduling a daily 8:00 AM run..."
PLIST="$HOME/Library/LaunchAgents/com.user.tennismonitor.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.user.tennismonitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/.venv/bin/python</string>
    <string>$DIR/monitor.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/tmp/tennismonitor.log</string>
  <key>StandardErrorPath</key><string>/tmp/tennismonitor.err</string>
</dict>
</plist>
P
launchctl bootout "gui/$(id -u)/com.user.tennismonitor" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo
echo "Done. Edit config.yaml to choose locations/times, then try:  .venv/bin/python monitor.py && open report.html"
