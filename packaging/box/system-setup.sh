#!/bin/sh
# Azan SIP Box: the parts of the Box that need root (system packages and services), for this version.
# Run as root from the repository folder. install.sh runs it, and after that the box runs it by
# itself after each update (azan-sip-system.service downloads the new release from GitHub and runs
# its copy of this script). It must be safe to run again and again, without questions.
set -eu
cd "$(dirname "$0")/../.."
[ "$(id -u)" = 0 ] || { echo "Please run as root (sudo)"; exit 1; }
[ -d /opt/azan-sip ] || { echo "Install the Box first: sudo sh packaging/box/install.sh"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

# --- Security page (0.5.0): fail2ban, whitelist and blacklist
apt-get update
# python3-systemd lets fail2ban read SSH sign-ins from the journal (Debian 12 and 13 have no auth.log)
apt-get install -y --no-install-recommends fail2ban nftables python3-systemd
install -d -m 755 /usr/local/lib/azan-sip
install -m 755 packaging/box/security-helper.py /usr/local/lib/azan-sip/security-helper.py
# The panel writes the owner's choices here; the helper reads them as root
install -d -m 750 -o azan -g azan /opt/azan-sip/security
cp packaging/box/azan-sip-security.service packaging/box/azan-sip-security.path \
   packaging/box/azan-sip-security.timer /etc/systemd/system/

# --- Setting up each new version by itself (0.5.1)
install -m 755 packaging/box/system-update.py /usr/local/lib/azan-sip/system-update.py
cp packaging/box/azan-sip-system.service packaging/box/azan-sip-system.path \
   packaging/box/azan-sip-system.timer /etc/systemd/system/

systemctl daemon-reload
systemctl enable azan-sip-security.service azan-sip-system.service
systemctl enable --now azan-sip-security.path azan-sip-security.timer azan-sip-system.path azan-sip-system.timer
# --no-block: when azan-sip-system.service is what is running this script, waiting would hang
systemctl start --no-block azan-sip-security.service

# Record that this version's system parts are set up, so azan-sip-system.service doesn't do it again
version=$(sed -n 's/^## \([0-9]*\.[0-9]*\.[0-9]*\).*/\1/p' BOX_CHANGELOG.md | head -n 1)
install -d -m 755 /var/lib/azan-sip
printf '{"version": "%s", "ok": true, "running": false, "tried": %s, "at": "%s", "message": "box-v%s set up"}\n' \
    "$version" "$(date +%s)" "$(date '+%Y-%m-%d %H:%M:%S')" "$version" > /var/lib/azan-sip/system.json
echo "Done. Version $version's system parts are set up, and later versions set themselves up after each update."
