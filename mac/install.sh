#!/bin/bash
# Runs bridge.py under launchd. Needs mac/.env with TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
set -euo pipefail
cd "$(dirname "$0")"
HERE="$PWD"
LABEL=local.smsbridge
[ -f .env ] || { echo "missing $HERE/.env" >&2; exit 1; }
swiftc -parse-as-library -O translate.swift -o translate
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$HERE/bridge.py</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardErrorPath</key><string>$HERE/bridge.log</string>
    <key>StandardOutPath</key><string>$HERE/bridge.log</string>
</dict>
</plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
while launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; do sleep 1; done
launchctl bootstrap "gui/$(id -u)" "$PLIST"
