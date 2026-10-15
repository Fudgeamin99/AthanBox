# Azan SIP Client changelog

Versions of the Azan SIP Client, the TV screen for an Azan SIP Box on a Dell Wyse or a laptop. The
client has its own releases, tagged `client-vx.y.z`, apart from the Box's (`box-vx.y.z`), so a change
to the client never makes a new Box version or Docker image. To publish a version, set
`__version__` in `azan_client.py`, add a new `## x.y.z - title` section at the top, point the
install link in the README at the new tag, and merge it to main: the release is created by itself.

## 0.3.1 - The client gets its own releases

- **Updates come from the client's own releases** on GitHub (tagged `client-v...`) instead of the Box's, so updating the client no longer makes a new Box version or Docker image, and a Box no longer offers an update when only the client changed.
- **Once, on a screen with 0.3.0:** its Update button still looks in the Box's releases, so install 0.3.1 with the commands in the README one more time. From 0.3.1 on, **Update now** finds new versions by itself.
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
