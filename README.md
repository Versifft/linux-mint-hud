# linux-mint-hud

A system panel that lives on your desktop. It sits below your windows, on every
workspace, and clicks fall straight through it to the desktop underneath. The
top slot shows Claude plan quota, the weather, or alternates between the two.

<img src="docs/hero-loop.gif" alt="The panel in the top-right of a Linux Mint desktop, its top slot cutting between Claude plan quota and the local weather while everything below — the CPU/GPU/RAM gauges, history, thermals, memory, disk, network, power, battery and process sections — stays put.">

## What it shows

- **Claude plan quota** — session and weekly usage. The session window counts
  down; the weekly one names a weekday and time.
- **Weather** shares that top slot. With a Claude subscription the slot
  alternates between quota and weather; without one it just shows weather — a
  hand-drawn sky icon, the temperature, and the day's min, max and feels-like.
- **Uptime and load** — the 1, 5 and 15 minute averages.
- **CPU, GPU and RAM** — ring gauges, a per-core strip, and an hour of history.
- **Thermals** — temperatures on one line, each on a green-to-red gradient, with
  friendly names (CPU, SSD, WiFi, GPU, …) you can rename.
- **Memory, swap and disk** — one disk by default, or several chosen mounts.
- **Network** — download above the axis, upload below.
- **Power and battery** — what the machine draws (and, while charging, what the
  wall delivers), plus charge, direction and voltage.
- **Devices** — the charge of a wireless mouse, keyboard or headset.
- **Top three processes** by CPU and by memory.

Every section can be switched off and dragged into any order.

## Installing

**The easy way — download and double-click.** Grab the latest `.deb` from the
[**Releases page**](https://github.com/Versifft/linux-mint-hud/releases/latest)
and open it; the Software Installer sets everything up, and the panel and its
settings window open by themselves when it's done. Nothing else to do.

Prefer a one-liner? This clones the app and runs a graphical installer:

```
bash <(curl -fsSL https://raw.githubusercontent.com/Versifft/linux-mint-hud/main/bootstrap.sh)
```

Either way it adds a **Linux Mint HUD — Settings** entry to your application
menu, and the panel starts automatically every time you log in.

## Updating

If you installed the `.deb`, updates arrive through Linux Mint's normal **Update
Manager** — nothing to download by hand. When a new version is published you're
simply offered `linux-mint-hud` like any other system update, and the panel
restarts itself with the new version. Your settings are never touched.

## Uninstalling

From the application menu, **Linux Mint HUD — Uninstall** — or, if you installed
the `.deb`, remove `linux-mint-hud` from the Software Manager like any other
package. Your settings in `~/.config/mint-hud` are left in place unless you ask
for them to be removed.

## Settings

Everything is configured from a graphical window — no files to hand-edit. Open
**Linux Mint HUD — Settings** from the menu (opening it also starts the panel if
it isn't running). Changes apply live, no restart.

- **Panels.** Run more than one — name them, duplicate one onto a second
  monitor, add or remove them. Each keeps its own sections, order, disks,
  sensors, temperature unit and size.
- **Place and size.** Hit *Move panel…*, drag it anywhere (it stays where you
  drop it), and drag the corner grip to resize. The width is yours to set; the
  height follows the content, and dragging it taller spreads the sections apart.
- **Sections.** Tick what to show and drag the enabled ones into any order.
- **Weather & temperatures.** The weather has its own °C/°F, separate from the
  hardware temperatures; the location name can be hidden per panel.

## Weather

The panel picks your location automatically on first run and shows the local
weather straight away (from open-meteo, no API key). To change it, use **Weather
→ Location → Look up** in the settings window.

## Claude quota

The Claude section shows your claude.ai plan usage. It reads it straight from
claude.ai using your **browser session cookie** — the same login you already
use — so there are no keys or passwords to enter.

It finds that cookie on its own from a running browser, so all you have to do is
stay **logged in to claude.ai** in one of the supported browsers:

- **Firefox** and its forks (LibreWolf, Waterfox, …)
- **Chromium-family** browsers — Chrome, Chromium, Brave, Edge

As long as you're logged in there, the slot keeps itself current; the cookie is
only ever cached locally (`~/.config/mint-hud/.claude_web_cookie`, mode 600) and
never leaves your machine except to claude.ai. With no Claude login at all, the
slot simply shows the weather instead.

**Using a different or unsupported browser?** You can hand the panel the cookie
once, from a saved network capture — see **[Setting the Claude cookie by
hand](docs/claude-cookie.md)**.

## How it's built

There is no separate widget engine. `hud.py` is the whole thing: it samples the
metrics from `/proc` and `/sys`, lays each panel out top to bottom, renders it
with Pillow, and paints it into a GTK/cairo desktop window. Rates are deltas
against the previous frame, so there's no sampling delay, and each panel is
drawn natively at its chosen width, so it stays crisp at any size.

It adapts to the hardware it finds — CPU temperature from `coretemp`/`k10temp`
or a thermal zone, GPU load from Intel/AMD DRM counters or `nvidia-smi`, system
power from whichever RAPL domain is readable. What a machine can't report simply
drops out rather than showing dead zeros. Written for Linux Mint (Cinnamon); it
should work on other X11 desktops but hasn't been tested there.

The supporting files: `weather.py` (open-meteo), `claude_quota.py` with
`browser_cookie.py`/`chromium_cookies.py` (the Claude reading), `install.sh` /
`install-gui.sh` / `uninstall.sh` / `bootstrap.sh` (setup), `build-deb.sh` and
`publish-apt.sh` (packaging and updates), and `make_icon.py` (the app icon).

## License

MIT — use it, change it, ship it, no warranty.

```
MIT License

Copyright (c) 2026 Versifft

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
