# Azan SIP Box changelog

Versions of the Azan SIP Box, the plug-and-play appliance. To publish a version, add a new
`## x.y.z - title` section at the top and merge it to main: a release tagged `box-vx.y.z` is
created automatically. The desktop app's own history is in `CHANGELOG.md`.



## 0.7.2 - The Wyse client: volume buttons, Update button and two screens

- **Volume buttons on the Wyse's screen:** move the mouse over the prayer times and the volume buttons appear at the top, next to Screen settings. A keyboard's **+** and **−** (and its volume keys) work too. Each press plays a short note at the new volume.
- **Update button:** Screen settings on the Wyse now says when a new Azan SIP Client is out, and **Update now** downloads it from GitHub and installs it by itself. The screen restarts with the new version. Over SSH, `azan-sip-client update` as root does the same.
- **Two screens or more:** with two TVs or monitors plugged into the Wyse (or a laptop and a TV), every screen shows the prayer times, each laid out for its own size, and the azan plays once. Plug a screen in or out at any time: the screen restarts its browser by itself, so the prayer times fill the screens that are left (before, unplugging the TV from a laptop left the laptop showing only part of the page), and the azan moves to the TV's speakers or back to the laptop's own.
- To get this on a Wyse or laptop with an older client, download `azan-sip-client.deb` again and run `sudo apt install ./azan-sip-client.deb` (or `su -` first, then the same without `sudo`, where Debian has no sudo). Later versions install from the Update button.
- **Debian:** only the client package changes (Azan SIP Client 0.3.0). The Box's own Debian install is unchanged.
- **Docker:** nothing changes. The image is rebuilt with the new version number only, and the compose file stays the same.

## 0.7.1 - The Wyse client on laptops and 32-bit PCs

- **Laptops as TV screens:** the Azan SIP Client now works on an old laptop as well as a Wyse. With no TV plugged in, it shows the prayer times on the laptop's own screen and plays the azan through its built-in speakers, switching them on where a laptop starts with them off. With a TV on HDMI, the prayer times fill the TV instead of being stretched across both screens, and the azan plays through the TV. The laptop keeps going with its lid closed.
- **64-bit and 32-bit:** the same `azan-sip-client.deb` installs on 64-bit and 32-bit computers. For a 32-bit one, install Debian 12 (i386); Debian 13 no longer has a 32-bit installer. It's now checked on 32-bit Debian before each release.
- To update a Wyse or laptop already running the client, download `azan-sip-client.deb` again and run `sudo apt install ./azan-sip-client.deb`.
- **Debian:** only the client package changes. The Box's own Debian install is unchanged.
- **Docker:** nothing changes. The image and compose file stay the same.

## 0.7.0 - Fail2ban and the firewall in Docker

- **The Security page works fully in Docker.** Fail2ban bans addresses that keep guessing the control panel or SIP password, and the whitelist and blacklist go on the computer's firewall, so a banned or blocked address can't reach it on any port. Before, Docker only had the blacklist, enforced by the Box itself on the control panel and SIP.
- **NAS boxes without nftables**, such as many Synology models, use iptables instead, found by itself.
- **Docker, once:** add the new `security` part of `compose.yaml` (it's in the README's Docker section) to your compose file and run `docker compose up -d`, or redeploy the project in your NAS's Docker app. The Update button can't add a new container, but after this it updates the security container along with the Box. Until it's added, the Security page says how. SSH sign-ins aren't watched in Docker.
- **Debian:** nothing to run, and nothing changes in how it works. A minute or two after **Update now**, the box installs the new version of its security helper by itself.

## 0.6.1 - The volume slider sets the computer's own volume

- **Volume on the box's own screen:** the slider on the TV app page now sets the volume of the computer the Box runs on, such as a laptop with its screen, like its volume keys would. If that computer has no volume control (a TV on HDMI often doesn't), the box's screen turns the azan down itself, as before.
- **Debian:** nothing to run. A minute or two after **Update now**, the box sets itself up to change the volume.
- **Docker:** nothing changes. A container can't set the NAS's volume, so the slider keeps turning down the box's own screen.

## 0.6.0 - A TV screen on a Dell Wyse

