#!/bin/sh
# The prayer-times screen on the TV plugged into the box. azan-sip-screen.service runs this inside
# cage as the azan-screen user (installed as /usr/local/bin/azan-sip-screen by screen.sh).
# It is the TV app opened by the box itself, so it plays the azan through the TV's speakers and
# doesn't need "Let TV screens connect". It can't open the settings.
url="http://127.0.0.1:8765/tv?name=Screen+on+the+box"

# At boot, wait for the box (up to 90 s) so the screen doesn't open on an error page.
i=0
until python3 -c 'import urllib.request as u; u.urlopen("http://127.0.0.1:8765/api/ping", timeout=2)' 2>/dev/null; do
    i=$((i + 1))
    [ "$i" -ge 90 ] && break
    sleep 1
done

# After a power cut Chromium offers to "restore pages": tell it the last exit was a normal one.
prefs="$HOME/chromium/Default/Preferences"
[ -f "$prefs" ] && sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' "$prefs"

# no-user-gesture-required: the azan plays without anyone tapping the screen first.
exec chromium --kiosk --ozone-platform=wayland --user-data-dir="$HOME/chromium" \
    --autoplay-policy=no-user-gesture-required --no-first-run --no-default-browser-check \
    --noerrdialogs --hide-crash-restore-bubble --disable-features=Translate --disable-pinch \
    --overscroll-history-navigation=0 --password-store=basic "$url"
