#!/bin/zsh
# Installs a launchd job that runs the news agent every 2 hours from 08:00 to 22:00,
# quietly, and uploads the StockTwits data it collects to the website (push.json).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.alexdrone.be-news-agent
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(which python3)"
mkdir -p "$HOME/Library/LaunchAgents" "$DIR/output"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$DIR/be_news_agent.py</string>
    <string>--no-open</string><string>--no-notify</string><string>--push</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartCalendarInterval</key><array>
$(for h in 8 10 12 14 16 18 20 22; do echo "    <dict><key>Hour</key><integer>$h</integer><key>Minute</key><integer>5</integer></dict>"; done)
  </array>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$DIR/output/agent.log</string>
  <key>StandardErrorPath</key><string>$DIR/output/agent.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed: $LABEL runs every 2 hours 08:05-22:05 (local), pushing social data. Plist: $PLIST"
echo "Remove with:  launchctl bootout gui/$(id -u)/$LABEL && rm '$PLIST'"