- **Azan SIP Client**, a new program for a Dell Wyse (or any small PC) running Debian: it turns the Wyse into a TV screen for a Box elsewhere on the network. It shows the Box's prayer-times screen full screen on the TV plugged into it from every boot, and plays the azan through the TV's speakers at the same moment as the phones, with no tap needed.
- **Its own volume:** the Wyse's volume is set on the Wyse, so each room can be louder or quieter.
- **Finds the Box** by its address or by searching the network, and **reconnects by itself** after the Box restarts or updates, or the network drops, showing *Connecting to the Azan SIP Box* meanwhile.
- **Only shows:** the Wyse can't change the Box's settings. It needs *Let TV screens connect* on the TV app page, and shows there under its own name.
- Install it with `sudo apt install ./azan-sip-client.deb`, attached to this release. See "A TV screen on a Dell Wyse" in the README.
- **Debian:** the Box itself doesn't change. The client is a separate package for the Wyse, not part of the Box's install.
- **Docker:** nothing changes. The image and compose file are the same, and a Wyse connects to a Box in Docker the same way, on port 8765.

## 0.5.1 - Updates set up their system parts by themselves

- **On Debian, no more sudo commands after updating.** When a new version needs system changes, such as installing fail2ban, the box makes them by itself a minute or two after **Update now**. A small service that runs as root downloads that release from GitHub and runs its setup. The control panel still never gets root, and nothing that runs as root comes from files the panel can change. If it can't finish (say there's no internet), it tries again every hour, and the Security page says what went wrong.
- **One last time on a box installed earlier:** update to 0.5.1, then in the azan-sip-box folder run `git pull` and `sudo sh packaging/box/system-setup.sh`. That also sets up fail2ban and the firewall for the Security page.
- **Docker:** nothing changes. The image already carries everything, and the Update button replaces the whole container.

## 0.5.0 - Security: fail2ban, whitelist and blacklist

- **Security** in the menu, on the Box. Changes apply straight away, without a restart.
- **Fail2ban** bans an address after too many wrong passwords: SSH sign-ins to the box, the control panel's password and SIP sign-ins (Register to Azan SIP). A banned address is blocked on every port. Choose the wrong tries allowed (5 by default), over how long (10 minutes) and how long the ban lasts (1 hour, longer for repeat offenders). **Banned right now** lists who is banned, with **Unban** and **Always block**.
- **Whitelist:** addresses and ranges that are never banned or blocked. It starts with the box's own network, so you can't lock yourself out from inside the masjid.
- **Blacklist:** addresses and ranges that are always blocked, with an optional note. The Box refuses to block the address you're using, its own address or anything already covered by the whitelist.
- On Debian, `install.sh` sets this up. On a box installed earlier, update it, then run `git pull` and `sudo sh packaging/box/security.sh` once. In Docker the blacklist works for the control panel and SIP, without fail2ban.
- Wrong control panel passwords are now written to the log, with the address they came from.

## 0.4.4 - Volume for the box's own screen

- **The volume slider** on the TV app page now changes only the screen plugged into the box (a laptop or a Wyse on a TV), not iPads and other TV screens, which keep their own volume.
- **Screens update themselves:** TV screens load the new version after the Box updates, instead of carrying on with the old page until they're restarted.

## 0.4.3 - The box's own screen starts on more computers

- **Volume for TV screens:** a new slider on the TV app page sets how loud the azan plays on TV screens, including the box's own screen. Screens follow within a second, even in the middle of an azan.
- **Prayer times on the box's screen:** `screen.sh` now works on computers where the screen stayed black with a blinking cursor, such as a Lenovo IdeaPad.
- **Sound on laptops:** `screen.sh` installs the sound firmware small Intel laptops need and switches their built-in speaker on. Restart the computer once afterwards if there's no sound.
- Run `git pull` and `sudo sh packaging/box/screen.sh` again on a box with a screen to get these fixes.

## 0.4.2 - Makkah live doesn't use the box's data by default

- **Screens fetch Makkah live themselves** is now on unless you switch it off, so a box on a cloud server doesn't pay for the stream. Boxes that had it switched off keep their choice.
- **Data warning:** when Makkah live goes through the box, the Display page says it uses about 1 GB an hour for each screen and can add to the charges on a hosted server.

