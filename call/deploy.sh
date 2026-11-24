#!/bin/bash
# Copies the calling bridge to the Pi 5, restarts it, and serves it on the tailnet at :8443.
set -euo pipefail
cd "$(dirname "$0")"
# TWCALL_PI: user@host of the Pi. TWCALL_SSH_KEY: optional private key for it.
PI="${TWCALL_PI:?set TWCALL_PI=user@host}"
SSH=(ssh -o BatchMode=yes ${TWCALL_SSH_KEY:+-i "$TWCALL_SSH_KEY"})
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$CHROME" --headless=new --hide-scrollbars --window-size=512,512 --screenshot="$PWD/static/icon-512.png" "file://$PWD/icon.html" 2>/dev/null
sips -z 180 180 static/icon-512.png --out static/icon-180.png >/dev/null
rsync -a --delete -e "${SSH[*]}" twcall.py twcall.service static "$PI:/opt/twcall/"
"${SSH[@]}" "$PI" 'sudo cp /opt/twcall/twcall.service /etc/systemd/system/ && sudo systemctl daemon-reload \
  && sudo systemctl enable --now twcall && sudo systemctl restart twcall \
  && sudo tailscale serve --bg --https=8443 http://127.0.0.1:8091 >/dev/null'
