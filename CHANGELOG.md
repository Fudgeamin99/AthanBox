# Changelog

## 1.11.2 - Settings at the end of the menu
- **Settings** is now the last item in the menu, after Makkah live.

## 1.11.1 - Switching TV screens off
- Switching off **Let TV screens connect** now disconnects every TV screen straight away, even while phone access is on. They also disappear from the TV app page.
- A screen that's still open goes quiet and shows "TV screens are switched off". It stops Makkah live, then reconnects by itself when you switch TV screens back on.
- If a TV screen can't reach Azan SIP at all, it says that Azan SIP may be closed or TV screens switched off, instead of only "isn't running".

## 1.11.0 - Android app
- **Azan SIP TV for Android:** an app (AzanTV.apk) for phones, tablets, Android TV boxes and Fire TV. It shows the prayer-times display full screen and plays the azan, at the same moment as the phones.
- **Sign-in screen:** the first time it opens, it asks for the address of the computer running Azan SIP. **Search the network** finds it, and the app checks the address before saving it. An optional screen name tells screens apart on the TV app page.
- **After that** it opens straight into the display. Unlike a browser, it plays the azan without a tap, keeps the screen on by itself and hides the system bars.
- **The menu:** press **Back** (on a TV remote too) for Makkah live, the azan sound on that screen, Reload and Change server.
- **When Azan SIP can't be reached,** the app says why and tries again every 10 seconds. It also recovers by itself if the page crashes.
- **Getting the app:** AzanSIP.exe includes it. Scan the QR code on the **TV app** page, or open `http://<this PC's address>:8765/apk` on the device. On Android TV or Fire TV, use the free Downloader app. It's also attached to each release. The page has the install steps.
- **Works with:** Android 5.0 or newer, with Android TV and Fire TV launcher icons.

## 1.10.0 - TV app
- A new **TV app** for a big screen TV. It shows the prayer times and plays the azan through the TV's speakers, at the same moment as the phones. It's made for an old iPad (5th or 6th generation) connected to the TV; a smart TV's browser or a small PC works too.
- **On the iPad:** open the link or scan the QR code from the new **TV app** page, then **Add to Home Screen**. It opens full screen as **Azan TV**, with its own icon.
- **Sound:** tap the screen once when the app opens, so the iPad allows sound. A reminder shows until then.
- **The recording:** the screen downloads the next azan beforehand and starts it in time with the phones. It plays your recording at full quality, with the same lead-in, length limit and takbeer repeat as the phones.
- **Joining and stopping:** a screen that connects during an azan joins at the right point. Pressing **Stop** stops it on the screens too.
- **On each screen:** tap to show **Azan sound**, which keeps that screen quiet, and **Makkah live**. Makkah live uses the iPad's own player and goes quiet while the azan plays.
- **New switch, Let TV screens connect:** screens on the local network can show the display and play the azan without the password. They can't open or change the settings. The display on other screens also works with this switch, without turning on phone access.
- **The TV app page:**
  - a QR code;
  - the connected screens and whether each is ready;
  - **Test chime** and **Test the azan** for the screens only;
  - step-by-step iPad setup: Home Screen, Auto-Lock, the HDMI adapter or AirPlay, and Guided Access.
- **No phones needed:** the azan and tests also play when TV screens are the only speakers, for a masjid without VoIP phones.

## 1.9.1 - More sign-ins per SIP trunk
- Each SIP trunk account now has **Sign-ins allowed** (1 to 50). Several phone systems, for example a main and a backup PBX, can sign in with the same account, and Azan SIP calls every one that's signed in.
- When an account is full, a device signing in again from the same address takes its old place. Other sign-ins are refused until one ends. The Phones page shows each account's sign-ins and where they're from.
- Every account is now a **SIP trunk**; the separate "desk phone" account has gone. Leave **Number to call** empty and Azan SIP calls the device itself (a desk phone), with auto-answer as before. Existing desk-phone accounts keep working the same way.

