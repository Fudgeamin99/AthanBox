# AthanBox Client (for a Dell Wyse or a laptop)

A TV screen for AthanBox, on a Dell Wyse thin client, a laptop or any small computer running
Debian, 64-bit or 32-bit (Debian 12's i386 for a 32-bit computer).
The Wyse connects to AthanBox over the network, shows its prayer-times screen full screen on the TV,
and plays the azan through the TV's speakers at the same moment as the phones. Its volume is its
own. Install instructions for users are in the main [README](../README.md#a-tv-screen-on-a-dell-wyse-or-a-laptop).

## How it works

```
TV ── HDMI ── Wyse: Chromium (kiosk, in cage) ──> azan-sip-client on 127.0.0.1:8771 ──LAN──> AthanBox (/tv)
Phone or computer ──LAN──> azan-sip-client on port 8772 (Screen settings only)
```

- **azan-sip-client.service** runs `azan_client.py serve`. It passes the kiosk's requests on to the
  AthanBox's TV app (`/tv`, `/api/tv`, the recordings and the rest), reading only: anything that would
  change a setting is refused. It sets the azan's volume in `/api/tv` to this screen's own, and adds
  `web/screen.js` to the page (the *Screen settings* button, and *Connecting to AthanBox*
  while AthanBox can't be reached, back to the prayer times by itself when it answers).
- **From a phone or computer:** `serve` also listens on port 8772 (`AZAN_CLIENT_REMOTE_PORT`, 0 for
  none) on every network address, for Screen settings and its API only (never AthanBox's pages). A
  browser that isn't signed in gets `web/signin.html`: it asks for a code, which `web/code.js` shows
  on the TV (on the prayer times, the Connecting page and Screen settings) for 5 minutes. The right
  code sets an `HttpOnly`, `SameSite=Strict` cookie good for 90 days; only its SHA-256 is kept, in
  `/var/lib/azan-sip-client/sessions.json`. Five wrong codes end a code and ten in an hour stop new
  ones for the hour. Posts must be JSON and, when the browser says where from, from this address.
  The status a phone gets never includes the code. Only the TV can switch it off (`"remote": false`
  in `client.json`) or sign everyone out. *Play a test chime on the TV* is played by `code.js` on
  the TV.
- **azan-sip-client-screen.service** runs `kiosk.sh` in cage on the first console, like AthanBox's
  own screen (`packaging/box/screen.sh`). It picks the TV's sound output at every start, or the
  computer's own speakers when no TV is plugged in (switching a laptop's built-in speaker on).
  It runs `azan-sip-client watch-screens` alongside the browser, which keeps the list of screens
  cage shows (from `wayland-info`) in `/var/lib/azan-sip-client/screens.json`. When a screen is
  plugged in or out it stops the browser, and `kiosk.sh` picks the sound output again and starts
  a new one, which cage sizes to the screens there are now (cage before 0.2, in Debian 12, leaves
  the old window the size of both screens when one is unplugged).
- **Two screens or more** (two TVs, or a laptop and a TV): cage spreads the browser over all of
  them, and the client opens `/client/wall`, which puts a copy of the prayer times where each screen
  is. The first copy plays the azan; the others get `mirror=1` and the client answers their
  `/api/tv` with volume 0 (and Makkah live is muted in them), so it's heard once. A laptop's own
  screen is left out while its lid is closed. Unplugging a screen goes back to one by itself.
- **Volume buttons** on the prayer times (with the mouse, or **+**, **−** and a keyboard's volume
  keys) change this screen's volume in steps of 10, with a short note at the new volume. The
  volume is the azan's and Makkah live's: `screen.js` sets the live video's own volume to match.
  When the volume changes anywhere else (Screen settings, a phone, `azan-sip-client volume`),
  `screen.js` shows it at the top of the screen for a few seconds.
- **Makkah live's sound** is `"live_sound"` in `client.json`, switched in Screen settings. The TV
  app keeps its own choice (its *Sound on/off* button on the video) in the browser, as
  `azanLiveSound`, and in its `LV` object while it runs: `screen.js` sets both to match, and when
  that button is pressed it sets `"live_sound"` in turn. Until it is first set, `screen.js` sets
  it from the TV app's own choice. The TV app still keeps Makkah live quiet while the azan plays.
- **Updates:** *Update now* in Screen settings writes `/var/lib/azan-sip-client/update-now`;
  `azan-sip-client-update.path` sees it and starts `azan-sip-client-update.service`, which runs
  `azan_client.py update` as root. That downloads `azan-sip-client_<version>_all.deb` from the
  newest of the client's own releases on GitHub (tagged `client-v<version>`), checks GitHub's SHA-256 for it and that it is this package at that
  version, and installs it with apt, which restarts the client and the screen. It takes nothing but
  the trigger from the client's own (unprivileged) files. Progress is in
  `/var/lib/azan-sip-client-update/state.json`.
- **Restart and Shut down** in Screen settings (on the TV or from a phone) write
  `/var/lib/azan-sip-client/restart-now` or `shutdown-now`; `azan-sip-client-shutdown.path` sees
  it and starts `azan-sip-client-shutdown.service`, which runs `azan_client.py power` as root. That
  runs `systemctl reboot` or `poweroff` for the newest file written in the last two minutes (a
  regular file, not a link), and reads nothing in it.
- **Night sleep** (`"night"` in `client.json`) takes AthanBox's prayer times from its `/api/state`
  every ten minutes. *TV screen*: from a while after Isha to a while before Fajr, `/client/api/status`
  says `night.asleep`, and `screen.js` covers the prayer times in black. The screens stay on
  (cage doesn't let them be switched off, and a TV that goes to standby may not wake for the azan
  it plays). Anything AthanBox plays, or plays within a minute and a half, keeps it awake, and a key or the mouse wakes
  it for 10 minutes. *Whole computer*: as the night begins (once a night) the client writes
  `sleep-now` with the time to come back on, and the same root service checks that time (no more than 18
  hours ahead), sets the computer's clock alarm (`/sys/class/rtc/rtc0/wakealarm`, in local time when
  `/etc/adjtime` says so), notes it in `/var/lib/azan-sip-client-power/state.json`, and switches it
  off. A computer with no clock alarm, or one that failed, only lets its screen sleep. *Try it* in
  Screen settings does the same for 3 minutes.
- **Sound for each prayer** (`"prayer_sounds"`): in `/api/tv` the client sets the volume to 0 for a
  prayer set to Quiet (its azan and its reminder), and swaps the sound for `/client/chime.wav` (the
  test chime's four notes) for a prayer set to Chime. Other sounds pass through as AthanBox sent them.
- **Picture** (`"picture"`): *rotate* is done by `watch-screens` (asking `/client/api/display`) with `wlr-randr --transform` on
  every screen but a laptop's own (cage 0.2, Debian 13; `display.json` says how it went, for Screen
  settings), *margin* makes `/` open `/client/wall`, which keeps each copy that far off the edges on
  black, and *text* multiplies the `look.scale` AthanBox's `/api/state` sends.
- **Wi-Fi:** Screen settings writes its request (scan, connect or forget, and the network's name and
  password) to `/var/lib/azan-sip-client/wifi-now`, mode 0600; `azan-sip-client-wifi.path` starts
  `azan-sip-client-wifi.service`, which runs `azan_client.py wifi` as root, deletes the file, and
  writes the answer to `/var/lib/azan-sip-client-power/wifi.json`. With NetworkManager running, it
  writes `/etc/NetworkManager/system-connections/athanbox-wifi.nmconnection`; otherwise
  `/etc/network/interfaces.d/azan-sip-client-wifi` and
  `/etc/wpa_supplicant/azan-sip-client-wifi.conf` (ifupdown and wpa_supplicant). Only the
  key derived from the password is kept (WPA's PBKDF2). If the network can't be joined, the old
  files are put back. Forget removes only these files.
- **This screen** (`/client/api/health`): the addresses (`ip addr`), temperature
  (`/sys/class/thermal`), storage, memory, uptime and the last sound AthanBox sent. `"show_ip"`
  (on unless switched off) shows the first address in the corner of the prayer times.
- **Laptops** keep running with the lid closed (`/etc/systemd/logind.conf.d/azan-sip-client.conf`,
  removed again when the package is removed).
- **32-bit:** the package is `Architecture: all` (Python and shell only), so the same .deb installs on
  64-bit and 32-bit Debian. CI installs it on Debian 12 and 13 (amd64) and Debian 12 (i386).
- **Screen settings** (`/client/`, or press **S** on a keyboard) chooses AthanBox (by address or by
  searching the network), the screen's name on AthanBox's TV app page, the volume, each prayer's
  sound, Night sleep, the picture, Wi-Fi, Makkah live's sound, updates, and This screen. With
  several screens it opens on the first one.
- AthanBox sees the Wyse like any other TV screen: *Let TV screens connect* must be on, and the
  screen is listed on the TV app page under its name.
- Settings are kept in `/var/lib/azan-sip-client/client.json`.

It works with any AthanBox that has the TV app: AthanBox, or the desktop app 1.10.0 or newer. The
volume of its own needs AthanBox 0.4.3 or newer.

## Commands

```sh
azan-sip-client status                  # which AthanBox, connected or not, the volume
azan-sip-client search                  # find AthanBoxes on the network
azan-sip-client connect 192.168.1.20    # show this AthanBox (--name "Main hall" for its name)
azan-sip-client volume 60               # the azan's volume on this screen, 0-100
azan-sip-client update --check          # is there a newer client?
azan-sip-client update                  # install it (as root)
```

`AZAN_CHROMIUM_FLAGS` in `/etc/default/azan-sip-client` adds flags to the screen's browser, such
as `--force-device-scale-factor=1.5` to make everything bigger.

## Building and testing

```sh
sh client/build-deb.sh                  # client/dist/azan-sip-client_<version>_all.deb
python3 -m unittest discover -s client  # the tests
sh client/test-two-screens.sh           # as root in a throwaway Debian container: two screens in cage
```

The version is `__version__` in `azan_client.py`. The client has its own releases, apart from the
AthanBox's: set the new version there, add a `## x.y.z - title` section at the top of
[`CHANGELOG.md`](CHANGELOG.md), point the install link in the main README at `client-vx.y.z`, and
merge it to main. `.github/workflows/client-release.yml` then publishes release `client-vx.y.z`
with the package attached as `azan-sip-client.deb` and `azan-sip-client_x.y.z_all.deb`. It isn't
marked as the repository's latest release, which stays AthanBox's, so AthanBox and its Docker image
don't change. (AthanBox releases up to 0.7.2 carried the client, and client 0.3.0 looks for updates
there: while AthanBox's latest release still has a client package, the workflow swaps it for the new
one, so 0.3.0 screens can update from their button.)
