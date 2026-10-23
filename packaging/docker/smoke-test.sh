#!/bin/sh
# Checks a running AthanBox container: the panel answers, a new box opens first-time setup,
# the prayer-times screen is there, settings need the password, the password works,
# Settings > Updates knows it is running in Docker, and TV screens connect only when allowed.
#   sh packaging/docker/smoke-test.sh http://127.0.0.1:8765 <password>
set -eu
url=${1:-http://127.0.0.1:8765}
password=${2:?usage: smoke-test.sh URL PASSWORD}
jar=$(mktemp)
trap 'rm -f "$jar"' EXIT

for i in $(seq 1 30); do
    curl -fsS "$url/api/ping" >/dev/null 2>&1 && break
    [ "$i" = 30 ] && { echo "FAIL: the panel did not start"; exit 1; }
    sleep 1
done
echo "ok: panel answers - $(curl -fsS "$url/api/ping")"

# Run from the repository folder (as in CI), AthanBox should say it is the newest BOX_CHANGELOG.md version
want=$(sed -n 's/^## \([0-9]*\.[0-9]*\.[0-9]*\).*/\1/p' BOX_CHANGELOG.md 2>/dev/null | head -n 1)
if [ -n "$want" ]; then
    got=$(curl -fsS "$url/api/session" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("version"))')
    [ "$got" = "$want" ] || { echo "FAIL: AthanBox says it is version '$got' (expected $want)"; exit 1; }
    echo "ok: AthanBox says it is version $got"
fi

if curl -fsS "$url/api/session" | grep -q '"configured": false'; then
    loc=$(curl -s -o /dev/null -w '%{redirect_url}' "$url/")
    case "$loc" in */setup) ;; *) echo "FAIL: a new box should open first-time setup (went to '$loc')"; exit 1;; esac
fi
for page in /display /static/box.css /static/figtree.woff2 /static/naskh-arabic.woff2 /static/hls.min.js; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$url$page")
    [ "$code" = 200 ] || { echo "FAIL: $page answered $code"; exit 1; }
done
echo "ok: the setup wizard and the prayer-times screen are there"

code=$(curl -s -o /dev/null -w '%{http_code}' "$url/api/update")
[ "$code" = 401 ] || { echo "FAIL: settings answered $code without the password (expected 401)"; exit 1; }
echo "ok: settings need the password"

curl -fsS -c "$jar" -H 'X-Azan-Panel: 1' -H 'Content-Type: application/json' \
     -d "{\"password\": \"$password\"}" "$url/api/login" >/dev/null \
    || { echo "FAIL: the password was refused"; exit 1; }
echo "ok: signed in"

mode=$(curl -fsS -b "$jar" "$url/api/update" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("mode"))')
[ "$mode" = docker ] || { echo "FAIL: update mode is '$mode' (expected docker)"; exit 1; }
echo "ok: Settings > Updates says to pull a new image"

code=$(curl -s -o /dev/null -w '%{http_code}' "$url/ramadan/timetable")
[ "$code" = 200 ] || { echo "FAIL: the Ramadan timetable answered $code"; exit 1; }
curl -fsS "$url/api/state" | grep -q '"ramadan_screen"' || { echo "FAIL: the prayer-times screen gets no Ramadan data"; exit 1; }
echo "ok: Ramadan timetable and screens"

# From another device (here the container's host) the TV app needs "Let TV screens connect"
code=$(curl -s -o /dev/null -w '%{http_code}' "$url/api/tv?id=ci")
[ "$code" = 403 ] || { echo "FAIL: a TV screen got in ($code) while TV screens are switched off"; exit 1; }
curl -fsS -b "$jar" -H 'X-Azan-Panel: 1' -H 'Content-Type: application/json' -d '{"on": true}' "$url/api/screens" >/dev/null
code=$(curl -s -o /dev/null -w '%{http_code}' "$url/api/tv?id=ci")
[ "$code" = 200 ] || { echo "FAIL: a TV screen couldn't connect ($code) with TV screens switched on"; exit 1; }
code=$(curl -s -o /dev/null -w '%{http_code}' "$url/apk")
case "$code" in 200|302) ;; *) echo "FAIL: /apk answered $code"; exit 1;; esac
curl -fsS -b "$jar" -H 'X-Azan-Panel: 1' -H 'Content-Type: application/json' -d '{"on": false}' "$url/api/screens" >/dev/null
echo "ok: TV screens connect only when allowed (the Android app: $([ "$code" = 200 ] && echo "in the image" || echo "from GitHub"))"

# Remote provisioning: a phone downloads its settings from the link; a wrong link gets nothing
link=$(curl -fsS -b "$jar" -H 'X-Azan-Panel: 1' -H 'Content-Type: application/json' -d '{"enabled": true}' \
            "$url/api/provisioning" | python3 -c 'import json, sys; print(json.load(sys.stdin)["links"][-1]["url"])')
path=/${link#http://*/}
code=$(curl -s -o /dev/null -w '%{http_code}' "$url${path}y000000000077.cfg")
[ "$code" = 200 ] || { echo "FAIL: the provisioning link answered $code"; exit 1; }
curl -fsS -b "$jar" -H 'X-Azan-Panel: 1' -H 'Content-Type: application/json' -d '{"mac": "80:5E:C0:00:00:01", "name": "CI"}' \
     "$url/api/provisioning/add" | grep -q '"added"' || { echo "FAIL: Add a phone didn't add it"; exit 1; }
curl -fsS "$url${path}805ec0000001.cfg" | grep -q '^account.1.auto_answer = 1' || { echo "FAIL: the added phone got no line"; exit 1; }
curl -fsS -b "$jar" -H 'X-Azan-Panel: 1' -H 'Content-Type: application/json' \
     -d '{"mac": "80:5E:C0:00:00:02", "name": "Base", "lines": 2, "cordless": true, "keep": "1"}' \
     "$url/api/provisioning/add" | grep -q '"handset": 2' || { echo "FAIL: Add a phone didn't add a cordless base"; exit 1; }
curl -fsS "$url${path}805ec0000002.cfg" | grep -q '^handset.2.incoming_lines = 1,3' || { echo "FAIL: the cordless base got no handset lines"; exit 1; }
code=$(curl -s -o /dev/null -w '%{http_code}' "$url/provision/not-the-right-key-at-all-0000/yealink/y000000000077.cfg")
[ "$code" = 404 ] || { echo "FAIL: a wrong provisioning link answered $code (expected 404)"; exit 1; }
curl -fsS -b "$jar" -H 'X-Azan-Panel: 1' -H 'Content-Type: application/json' -d '{"enabled": false}' "$url/api/provisioning" >/dev/null
echo "ok: remote provisioning answers its link only"
