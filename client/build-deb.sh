#!/bin/sh
# Builds the Azan SIP Client's Debian package: client/dist/azan-sip-client_<version>_all.deb
# (and a copy named azan-sip-client.deb, which each Box release carries). Needs dpkg-deb only:
#     sh client/build-deb.sh
set -eu
cd "$(dirname "$0")"
version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' azan_client.py)
root=$(mktemp -d)
chmod 755 "$root"
trap 'rm -rf "$root"' EXIT

install -d "$root/DEBIAN" "$root/usr/bin" "$root/usr/lib/azan-sip-client/web" "$root/lib/systemd/system" \
    "$root/etc/pam.d" "$root/etc/default" "$root/usr/share/doc/azan-sip-client"
sed "s/@VERSION@/$version/" debian/control > "$root/DEBIAN/control"
install -m 755 debian/postinst debian/prerm debian/postrm "$root/DEBIAN/"
printf '/etc/default/azan-sip-client\n/etc/pam.d/azan-client\n' > "$root/DEBIAN/conffiles"

install -m 644 azan_client.py "$root/usr/lib/azan-sip-client/"
install -m 644 web/* "$root/usr/lib/azan-sip-client/web/"
install -m 755 kiosk.sh "$root/usr/lib/azan-sip-client/kiosk"
cat > "$root/usr/bin/azan-sip-client" <<'SH'
#!/bin/sh
exec python3 /usr/lib/azan-sip-client/azan_client.py "$@"
SH
chmod 755 "$root/usr/bin/azan-sip-client"
install -m 644 azan-sip-client.service azan-sip-client-screen.service azan-sip-client-update.service \
    azan-sip-client-update.path "$root/lib/systemd/system/"
install -m 644 debian/azan-client.pam "$root/etc/pam.d/azan-client"
install -m 644 debian/default "$root/etc/default/azan-sip-client"
install -m 644 README.md "$root/usr/share/doc/azan-sip-client/"
printf 'Azan SIP Client, part of the Azan SIP Box\nhttps://github.com/Fudgeamin99/azan-sip-box\n' \
    > "$root/usr/share/doc/azan-sip-client/copyright"

mkdir -p dist
dpkg-deb --root-owner-group -Zxz --build "$root" "dist/azan-sip-client_${version}_all.deb" >/dev/null
cp "dist/azan-sip-client_${version}_all.deb" dist/azan-sip-client.deb
echo "Built client/dist/azan-sip-client_${version}_all.deb"
