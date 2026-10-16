#!/bin/sh
# CI check of Makkah live's sound on AthanBox Client, the way a Wyse plays it: the real package,
# cage, Chromium and a real AthanBox relaying the real Makkah live stream. A container has no sound
# card, so ALSA's default output is a stand-in that keeps time like one (PulseAudio's null sink,
# reached only through ALSA, as Chromium reaches a Wyse's sound card). It records what the screen
# plays: Makkah live must stay quiet while its sound is off, and be heard once it is switched on in
# Screen settings (as from a phone).
# Run as root in a throwaway Debian container; it leaves the recording and the logs in /tmp/live-sound.
set -eu
out=/tmp/live-sound
mkdir -p "$out"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y tzdata grim curl pulseaudio pulseaudio-utils libasound2-plugins python3-websocket
sh client/build-deb.sh
apt-get install -y ./client/dist/azan-sip-client.deb
dpkg-query -W -f '${Package} ${Version}\n' cage chromium | tee "$out/versions.txt"

# The stand-in sound card. Chromium finds no PulseAudio of its own (none at its usual address, and
# none started for it), so it plays through ALSA, whose default output is this one.
mkdir -p /tmp/pa /etc/pulse/client.conf.d && chown azan-client /tmp/pa
echo 'autospawn = no' > /etc/pulse/client.conf.d/no-autospawn.conf
runuser -u azan-client -- env HOME=/tmp/pa XDG_RUNTIME_DIR=/tmp/pa pulseaudio -n --daemonize=yes --exit-idle-time=-1 \
    -L "module-native-protocol-unix socket=/tmp/pa/native auth-anonymous=1" -L "module-null-sink sink_name=tv rate=48000 channels=2"
printf 'pcm.!default { type pulse; server "unix:/tmp/pa/native"; device "tv" }\nctl.!default { type pulse; server "unix:/tmp/pa/native" }\n' > /etc/asound.conf

# An AthanBox on this computer: TV screens allowed, and Makkah live on screens that haven't chosen
mkdir -p /tmp/box
printf '[general]\nconfigured = true\nscreens = true\n\n[location]\nname = "London, United Kingdom"\nlatitude = 51.5074\nlongitude = -0.1278\ntimezone = "Europe/London"\nelevation = 0\n\n[display]\nlive_start = true\n' \
    > /tmp/box/config.toml
python3 -m azan_sip -c /tmp/box/config.toml run --box > "$out/box.log" 2>&1 &

# The client showing it, with Makkah live's sound off
printf '{"box": "http://127.0.0.1:8765", "name": "Main hall", "volume": 70, "live_sound": true}\n' > /var/lib/azan-sip-client/client.json
chown azan-client: /var/lib/azan-sip-client/client.json
runuser -u azan-client -- azan-sip-client serve > "$out/client.log" 2>&1 &

# Everything the screen plays, from the start: the loopback's other end
timeout 120 parec --server=unix:/tmp/pa/native -d tv.monitor --rate=48000 --channels=2 --format=s16le --raw > "$out/heard.raw" 2> "$out/parec.log" &
rec=$!
start=$(date +%s)

# The screen: the kiosk in cage, one 1280x720 screen, Chromium in software without its sandbox
echo 'AZAN_CHROMIUM_FLAGS="--no-sandbox --disable-gpu --disable-dev-shm-usage --enable-logging=stderr --v=0 --remote-debugging-port=9222 --remote-allow-origins=*"' >> /etc/default/azan-sip-client
mkdir -p /tmp/xdg && chown azan-client /tmp/xdg && chmod 700 /tmp/xdg
touch "$out/busy.log" && chmod 666 "$out/busy.log"
runuser -u azan-client -- env LD_PRELOAD=${BUSY_SO:-} BUSY_LOG=$out/busy.log HOME=/var/lib/azan-sip-client XDG_RUNTIME_DIR=/tmp/xdg WLR_BACKENDS=headless \
    WLR_HEADLESS_OUTPUTS=1 WLR_LIBINPUT_NO_DEVICES=1 WLR_RENDERER=pixman \
    cage -- /usr/lib/azan-sip-client/kiosk > "$out/cage.log" 2>&1 &
wl() { runuser -u azan-client -- env XDG_RUNTIME_DIR=/tmp/xdg WAYLAND_DISPLAY=wayland-0 "$@"; }

# What the screen's Makkah live player is doing, every 2 seconds (through Chromium's debugging port)
cat > "$out/watch.py" <<'PY'
import json, sys, time, urllib.request, websocket
t0 = float(sys.argv[1])
JS = '''(() => { const v = document.getElementById("video"), b = document.getElementById("sound");
  return JSON.stringify({on: LV.on, want: LV.want, azanMuted: LV.azanMuted, muted: v.muted, paused: v.paused,
    t: +v.currentTime.toFixed(1), ready: v.readyState, vol: v.volume, msg: document.getElementById("pmsg").hidden ? "" : document.getElementById("pmsg").textContent.trim().slice(0, 60),
    button: b.hidden ? "(hidden)" : b.textContent.trim(), stored: localStorage.getItem("azanLiveSound")}); })()'''
ws = None
while time.time() - t0 < 118:
    try:
        if ws is None:
            pages = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=3))
            page = next(p for p in pages if p.get("type") == "page" and "/tv" in p.get("url", "") or p.get("url", "").endswith(":8771/"))
            ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=5)
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": JS, "returnByValue": True}}))
        r = json.loads(ws.recv())
        print(f"{time.time() - t0:5.0f}s", r.get("result", {}).get("result", {}).get("value") or r, flush=True)
    except Exception as e:
        print(f"{time.time() - t0:5.0f}s", "-", type(e).__name__, e, flush=True)
        ws = None
    time.sleep(2)
PY
python3 "$out/watch.py" "$(date +%s)" > "$out/timeline.txt" 2>&1 &

# Makkah live plays with its sound off, then its sound is switched on in Screen settings
sleep 50
wl grim - > "$out/sound-off.png" || true
curl -sS -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8771/client/api/chime > "$out/settings.json"
on=$(( $(date +%s) - start ))
echo "Test chime at ${on}s"
sleep 45
wl grim - > "$out/sound-on.png" || true
wait "$rec" || true

grep -v 'Basic output test failed' "$out/cage.log" | grep -iE 'CONSOLE|alsa|audio|Sound:' | cut -c1-300 | tail -n 60 || true
echo "The Makkah live player, every 2 seconds:"; cat "$out/timeline.txt"
echo "ALSA opens:"; cat "$out/busy.log"
grep -iE 'live|stream' "$out/box.log" | tail -n 20 || true
python3 - "$out/heard.raw" "$on" <<'PY'
import array, math, sys
on, raw = int(sys.argv[2]), open(sys.argv[1], "rb").read()
second = 48000 * 2 * 2
level = []   # how loud each second is
for i in range(0, len(raw) - second + 1, second):
    a = array.array("h", raw[i:i + second])
    level.append(int(math.sqrt(sum(x * x for x in a) / len(a))))
print("Level each second:", " ".join(f"{i}:{v}" for i, v in enumerate(level)))
off = sum(1 for v in level[20:on - 2] if v > 30)
heard = sum(1 for v in level[on + 10:on + 40] if v > 30)
print(f"Seconds heard while the sound was off: {off} of {len(level[20:on - 2])}")
print(f"Seconds heard after it was switched on: {heard} of {len(level[on + 10:on + 40])}")
print(f"Seconds heard before the chime (sound on from the start): {off}")
PY
