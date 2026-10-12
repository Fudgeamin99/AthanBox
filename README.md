# Azan SIP (ATHAN-TO-SIP)

Azan SIP plays the azan (call to prayer) automatically on VoIP desk phones and IP speakers, much like iAthan SIP.
Everything is set up in its control panel; no files need editing.

## Install on Windows (recommended)

1. Go to **[Releases](https://github.com/Fudgeamin99/ATHAN-TO-SIP/releases/latest)** and download **AzanSIP.exe**.
2. Put it in a folder of its own, for example `C:\AzanSIP`. Your settings, recordings and log are kept in that folder.
3. Double-click **AzanSIP.exe**. It runs in the background and opens the control panel in your browser at http://127.0.0.1:8765. No Python is needed.

Double-clicking it again while it's running just reopens the control panel.

Windows may show "Windows protected your PC" the first time, because the program isn't signed by a paid certificate. Click **More info → Run anyway**.

**Already using the Python version?** Quit it first from **Settings → Quit Azan SIP**. Then put AzanSIP.exe in the same folder as before, so it picks up your existing settings and recordings, and double-click it. If **Start with Windows** was on, it switches over to the .exe automatically.

## Install from source (any OS)

1. Install **Python 3.11 or newer** from python.org. On Windows, tick "Add python.exe to PATH" during setup.
2. On this GitHub page, click **Code → Download ZIP**, then extract it somewhere permanent.
   - If you have git, you can run `git clone https://github.com/Fudgeamin99/ATHAN-TO-SIP.git` instead. Updates then use `git pull`.
3. Double-click **Azan SIP** (`Azan SIP.pyw`).
   - If double-clicking does nothing, run `run-azan.bat` instead. It shows a console window with any error messages.

## First-time setup in the control panel

1. **Prayer times:** search for your town, then pick the calculation method and Asr setting your mosque uses.
   - Methods include Moonsighting Committee Worldwide (with the General, Ahmer or Abyad shafaq), MWL, ISNA, Umm al-Qura, Karachi, Egypt and others.
   - For an exact match with your masjid, switch on *Use my mosque's timetable*. Then download the template, fill it in with Excel, and upload it.
2. **Azan sound:** use *Add recording…* to add your azan, and optionally a separate Fajr azan. MP3 files are converted automatically.
3. **Phones:** choose how the azan reaches your phones, then press *Test now*. The setup guide on that page shows exactly what to set on Yealink, Grandstream, Fanvil, Snom, Cisco, Poly, FreePBX and 3CX.
4. Press **Save changes**.
5. **Settings:** switch on **Start with Windows**, so the azan keeps working after the PC restarts.

The **Today** page shows the countdown to the next azan and has on/off switches for each prayer. It also has quick test buttons, recent activity and the log.

## Updating

What's new in each version is listed under **Releases** on this page, and in `CHANGELOG.md`.

Go to **Settings → Updates** and press **Check for updates**, then **Update now**. Azan SIP is also checked for updates once a day automatically, and a notice appears on the Today page when one is available.

- The new version is downloaded from this repository and checked to make sure it starts before anything is replaced. Then Azan SIP restarts itself.
  - **AzanSIP.exe** updates to the latest release's AzanSIP.exe.
  - The source version updates to the latest code.
- Your settings (`config.toml`), recordings (`audio/`), timetable and logs are never touched.
- The previous version is kept in `backups/`. If an update causes a problem, use **Restore** on the same page to go back.

## Ways to reach the phones

| Method | Best for |
|---|---|
| Multicast paging | Yealink, Grandstream, Fanvil, Snom, Cisco MPP. Each phone listens on one address; no PBX changes. |
| Phone system page group | Grandstream UCM, FreePBX, Asterisk, 3CX and others. Azan SIP stays registered as its own extension (shows as Idle) and calls your paging group. |
| Call each phone directly | Poly/Polycom, or when multicast isn't possible. Needs direct IP calls and auto-answer on the phones. |
| IP speaker (RTP) | Paging speakers that listen on a fixed IP address and port. |

## Good to know

- Keep this PC switched on, and on the same network as the phones.
- Keep the PC's clock correct; Windows time sync does this.
- Settings are stored in `config.toml`, the log in `azan.log`, and recordings in `audio/`.
- The control panel only works on this PC; other computers cannot reach it.
- **Command line, optional:**
  - `python -m azan_sip times` prints the prayer times.
  - `python -m azan_sip test --tone` plays a test chime.
  - `python -m azan_sip run` runs without opening a browser, for example as a Linux service. `azan-sip.service` is an example systemd unit.

## Azan SIP Box (in development)

The Azan SIP Box is a small Debian computer that runs Azan SIP on its own. The Windows and source installs above work exactly as before; nothing here changes them.

- `python -m azan_sip run --box` opens the control panel to the local network, on port 8765 and port 80, so it is reachable at **http://azan.local**.
- Viewing the prayer times needs no password. Changing anything needs the box's password: the factory one on the sticker, or the owner's own.
- Passwords are stored hashed in `auth/`, which updates never touch. `python -m azan_sip box-password --factory` sets the sticker password.
- **Settings → Password and reset** changes the password, signs out, or does a factory reset (removes settings, recordings and the owner's password).
- Until first-time setup is saved, a one-time setup code lets a phone sign in by scanning the QR code on the box's screen.
- To try it on a Debian machine: `sudo sh packaging/box/install.sh`. This installs `packaging/box/azan-sip-box.service` and names the machine `azan` on the network.

### Run it with Docker (Linux)

The same Box software can run in Docker on any Linux computer that stays on, such as a home server, a NAS or a Raspberry Pi. There's no full-screen display this way, just the control panel.

1. Copy [`compose.yaml`](compose.yaml) to that computer and set `AZAN_PASSWORD` in it (at least 8 characters).
2. Run `docker compose up -d`. To build from a copy of this repository instead, run `docker compose up -d --build`.
3. Open `http://<that computer's address>:8765` and sign in with the password.

- **Linux only.** The container uses the computer's own network (`network_mode: host`), because multicast paging and SIP don't work through Docker's private network. Docker Desktop on Windows or Mac isn't supported.
- **Your data is kept.** Settings, recordings, the password and the log are kept in the `azan-data` volume. To keep them in a folder you choose, such as a NAS share, use the folder line in `compose.yaml` instead. Docker creates the folder if needed, and the container gives it to its own user (1000) when it starts, so there's nothing to set up.
- **No ports to add.** The container uses the computer's own network, so the panel is simply on port 8765.
- **Updates come from the image.** Run `docker compose pull && docker compose up -d`. **Update now** in the panel is switched off in Docker.
- **Trying a change before it's released.** Each pull request publishes a test image. Put `image: ghcr.io/fudgeamin99/azan-sip-box:pr-<number>` in `compose.yaml` and remove the `build:` line.
- **Address.** The panel is on port 8765. `azan.local` isn't set up in Docker, so use the computer's address or its own `.local` name.
