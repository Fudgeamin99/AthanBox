# Changelog

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
