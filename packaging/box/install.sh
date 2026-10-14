#!/bin/sh
# Install Azan SIP on a Debian machine as an "Azan SIP Box" (for testing the box before the
# disk image exists). Run as root from the repository folder:
#     sudo sh packaging/box/install.sh
# The control panel is then at http://azan.local (and http://<ip>:8765) on the local network.
set -eu
cd "$(dirname "$0")/../.."
[ "$(id -u)" = 0 ] || { echo "Please run as root (sudo)"; exit 1; }

apt-get update
apt-get install -y --no-install-recommends python3 ffmpeg avahi-daemon libnss-mdns

# azan.local: the box calls itself "azan" on the network (mDNS). If another box already uses the
# name, avahi picks azan-2.local etc. The panel only listens on IPv4, so only publish IPv4.
hostnamectl set-hostname azan
sed -i 's/^#\?use-ipv6=.*/use-ipv6=no/; s/^#\?publish-aaaa-on-ipv4=.*/publish-aaaa-on-ipv4=no/' /etc/avahi/avahi-daemon.conf
systemctl restart avahi-daemon

id azan >/dev/null 2>&1 || useradd --system --home /opt/azan-sip --shell /usr/sbin/nologin azan
# The volume slider on the TV app page sets the computer's own volume
if getent group audio >/dev/null; then usermod -a -G audio azan; fi
mkdir -p /opt/azan-sip
cp -r azan_sip audio BOX_CHANGELOG.md CHANGELOG.md README.md /opt/azan-sip/
# Record which commit this is. Settings > Updates installs each new Box release from GitHub.
# (safe.directory: the clone usually belongs to the user who ran sudo, not root.)
commit=$(git -c safe.directory="$(pwd)" rev-parse HEAD 2>/dev/null || true)
if [ -n "$commit" ]; then
    version=$(sed -n 's/^## \([0-9]*\.[0-9]*\.[0-9]*\).*/\1/p' BOX_CHANGELOG.md | head -n 1)
    printf '{"commit": "%s", "version": "%s", "installed_at": "%s", "source": "https://github.com/Fudgeamin99/azan-sip-box/commit/%s"}\n' \
        "$commit" "$version" "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" "$commit" > /opt/azan-sip/.installed.json
fi
chown -R azan:azan /opt/azan-sip

if [ ! -f /opt/azan-sip/auth/factory.json ]; then
    echo "Choose the factory password for this box (the one that goes on the sticker):"
    python3 -m azan_sip -c /opt/azan-sip/config.toml box-password --factory
    chown -R azan:azan /opt/azan-sip/auth
fi

# A laptop keeps running with its lid closed (from the next restart).
if ls /proc/acpi/button/lid/*/state >/dev/null 2>&1; then
    mkdir -p /etc/systemd/logind.conf.d
    printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\n' \
        > /etc/systemd/logind.conf.d/azan-sip-box.conf
fi

cp packaging/box/azan-sip-box.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now azan-sip-box
# What needs root: fail2ban, whitelist and blacklist (the Security page), and setting up each
# new version's system parts by itself after an update
sh packaging/box/system-setup.sh
echo "Done. Open http://azan.local on a computer or phone on the same network."
echo "To show the prayer times on a TV plugged into this box: sudo sh packaging/box/screen.sh"
