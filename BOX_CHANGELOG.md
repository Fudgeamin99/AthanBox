# Azan SIP Box changelog

Versions of the Azan SIP Box, the plug-and-play appliance. To publish a version, add a new
`## x.y.z - title` section at the top and merge it to main: a release tagged `box-vx.y.z` is
created automatically. The desktop app's own history is in `CHANGELOG.md`.

## 0.3.0 - Setup wizard, prayer-times screen and Makkah live
- A new box opens a six-step setup wizard: password, location, prayer times, azan sound, phone system, then a test chime and Finish.
- The box's screen shows a QR code. Scan it to set up from your phone instead; the screen follows along, and you can switch back at any step.
- Azan recordings on a USB stick plugged into the box are copied over and chosen automatically. From a phone or PC you can add them directly.
- Connect and test signs in to the phone system as the extension and plays a test chime through the paging group before anything is saved, and says what went wrong if it can't.
- A full-screen prayer-times screen at `/display`: the clock, the next azan with a countdown, today's six times in English and Arabic, and whether the phones are connected. Settings opens after the password. It also works on a TV or tablet pointed at a NAS.
- After the last azan of the day, the screen shows tomorrow's times with tomorrow's Fajr highlighted. On a PC or TV browser, a Full screen button fills the screen.
- Prefer to set things up yourself? Choose "I'll set it up myself" on the welcome page and the control panel opens instead. The guide is one click away from there.
- Run the setup guide again any time from Settings. It starts from your current settings and saves nothing until you press Finish.
- Phones and PCs stay signed in for 30 days, even when the box restarts or updates. Changing the password signs every other device out.
- In Docker, **Settings → Updates** can update the Box itself: **Check for updates**, then **Update now**. It needs the new `updater` part of the compose file, a small helper that downloads the new image and restarts the Box with it. If the new version won't start, the old one is put back.
- The container now stops in a moment, signing out of the phone system first, instead of being killed after Docker's timeout.
- **Makkah live** on the prayer-times screen: the live picture from Masjid al-Haram (Saudi Quran TV) next to the next azan, the countdown and the day's times. Press **Makkah live** on the screen or in the menu, or open `/display?live=1`. The sound starts off and goes quiet by itself while your own azan plays. The Box relays the stream, so screens watching at the same time share one download, and it reconnects by itself if the stream drops or freezes.
- **Display page** in the menu: change how the prayer-times screen looks, with a live preview. Themes (Masjid green, Night, Navy and gold, Maroon, Sand, or your own colours), cards along the bottom or a list at the side, text size from 70% to 160%, a masjid name at the top, a notice line along the bottom that scrolls when it's long, and which parts to show. The buttons can show only when the screen is tapped or the mouse moves. Makkah live has its own layout, side and size, and a stream link with **Check the stream**. **Save changes** updates every screen within a few seconds.
- **Settings → Appearance:** light, dark or automatic for the control panel, and its colour (six colours or your own).

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
