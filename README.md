# AthanBox

AthanBox plays the azan (call to prayer) automatically at every prayer time on your VoIP desk phones and IP speakers. It runs on its own, around the clock, and you set it up from a control panel in your browser on any computer or phone on the same network.

It runs in **Docker** on a Linux computer that stays on, such as a NAS, a home server or a Raspberry Pi, or [straight on Debian](#run-it-on-debian) on a small PC such as a Dell Wyse thin client. A ready-to-plug-in AthanBox (a Dell Wyse 3040 with its own prayer-times screen) is on the way. See [What's next](#whats-next).

**Latest release:** [box-v0.11.1, Docker](https://github.com/Fudgeamin99/AthanBox/releases/latest). Wyse client: [client-v0.5.4](https://github.com/Fudgeamin99/AthanBox/releases/tag/client-v0.5.4). Tested on a UGREEN NAS paging the phones through a Grandstream UCM.

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

  # Optional: fail2ban, the whitelist and the blacklist on the firewall (see "Security" below)
  security:
    image: ghcr.io/fudgeamin99/azan-sip-box:latest
    container_name: azan-sip-security
    network_mode: host
    cap_add: [NET_ADMIN, NET_RAW]
    restart: unless-stopped
    entrypoint: ["python", "-I", "/usr/local/lib/azan-sip/security-helper.py", "watch", "/data"]
    volumes:
      - ./data:/data
```

Then start it:

- **UGREEN, Synology and other NAS Docker apps:** create a new project (or stack), paste the file above, and deploy it.
- **Command line:** run `docker compose up -d` in that folder.

Open `http://<the computer's address>:8765`, sign in with your password, and the setup wizard starts.

- **Linux only.** Host networking doesn't work in Docker Desktop on Windows or Mac.
- **The image keeps its name,** `ghcr.io/fudgeamin99/azan-sip-box`, although the repository is now called AthanBox. Keep that name in `compose.yaml`: the Update button pulls the name the container already runs.
- **No ports to add.** The panel is always on port 8765 of the computer it runs on.
- **Nothing to set up for the folder.** Docker creates `data` on first start, and the container takes it over itself.
- **The image** is for 64-bit PCs (amd64) and ARM boards (arm64), and includes ffmpeg for MP3 recordings.

## Run it on Debian

On a small PC that stays on, such as a Dell Wyse thin client, install Debian 12 or 13 without a desktop, then run:

```sh
sudo apt update && sudo apt install -y git
git clone https://github.com/Fudgeamin99/AthanBox.git azan-sip-box
cd azan-sip-box
sudo sh packaging/box/install.sh
```

If it says `sudo: command not found`, Debian left sudo out because you set a root password in the installer. Sign in as `root` (or run `su -` and type the root password), then run the same commands without `sudo`. The same goes for every `sudo` command below.

It installs Python and ffmpeg, names AthanBox `azan` on the network, asks for the factory password, and starts AthanBox on every boot. Open `http://azan.local` (or `http://<its address>`) and the setup wizard starts. AthanBox needs to be on the same network as your phone system.

### Prayer times on a TV plugged into AthanBox

A Dell Wyse or another small PC can show the prayer-times screen on a TV plugged into its HDMI or DisplayPort, and play the azan through the TV's speakers at the same moment as the phones. An old laptop shows it on its own screen and plays the azan through its own speakers, unless a TV is plugged in. Closing a laptop's lid doesn't put AthanBox to sleep. Plug the TV in and switch it on, then run:

```sh
cd azan-sip-box
git pull
sudo sh packaging/box/screen.sh
```

It installs a small full-screen browser (cage and Chromium) and opens the TV app on the TV, on every boot, with no tap needed for the sound. It works with *Let TV screens connect* switched off, and it can't open the settings. The screen shows on the **TV app** page as *Screen on AthanBox*, where *Test chime* checks the sound. Theme, layout and Ramadan screens come from the **Display** page.

- **No sound from the TV:** run the command again with the TV switched on, so it picks the sound output the TV is on. `aplay -l` lists the outputs. To pick one by hand, say card `HdmiLpeAudio`, device `1`, run `sudo AZAN_SCREEN_SOUND=HdmiLpeAudio,1 sh packaging/box/screen.sh`.
- **Switch it off:** `sudo sh packaging/box/screen.sh off`. Ctrl+Alt+F2 on a keyboard plugged into AthanBox also opens a text login while the screen is on.

### A TV screen on a Dell Wyse or a laptop

A spare Dell Wyse, an old laptop or any small PC can be a TV screen for an AthanBox somewhere else on the network: it shows AthanBox's prayer-times screen full screen on the TV plugged into it, and plays the azan through the TV's speakers at the same moment as the phones, at its own volume. It starts by itself on every boot, and if AthanBox restarts or the network drops it shows *Connecting to AthanBox* and goes back to the prayer times by itself. With two TVs or monitors plugged into the same Wyse, each one shows the prayer times, and the azan plays once. Use one Wyse per room, in as many rooms as you like.

1. On AthanBox, open **TV app** and switch on **Let TV screens connect**.
2. Install Debian 12 or 13 on the Wyse or laptop without a desktop (in the installer, tick only *SSH server* and *standard system utilities*), and plug in the TV. For a 32-bit PC or laptop, use Debian 12's **i386** installer: Debian 13 no longer has one for 32-bit computers.
3. On the Wyse, run:

```sh
sudo apt update && sudo apt install -y wget
wget https://github.com/Fudgeamin99/AthanBox/releases/download/client-v0.5.4/azan-sip-client.deb
sudo apt install -y ./azan-sip-client.deb
```

If it says `sudo: command not found` (Debian leaves sudo out when you set a root password in the installer), run `su -` first, type the root password, and run the same commands without `sudo`, with the file's full path: `apt install -y /home/<your user>/azan-sip-client.deb`.

4. Choose AthanBox: on the TV, type its address (such as `192.168.1.20` or `azan.local`) or press **Search the network**, then **Connect**. Over SSH instead: `azan-sip-client search`, then `azan-sip-client connect 192.168.1.20 --name "Main hall"`.

**On a laptop:** with no TV plugged in, it shows the prayer times on the laptop's own screen and plays the azan through its speakers. With a TV on HDMI, the prayer times and the azan go to the TV instead, and the laptop keeps going with its lid closed. Keep it plugged into power. It works on 64-bit and 32-bit laptops alike.

The Wyse shows on AthanBox's **TV app** page under its name, where *Test chime* checks the sound. **Volume:** move the mouse and use the volume buttons at the top of the screen, or press **+** and **−** (or a keyboard's volume keys), or run `azan-sip-client volume 60` over SSH. It sets Makkah live's volume as well as the azan's. To change AthanBox or the name later, press **Screen settings** next to them (or **S** on a keyboard). The Wyse can only show the prayer times: it can't change AthanBox's settings. More in [`client/README.md`](client/README.md).

**From a phone or computer:** Screen settings also opens in a browser on the same network, at the address shown under **Phones and computers** in Screen settings on the TV, such as `http://192.168.1.31:8772`. The first time, type the code that shows on the TV; the browser then stays signed in. From there you can choose AthanBox, rename the screen, set the volume, switch Makkah live's sound on or off, play a test chime on the TV, update, and shut the Wyse down. A change to the volume or Makkah live's sound shows on the TV for a moment. It can be switched off, or everyone signed out, in Screen settings on the TV.

**Updates:** open **Screen settings** and press **Update now** when it says a new version is available. The Wyse downloads it from the client's own releases on this repository (tagged `client-v...`, apart from AthanBox's, so a client update never changes AthanBox or its Docker image), installs it and restarts the screen by itself. Over SSH, `azan-sip-client update` as root does the same. A Wyse with a client older than 0.5.2 needs the three commands above once more: the Update button arrived in 0.3.0, and up to 0.5.1 it refuses updates since the repository was renamed AthanBox. What's new in each version is in [`client/CHANGELOG.md`](client/CHANGELOG.md).

