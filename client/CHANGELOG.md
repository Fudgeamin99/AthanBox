# Azan SIP Client changelog

Versions of the Azan SIP Client, the TV screen for an Azan SIP Box on a Dell Wyse or a laptop. The
client has its own releases, tagged `client-vx.y.z`, apart from the Box's (`box-vx.y.z`), so a change
to the client never makes a new Box version or Docker image. To publish a version, set
`__version__` in `azan_client.py`, add a new `## x.y.z - title` section at the top, point the
install link in the README at the new tag, and merge it to main: the release is created by itself.

## 0.5.3 - Shut down in Screen settings

- **Shut down** in Screen settings turns the Wyse (or laptop) off, on the TV or from a phone or computer. It asks *Shut down now?* first, and *Yes, shut down* turns it off. The prayer times and the azan stop on that screen until it's switched back on with its power button.
- It works like Update now: the client writes `/var/lib/azan-sip-client/shutdown-now`, and `azan-sip-client-shutdown.path` starts `azan-sip-client-shutdown.service`, which runs `systemctl poweroff` as root. It only turns the computer off if the button was pressed in the last two minutes.
- **Screens with 0.5.2** get it from Update now. Older screens need 0.5.2 installed by hand first (see 0.5.2 below).
- **Box and Docker:** nothing changes.

## 0.5.2 - Makkah live's sound on a Wyse's HDMI, and Update now works again

- **Makkah live's sound plays on the TV** of a Dell Wyse 3040 on HDMI. 0.5.1 didn't fix it: Chromium's own log showed the TV's sound was busy. The TV app keeps a sound open for the azan from the moment it starts, Makkah live opens another, and an HDMI output takes one sound at a time, so Makkah live was turned away and played silently. Now the screen mixes its sounds before they reach the TV (ALSA's dmix, at 48000 Hz), so Makkah live, the azan and the test chime all play. A sound output that can't mix plays one sound at a time, as before.
- **Update now** (and `azan-sip-client update`) installs updates again. Since the repository was renamed AthanBox, GitHub serves the packages under the new name, and the screen refused them as not AthanBox's own: *The update isn't from AthanBox's own releases, so it wasn't installed.* Now it takes them under either name.
- **Screens with 0.5.1 or older** have that check too, so they need this version installed by hand once, as root: `wget -O /tmp/azan-sip-client.deb https://github.com/Fudgeamin99/AthanBox/releases/download/client-v0.5.2/azan-sip-client.deb`, then `apt install /tmp/azan-sip-client.deb`. Updates come from the button again after that.
- **The screen stops within 15 seconds** (`systemctl stop` or `restart azan-sip-client-screen`, and while updating). The browser could take a minute and a half to stop by itself.
- **Box:** a TV plugged straight into AthanBox gets the same sound fix (and the quicker stop) by running `sudo sh packaging/box/screen.sh` again after `git pull`.
- **Docker:** nothing changes.

## 0.5.1 - Makkah live's sound on a Dell Wyse 3040's HDMI

- **Makkah live's sound plays on the TV** of a Dell Wyse 3040 on HDMI: it was silent there, while the azan and the test chime played. Chromium sends Makkah live's sound at 22050 Hz, which HDMI has no setting for, and the rate it was changed to on the way played nothing. Now every sound reaches the TV at 48000 Hz, which every TV plays.
- **Box:** a TV plugged straight into AthanBox gets the same fix by running `sudo sh packaging/box/screen.sh` again after `git pull`.
- **Docker:** nothing changes.

## 0.5.0 - Makkah live sound in Screen settings, and a note when the volume changes

- **Makkah live sound** can be switched on and off in Screen settings, on the TV or from a phone or computer. Before, it could only be changed with the *Sound on/off* button on the video itself, which needed a mouse. That button still works, and Screen settings follows it. Makkah live still goes quiet by itself while the azan plays.
- **A note on the TV** when the volume or Makkah live's sound is changed in Screen settings (from a phone, say): such as *Volume 40%* at the top of the screen for a few seconds.
- **Screen settings** says *Volume set to 40%* (or *Makkah live sound off*) when it has saved the change.
- `azan-sip-client status` says whether Makkah live's sound is on.
- **Box and Docker:** nothing changes.

## 0.4.0 - Screen settings from a phone or computer

- **Change a screen's settings from a phone or computer** on the same network: open the address shown in Screen settings on the TV, such as `http://192.168.1.31:8772`. The box, the screen's name, the volume and updates can all be changed there, and **Play a test chime on the TV** plays it through the TV's speakers.
- **Signing in:** the first time, the phone or computer asks for a 6-digit code that shows on the TV for 5 minutes. After that it stays signed in for 90 days. Five wrong codes end a code, and ten wrong codes in an hour stop new ones for the rest of the hour.
- **On the TV,** Screen settings has a new **Phones and computers** section: the address to open, how many are signed in, **Sign them all out**, and a switch to turn it off. Only the TV can turn it off or sign them out.
- Only the settings open on that port: not the Box's prayer times or anything else.
- **Box and Docker:** nothing changes.

## 0.3.1 - Makkah live follows the volume, and the client gets its own releases

- **Makkah live follows the screen's volume:** it played at full volume whatever the volume was set to. Now the volume buttons and Screen settings set Makkah live's volume as well as the azan's.
- **Updates come from the client's own releases** on GitHub (tagged `client-v...`) instead of the Box's, so updating the client no longer makes a new Box version or Docker image, and a Box no longer offers an update when only the client changed.
- **Screens with 0.3.0** get 0.3.1 from their **Update now** button as usual: 0.3.0 looks in the Box's latest release, so the 0.3.1 package is put there too. From 0.3.1 on, updates come from the client's own releases.
- **Box and Docker:** nothing changes.

## 0.3.0 - Volume buttons, Update button and two screens

Released with Box 0.7.2.

- **Volume buttons on the screen:** move the mouse over the prayer times and the volume buttons appear at the top, next to Screen settings. A keyboard's **+** and **−** (and its volume keys) work too. Each press plays a short note at the new volume.
- **Update button:** Screen settings says when a new client is out, and **Update now** downloads it from GitHub and installs it by itself.
- **Two screens or more:** with two TVs or monitors (or a laptop and a TV), every screen shows the prayer times, each laid out for its own size, and the azan plays once. Plugging a screen in or out refits the prayer times to the screens that are left, and the azan follows the TV.

## 0.2.0 - Laptops and 32-bit PCs

Released with Box 0.7.1.

- **Laptops as TV screens:** with no TV plugged in, the prayer times show on the laptop's own screen and the azan plays through its speakers. With a TV on HDMI, they go to the TV. The laptop keeps going with its lid closed.
- **64-bit and 32-bit:** the same package installs on both (Debian 12 i386 for 32-bit).

## 0.1.0 - A TV screen on a Dell Wyse

Released with Box 0.6.0.

- Shows a Box's prayer-times screen full screen on the TV plugged into a Wyse, from every boot, and plays the azan through the TV's speakers at its own volume. Finds the Box by its address or by searching the network, and reconnects by itself.
