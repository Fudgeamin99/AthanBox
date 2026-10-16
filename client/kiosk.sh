#!/bin/sh
# AthanBox Client's screen: AthanBox's prayer times full screen on the TV plugged into this
# computer, with the azan through the TV's speakers. azan-sip-client-screen.service runs this inside
# cage as the azan-client user (installed as /usr/lib/azan-sip-client/kiosk).
# Everything goes through the client on 127.0.0.1:8771, which shows "Connecting to AthanBox"
# when AthanBox can't be reached and the settings page when no box is chosen yet.
[ -f /etc/default/azan-sip-client ] && . /etc/default/azan-sip-client

# The azan plays through the TV: the HDMI/DisplayPort sound output with a TV plugged in, otherwise
# the computer's own sound output. Picked at every start of the browser, so it follows the TV.
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
# The screen's sound, on card $1 device $2 ($3 says which, for the file). Chromium opens one sound
# for the TV app's azan as soon as it starts and another for Makkah live, but a sound card's own
# output takes one at a time: on a Dell Wyse 3040's HDMI, Makkah live was turned away as busy and
# played silently. So the sounds are mixed first (dmix), always at 48000 Hz, which every TV and
# sound chip plays (Chromium sends Makkah live at 22050 Hz, which HDMI has no setting for), and plug
# converts each sound to the format and rate the mix takes. A card that can't mix plays one sound
# at a time, as before.
write_sound() {
    cat > "$HOME/.asoundrc" <<ASOUND
# Written by AthanBox Client at each start: $3 (card $1, device $2).
pcm.!default { type plug; slave.pcm "screen" }
pcm.screen { type dmix; ipc_key 5978293; ipc_key_add_uid true; slave { pcm "hw:CARD=$1,DEV=$2"; rate 48000; channels 2; period_size 1024; buffer_size 4096 } }
ctl.!default { type hw; card $1 }
ASOUND
    if timeout 10 aplay -q -t raw -f S16_LE -r 48000 -c 2 -d 1 /dev/zero 2>/dev/null; then
        return
    fi
    cat > "$HOME/.asoundrc" <<ASOUND
# Written by AthanBox Client at each start: $3 (card $1, device $2), one sound at a time.
pcm.!default { type plug; slave { pcm "hw:CARD=$1,DEV=$2"; rate 48000 } }
ctl.!default { type hw; card $1 }
ASOUND
    echo "Sound: card $1 can't mix sounds, so it plays one at a time"
}
# Before each start of the browser (at boot, and when a screen is plugged in or out): the sound
# output, and the sound card itself at full volume (the volume is set on the screen, in Screen settings).
set_sound() {
    sound=${AZAN_SCREEN_SOUND:-$(pick_sound)}
    if [ -n "$sound" ]; then
        card=${sound%,*} dev=${sound#*,}
        write_sound "$card" "$dev" "the sound goes to the TV"
        echo "Sound: the TV on card $card, device $dev"
    else
        # No TV: the computer's own speakers or sound output (a laptop's speakers, a Wyse's line out).
        # The first output that isn't HDMI, since on some laptops the HDMI card comes first.
        own=$(LC_ALL=C aplay -l 2>/dev/null | grep -vi 'hdmi\|displayport' \
              | sed -n 's/^card [0-9]*: \([^ ]*\) .*, device \([0-9]*\):.*/\1,\2/p' | head -n 1)
        if [ -n "$own" ]; then
            card=${own%,*} dev=${own#*,}
            write_sound "$card" "$dev" "no TV, so the computer's own sound"
            # Some laptops' sound chips start with the built-in speaker off: their ALSA profile switches it on
            alsaucm -c "hw:$card" set _verb HiFi set _enadev Speaker >/dev/null 2>&1 || true
            echo "Sound: no TV found on HDMI or DisplayPort, so the computer's own speakers play the azan (card $card, device $dev)"
        else
            rm -f "$HOME/.asoundrc"
            echo "Sound: no sound output found"
        fi
    fi
    # The volume is set on the screen (Screen settings): the sound card itself stays at full volume.
    for card in $(LC_ALL=C aplay -l 2>/dev/null | sed -n 's/^card \([0-9]*\): .*/\1/p' | sort -u); do
        for ctl in Master PCM Speaker Headphone; do amixer -c "$card" -q sset "$ctl" 100% unmute >/dev/null 2>&1 || true; done
    done
}

# Wait for the client (up to 30 s) so the screen doesn't open on an error page.
i=0
until python3 -c 'import urllib.request as u; u.urlopen("http://127.0.0.1:8771/client/api/status", timeout=2)' 2>/dev/null; do
    i=$((i + 1))
    [ "$i" -ge 30 ] && break
    sleep 1
done

# Every screen plugged in shows the prayer times: cage spreads the browser over all of them, and the
# client puts a copy of the prayer times on each from the list this keeps up to date.
rm -f "$HOME/screens.json"
azan-sip-client watch-screens &

# The browser, started again whenever it stops: watch-screens stops it when a screen is plugged in
# or out, so the sound output is picked again (the azan follows the TV) and the new window fills
# the screens there are now.
while :; do
    set_sound
    # After a power cut Chromium offers to "restore pages": tell it the last exit was a normal one.
    prefs="$HOME/chromium/Default/Preferences"
    [ -f "$prefs" ] && sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' "$prefs"
    # no-user-gesture-required: the azan plays without anyone tapping the screen first.
    # AZAN_CHROMIUM_FLAGS in /etc/default/azan-sip-client adds flags of your own.
    # shellcheck disable=SC2086
    chromium --kiosk --ozone-platform=wayland --user-data-dir="$HOME/chromium" \
        --autoplay-policy=no-user-gesture-required --no-first-run --no-default-browser-check \
        --noerrdialogs --hide-crash-restore-bubble --disable-features=Translate --disable-pinch \
        --overscroll-history-navigation=0 --password-store=basic $AZAN_CHROMIUM_FLAGS "http://127.0.0.1:8771/"
    sleep 1
done
