# Azan SIP Box

The Azan SIP Box plays the azan (call to prayer) automatically at every prayer time on your VoIP desk phones and IP speakers. It runs on its own, around the clock, and you set it up from a control panel in your browser on any computer or phone on the same network.

It runs in **Docker** on a Linux computer that stays on, such as a NAS, a home server or a Raspberry Pi, or [straight on Debian](#run-it-on-debian) on a small PC such as a Dell Wyse thin client. A ready-to-plug-in box (a Dell Wyse 3040 with its own prayer-times screen) is on the way. See [What's next](#whats-next).

**Latest release:** [box-v0.3.2, Docker](https://github.com/Fudgeamin99/azan-sip-box/releases/latest). Tested on a UGREEN NAS paging the phones through a Grandstream UCM.

> Looking for the Windows program (AzanSIP.exe)? That's the desktop app, [Azan SIP](https://github.com/Fudgeamin99/ATHAN-TO-SIP), which the Box is built from.

## Run it with Docker

Save this as `compose.yaml` in a folder of its own, and change `AZAN_PASSWORD`:

```yaml
services:
  azan-sip:
    image: ghcr.io/fudgeamin99/azan-sip-box:latest
    container_name: azan-sip
    network_mode: host        # SIP and multicast paging need the computer's own network: no ports to add
    restart: unless-stopped
    environment:
      AZAN_PASSWORD: "choose-a-password"   # for changing settings, at least 8 characters
      TZ: Europe/London
    volumes:
      - ./data:/data          # settings, recordings, password and log, in a "data" folder next to this file

  # Optional: the Update button in Settings > Updates (see "Updating" below)
  updater:
    image: ghcr.io/fudgeamin99/azan-sip-box:latest
    container_name: azan-sip-updater
    network_mode: host
    restart: unless-stopped
    entrypoint: ["python", "-m", "azan_sip.dockerupdate"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./data:/data:ro
```

Then start it:

- **UGREEN, Synology and other NAS Docker apps:** create a new project (or stack), paste the file above, and deploy it.
- **Command line:** run `docker compose up -d` in that folder.

Open `http://<the computer's address>:8765`, sign in with your password, and the setup wizard starts.

- **Linux only.** Host networking doesn't work in Docker Desktop on Windows or Mac.
- **No ports to add.** The panel is always on port 8765 of the computer it runs on.
- **Nothing to set up for the folder.** Docker creates `data` on first start, and the container takes it over itself.
- **The image** is for 64-bit PCs (amd64) and ARM boards (arm64), and includes ffmpeg for MP3 recordings.

## Run it on Debian

On a small PC that stays on, such as a Dell Wyse thin client, install Debian 12 or 13 without a desktop, then run:

```sh
sudo apt update && sudo apt install -y git
git clone https://github.com/Fudgeamin99/azan-sip-box.git
cd azan-sip-box
sudo sh packaging/box/install.sh
```

It installs Python and ffmpeg, names the box `azan` on the network, asks for the factory password, and starts the Box on every boot. Open `http://azan.local` (or `http://<its address>`) and the setup wizard starts. The Box needs to be on the same network as your phone system.

## First-time setup

A new box opens the **setup wizard** in the browser. It takes about five minutes:

1. **Password:** keep `AZAN_PASSWORD`, or choose your own.
2. **Location:** search for your town. The time zone is set for you.
3. **Prayer times:** pick the calculation method and Asr setting your mosque uses, and compare today's times. For an exact match with your masjid, use *Use a timetable*: download the template, fill it in with Excel, and upload it.
4. **Azan sound:** add your azan, and optionally a separate Fajr azan and a reminder before each prayer. MP3 files are converted automatically.
5. **Phone system:** Grandstream UCM, FreePBX, 3CX, phones only (multicast), or something else. *Connect and test* signs in as the extension and plays a test chime through your paging group, so you know it works before anything is saved.
6. **Test and finish:** check the summary and press *Finish*.

The wizard shows a QR code: scan it to carry on from your phone. If you'd rather set everything up yourself, choose *I'll set it up myself*. You can run the guide again later from **Settings**, starting from your current settings. Afterwards the control panel opens. Its **Today** page shows the countdown to the next azan and has on/off switches for each prayer, quick test buttons, recent activity and the log.

**Prayer-times screen:** open `http://<the computer's address>:8765/display` on a TV, tablet or spare monitor for a full-screen clock, the next azan with a countdown, and today's times in English and Arabic. After the last azan of the day it shows tomorrow's times. No password is needed to view it, and *Full screen* fills the screen.

**Makkah live:** press *Makkah live* on the prayer-times screen (or open `/display?live=1`) to show the live picture from Masjid al-Haram next to the next azan and today's times. The sound starts off and goes quiet by itself while your own azan plays. The Box relays the stream, so screens watching at the same time share one download. Each screen remembers whether it shows Makkah live. If the Box runs on a cloud server, turn on *Screens fetch the stream themselves* on the Display page, so each screen downloads the stream over its own internet connection and the server uses no data for it.

**Display page:** in the control panel, choose how the prayer-times screen looks, with a live preview: a theme (Masjid green, Night, Navy and gold, Maroon, Sand, or your own colours), cards along the bottom or a list at the side, the text size, the masjid name, a notice line along the bottom, and which parts to show. *Save changes* updates every screen within a few seconds. The Makkah live layout and stream link are there too. **Settings → Appearance** sets light or dark and the colour of the control panel itself.

| Ways to reach the phones | Best for |
|---|---|
| Multicast paging | Yealink, Grandstream, Fanvil, Snom, Cisco MPP. Each phone listens on one address; no PBX changes. |
| Phone system page group | Grandstream UCM, FreePBX, Asterisk, 3CX and others. The Box stays registered as its own extension (shows as Idle) and calls your paging group. |
| Call each phone directly | Poly/Polycom, or when multicast isn't possible. Needs direct IP calls and auto-answer on the phones. |
| IP speaker (RTP) | Paging speakers that listen on a fixed IP address and port. |

## Password

- Anyone on the network can view the prayer times. Changing anything needs the password.
- `AZAN_PASSWORD` is the box's factory password. Under **Settings → Password and reset** you can choose your own, which then replaces it.
- **Factory reset** on the same page removes settings, recordings and your own password, and goes back to `AZAN_PASSWORD`.

## Updating

**With the Update button:** go to **Settings → Updates** and press **Check for updates**. If there's a new version, press **Update now**. The Box restarts with it in a few seconds, and your `data` folder is kept. If the new version won't start, the old one is put back.

The button needs the `updater` part of the compose file above. It's a small helper, from the same image, that downloads the new image and restarts the `azan-sip` container with it. Because it can control Docker on that computer, it only listens on the computer itself (127.0.0.1, port 8770) and only does what the control panel asks, using a secret key the panel keeps in `data/auth`. If you leave it out, Settings → Updates says how to update by hand.

**Installed on Debian:** the same button installs each new release from GitHub, and keeps the previous version so you can go back.

**By hand:** pull the new image and restart: `docker compose pull && docker compose up -d`, or redeploy the project in your NAS's Docker app. This also updates the helper itself.

What's new in each version is under [Releases](https://github.com/Fudgeamin99/azan-sip-box/releases) and in [`BOX_CHANGELOG.md`](BOX_CHANGELOG.md).

## What's next

| Step | What it adds | Status |
|---|---|---|
| 1 | Control panel on the network, password, factory reset, `http://azan.local`, Debian installer | Panel, password and reset in 0.2.0, the Debian installer in 0.3.0 |
| Docker | Run it on a NAS or home server | Released in 0.2.0. The Update button in Settings came in 0.3.0. |
| 2 | Six-step setup wizard, with phone setup by QR code, and the prayer-times screen | Released in 0.3.0, with Makkah live and the Display page |
| 3 | Debian disk image for the Dell Wyse 3040: full-screen prayer-times display, bulk flasher | Planned |
| 4 | Sticker tool: per-box password and QR code | Planned |

## About this repository

`main` holds the Box's code. To publish a version, add a `## x.y.z - title` section at the top of [`BOX_CHANGELOG.md`](BOX_CHANGELOG.md) and merge it to `main`: the release and the Docker image are published automatically. The Box started from the desktop app, [ATHAN-TO-SIP](https://github.com/Fudgeamin99/ATHAN-TO-SIP); its new features are brought over here by hand.
