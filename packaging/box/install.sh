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
mkdir -p /opt/azan-sip
cp -r azan_sip audio CHANGELOG.md README.md /opt/azan-sip/
chown -R azan:azan /opt/azan-sip

if [ ! -f /opt/azan-sip/auth/factory.json ]; then
    echo "Choose the factory password for this box (the one that goes on the sticker):"
    python3 -m azan_sip -c /opt/azan-sip/config.toml box-password --factory
    chown -R azan:azan /opt/azan-sip/auth
fi

cp packaging/box/azan-sip-box.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now azan-sip-box
echo "Done. Open http://azan.local on a computer or phone on the same network."
