# Azan SIP Client (for a Dell Wyse)

A TV screen for the Azan SIP Box, on a Dell Wyse thin client or any small computer running Debian.
The Wyse connects to the box over the network, shows its prayer-times screen full screen on the TV,
and plays the azan through the TV's speakers at the same moment as the phones. Its volume is its
own. Install instructions for users are in the main [README](../README.md#a-tv-screen-on-a-dell-wyse).

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
  own screen (`packaging/box/screen.sh`). It picks the TV's sound output at every start.
- **Screen settings** (`/client/`, or press **S** on a keyboard) chooses the box (by address or by
  searching the network), the screen's name on the box's TV app page, and the volume.
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
```

## Building and testing

```sh
sh client/build-deb.sh                  # client/dist/azan-sip-client_<version>_all.deb
python3 -m unittest discover -s client  # the tests
```

The version is `__version__` in `azan_client.py`. Each Box release attaches the package as
`azan-sip-client.deb`.