**Shut down:** press **Shut down** in Screen settings, on the TV or from a phone or computer, then **Yes, shut down**. The Wyse turns off, and its power button turns it back on.

## First-time setup

A new AthanBox opens the **setup wizard** in the browser. It takes about five minutes:

1. **Password:** keep `AZAN_PASSWORD`, or choose your own.
2. **Location:** search for your town. The time zone is set for you.
3. **Prayer times:** pick the calculation method and Asr setting your mosque uses, and compare today's times. For an exact match with your masjid, use *Use a timetable*: download the template, fill it in with Excel, and upload it.
4. **Azan sound:** add your azan, and optionally a separate Fajr azan and a reminder before each prayer. MP3 files are converted automatically.
5. **Phone system:** Grandstream UCM, FreePBX, 3CX, phones only (multicast), or something else. *Connect and test* signs in as the extension and plays a test chime through your paging group, so you know it works before anything is saved.
6. **Test and finish:** check the summary and press *Finish*.

The wizard shows a QR code: scan it to carry on from your phone. If you'd rather set everything up yourself, choose *I'll set it up myself*. You can run the guide again later from **Settings**, starting from your current settings. Afterwards the control panel opens. Its **Today** page shows the countdown to the next azan and has on/off switches for each prayer, quick test buttons, recent activity and the log.

