# Azan SIP Client (for a Dell Wyse or a laptop)

A TV screen for the Azan SIP Box, on a Dell Wyse thin client, a laptop or any small computer running
Debian, 64-bit or 32-bit (Debian 12's i386 for a 32-bit computer).
The Wyse connects to the box over the network, shows its prayer-times screen full screen on the TV,
and plays the azan through the TV's speakers at the same moment as the phones. Its volume is its
own. Install instructions for users are in the main [README](../README.md#a-tv-screen-on-a-dell-wyse-or-a-laptop).

## How it works

```
TV ── HDMI ── Wyse: Chromium (kiosk, in cage) ──> azan-sip-client on 127.0.0.1:8771 ──LAN──> Azan SIP Box (/tv)
```

- **azan-sip-client.service** runs `azan_client.py serve`. It passes the kiosk's requests on to the
  box's TV app (`/tv`, `/api/tv`, the recordings and the rest), reading only: anything that would
  change a setting is refused. It sets the azan's volume in `/api/tv` to this screen's own, and adds
  `web/screen.js` to the page (the *Screen settings* button, and *Connecting to the Azan SIP Box*
  while the box can't be reached, back to the prayer times by itself when it answers).
- **azan-sip-client-screen.service** runs `kiosk.sh` in cage on the first console, like the box's
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
- **Updates:** *Update now* in Screen settings writes `/var/lib/azan-sip-client/update-now`;
  `azan-sip-client-update.path` sees it and starts `azan-sip-client-update.service`, which runs
  `azan_client.py update` as root. That downloads `azan-sip-client_<version>_all.deb` from the
  newest of the client's own releases on GitHub (tagged `client-v<version>`), checks GitHub's SHA-256 for it and that it is this package at that
  version, and installs it with apt, which restarts the client and the screen. It takes nothing but
  the trigger from the client's own (unprivileged) files. Progress is in
  `/var/lib/azan-sip-client-update/state.json`.
- **Laptops** keep running with the lid closed (`/etc/systemd/logind.conf.d/azan-sip-client.conf`,
  removed again when the package is removed).
- **32-bit:** the package is `Architecture: all` (Python and shell only), so the same .deb installs on
  64-bit and 32-bit Debian. CI installs it on Debian 12 and 13 (amd64) and Debian 12 (i386).
- **Screen settings** (`/client/`, or press **S** on a keyboard) chooses the box (by address or by
  searching the network), the screen's name on the box's TV app page, the volume and updates. With
  several screens it opens on the first one.
- The box sees the Wyse like any other TV screen: *Let TV screens connect* must be on, and the
  screen is listed on the TV app page under its name.
- Settings are kept in `/var/lib/azan-sip-client/client.json`.

It works with any Azan SIP that has the TV app: the Box, or the desktop app 1.10.0 or newer. The
volume of its own needs the Box 0.4.3 or newer.

## Commands

```sh
azan-sip-client status                  # which box, connected or not, the volume
azan-sip-client search                  # find Azan SIP Boxes on the network
azan-sip-client connect 192.168.1.20    # show this box (--name "Main hall" for its name)
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
Box's: set the new version there, add a `## x.y.z - title` section at the top of
[`CHANGELOG.md`](CHANGELOG.md), point the install link in the main README at `client-vx.y.z`, and
merge it to main. `.github/workflows/client-release.yml` then publishes release `client-vx.y.z`
with the package attached as `azan-sip-client.deb` and `azan-sip-client_x.y.z_all.deb`. It isn't
marked as the repository's latest release, which stays the Box's, so the Box and its Docker image
don't change. (Box releases up to 0.7.2 carried the client, and client 0.3.0 looks for updates
there: while the Box's latest release still has a client package, the workflow swaps it for the new
one, so 0.3.0 screens can update from their button.)
