#!/bin/sh
# CI check of "Update now": with the client installed, offers it a newer version (this tree
# numbered 9.9.9) from a stand-in for GitHub's list of releases, and runs the update as
# azan-sip-client-update.service does. Passes when apt has installed 9.9.9. Run as root in a
# throwaway Debian container with the client installed (.github/workflows/client.yml does).
set -eu
tmp=$(mktemp -d)
cp -r client "$tmp/"
sed -i 's/^__version__ = .*/__version__ = "9.9.9"/' "$tmp/client/azan_client.py"
sh "$tmp/client/build-deb.sh" >/dev/null
mkdir -p "$tmp/release"
cp "$tmp/client/dist/azan-sip-client_9.9.9_all.deb" "$tmp/release/"
sha=$(sha256sum "$tmp/release/azan-sip-client_9.9.9_all.deb" | cut -d' ' -f1)
# The client's release, and a newer Box release (which doesn't count)
printf '[{"tag_name": "box-v99.0.0", "assets": []}, {"tag_name": "client-v9.9.9", "assets": [{"name": "azan-sip-client_9.9.9_all.deb", "browser_download_url": "http://127.0.0.1:8798/azan-sip-client_9.9.9_all.deb", "digest": "sha256:%s"}]}]\n' \
    "$sha" > "$tmp/release/releases"
python3 -m http.server 8798 --bind 127.0.0.1 --directory "$tmp/release" >/dev/null 2>&1 &
server=$!
trap 'kill $server' EXIT
sleep 2
export AZAN_CLIENT_RELEASES=http://127.0.0.1:8798/releases
azan-sip-client update --check
azan-sip-client update
cat /var/lib/azan-sip-client-update/state.json
test "$(dpkg-query -W -f='${Version}' azan-sip-client)" = 9.9.9
test "$(azan-sip-client --version)" = 9.9.9
azan-sip-client update   # nothing newer now
echo "ok: Update now installed the newer client"
