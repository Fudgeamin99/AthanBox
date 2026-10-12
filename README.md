# Azan SIP Box

The Azan SIP Box plays the azan (call to prayer) automatically at every prayer time on your VoIP desk phones and IP speakers. It runs on its own, around the clock, and you set it up from a control panel in your browser on any computer or phone on the same network.

Right now it runs in **Docker** on a Linux computer that stays on, such as a NAS, a home server or a Raspberry Pi. A ready-to-plug-in box (a Dell Wyse 3040 with its own prayer-times screen) is on the way. See [What's next](#whats-next).

**Latest release:** [box-v0.2.0, Docker](https://github.com/Fudgeamin99/azan-sip-box/releases/latest). Tested on a UGREEN NAS paging the phones through a Grandstream UCM.

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
```

Then start it:

- **UGREEN, Synology and other NAS Docker apps:** create a new project (or stack), paste the file above, and deploy it.
- **Command line:** run `docker compose up -d` in that folder.

Open `http://<the computer's address>:8765`, sign in with your password, and the setup wizard starts.

- **Linux only.** Host networking doesn't work in Docker Desktop on Windows or Mac.
- **No ports to add.** The panel is always on port 8765 of the computer it runs on.
- **Nothing to set up for the folder.** Docker creates `data` on first start, and the container takes it over itself.
- **The image** is for 64-bit PCs (amd64) and ARM boards (arm64), and includes ffmpeg for MP3 recordings.

## First-time setup

A new box opens the **setup wizard** in the browser. It takes about five minutes:

1. **Password:** keep `AZAN_PASSWORD`, or choose your own.
2. **Location:** search for your town. The time zone is set for you.
3. **Prayer times:** pick the calculation method and Asr setting your mosque uses, and compare today's times. For an exact match with your masjid, use *Use a timetable*: download the template, fill it in with Excel, and upload it.
4. **Azan sound:** add your azan, and optionally a separate Fajr azan and a reminder before each prayer. MP3 files are converted automatically.
5. **Phone system:** Grandstream UCM, FreePBX, 3CX, phones only (multicast), or something else. *Connect and test* signs in as the extension and plays a test chime through your paging group, so you know it works before anything is saved.
6. **Test and finish:** check the summary and press *Finish*.

The wizard shows a QR code: scan it to carry on from your phone. Afterwards the control panel opens. Its **Today** page shows the countdown to the next azan and has on/off switches for each prayer, quick test buttons, recent activity and the log.

**Prayer-times screen:** open `http://<the computer's address>:8765/display` on a TV, tablet or spare monitor for a full-screen clock, the next azan with a countdown, and today's times in English and Arabic. No password is needed to view it.

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

Pull the new image and restart: `docker compose pull && docker compose up -d`, or redeploy the project in your NAS's Docker app. Your `data` folder is kept. **Update now** in the panel is switched off in Docker, and it explains this.

What's new in each version is under [Releases](https://github.com/Fudgeamin99/azan-sip-box/releases) and in [`BOX_CHANGELOG.md`](https://github.com/Fudgeamin99/azan-sip-box/blob/box-v0.2.0/BOX_CHANGELOG.md).

## What's next

| Step | What it adds | Status |
|---|---|---|
| 1 | Control panel on the network, password, factory reset, `http://azan.local`, Debian installer | Panel, password and reset are in 0.2.0. The Debian installer is [in review](https://github.com/Fudgeamin99/azan-sip-box/pull/1). |
| Docker | Run it on a NAS or home server | Released in 0.2.0 |
| 2 | Six-step setup wizard, with phone setup by QR code, and the prayer-times screen | In review |
| 3 | Debian disk image for the Dell Wyse 3040: full-screen prayer-times display, bulk flasher | Planned |
| 4 | Sticker tool: per-box password and QR code | Planned |

## About this repository

The Box's code is in pull requests [#1](https://github.com/Fudgeamin99/azan-sip-box/pull/1) (step 1) and [#2](https://github.com/Fudgeamin99/azan-sip-box/pull/2) (Docker) until they're merged, and releases are built from those branches. Until then, `main` holds the desktop app's code the Box started from.
