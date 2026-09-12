#!/bin/zsh
# Installs a launchd job that runs the Bloom Energy news agent every day at 08:00.
# Re-run to change the time:  ./install_schedule.sh 7 30   -> 07:30
set -e
HOUR=${1:-8}; MIN=${2:-0}
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
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MIN</integer>
  </dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$DIR/output/agent.log</string>
  <key>StandardErrorPath</key><string>$DIR/output/agent.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed: $LABEL runs daily at $(printf '%02d:%02d' $HOUR $MIN) (local). Plist: $PLIST"
echo "Remove with:  launchctl bootout gui/$(id -u)/$LABEL && rm '$PLIST'"
