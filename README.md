# linux-mint-hud

A system panel that lives on the desktop. One Python process reads every
metric from `/proc` and `/sys`, draws the whole panel with Pillow, and paints
it into its own desktop window through GTK and cairo. It sits below your
windows, on every workspace, and clicks fall straight through it to the
desktop underneath. The top slot shows Claude plan quota, weather, or
alternates between the two.

<img src="docs/hero-loop.gif" alt="The panel in the top-right of a Linux Mint desktop, its top slot cutting between Claude plan quota and the local weather while everything below — the CPU/GPU/RAM gauges, history, thermals, memory, disk, network, power, battery and process sections — stays put.">

## What it shows

- **Claude plan quota** — session and weekly usage, read from the endpoint
  claude.ai's own settings page uses. The session window counts down; the
  weekly one names a weekday and time.
- **Weather** shares that top slot. With a Claude subscription the slot
  alternates between quota and current weather every few seconds; without one
  (no subscription, a lapsed login, no Claude Code at all) it just shows
  weather — a hand-drawn icon for the sky, the temperature large, and the
  day's min, max and feels-like, each tinted by how cold or hot it is. From
  open-meteo, no API key; set no location and the slot is simply empty.
- **Uptime and load** — the 1, 5 and 15 minute averages, coloured against the
  thread count so they only light up when work is actually queuing.
- **CPU, GPU and RAM** — ring gauges, a strip with one column per logical
  core, and an hour-long history where the GPU rides along as a line over the
  CPU columns.
- **Thermals** — temperatures on one line, each on a green-to-red gradient.
  It auto-detects CPU/SSD/wifi by default, or you pick exactly which sensors
  to show and rename them (it also gives the common chips human-readable names
  — coretemp → CPU, nvme → SSD, iwlwifi → WiFi, amdgpu → GPU, …).
- **Memory, swap and disk** usage — one disk by default, or several chosen
  mounts, each with its own bar.
- **Network** — one chart, download above the axis and upload below it.
- **Power** — what the machine draws and, while charging, what the wall
  delivers on top of that, coloured by where the energy is coming from. On
  mains it reads full system power from Intel RAPL when that is permitted (see
  the installer); otherwise it falls back to what the battery reports. The
  battery line carries its charge, direction and terminal voltage.
- **Devices** — the charge of a wireless mouse, keyboard or headset, read from
  UPower and sysfs; shown when you pick which peripherals to display.
- **Top three processes** by CPU and by memory.

Every section can be switched off, and dragged into whatever order you like,
in the settings window (below).

## Power states

The power section changes colour with where the energy is coming from, which is
most of what there is to watch. On battery it is red and shows only what the
machine draws; on mains it splits consumption from the charge going into the
pack, and reads full system power from RAPL where that is permitted.

<table>
<tr>
<td width="33%"><img src="docs/panel-battery-low.png" alt="On battery at 10%: battery bar red, power chart dark red, no AC figure."></td>
<td width="33%"><img src="docs/panel-charging.png" alt="Charging at 82%: amber consumption with green charge stacked on it, a short red stretch on the left, battery bar green."></td>
<td width="33%"><img src="docs/panel-full.png" alt="Full and under load: every core red, the CPU gauge at 100%, a load plateau in the history, and the battery bar green at 100%."></td>
</tr>
<tr>
<td valign="top"><b>On battery, 10%.</b> The bar is red, and the power header shows only what the machine draws — there is no wall to measure.</td>
<td valign="top"><b>Charging, 82%.</b> Amber consumption with the charge into the pack stacked in green, and just enough of the earlier on-battery period (red) left to show the switch.</td>
<td valign="top"><b>Full, under load.</b> Twenty cores pegged: the gauge and per-core strip go red, the history fills, and the CPU runs hot. The battery bar is green now that it is topped up.</td>
</tr>
</table>

## Settings

Everything is configured from a graphical window — no files to hand-edit:

```
~/.config/mint-hud/hud.py --settings
```

After installing it's on the application menu too, as **Linux Mint HUD —
Settings**. Changes apply live; the running panel picks them up within a
moment, no restart.