## 0.4.1 - Phones signed in over the internet hear the azan

- **Phones behind a router:** a phone or phone system signed in to the Box over the internet (say a Yealink at home and the Box on a cloud server) now hears the azan. The Box sends the sound to the router's public address and follows the port the phone's own audio comes from, instead of the phone's private address, which can't be reached.
- **No more "Held":** azan calls no longer show as held on Yealink and other phones.

## 0.4.0 - Ramadan mode, SIP trunk and the TV app
Brings the Box up to the desktop app's 1.11.2, with everything from its versions 1.8.0 to 1.11.2.
- **Ramadan** page in the menu. Ramadan mode switches itself on for the month and off on Eid, from the expected date (8 February 2027 for Ramadan 1448); move it a day either way after the moonsighting, or choose 29 or 30 days. On the phones: a **Suhoor ends** alert before Fajr, a different **Iftar azan** for Maghrib and a **Taraweeh** chime after Isha, with an optional wake-up, a reminder before Iftar, a Qiyam chime in the last ten nights and **takbeer** before the Eid prayer. Isha in Ramadan can be as calculated, a fixed time or a set time after Maghrib.
- **Ramadan on the prayer-times screen:** a Ramadan theme with lanterns, "Day 13 of 30" with the Hijri date, a big **Iftar** countdown in the day and a **Suhoor** countdown overnight, a Sehri card, the **dua for breaking the fast** at Maghrib, a "Last ten nights" badge and an **Eid Mubarak** screen with the Eid prayer times. **Preview on the display** shows them at any time of year, and **Print the Ramadan timetable** makes an A4 page for the month. The **Ramadan night** theme is on the Display page too.
- **Register to Azan SIP (SIP trunk)**, a fifth way to reach the phones. The Box becomes a small SIP server: a phone system such as the Grandstream UCM adds it as a **register SIP trunk** and the Box calls the number you choose over it, such as the paging group, so no extension is needed. Desk phones can add it as an extra line instead. Each account has a generated password and a number of sign-ins allowed, and the Phones page shows who is signed in. Calls into the Box are always refused, and repeated wrong passwords are blocked. It listens on UDP port 5060 of the box; choose another port if something else there uses it.
- **TV app** page in the menu: the prayer-times screen on a TV that also plays the azan through the TV's speakers, at the same moment as the phones. Use an old iPad (open the link or scan the QR code, then Add to Home Screen), the **Azan SIP TV** Android app on a phone, tablet, Android TV or Fire TV, or a smart TV's browser. The page lists the connected screens and has **Test chime** and **Test the azan** for the screens only. The azan also plays when TV screens are the only speakers.
- **Let TV screens connect**, on the TV app page, lets TV screens in without the password. It starts off; switching it off disconnects every TV screen straight away. The box's own screen and the prayer-times screen at `/display` work as before.
- **The Android app** is the desktop app's AzanTV.apk. The Docker image carries a copy, so screens can download it from the box at `/apk` even without internet; otherwise `/apk` sends them to GitHub. It's attached to each Box release too.
- **Settings** is now the last item in the menu.

## 0.3.3 - Makkah live without using the Box's data
- With **Screens fetch the stream themselves** on, a new switch on the Display page, **If a screen can't fetch it, go through the box**, can be turned off. The Box then never relays Makkah live, so it uses no data for it, and a screen that can't fetch the stream itself shows a message and keeps trying.

## 0.3.2 - Makkah live straight from the stream
- New on the Display page: **Screens fetch the stream themselves**. Each screen then downloads Makkah live over its own internet connection instead of through the Box, so a Box on a cloud server uses no data for it. If the stream doesn't allow it, screens go through the Box as before.
- **Check the stream** now also says whether screens can fetch the stream themselves.

## 0.3.1 - Updates from the control panel on Debian
- On a box installed with the Debian installer, such as a Wyse thin client, **Settings → Updates** now installs each new Box release (the same versions as the Docker image) instead of every change made on GitHub. **Check for updates** shows the new version with a **What's new** link, and **Update now** installs it and restarts. The previous version is kept, so you can go back.
- Settings shows the Box's own version number, such as 0.3.1, instead of the desktop app's.

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
