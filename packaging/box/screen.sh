#!/bin/sh
# Show the prayer-times screen on a TV plugged into the box (HDMI or DisplayPort), full screen
# from every boot, with the azan through the TV's speakers. Run as root from the repository
# folder, after install.sh:
#     sudo sh packaging/box/screen.sh        switch the screen on (installs a small kiosk browser)
#     sudo sh packaging/box/screen.sh off    switch it off again
# Run it again after moving the box to another TV, so the sound follows the TV.
set -eu
cd "$(dirname "$0")/../.."
[ "$(id -u)" = 0 ] || { echo "Please run as root (sudo)"; exit 1; }

if [ "${1:-}" = off ]; then
    systemctl disable --now azan-sip-screen 2>/dev/null || true
    rm -f /etc/systemd/system/azan-sip-screen.service
    systemctl daemon-reload
    systemctl start getty@tty1 2>/dev/null || true
    echo "The screen is off. Run sudo sh packaging/box/screen.sh to switch it on again."
    exit 0
fi

[ -f /etc/systemd/system/azan-sip-box.service ] \
    || { echo "Install the box first: sudo sh packaging/box/install.sh"; exit 1; }
ls /dev/dri/card* >/dev/null 2>&1 \
    || { echo "This computer has no screen output (no graphics found), so there is nothing to show the prayer times on."; exit 1; }

apt-get update
apt-get install -y --no-install-recommends cage xwayland seatd chromium libgl1-mesa-dri \
    fonts-dejavu-core alsa-utils dbus libpam-systemd
# Sound firmware for small Intel computers (such as Atom laptops and tablets), from Debian's
# non-free-firmware section; without it their speakers stay silent.
apt-get install -y --no-install-recommends alsa-ucm-conf firmware-intel-sound 2>/dev/null \
    || echo "Note: firmware-intel-sound isn't available (non-free-firmware isn't in the apt sources). Some laptops need it for sound."
# seatd hands the screen to the kiosk: logind refuses to on some computers (such as an IdeaPad).
systemctl enable --now seatd

id azan-screen >/dev/null 2>&1 \
    || useradd --system --create-home --home-dir /var/lib/azan-screen --shell /usr/sbin/nologin azan-screen
for group in video render input audio _seatd; do
    if getent group "$group" >/dev/null; then usermod -a -G "$group" azan-screen; fi
done

# The screen gets a login session on the first console, like a person signing in there.
cat > /etc/pam.d/azan-screen <<'EOF'
# The prayer-times screen (azan-sip-screen.service): a session on the first console.
auth     required  pam_unix.so nullok
account  required  pam_unix.so
session  required  pam_unix.so
session  required  pam_systemd.so
EOF

# The azan plays through the TV: use the HDMI/DisplayPort sound output with a TV plugged in.
# With none (say a laptop on its own screen) the computer's own speakers play it.
# AZAN_SCREEN_SOUND=card,device picks one by hand (see aplay -l).
pick_sound() {
    for cd in $(LC_ALL=C aplay -l 2>/dev/null | grep -i 'hdmi\|displayport' \
                | sed -n 's/^card [0-9]*: \([^ ]*\) .*, device \([0-9]*\):.*/\1,\2/p'); do
        card=${cd%,*} dev=${cd#*,}
        if LC_ALL=C amixer -c "$card" cget "iface=PCM,name=ELD,device=$dev" 2>/dev/null | grep -q 'values=.*0x\(0[1-9a-f]\|[1-9a-f][0-9a-f]\)' \
           || LC_ALL=C amixer -c "$card" cget "iface=CARD,name='HDMI/DP,pcm=$dev Jack'" 2>/dev/null | grep -q 'values=on'; then
            echo "$cd"
            return
        fi
    done
}
sound=${AZAN_SCREEN_SOUND:-$(pick_sound)}
if [ -n "$sound" ]; then
    card=${sound%,*} dev=${sound#*,}
    cat > /var/lib/azan-screen/.asoundrc <<EOF
# Written by packaging/box/screen.sh: the screen's sound goes to the TV (card $card, device $dev).
pcm.!default { type plug; slave.pcm "hw:CARD=$card,DEV=$dev" }
ctl.!default { type hw; card $card }
EOF
    echo "Sound: the TV on card $card, device $dev"
else
    rm -f /var/lib/azan-screen/.asoundrc
    # Switch the built-in speaker on: some laptops' sound chips start with it off and need their
    # ALSA profile (UCM) to route it, which a desktop would normally do.
    for card in $(LC_ALL=C aplay -l 2>/dev/null | grep -vi 'hdmi\|displayport' | sed -n 's/^card \([0-9]*\): .*/\1/p' | sort -u); do
        alsaucm -c "hw:$card" set _verb HiFi set _enadev Speaker >/dev/null 2>&1 || true
        for ctl in Master Speaker PCM; do amixer -c "$card" -q sset "$ctl" 100% unmute >/dev/null 2>&1 || true; done
    done
    alsactl store 2>/dev/null || true
    echo "No TV found on HDMI or DisplayPort, so the computer's own speakers play the azan."
    echo "For a TV: plug it in, switch it on and run this again."
fi
chown -R azan-screen:azan-screen /var/lib/azan-screen
# The volume slider on the TV app page sets the computer's volume: the Box needs the audio group
if getent group audio >/dev/null && id azan >/dev/null 2>&1; then usermod -a -G audio azan; systemctl restart azan-sip-box; fi

install -m 755 packaging/box/azan-sip-screen.sh /usr/local/bin/azan-sip-screen
cp packaging/box/azan-sip-screen.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable azan-sip-screen
systemctl restart azan-sip-screen
echo "Done. The prayer times are on the TV now, and after every restart."
echo "To hear it, open the control panel, go to TV app and press Test chime. No sound? Restart the computer once."
