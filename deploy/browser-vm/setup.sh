#!/usr/bin/env bash
# One-time setup of a Scout browser host (Ubuntu 22.04+/Debian 12+, amd64 or arm64).
#
#   sudo bash setup.sh
#
# Installs Brave, a virtual screen (Xvfb + fluxbox), a live view (x11vnc +
# noVNC) and the scout-browser helper, creates the `scout` user, and turns on
# SSH key login for `scout` and a tailnet-only HTTPS address for the live view.
# Safe to run again: every step checks before it changes anything.
set -euo pipefail

SCOUT_USER=scout
DISPLAY_NUM=99
SCREEN=1920x1080x24
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0" >&2; exit 1; }
command -v apt-get >/dev/null || { echo "This script needs a Debian or Ubuntu system." >&2; exit 1; }

say() { printf '\n== %s\n' "$*"; }

say "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q curl gnupg ca-certificates python3 \
  xvfb fluxbox x11vnc novnc websockify tor \
  fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji fonts-liberation

say "Brave"
if ! command -v brave-browser >/dev/null; then
  curl -fsSLo /usr/share/keyrings/brave-browser-archive-keyring.gpg \
    https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/brave-browser-archive-keyring.gpg] https://brave-browser-apt-release.s3.brave.com/ stable main" \
    > /etc/apt/sources.list.d/brave-browser-release.list
  apt-get update -q
  apt-get install -y -q brave-browser
fi
brave-browser --version

say "User $SCOUT_USER"
id "$SCOUT_USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$SCOUT_USER"
sudo -u "$SCOUT_USER" mkdir -p "/home/$SCOUT_USER/profiles" "/home/$SCOUT_USER/run" "/home/$SCOUT_USER/logs"
# Public keys allowed to log in as scout: put the Mac's key in authorized_keys next to this script.
if [[ -f "$HERE/authorized_keys" ]]; then
  install -d -m 0700 -o "$SCOUT_USER" -g "$SCOUT_USER" "/home/$SCOUT_USER/.ssh"
  touch "/home/$SCOUT_USER/.ssh/authorized_keys"
  while read -r key; do
    [[ -z "$key" || "$key" == \#* ]] && continue
    grep -qxF "$key" "/home/$SCOUT_USER/.ssh/authorized_keys" || echo "$key" >> "/home/$SCOUT_USER/.ssh/authorized_keys"
  done < "$HERE/authorized_keys"
  chown "$SCOUT_USER:$SCOUT_USER" "/home/$SCOUT_USER/.ssh/authorized_keys"
  chmod 0600 "/home/$SCOUT_USER/.ssh/authorized_keys"
fi

say "scout-browser helper"
install -m 0755 "$HERE/scout-browser" /usr/local/bin/scout-browser

say "Virtual screen and live view (systemd)"
cat > /etc/systemd/system/scout-xvfb.service <<EOF
[Unit]
Description=Scout virtual screen :$DISPLAY_NUM
After=network.target

[Service]
User=$SCOUT_USER
ExecStart=/usr/bin/Xvfb :$DISPLAY_NUM -screen 0 $SCREEN -nolisten tcp
Restart=always

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/scout-wm.service <<EOF
[Unit]
Description=Scout window manager
After=scout-xvfb.service
Requires=scout-xvfb.service

[Service]
User=$SCOUT_USER
Environment=DISPLAY=:$DISPLAY_NUM
ExecStart=/usr/bin/fluxbox
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# VNC and noVNC listen on loopback only; tailscale serve publishes noVNC to the tailnet.
cat > /etc/systemd/system/scout-vnc.service <<EOF
[Unit]
Description=Scout VNC server for the live view
After=scout-xvfb.service
Requires=scout-xvfb.service

[Service]
User=$SCOUT_USER
ExecStart=/usr/bin/x11vnc -display :$DISPLAY_NUM -localhost -rfbport 5900 -forever -shared -nopw -quiet
Restart=always

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/scout-novnc.service <<EOF
[Unit]
Description=Scout noVNC web live view
After=scout-vnc.service
Requires=scout-vnc.service

[Service]
User=$SCOUT_USER
ExecStart=/usr/bin/websockify --web /usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now scout-xvfb scout-wm scout-vnc scout-novnc
# Tor for discreet profiles (proxy socks5://127.0.0.1:9050); listens on loopback only.
systemctl enable --now tor

say "Tailscale"
if command -v tailscale >/dev/null; then
  # Scout connects with its own key over OpenSSH. Tailscale SSH stays off: its
  # "check" mode would stop every unattended connection for a browser approval.
  tailscale set --ssh=false
  tailscale serve --bg --https=443 http://127.0.0.1:6080 || \
    echo "!! tailscale serve failed: turn on HTTPS certificates for the tailnet in the admin console, then rerun."
  tailscale serve status || true
else
  echo "!! tailscale is not installed: install it and join the tailnet, then rerun this script."
fi

say "Smoke test"
sudo -u "$SCOUT_USER" scout-browser start smoke --port 9399 --browser brave --headless
sudo -u "$SCOUT_USER" scout-browser stop smoke
rm -rf "/home/$SCOUT_USER/profiles/smoke"

say "Done"
echo "Live view: https://$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || hostname)/vnc.html"
echo "From the Mac: ssh $SCOUT_USER@$(hostname) scout-browser status"