**Prayer-times screen:** open `http://<the computer's address>:8765/display` on a TV, tablet or spare monitor for a full-screen clock, the next azan with a countdown, and today's times in English and Arabic. After the last azan of the day it shows tomorrow's times. No password is needed to view it, and *Full screen* fills the screen.

**Makkah live:** press *Makkah live* on the prayer-times screen (or open `/display?live=1`) to show the live picture from Masjid al-Haram next to the next azan and today's times. The sound starts off and goes quiet by itself while your own azan plays. AthanBox relays the stream, so screens watching at the same time share one download. Each screen remembers whether it shows Makkah live. If AthanBox runs on a cloud server, turn on *Screens fetch the stream themselves* on the Display page, so each screen downloads the stream over its own internet connection and the server uses no data for it. To make sure it never does, also turn off *If a screen can't fetch it, go through AthanBox*.

**TV app:** on the **TV app** page, switch on *Let TV screens connect*. A TV screen then shows the prayer times and plays the azan through the TV's speakers, at the same moment as the phones. Use an old iPad (scan the QR code, then *Add to Home Screen*), the **Azan SIP TV** Android app (scan the second QR code, or open `http://<AthanBox's address>:8765/apk` on the device), or a smart TV's browser at `http://<AthanBox's address>:8765/tv`. The page lists the screens and can test them.