- **Panels.** Run more than one — name them, duplicate one onto a second
  monitor, add or remove them. Each panel keeps its own sections, order, disks,
  sensors, temperature unit and size.
- **Place and size by hand.** Hit *Move panel…*, drag it anywhere (it stays
  exactly where you drop it), and drag the corner grip to resize. Four
  independent edge margins — top, bottom, left, right — fine-tune the gaps and
  size afterwards. The width is yours to set and never shifts as sections are
  toggled; a panel is only as tall as its content, growing and shrinking as you
  switch sections on and off.
- **Sections.** Tick what to show and drag the enabled ones into any order.
- **Weather & temperatures.** The weather has its own °C/°F, separate from the
  hardware temperatures (most people keep PC temps in Celsius); the location
  name can be hidden per panel.

## How it's built

There is no separate widget engine. `hud.py` is the whole thing: it samples
the metrics once, lays each panel out top to bottom as a single running cursor,
renders it natively at its chosen width with Pillow, and hands the pixels to a
GTK window via cairo. Rates are deltas against the previous frame, so there is
no sampling delay. Each panel is drawn to fit its own box — the gaps between
sections stretch or compress to reach the height, and the width is the one you
set — so it stays crisp at any size and its width never follows the content.

| | |
|---|---|
| `hud.py` | everything: metrics, layout, rendering, the window, the settings GUI (`--settings`) |
| `install-gui.sh` | graphical installer/updater (zenity + pkexec) |
| `uninstall.sh` | graphical/terminal uninstaller |
| `install.sh` | guided terminal installer — dependencies, fonts, autostart, menu entries |
| `bootstrap.sh` | clone + install, for the one-liner |
| `make_icon.py` | renders the app/menu icon (`icons/`) |
| `claude_quota.py` | Claude plan quota from claude.ai |
| `weather.py` | current weather from open-meteo (reads `weather.json`) |
| `browser_cookie.py` | reads the session cookie from the running browser — Firefox first, then Chromium |
| `chromium_cookies.py` | the Chromium half: decrypts its cookie store via the desktop keyring |
| `99-rapl-psys.rules` | optional udev rule for the system-power reading |

It adapts to the hardware it finds. CPU temperature comes from Intel
`coretemp`, AMD `k10temp` or a thermal zone; GPU load from Intel/AMD DRM
counters or NVIDIA's `nvidia-smi`; system power from whichever RAPL domain is
readable. What a given machine can't report simply drops out — a desktop's
missing battery, a GPU with no readable utilisation, temperatures a board
doesn't expose — rather than showing dead zeros.

Written for Linux Mint (Cinnamon). It should work on other X11 desktops but
has not been tested there.

## On any desktop

The panel is translucent dark glass, so it settles onto whatever wallpaper is
behind it.

<table>
<tr>
<td width="50%"><img src="docs/desktop-1.jpg" alt="the panel over a gold wallpaper, top slot showing weather for Marrakesh"></td>
<td width="50%"><img src="docs/desktop-2.jpg" alt="the panel over a purple 3D-cubes wallpaper, top slot showing Claude quota"></td>
</tr>
<tr>
<td width="50%"><img src="docs/desktop-3.jpg" alt="the panel over a teal confetti wallpaper, top slot showing weather for Reykjavík"></td>
<td width="50%"><img src="docs/desktop-4.jpg" alt="the panel over a dark charcoal wallpaper with the Mint logo, top slot showing Claude quota"></td>
</tr>
</table>

## Installing

```
bash <(curl -fsSL https://raw.githubusercontent.com/Versifft/linux-mint-hud/main/bootstrap.sh)
```

That clones the repo to `~/.config/mint-hud` and opens the installer. On a
desktop it's **graphical** — one checklist (everything ticked; untick what you
don't want), then it does the rest behind a progress bar, asking for your
password once for the parts that need root. With no desktop session it falls
back to the same steps as terminal prompts. Either way it's safe to re-run, and
it adds application-menu entries: **Settings**, **Install / Update** and
**Uninstall**, so you rarely need a terminal again.

Or clone it yourself and run whichever installer you prefer:

```
git clone https://github.com/Versifft/linux-mint-hud.git ~/.config/mint-hud
~/.config/mint-hud/install-gui.sh   # graphical (falls back to the terminal one)
~/.config/mint-hud/install.sh       # guided terminal installer
```

