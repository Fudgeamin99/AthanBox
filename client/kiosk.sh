#!/bin/sh
# The Azan SIP Client's screen: the box's prayer times full screen on the TV plugged into this
# computer, with the azan through the TV's speakers. azan-sip-client-screen.service runs this inside
# cage as the azan-client user (installed as /usr/lib/azan-sip-client/kiosk).
# Everything goes through the client on 127.0.0.1:8771, which shows "Connecting to the Azan SIP Box"
# when the box can't be reached and the settings page when no box is chosen yet.
[ -f /etc/default/azan-sip-client ] && . /etc/default/azan-sip-client

# The azan plays through the TV: the HDMI/DisplayPort sound output with a TV plugged in, otherwise
# the computer's own sound output. Picked at every start, so it follows the TV.
# AZAN_SCREEN_SOUND=card,device in /etc/default/azan-sip-client picks one by hand (see aplay -l).
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
    cat > "$HOME/.asoundrc" <<ASOUND
# Written by the Azan SIP Client at each start: the sound goes to the TV (card $card, device $dev).
pcm.!default { type plug; slave.pcm "hw:CARD=$card,DEV=$dev" }
ctl.!default { type hw; card $card }
ASOUND
    echo "Sound: the TV on card $card, device $dev"
else
    rm -f "$HOME/.asoundrc"
    echo "Sound: no TV found on HDMI or DisplayPort, so the computer's own sound output plays the azan"
fi
# The volume is set on the screen (Screen settings): the sound card itself stays at full volume.
for card in $(LC_ALL=C aplay -l 2>/dev/null | sed -n 's/^card \([0-9]*\): .*/\1/p' | sort -u); do
    for ctl in Master PCM Speaker Headphone; do amixer -c "$card" -q sset "$ctl" 100% unmute >/dev/null 2>&1 || true; done
done

# Wait for the client (up to 30 s) so the screen doesn't open on an error page.
i=0
until python3 -c 'import urllib.request as u; u.urlopen("http://127.0.0.1:8771/client/api/status", timeout=2)' 2>/dev/null; do
    i=$((i + 1))
    [ "$i" -ge 30 ] && break
    sleep 1
done

# After a power cut Chromium offers to "restore pages": tell it the last exit was a normal one.
prefs="$HOME/chromium/Default/Preferences"
[ -f "$prefs" ] && sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' "$prefs"

# no-user-gesture-required: the azan plays without anyone tapping the screen first.
exec chromium --kiosk --ozone-platform=wayland --user-data-dir="$HOME/chromium" \
    --autoplay-policy=no-user-gesture-required --no-first-run --no-default-browser-check \
    --noerrdialogs --hide-crash-restore-bubble --disable-features=Translate --disable-pinch \
    --overscroll-history-navigation=0 --password-store=basic "http://127.0.0.1:8771/"
