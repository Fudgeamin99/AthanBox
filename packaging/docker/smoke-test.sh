#!/bin/sh
# Checks a running Azan SIP Box container: the panel answers, settings need the password,
# the password works, and Settings > Updates knows it is running in Docker.
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