### By hand

The core dependencies, all in the Mint repositories:

```
sudo apt install python3-pil python3-gi python3-gi-cairo python3-cairo
```

`python3-cryptography` and `gir1.2-secret-1` in addition let the Claude section
read a cookie from a Chromium-family browser; skip them if you use Firefox.
Fonts are optional — it falls back to DejaVu and just looks plainer. Inter and
JetBrains Mono install per-user, no root:

```
apt-get download fonts-inter fonts-jetbrains-mono
for d in *.deb; do dpkg-deb -x "$d" x; done
mkdir -p ~/.local/share/fonts
find x -name '*.[ot]tf' -exec cp {} ~/.local/share/fonts/ \;
fc-cache -f ~/.local/share/fonts
```

Then run `~/.config/mint-hud/hud.py`, and for autostart drop a `.desktop` file
in `~/.config/autostart/` pointing at it with a few seconds of
`X-GNOME-Autostart-Delay`.

## Uninstalling

From the menu, **Linux Mint HUD — Uninstall**, or:

```
~/.config/mint-hud/uninstall.sh
```

It stops the panel and removes the autostart and menu entries. It also
offers — off by default — to remove the system-power udev rule (one password)
and the app folder itself, settings and all. Fonts and any packages it
installed are left in place.

## The Claude quota cookie

The Claude section talks to a private claude.ai endpoint that authenticates
with your browser session cookie. `browser_cookie.py` reads it straight out of
the running browser each time, so it stays current on its own as long as you
are logged in — Firefox and its forks, or Chromium-family browsers (Chrome,
Brave, Edge). Nothing is stored beyond a cached copy at
`~/.config/mint-hud/.claude_web_cookie` (mode 600, never committed).

If your browser isn't supported, or the section shows a login message it
shouldn't, you can drop a cookie in by hand. In the browser's dev tools,
export a HAR of any request to `claude.ai/api/.../usage`, save it to
`~/Downloads`, and run:

```
python3 -c "
import json, os, glob
hars = sorted(glob.glob(os.path.expanduser('~/Downloads/*.har')), key=os.path.getmtime)
p = hars[-1]
with open(p) as f:
    d = json.load(f)
e = next(x for x in d['log']['entries'] if x['request']['url'].endswith('/usage'))
cookie = next(h['value'] for h in e['request']['headers'] if h['name'].lower() == 'cookie')
out = os.path.expanduser('~/.config/mint-hud/.claude_web_cookie')
with open(out, 'w') as f:
    f.write(cookie.strip() + '\n')
os.chmod(out, 0o600)
print('saved from', p, '- length:', len(cookie))
"
```

Delete the `.har` afterwards — it contains your full session cookie in plain
text.

## Weather

<img src="docs/weather-states.png" alt="Six example weather cards: Reykjavík light snow, Lisbon clear, London rain, Singapore thunderstorm, Bergen fog, Nairobi partly cloudy — each with a hand-drawn icon, temperature, condition, and high/low/feels.">

*(example locations — the icon follows the WMO weather code)*

Set a location in the settings window (**Weather → Location → Look up**), or
let the installer do it; failing that, create `weather.json` in the repo
yourself:

```
{"lat": 47.01, "lon": 7.69, "name": "Lützelflüh"}
```

open-meteo needs no key, and once the coordinates are set nothing identifying
is sent — no IP geolocation. The file is gitignored, so your location stays
local. With no `weather.json` the weather slot is simply not shown.

## Running it

```
~/.config/mint-hud/hud.py            # start the panel
~/.config/mint-hud/hud.py --settings # open the settings window
~/.config/mint-hud/hud.py --png      # render one frame to cache/hud.png
pkill -f "hud[.]py$"                # stop it
```

Note the `[.]` in that pattern — plain `hud.py` also matches the shell you type
it in, and `pkill -f` will happily kill your own terminal. Failures land in
`cache/hud.log`.

## Support

It's free and always will be. If it turned out useful and you feel like it,
you can [buy me a coffee](https://paypal.me/RogerWiedmer) ☕ — entirely
optional, and thanks either way.

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
