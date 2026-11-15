# FAQ

Questions people actually ask about linux-mint-hud. See also:
[Installation guide](installation.md) · [App guide](guide.md) ·
[Tray menu](tray.md) · [Custom modules](custom-modules.md) ·
[README](../README.md)

## Is it safe to run?

It's a single Python file (`hud.py`) plus a handful of small supporting
scripts, all readable and MIT-licensed — there's nothing hidden to audit.
It doesn't need root to run (only the installer asks for `sudo`, to place
files and a menu entry). The one thing that runs with your full permissions
on an ongoing basis is **custom modules**, because a module is a shell
command you supply — see [Security](custom-modules.md#security) in the
custom modules guide.

## Does it phone home?

No telemetry, no analytics, no accounts. The only network traffic the panel
ever makes is:

- **Weather** — a request to [open-meteo](https://open-meteo.com) (no API
  key, no account) for the location(s) you've set.
- **Claude quota** — a request to claude.ai's own usage endpoint, using your
  existing browser session cookie, only if you've enabled that slot (see
  below).
- **Custom modules / catalog modules** — whatever the commands you've added
  do. Catalog modules for crypto/stocks/indices/forex hit free, no-key
  sources (CoinGecko, Yahoo Finance) — only when you've added one and only
  on its own interval.

Nothing is sent to the project's author or anywhere else.

## What about the Claude cookie — is that private?

The Claude quota slot reads your claude.ai plan usage using the session
cookie from a browser you're already logged into (Firefox/forks or a
Chromium-family browser). That cookie is cached **locally only**, at
`~/.config/mint-hud/.claude_web_cookie` with file permissions `600` (readable
only by you), and it is never sent anywhere except claude.ai's own usage
endpoint. It never reaches the project maintainers or any third party.

If you're on a browser the panel can't read cookies from automatically, you
can supply the cookie by hand once — see
[Setting the Claude cookie by hand](claude-cookie.md).

If you're not logged into claude.ai anywhere, that slot just shows the
weather instead — nothing breaks.

## Does it work on Wayland?

**Partly, and only experimentally.** The tested, supported target is **X11**
(Linux Mint / Cinnamon), where the whole desktop-widget behaviour — sitting
below your windows, passing clicks through, placing and moving itself — works
as designed. It should work on other X11 desktops too, though that's untested.

There is an **experimental Wayland path** built on the
[gtk-layer-shell](https://github.com/wmww/gtk-layer-shell) library and the
`wlr-layer-shell` protocol. On a compositor that supports that protocol —
notably **KDE Plasma (KWin)** and wlroots-based compositors (Sway, Hyprland,
…) — the panel can run as a desktop-layer widget: it renders, anchors itself
on the desktop layer, stays click-through, and handles multiple monitors. You
need the `gtk-layer-shell` library installed for this.

Two caveats, which is why it's experimental and **not yet verified on real
Wayland hardware**:

- The interactive **Move / resize** edit mode is written around X11 window
  behaviour and may not work correctly on Wayland (the panel can't raise
  itself above your windows to catch the drag). You may have to place a panel
  and then leave it where it is.
- **GNOME (Mutter)** does not implement `wlr-layer-shell`, so on GNOME Wayland
  the panel falls back to a plain XWayland window and the desktop-widget
  behaviour won't work properly.

In short: run it on **X11** for the supported experience. On **KDE Plasma /
wlroots Wayland** a static, placed panel may well work, but treat it as
experimental and expect rough edges — especially around placing and moving.

## Can I use it with multiple monitors?

Yes. [Run as many panels as you like](guide.md#multiple-panels) and drag
each one to wherever you want, including onto a second (or third) monitor.
Each panel is independent, so you could, for example, put a slim horizontal
bar across the top of one screen and a tall full panel on another.

## How much CPU/memory does it use?

It's a single lightweight Python process drawing into one window per panel.
Metrics are sampled from `/proc` and `/sys` on a timer, each panel is a
Pillow image redraw rather than a live widget tree, and custom-module
commands run off the main render loop on their own interval with a timeout,
so a slow command doesn't stall rendering. On typical hardware it's a small
fraction of a single core; a panel with a very short refresh and several
heavy custom modules will use a bit more than a minimal one.

## How do I hide or quit the panel from the tray?

Right-click the tray icon to open its menu, then use **Show panel** to
hide/show all panels, or **Quit panel** to exit entirely. Full details in
[Tray menu](tray.md).

## Why don't I see a tray icon?

Some desktop environments and configurations don't provide a system tray
area, in which case the icon is skipped silently rather than erroring. You
can still reach Settings from the **Linux Mint HUD — Settings** entry in
your application menu. See [No tray icon](tray.md#no-tray-icon) for details.

## Can I run several panels at once?

Yes — that's the normal way to use it for anything beyond a single glance
panel. **Add** a fresh panel or **Duplicate** your current one from the
Settings window; each keeps its own sections, order, disks, sensors,
devices, units, weather location(s), size, position, theme and appearance.
See [Multiple panels](guide.md#multiple-panels) in the app guide.

## How are updates delivered?

If you installed the `.deb`, through Linux Mint's Update Manager (APT) —
exactly like any other system package. If you installed via the bootstrap
script or a manual clone, by re-running that installer. Either way, the
panel restarts itself with the new version and your settings are untouched.
Details in [Updating](installation.md#updating).

## How do I reset a panel?

For appearance, open that panel's **Appearance** group in Settings and use
**Reset to theme defaults** — it clears *every* appearance override on that
panel (accent colour, opacity, glow, border, radius, background mode/image,
size, the Text group's text size and per-role colours, contrast) so the
panel follows its theme's defaults again. (Note the difference from simply
*switching theme*, which adopts the new theme's look but deliberately keeps
your accent, background image and size.) Reset leaves your sections, order,
position and weather alone — there's no single "factory reset" for a panel's
full configuration, so the straightforward way to start completely over is
to **Remove** the panel and **Add** a fresh one.

## Does it touch my settings when it updates?

No. Updating (through Update Manager or by re-running an installer) replaces
the application files only. Your configuration lives in
`~/.config/mint-hud/settings.json` and is never modified, overwritten or
reset by an update. The same is true for uninstalling — settings are kept
unless you specifically ask for them to be removed too.
