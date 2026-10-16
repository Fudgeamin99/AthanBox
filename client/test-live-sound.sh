#!/bin/sh
# CI check of Makkah live's sound on AthanBox Client, the way a Wyse plays it: the real package,
# cage, Chromium and a real AthanBox relaying the real Makkah live stream, with a sound card that
# takes one stream at a time like a TV's HDMI output (snd-aloop with one substream: the workflow
# loads it and passes /dev/snd in). It records what the screen plays: Makkah live must stay quiet
# while its sound is off, and be heard once it is switched on in Screen settings (as from a phone).
# Run as root in a throwaway Debian container; it leaves the recording and the logs in /tmp/live-sound.
set -eu
out=/tmp/live-sound
mkdir -p "$out"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y tzdata grim curl
sh client/build-deb.sh
apt-get install -y ./client/dist/azan-sip-client.deb
dpkg-query -W -f '${Package} ${Version}\n' cage chromium | tee "$out/versions.txt"
aplay -l | tee "$out/aplay.txt"
chmod a+rw /dev/snd/*   # (on a Wyse, the screen's login session gets the sound card)

# An AthanBox on this computer: TV screens allowed, and Makkah live on screens that haven't chosen
mkdir -p /tmp/box
printf '[general]\nconfigured = true\nscreens = true\n\n[location]\nname = "London, United Kingdom"\nlatitude = 51.5074\nlongitude = -0.1278\ntimezone = "Europe/London"\nelevation = 0\n\n[display]\nlive_start = true\n' \
    > /tmp/box/config.toml
python3 -m azan_sip -c /tmp/box/config.toml run --box > "$out/box.log" 2>&1 &

# The client showing it, with Makkah live's sound off
printf '{"box": "http://127.0.0.1:8765", "name": "Main hall", "volume": 70, "live_sound": false}\n' > /var/lib/azan-sip-client/client.json
chown azan-client: /var/lib/azan-sip-client/client.json
runuser -u azan-client -- azan-sip-client serve > "$out/client.log" 2>&1 &

# Everything the screen plays, from the start: the loopback's other end
arecord -D hw:Loopback,1,0 -f S16_LE -r 48000 -c 2 -d 120 "$out/heard.wav" > "$out/arecord.log" 2>&1 &
rec=$!
start=$(date +%s)

# The screen: the kiosk in cage, one 1280x720 screen, Chromium in software without its sandbox
echo 'AZAN_CHROMIUM_FLAGS="--no-sandbox --disable-gpu --disable-dev-shm-usage --enable-logging=stderr --v=0"' >> /etc/default/azan-sip-client
mkdir -p /tmp/xdg && chown azan-client /tmp/xdg && chmod 700 /tmp/xdg
runuser -u azan-client -- env HOME=/var/lib/azan-sip-client XDG_RUNTIME_DIR=/tmp/xdg WLR_BACKENDS=headless \
    WLR_HEADLESS_OUTPUTS=1 WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman \
    cage -- /usr/lib/azan-sip-client/kiosk > "$out/cage.log" 2>&1 &
wl() { runuser -u azan-client -- env XDG_RUNTIME_DIR=/tmp/xdg WAYLAND_DISPLAY=wayland-0 "$@"; }

# Makkah live plays with its sound off, then its sound is switched on in Screen settings
sleep 50
wl grim "$out/sound-off.png" || true
curl -sS -H 'Content-Type: application/json' -d '{"live_sound": true}' http://127.0.0.1:8771/client/api/settings > "$out/settings.json"
on=$(( $(date +%s) - start ))
echo "Makkah live's sound switched on at ${on}s"
sleep 45
wl grim "$out/sound-on.png" || true
wait "$rec" || true

grep -v 'Basic output test failed' "$out/cage.log" | grep -iE 'CONSOLE|alsa|audio|Sound:' | cut -c1-300 | tail -n 60 || true
python3 - "$out/heard.wav" "$on" <<'PY'
import array, math, sys, wave
w = wave.open(sys.argv[1])
on, rate = int(sys.argv[2]), w.getframerate()
level = []   # how loud each second is
while True:
    f = w.readframes(rate)
    if not f:
        break
    a = array.array("h", f)
    level.append(int(math.sqrt(sum(x * x for x in a) / len(a))))
print("Level each second:", " ".join(f"{i}:{v}" for i, v in enumerate(level)))
off = sum(1 for v in level[20:on - 2] if v > 30)
heard = sum(1 for v in level[on + 10:on + 40] if v > 30)
print(f"Seconds heard while the sound was off: {off} of {len(level[20:on - 2])}")
print(f"Seconds heard after it was switched on: {heard} of {len(level[on + 10:on + 40])}")
assert off <= 2, "FAIL: Makkah live was heard with its sound off"
assert heard >= 20, "FAIL: Makkah live's sound wasn't heard after it was switched on"
print("ok: Makkah live is quiet with its sound off and heard with it on")
PY