## 1.9.0 - Register to Azan SIP (SIP trunk)
- A fifth way to reach the phones: **Register to Azan SIP**. Azan SIP becomes a small SIP server that phones and phone systems sign in to:
  - **Desk phones** add Azan SIP as an extra line (account). At azan time Azan SIP calls the phone, and it answers on speaker by itself.
  - **A phone system** (Grandstream UCM, FreePBX, 3CX and others) adds Azan SIP as a **register SIP trunk**. At azan time Azan SIP calls the number you choose over the trunk, such as the paging group. No extension is needed for Azan SIP.
- Each phone or phone system gets its own account with a strong generated password. The Phones page shows which are signed in and from where.
- Calls into Azan SIP are always refused, and repeated wrong passwords from an address are blocked for 5 minutes.
- Phones stay signed in across restarts and updates.
- The SIP port is 5060 by default; choose another if a phone system or softphone on the same PC uses it. If Windows asks whether to allow Azan SIP on the network, choose **Allow**.
- Setup steps for desk phones, the UCM, FreePBX and 3CX are in the Phones page's setup guide.

## 1.8.0 - Ramadan mode
- A new **Ramadan** page. Ramadan mode switches itself on for the month and off on Eid. It starts on the expected date (Monday 8 February 2027 for Ramadan 1448); move it a day either way when your masjid announces the moonsighting, choose 29 or 30 days, or switch it on or off by hand.
- **On the phones:** a **Suhoor ends** alert before Fajr (10 minutes by default), a different **Iftar azan** recording for Maghrib, and a **Taraweeh** chime after Isha. Optional: a Suhoor wake-up, a reminder before Iftar and a Qiyam chime in the last ten nights. On Eid, **takbeer** can play on repeat before the Eid prayer.
- **Isha in Ramadan** can be as calculated, a fixed time, or a set time after Maghrib.
- **On the display:**
  - a Ramadan theme with lanterns;
  - "Day 13 of 30" and the Hijri date;
  - a big **Iftar** countdown in the day and a **Suhoor** countdown overnight;
  - a Sehri card;
  - the **dua for breaking the fast** at Maghrib;
  - a "Last ten nights" badge;
  - an **Eid Mubarak** screen with the Eid prayer times.
- **Print the Ramadan timetable:** an A4 page with Sehri, the prayers and Iftar for every day of the month.
- **Preview on the display** shows the Ramadan screens at any time of year.
- There's also a **Ramadan night** theme on the Display page, for use any time.
- The Suhoor alert and Taraweeh chime are on by default. The rest are off until you switch them on.

## 1.7.0 - Themes and layout
- **Display page** in the menu: change how the prayer-times display looks, with a live preview. Press **Save changes** and every screen updates within a few seconds.
  - **Themes:** Masjid green, Night, Navy and gold, Maroon, Sand (light), or your own background and highlight colours.
  - **Layout:** prayer times as cards along the bottom or a list at the side, set separately for Makkah live. Text size from 70% to 160%; text shrinks by itself if it wouldn't fit.
  - **Masjid name** at the top, and a **notice** line along the bottom that scrolls when it's long.
  - **Show or hide:** clock, date, Arabic names, countdown, sunrise, phone status and the background pattern. The buttons can show only when the mouse moves or the screen is tapped.
  - **Makkah live:** picture on the left or right, how wide it can be, and whether new screens start with it. The stream link and **Check the stream** moved here from Settings.
- **Settings → Appearance:** light, dark or automatic for the control panel, and its colour (six colours or your own).

## 1.6.0 - Makkah live
- **Makkah live** on the prayer-times display: the live picture from Masjid al-Haram (Saudi Quran TV, via its IPTV stream) next to the next azan, the countdown and today's times. Press **Makkah live** on the display, or in the menu.
- The sound starts off; press **Sound on** on the picture. It goes quiet by itself while your own azan is playing, then comes back.
- Each screen remembers whether it shows Makkah live. A TV can also open `/display?live=1` directly.
- The stream is relayed through Azan SIP, so it plays in any browser and on TVs and phones on the network (with phone access on). Screens showing it at the same time share one download.
- If the stream drops or freezes it reconnects by itself, and says what's wrong if it can't.
- **Settings → Makkah live:** check the stream, or paste a different IPTV (.m3u8) link if the channel moves or to show another channel.

