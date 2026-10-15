#!/bin/sh
# CI check of the Azan SIP Client with two screens, the way a Wyse with two TVs runs it: the real
# package, cage with two (headless) screens, Chromium, and a real Box from this repository. It
# passes when the client finds both screens and each one shows the prayer times. Run as root in a
# throwaway Debian container (.github/workflows/client.yml does); it leaves a screenshot and the
# logs in /tmp/two-screens.
set -eu
out=/tmp/two-screens
mkdir -p "$out"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y tzdata grim
sh client/build-deb.sh
apt-get install -y ./client/dist/azan-sip-client.deb
dpkg-query -W -f '${Package} ${Version}\n' cage chromium wayland-utils | tee "$out/versions.txt"

# A Box on this computer, with TV screens allowed
mkdir -p /tmp/box
printf '[general]\nconfigured = true\nscreens = true\n\n[location]\nname = "London, United Kingdom"\nlatitude = 51.5074\nlongitude = -0.1278\ntimezone = "Europe/London"\nelevation = 0\n' \
    > /tmp/box/config.toml
python3 -m azan_sip -c /tmp/box/config.toml run --box > "$out/box.log" 2>&1 &

# The client, set to show that Box (written straight into its settings: "connect" refuses this
# computer's own address)
printf '{"box": "http://127.0.0.1:8765", "name": "Main hall", "volume": 70}\n' > /var/lib/azan-sip-client/client.json
chown azan-client: /var/lib/azan-sip-client/client.json
runuser -u azan-client -- azan-sip-client serve > "$out/client.log" 2>&1 &

# The screen: the kiosk in cage, with two 1280x720 screens side by side. A container has no
# graphics card or user namespaces, so Chromium draws in software without its sandbox.
echo 'AZAN_CHROMIUM_FLAGS="--no-sandbox --disable-gpu --disable-dev-shm-usage"' >> /etc/default/azan-sip-client
mkdir -p /tmp/xdg && chown azan-client /tmp/xdg && chmod 700 /tmp/xdg
runuser -u azan-client -- env HOME=/var/lib/azan-sip-client XDG_RUNTIME_DIR=/tmp/xdg WLR_BACKENDS=headless \
    WLR_HEADLESS_OUTPUTS=2 WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman \
    cage -- /usr/lib/azan-sip-client/kiosk > "$out/cage.log" 2>&1 &

wl() { runuser -u azan-client -- env XDG_RUNTIME_DIR=/tmp/xdg WAYLAND_DISPLAY=wayland-0 "$@"; }
ok=
for i in $(seq 1 90); do
    sleep 2
    if grep -q '"x": 1280' /var/lib/azan-sip-client/screens.json 2>/dev/null; then ok=1; break; fi
done
wl wayland-info > "$out/wayland-info.txt" 2>&1 || true
cp /var/lib/azan-sip-client/screens.json "$out/" 2>/dev/null || true
[ -n "$ok" ] || { echo "FAIL: the client didn't find two screens"; cat "$out/cage.log" "$out/client.log"; exit 1; }
echo "ok: the client found two screens"
cat "$out/screens.json"

# Checks a picture of the two screens: each half must show the prayer times
cat > "$out/check.py" <<'PY'
import re, sys
data = open(sys.argv[1], "rb").read()
# A binary PPM: "P6\n<width> <height>\n255\n" then RGB bytes
m = re.match(rb"P6\s+(\d+)\s+(\d+)\s+255\s", data)
w, h, px = int(m.group(1)), int(m.group(2)), data[m.end():]
def share(x0, x1, test):   # the share of pixels in a column range that pass test
    n = hit = 0
    for y in range(0, h, 4):
        row = y * w * 3
        for x in range(x0, x1, 4):
            r, g, b = px[row + x * 3: row + x * 3 + 3]
            n += 1
            hit += test(r, g, b)
    return hit / n
white = lambda r, g, b: r > 200 and g > 200 and b > 200            # the prayer times' text
cream = lambda r, g, b: r > 225 and 200 < g < 240 and 140 < b < 205  # the next prayer's card
assert w == 2560 and h == 720, f"expected two 1280x720 screens side by side, got {w}x{h}"
for name, x0, x1 in (("left", 0, w // 2), ("right", w // 2, w)):
    t, c = share(x0, x1, white), share(x0, x1, cream)
    print(f"{name} screen: text {t:.4f}, next prayer's card {c:.4f}")
    assert t > 0.005 and c > 0.005, f"the {name} screen doesn't show the prayer times"
print("ok: both screens show the prayer times")
PY
shot() { wl grim -t ppm - > "$out/$1.ppm"; wl grim - > "$out/$1.png" || true; python3 "$out/check.py" "$out/$1.ppm"; }
chromium_pids() { grep -lx chromium /proc/[0-9]*/comm 2>/dev/null | cut -d/ -f3 | sort; }

# Let the prayer times load on both, then take a picture of the two screens
sleep 25
shot screens

# A screen plugged in or out: the client stops the browser (so the new window fits the screens
# there are now) and the kiosk starts it again straight away
before=$(chromium_pids)
runuser -u azan-client -- python3 -I -c 'import sys; sys.path.insert(0, "/usr/lib/azan-sip-client"); import azan_client; azan_client.restart_browser()'
ok=
for i in $(seq 1 60); do
    sleep 1
    now=$(chromium_pids)
    # shellcheck disable=SC2086
    if [ -n "$now" ] && [ -z "$(printf '%s\n' $before $now | sort | uniq -d)" ]; then ok=1; break; fi
done
[ -n "$ok" ] || { echo "FAIL: the browser didn't start again"; cat "$out/cage.log"; exit 1; }
echo "ok: the browser started again"
sleep 25
shot screens-after-restart
grep -i 'screens:' "$out/cage.log" || true
