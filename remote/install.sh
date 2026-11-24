#!/bin/bash
# Builds ws-scrcpy (pinned, patched), adds the TW Phone home-screen launcher, and runs it under launchd.
set -euo pipefail
cd "$(dirname "$0")"
HERE="$PWD"
WS="$HERE/ws-scrcpy"
COMMIT=cd6cea6
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LABEL=local.phoneremote

[ -d "$WS" ] || git clone -q https://github.com/NetrisTV/ws-scrcpy.git "$WS"
git -C "$WS" fetch -q origin && git -C "$WS" checkout -q -f "$COMMIT"
# Listen on loopback only: Tailscale Serve is the sole way in.
sed -i '' "s/server.listen(port, () => {/server.listen(port, '127.0.0.1', () => {/" "$WS/src/server/services/HttpServer.ts"
# express 4.22.3 is the first release without the vulnerable qs (GHSA-4mjr-xmp4-gh2g, GHSA-6pq8-jhm2-2vcw).
(cd "$WS" && npm pkg set dependencies.express=4.22.3)
cat > "$WS/build.config.override.json" <<'JSON'
{
  "SCRCPY_LISTENS_ON_ALL_INTERFACES": false,
  "INCLUDE_DEV_TOOLS": false,
  "INCLUDE_ADB_SHELL": false,
  "INCLUDE_FILE_LISTING": false,
  "USE_QVH_SERVER": false
}
JSON
# --ignore-scripts skips node-pty's native build; it only serves the disabled adb shell.
(cd "$WS" && npm install -q --ignore-scripts >/dev/null && npm run dist >/dev/null \
  && cd dist && npm install -q --omit=dev --ignore-scripts >/dev/null && npm audit --omit=dev)

"$CHROME" --headless=new --hide-scrollbars --window-size=512,512 --screenshot="$HERE/icon-512.png" "file://$HERE/icon.html" 2>/dev/null
sips -z 180 180 icon-512.png --out icon-180.png >/dev/null
cp app.html manifest.webmanifest icon-180.png icon-512.png "$WS/dist/public/"

PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key><array><string>/opt/homebrew/bin/node</string><string>index.js</string></array>
    <key>WorkingDirectory</key><string>$WS/dist</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>WS_SCRCPY_CONFIG</key><string>$HERE/ws-scrcpy.yaml</string>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardErrorPath</key><string>$HERE/remote.log</string>
    <key>StandardOutPath</key><string>$HERE/remote.log</string>
</dict>
</plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
while launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; do sleep 1; done
launchctl bootstrap "gui/$(id -u)" "$PLIST"
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg --https=8443 http://127.0.0.1:8000 >/dev/null
