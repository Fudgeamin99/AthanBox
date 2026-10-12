# Azan SIP Box changelog

Versions of the Azan SIP Box, the plug-and-play appliance. To publish a version, add a new
`## x.y.z - title` section at the top and merge it to main: a release tagged `box-vx.y.z` is
created automatically. The desktop app's own history is in `CHANGELOG.md`.

## 0.2.0 - Docker
- The Box software can run in Docker on a Linux computer, such as a home server, NAS or Raspberry Pi. See "Run it with Docker" in the README.
- Each release publishes the image `ghcr.io/fudgeamin99/azan-sip-box`, for 64-bit PCs and ARM boards.
- In Docker, Settings > Updates explains how to pull the new image. Settings, recordings and the password are kept in a volume, or in a folder you choose, such as a NAS share.

## 0.1.0 - Control panel on the network
- In Box mode the control panel can be opened from any computer or phone on the same network, at http://azan.local.
- Changing settings needs the box's password: the factory one on the sticker, or the owner's own. Viewing the prayer times doesn't.
- Factory reset puts the box back to how it left the factory.
- Updates come from the azan-sip-box repository, never from the desktop app's.
