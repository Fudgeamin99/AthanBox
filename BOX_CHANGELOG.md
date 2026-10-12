# Azan SIP Box changelog

Versions of the Azan SIP Box, the plug-and-play appliance. To publish a version, add a new
`## x.y.z - title` section at the top and merge it to main: a release tagged `box-vx.y.z` is
created automatically. The desktop app's own history is in `CHANGELOG.md`.

## 0.3.0 - Setup wizard and prayer-times screen
- A new box opens a six-step setup wizard: password, location, prayer times, azan sound, phone system, then a test chime and Finish.
- The box's screen shows a QR code. Scan it to set up from your phone instead; the screen follows along, and you can switch back at any step.
- Azan recordings on a USB stick plugged into the box are copied over and chosen automatically. From a phone or PC you can add them directly.
- Connect and test signs in to the phone system as the extension and plays a test chime through the paging group before anything is saved, and says what went wrong if it can't.
- A full-screen prayer-times screen at `/display`: the clock, the next azan with a countdown, today's six times in English and Arabic, and whether the phones are connected. Settings opens after the password. It also works on a TV or tablet pointed at a NAS.

## 0.2.0 - Docker
- The Box software can run in Docker on a Linux computer, such as a home server, NAS or Raspberry Pi. See "Run it with Docker" in the README.
- Each release publishes the image `ghcr.io/fudgeamin99/azan-sip-box`, for 64-bit PCs and ARM boards.
- In Docker, Settings > Updates explains how to pull the new image. Settings, recordings and the password are kept in a volume, or in a folder you choose, such as a NAS share.
- Also includes everything in 0.1.0 below, which wasn't released on its own.

## 0.1.0 - Control panel on the network
- In Box mode the control panel can be opened from any computer or phone on the same network, at http://azan.local.
- Changing settings needs the box's password: the factory one on the sticker, or the owner's own. Viewing the prayer times doesn't.
- Factory reset puts the box back to how it left the factory.
- Updates come from the azan-sip-box repository, never from the desktop app's.
