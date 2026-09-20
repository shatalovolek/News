#!/bin/zsh
# Installs a launchd job that writes the AI brief once a day with Claude Code (headless, under
# the user's subscription) and uploads it to the website. Default 08:45; pass HOUR [MINUTE].
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.alexdrone.be-news-brief
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(which python3)"
HOUR="${1:-8}"; MINUTE="${2:-45}"
NODE_BIN="$(dirname "$(which node 2>/dev/null || echo /usr/local/bin/node)")"
mkdir -p "$HOME/Library/LaunchAgents" "$DIR/output"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$DIR/daily_brief.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$NODE_BIN:$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$DIR/output/brief.log</string>
  <key>StandardErrorPath</key><string>$DIR/output/brief.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed: $LABEL runs every day at $HOUR:$(printf %02d $MINUTE) (local). Plist: $PLIST  Log: $DIR/output/brief.log"
echo "Remove with:  launchctl bootout gui/$(id -u)/$LABEL && rm '$PLIST'"