## 1.5.0 - Setup guide, phone access and prayer-times display
- **Setup guide:** a new install opens a step-by-step guide: password, location, prayer times, azan sound, phone system, then a test chime and the time of the first azan. Run it again any time from **Settings → Run the setup guide again**.
- **Settings password:** protects the settings. Viewing the prayer times never needs it.
- **Phone access:** phones and other computers on the same network can open the control panel. Scan the QR code in Settings or in the setup guide. It's off until you switch it on, and needs the password. If Windows asks whether to allow Azan SIP on the network, choose **Allow**.
- Optionally ask for the password on the PC itself too, for a shared computer.
- **Prayer-times display:** a full-screen screen for a TV or monitor, with the time, the next azan and today's times in English and Arabic. Open it from **Display** in the menu.
- Repeated wrong passwords are slowed down, and signed-in phones stay signed in for 30 days.
- Existing setups carry on as before: no guide, and phone access stays off until you turn it on.

## 1.4.1 - Exit button
- There's a new **Exit** button at the bottom of the menu. It asks before closing, because the azan stops until Azan SIP is started again.
- The confirmation explains that closing the browser tab is enough if you only want to close the page.

## 1.4.0 - Windows program (AzanSIP.exe)
- Azan SIP is now available as a single **AzanSIP.exe**. Download it from the release, put it in its own folder and double-click it. No Python is needed.
- AzanSIP.exe keeps its settings, recordings and log in the folder it's in. Place it in your existing folder to keep your current setup.
- **Update now** downloads the new AzanSIP.exe, checks that it starts, then swaps it in. The previous version is kept for **Restore**.
- **Start with Windows** points at AzanSIP.exe automatically.
- The program has its own crescent-and-star icon.

## 1.3.1 - More reliable updates
- The updater now only needs `api.github.com` and `codeload.github.com`. It no longer uses `raw.githubusercontent.com`, which some networks block.

## 1.3.0 - Testing and calls settings
There is a new **Settings → Testing and calls** section:
- **Azan test length**, from 10 seconds up to the whole recording. It was fixed at 20 seconds.
- **Ring time**, how long a call rings before giving up. It was fixed at 20 seconds.
- **Test chime** length and volume.

## 1.2.2 - Phones page fix
- Fixed the Phones page jumping to the top and closing the setup guide every few seconds.

## 1.2.1 - Clearer update message
- When nothing has been published yet, the app now says so plainly instead of showing "GitHub returned an error (409)".

## 1.2.0 - Stays registered on the phone system
- Azan SIP now registers as an extension on the phone system and stays registered. It shows as **Idle/online**, for example on a Grandstream UCM, instead of Unregistered.
- It renews automatically and answers keep-alive checks. If someone dials it, it returns busy. It signs out cleanly when you quit.
- The Phones page shows the registration status, with the reason if it fails.
- Grandstream UCM steps were added to the phone setup guide.

## 1.1.0 - First release
Plays the azan automatically on VoIP phones, managed from a control panel in the browser.
- **Prayer times:**
  - 14 calculation methods, including **Moonsighting Committee Worldwide** (General, Ahmer or Abyad shafaq)
  - Hanafi or standard Asr
  - UK summer-night rules
  - Your mosque's timetable as a CSV file
  - Per-prayer offsets
- **Playback:** to phones by **multicast paging**, a **phone system page group** (Grandstream UCM, FreePBX, 3CX and others), **direct SIP calls** with auto-answer, or **IP speakers**.
- **Control panel:**
  - Today's times and a countdown
  - Per-prayer on/off switches
  - Azan recordings, with MP3s converted automatically
  - A separate Fajr azan, Jumu'ah and reminders
  - A phone setup guide and a log
- Start with Windows, and built-in updates from this repository.
