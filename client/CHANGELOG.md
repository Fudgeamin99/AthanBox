# Azan SIP Client changelog

Versions of the Azan SIP Client, the TV screen for an Azan SIP Box on a Dell Wyse or a laptop. The
client has its own releases, tagged `client-vx.y.z`, apart from the Box's (`box-vx.y.z`), so a change
to the client never makes a new Box version or Docker image. To publish a version, set
`__version__` in `azan_client.py`, add a new `## x.y.z - title` section at the top, point the
install link in the README at the new tag, and merge it to main: the release is created by itself.

## 0.6.0 - Night sleep, a sound for each prayer, Wi-Fi, the picture, and This screen

- **The Wyse's address on the prayer times:** its network address (such as `192.168.1.31`) shows small in the bottom-left corner of the TV, so it's easy to find for SSH or for Screen settings from a phone. *Show its address on the prayer times* under **This screen** in Screen settings hides it.
- **Night sleep:** the TV screen goes dark a while after Isha (60 minutes unless you change it) and comes back a while before Fajr (20 minutes), from AthanBox's own prayer times. Anything AthanBox plays in the night (Ramadan's Qiyam or Suhoor wake-up, a test chime) wakes it, and so does a key or the mouse, for 10 minutes. On Debian 13 the TV gets no picture and goes to standby; on Debian 12 the screen shows black. **Whole computer** switches the Wyse off instead, and its own clock switches it back on before Fajr: *Try it: off for 3 minutes* checks this works, and if it doesn't come back by itself, switch on *Wake on RTC* (or *Auto power on*) in its BIOS. A computer that can't switch itself back on only lets its screen sleep.
- **Sound for each prayer:** each of the five prayers plays the **Azan**, a short **Chime** instead, or nothing (**Quiet**, its reminder too), on this screen only. The phones and the other screens keep the azan. Ramadan's sounds and test chimes always play.
- **Picture:** turn it for a TV on its side (90, 180 or 270 degrees, Debian 13), keep it off the TV's edges for a TV that cuts them off (up to 10 % on each side), and make the text bigger or smaller on this screen only (70 to 160 %, on top of AthanBox's own text size).
- **Wi-Fi:** **Look for networks** lists the Wi-Fi networks nearby, with their strength; choose one, type its password and it joins (and stays joined after a restart). **Forget this network** goes back to the network cable. It uses NetworkManager if the Wyse has it, or Debian's own network settings (`/etc/network/interfaces.d/azan-sip-client-wifi`) if not. Only Wi-Fi it set up itself is changed or forgotten.
- **This screen:** the Wyse's address (cable or Wi-Fi), temperature, storage, memory, how long it's been on, and the last sound AthanBox sent and whether it played here, with **Restart** and **Shut down** (moved here from their own section). Restart asks *Restart now?* first.
- How it works: Restart and Whole computer sleep use the Shut down service (`azan-sip-client-shutdown.path` now also watches `restart-now` and `sleep-now`). Wi-Fi has its own root service, `azan-sip-client-wifi.path` and `.service`, started by `/var/lib/azan-sip-client/wifi-now`. Both read only what they need from those files and act only on a request from the last two minutes. The client itself still runs as `azan-client` and only proxies AthanBox.
- **Screens with 0.5.2 or newer** get it from **Update now** in Screen settings.
- **Debian package:** new `azan-sip-client-wifi.path` and `.service`; it now recommends `wlopm` (the TV's standby), `wlr-randr` (turning the picture), `iw` and `wpasupplicant` (Wi-Fi), which apt installs with it. Removing it with `purge` also removes the Wi-Fi it set up.
- **Box and Docker:** nothing changes.

## 0.5.4 - Stars for kids mode

- AthanBox 0.8.0's **kids mode** works on the Wyse: when a kids screen asks "Did you pray?", pressing **Yes** (a tap, or OK on the remote) now reaches AthanBox and earns the star. The client passes on only that, so the screen still can't change AthanBox's settings.
- **Screens with 0.5.2 or newer** get it from **Update now** in Screen settings.
- **Box and Docker:** kids mode itself is AthanBox 0.8.0.

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
