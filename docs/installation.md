# Installing linux-mint-hud

This guide covers all three installation paths, what gets put where, how
updates and uninstalls work, and what to check if something doesn't come up
right.

See also: [App guide](guide.md) · [FAQ](faq.md) · [README](../README.md)

## Requirements

- Linux Mint (Cinnamon) on **X11** — the tested, supported target. Other X11
  desktops will likely work but are untested. Wayland has only **experimental**
  support (via `gtk-layer-shell`, on KDE Plasma / wlroots compositors) and isn't
  verified — see [Does it work on Wayland?](faq.md#does-it-work-on-wayland).
- A graphical session with a system tray area, if you want the tray icon
  (see [Tray menu](tray.md)). The panel itself works without one.
- Internet access is optional: it's only used for weather (open-meteo) and,
  if you use it, the Claude quota slot and any catalog modules that fetch
  prices. Everything else runs fully offline from `/proc` and `/sys`.

## Method 1 — the `.deb` package (recommended)

1. Go to the [Releases page](https://github.com/Versifft/linux-mint-hud/releases/latest)
   and download the latest `.deb`.
2. Double-click it to open it in the Software Installer, and install.
3. When installation finishes, the panel and its Settings window open on
   their own — there's nothing else to configure.

This is the easiest route and the one that plugs into Linux Mint's Update
Manager for future updates (see below).

## Method 2 — the bootstrap one-liner

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Versifft/linux-mint-hud/main/bootstrap.sh)
```

This downloads the repository and runs `install-gui.sh`, a graphical
installer that sets up the same files and menu entry as the `.deb`. Use this
if you don't want to download a `.deb` by hand, or you're on a system where
opening `.deb` files isn't convenient.

## Method 3 — from a cloned repository

If you've cloned the repo yourself (for development, or to inspect it before
running anything), you can run the installer scripts directly:

```bash
git clone https://github.com/Versifft/linux-mint-hud.git
cd linux-mint-hud
./install-gui.sh   # graphical installer
# or
./install.sh       # non-interactive installer
```

Both scripts install the same files as the other two methods.

## What installation does

Regardless of the method:

- Adds a **Linux Mint HUD — Settings** entry to your application menu.
- Sets the panel to **autostart at login** (it starts right away too, the
  first time).
- Installs the app's supporting scripts alongside `hud.py`: `weather.py`
  (open-meteo weather), `claude_quota.py` with `browser_cookie.py` /
  `chromium_cookies.py` (reading the Claude session cookie), and the
  install/uninstall/packaging scripts themselves (`install.sh`,
  `install-gui.sh`, `uninstall.sh`, `bootstrap.sh`, `build-deb.sh`,
  `publish-apt.sh`).
- Creates `~/.config/mint-hud/` for your settings (`settings.json`), the
  cached weather lookup, and — only if you use the Claude quota feature —
  the cached session cookie (`.claude_web_cookie`, permissions `600`).

No system files outside your own `$HOME` and the package's own install
location are touched, and nothing runs as root beyond what package
installation itself requires (`.deb`) or what `sudo` prompts you for
(`install.sh`/`install-gui.sh`, to place the launcher and menu entry).

## Updating

**If you installed the `.deb`:** updates are delivered through Linux Mint's
normal **Update Manager** (APT), the same way any other system package
updates. When a new version is published, `linux-mint-hud` simply shows up
as an available update — nothing to download by hand. Applying it restarts
the running panel with the new version; your settings in
`~/.config/mint-hud` are left exactly as they were.

**If you installed via the bootstrap script or a manual clone:** re-run the
installer (`bootstrap.sh`, or `git pull` followed by `install-gui.sh` /
`install.sh`) to pick up a new release. Your settings are not touched by
re-installing.

## Uninstalling

Either:

- Choose **Linux Mint HUD — Uninstall** from the application menu, or
- Remove the `linux-mint-hud` package from the Software Manager (if you
  installed the `.deb`), or
- Run `uninstall.sh` from a cloned repository.

All three remove the installed files, the autostart entry and the menu
entries. Your settings in `~/.config/mint-hud` are **left in place** unless
you explicitly ask the uninstaller to remove them too — so reinstalling
later picks up right where you left off.

## Troubleshooting installation

| Symptom | What to check |
|---|---|
| The `.deb` won't open, or the Software Installer complains | Make sure you downloaded the latest release for your architecture; try `sudo apt install ./linux-mint-hud_*.deb` from a terminal in the download folder to see the real error. |
| The bootstrap one-liner fails to download | Check you have internet access and that `curl` is installed (`sudo apt install curl`). |
| Nothing appears after installing | Look for **Linux Mint HUD — Settings** in the application menu and open it — it starts the panel if it isn't already running. If there's no tray icon, that's expected on some setups; see [No tray icon](tray.md#no-tray-icon). |
| The panel doesn't autostart after a reboot | Check your Cinnamon **Startup Applications** list for a Linux Mint HUD entry; re-running the installer re-creates it if it's missing. |
| An update doesn't appear in Update Manager | Only `.deb` installs are wired into APT updates. If you installed via the bootstrap script or a manual clone, re-run that installer instead — see [Updating](#updating) above. |
| Settings seem to have reset after reinstalling | They shouldn't — `~/.config/mint-hud/settings.json` is left alone by both update and uninstall. If it's genuinely gone, check you didn't choose to remove settings during an uninstall. |

For anything not covered here — including whether the app phones home, how
it treats your data, and general "is this safe" questions — see the
[FAQ](faq.md).
