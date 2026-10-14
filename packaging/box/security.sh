#!/bin/sh
# Azan SIP Box: fail2ban, whitelist and blacklist for the Security page of the control panel.
# install.sh runs this. On a box installed before version 0.5.0, update it first (Settings > Updates),
# then run this once from the repository folder (after git pull):
#     sudo sh packaging/box/security.sh
set -eu
cd "$(dirname "$0")/../.."
[ "$(id -u)" = 0 ] || { echo "Please run as root (sudo)"; exit 1; }
[ -d /opt/azan-sip ] || { echo "Install the Box first: sudo sh packaging/box/install.sh"; exit 1; }

apt-get update
# python3-systemd lets fail2ban read SSH sign-ins from the journal (Debian 12 and 13 have no auth.log)
apt-get install -y --no-install-recommends fail2ban nftables python3-systemd

install -d -m 755 /usr/local/lib/azan-sip
install -m 755 packaging/box/security-helper.py /usr/local/lib/azan-sip/security-helper.py
# The panel writes the owner's choices here; the helper reads them as root
install -d -m 750 -o azan -g azan /opt/azan-sip/security

cp packaging/box/azan-sip-security.service packaging/box/azan-sip-security.path \
   packaging/box/azan-sip-security.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable azan-sip-security.service
systemctl enable --now azan-sip-security.path azan-sip-security.timer
# The control panel writes security/settings.json (with this network on the whitelist) when it
# starts; until then there is nothing to apply.
systemctl start azan-sip-security.service
echo "Done. Open Security in the control panel to see fail2ban, the whitelist and the blacklist."