**Android app updates:** on the screen, press **Back** (or the remote's **Menu** button) for the app's menu, then **Update the app**. Android asks before it installs the update. A screen that got the app before AthanBox 0.7.7 needs it removed and installed again once, from `/apk`, because the app is now signed with its permanent key. If Makkah live doesn't show a picture on a screen, the same menu switches it to the other video player.

**Ramadan:** the **Ramadan** page sets the dates, the extra sounds on the phones (Suhoor ends, the Iftar azan, Taraweeh and more), Isha in Ramadan, the Ramadan screens (countdowns, the dua at Iftar, Eid) and prints the month's timetable. It switches itself on and off on the dates you set.

**Display page:** in the control panel, choose how the prayer-times screen looks, with a live preview: a theme (Masjid green, Night, Navy and gold, Maroon, Sand, or your own colours), cards along the bottom or a list at the side, the text size, the masjid name, a notice line along the bottom, and which parts to show. *Save changes* updates every screen within a few seconds. The Makkah live layout and stream link are there too. **Settings → Appearance** sets light or dark and the colour of the control panel itself.

| Ways to reach the phones | Best for |
|---|---|
| Multicast paging | Yealink, Grandstream, Fanvil, Snom, Cisco MPP. Each phone listens on one address; no PBX changes. |
| Phone system page group | Grandstream UCM, FreePBX, Asterisk, 3CX and others. AthanBox stays registered as its own extension (shows as Idle) and calls your paging group. |
| Call each phone directly | Poly/Polycom, or when multicast isn't possible. Needs direct IP calls and auto-answer on the phones. |
| IP speaker (RTP) | Paging speakers that listen on a fixed IP address and port. |
| Register to AthanBox (SIP trunk) | A phone system that adds AthanBox as a register SIP trunk (no extension needed). AthanBox listens on UDP port 5060. |
| Desk phones sign in to AthanBox | Desk phones with no phone system, or as a second line next to it. Each phone gets its own extension on AthanBox, and AthanBox rings them with auto-answer so they pick up by themselves. Same UDP port 5060. For Yealink cordless (DECT) handsets, give each handset its own account and turn on Auto Intercom on each handset. |

**Remote provisioning:** on the **Phones** page, switch on **Remote provisioning** to get a link such as `http://<AthanBox's address>:8766/provision/<key>/yealink/`. Type a desk phone's MAC address on its extension under *Desk phones sign in to AthanBox*, paste the link into the phone's **Settings → Auto Provision → Server URL**, and the phone downloads its AthanBox line with auto-answer on, so nobody types SIP details on it. Several extensions with the same MAC become lines 1, 2, 3 on that phone, which suits a Yealink DECT base (W60B with W52H/W56H handsets). Yealink desk phones and DECT bases for now.

- **Add a phone** on the same card makes its extensions in one step from its MAC address, a name and how many lines. **Update now** asks a signed-in phone to download its settings again straight away (SIP NOTIFY check-sync).
- **Phones outside the network:** forward TCP port 8766 (provisioning) and UDP port 5060 (SIP) on AthanBox's router to AthanBox, and fill in the **Public address** (your public IP or a dynamic DNS name). Only the provisioning links answer on port 8766, so the control panel stays private.
- **Keeping it safe:** the link carries a long random key, and **New link** replaces it. A wrong key is logged, and fail2ban's control panel jail bans an address that keeps guessing. **Allowed addresses** can limit downloads to your own sites. The link is plain http, so the SIP password crosses the internet unencrypted: keep to the allowed addresses where you can.
- Debian and Docker work the same. With Docker's host networking the port needs nothing in `compose.yaml`.

## Password

- Anyone on the network can view the prayer times. Changing anything needs the password.
- `AZAN_PASSWORD` is AthanBox's factory password. Under **Settings → Password and reset** you can choose your own, which then replaces it.
- **Factory reset** on the same page removes settings, recordings and your own password, and goes back to `AZAN_PASSWORD`.

## Security: fail2ban, whitelist and blacklist

Open **Security** in the menu.

- **Fail2ban** bans an address for a while after too many wrong passwords: SSH sign-ins to AthanBox, the control panel's password, and SIP sign-ins (desk phones and SIP trunks). A banned address can't reach AthanBox on any port. Choose how many wrong tries are allowed, over how long, and how long the ban lasts. **Banned right now** lists who is banned, with **Unban** and **Block**.
- **Whitelist:** addresses and ranges that are never banned or blocked. AthanBox's own network is on it from the start, so you can't lock yourself out from inside the masjid. Add your phone system or your home's address too.
- **Blacklist:** addresses and ranges that are always blocked, with a note. AthanBox won't let you block the address you're using, or its own.

Changes apply straight away, without a restart. On Debian, `install.sh` sets this up (it installs fail2ban and nftables). On an AthanBox installed before version 0.5.1, update it from Settings → Updates first, then run `git pull` and `sudo sh packaging/box/system-setup.sh` once in the `azan-sip-box` folder. In Docker, fail2ban and the firewall run in the `security` part of the compose file above, from the same image. It needs the `NET_ADMIN` capability to change the computer's firewall, so it runs as root in a container of its own, while AthanBox itself stays user 1000. It uses nftables, or iptables on a NAS whose kernel has no nftables, such as many Synology models. SSH sign-ins aren't watched in Docker, since the container can't see the NAS's own SSH log. Without the `security` part, the blacklist still works for the control panel and SIP: AthanBox refuses those addresses itself. To add it to a Box already running in Docker, copy that part into your compose file and run `docker compose up -d` (or redeploy the project in your NAS's Docker app). After that, the Update button updates it along with AthanBox.

## Updating

**With the Update button:** go to **Settings → Updates** and press **Check for updates**. If there's a new version, press **Update now**. AthanBox restarts with it in a few seconds, and your `data` folder is kept. If the new version won't start, the old one is put back.

The button needs the `updater` part of the compose file above. It's a small helper, from the same image, that downloads the new image and restarts the `azan-sip` container with it. Because it can control Docker on that computer, it only listens on the computer itself (127.0.0.1, port 8770) and only does what the control panel asks, using a secret key the panel keeps in `data/auth`. If you leave it out, Settings → Updates says how to update by hand.

**Installed on Debian:** the same button installs each new release from GitHub, and keeps the previous version so you can go back. When a new version needs system changes too (packages such as fail2ban, or system services), AthanBox makes them by itself a minute or two after updating: a small service that runs as root downloads that release from GitHub and runs its `packaging/box/system-setup.sh`. The control panel itself never gets root. An AthanBox installed before 0.5.1 needs this set up once: run `git pull` and `sudo sh packaging/box/system-setup.sh` in the `azan-sip-box` folder.

**By hand:** pull the new image and restart: `docker compose pull && docker compose up -d`, or redeploy the project in your NAS's Docker app. This also updates the helper itself.

What's new in each version is under [Releases](https://github.com/Fudgeamin99/AthanBox/releases) and in [`BOX_CHANGELOG.md`](BOX_CHANGELOG.md).

## What's next

| Step | What it adds | Status |
|---|---|---|
| 1 | Control panel on the network, password, factory reset, `http://azan.local`, Debian installer | Panel, password and reset in 0.2.0, the Debian installer in 0.3.0 |
| Docker | Run it on a NAS or home server | Released in 0.2.0. The Update button in Settings came in 0.3.0. |
| 2 | Six-step setup wizard, with phone setup by QR code, and the prayer-times screen | Released in 0.3.0, with Makkah live and the Display page |
| Desktop 1.11 | Ramadan mode, the SIP trunk, the TV app and the Android app from the desktop app | Released in 0.4.0 |
| 3 | Debian disk image for the Dell Wyse 3040: full-screen prayer-times display, bulk flasher | The full-screen display is ready on Debian (`packaging/box/screen.sh`). The disk image and flasher are planned. |
| 4 | Sticker tool: per-device password and QR code | Planned |

## About this repository

`main` holds AthanBox's code. To publish a version, add a `## x.y.z - title` section at the top of [`BOX_CHANGELOG.md`](BOX_CHANGELOG.md) and merge it to `main`: the release and the Docker image are published automatically. AthanBox started from the desktop app, [ATHAN-TO-SIP](https://github.com/Fudgeamin99/ATHAN-TO-SIP) (now archived); its new features are brought over here by hand. 0.4.0 matches the desktop app's 1.11.2. AthanBox runs on Debian and in Docker only; there's no Windows program.

The Android TV app (**Azan SIP TV**) is in [`android/`](android). Each release builds it (`packaging/apk.sh`), attaches `AzanTV.apk` and puts it in the Docker image. It's signed with the app's own key, which TVs need to install its updates, from two repository secrets: `ANDROID_KEYSTORE` (the keystore file, base64) and `ANDROID_KEYSTORE_PASSWORD`. Without them a release reuses the app from the newest release that has one. Its version is `android:versionCode` and `versionName` in [`android/AndroidManifest.xml`](android/AndroidManifest.xml): raise both for a new version of the app.
