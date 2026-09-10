#!/usr/bin/env bash
#
# Graphical installer / updater for linux-mint-hud, for people who would rather
# not use a terminal. It uses zenity for the dialogs and pkexec for the one or
# two steps that need root. With no graphical session (or no zenity) it hands
# straight over to the terminal installer, install.sh.
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="$HOME/.local/share/applications"
AUTOSTART="$HOME/.config/autostart/mint-hud.desktop"
FONT_DIR="$HOME/.local/share/fonts"
ICON="mint-hud"                    # our own icon (installed into the hicolor theme)

# No GUI available? Use the guided terminal installer instead.
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || ! command -v zenity >/dev/null 2>&1; then
    exec "$HERE/install.sh"
fi

zen() { zenity "$@" 2>/dev/null; }
err() { zen --error --title="Linux Mint HUD" --width=380 --text="$1"; }

# ---- install the app icon into the user's hicolor theme ------------------
install_icons() {
    local d="$HOME/.local/share/icons/hicolor" s
    for s in 16 24 32 48 64 128 256; do
        [ -f "$HERE/icons/mint-hud-$s.png" ] || continue
        mkdir -p "$d/${s}x${s}/apps"
        cp "$HERE/icons/mint-hud-$s.png" "$d/${s}x${s}/apps/mint-hud.png"
    done
    gtk-update-icon-cache -f -t "$d" >/dev/null 2>&1 || true
    xdg-icon-resource forceupdate >/dev/null 2>&1 || true
}

# ---- write the application-menu entries ----------------------------------
write_menu_entries() {
    mkdir -p "$APPS"
    cat > "$APPS/mint-hud-settings.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Linux Mint HUD — Settings
Comment=Configure the desktop system panel (opening this also starts it)
Exec=$HERE/hud.py --settings
Icon=mint-hud
Terminal=false
Categories=Settings;
EOF
    cat > "$APPS/mint-hud-install.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Linux Mint HUD — Install / Update
Comment=Set up or update the desktop system panel
Exec=$HERE/install-gui.sh
Icon=mint-hud
Terminal=false
Categories=Settings;
EOF
    cat > "$APPS/mint-hud-uninstall.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Linux Mint HUD — Uninstall
Comment=Remove the desktop system panel
Exec=$HERE/uninstall.sh
Icon=mint-hud
Terminal=false
Categories=Settings;
EOF
    update-desktop-database "$APPS" >/dev/null 2>&1 || true
}

write_autostart() {
    mkdir -p "$(dirname "$AUTOSTART")"
    cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Linux Mint HUD
Comment=Live system stats panel on the desktop
Exec=$HERE/hud.py
Icon=$ICON
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=3
EOF
}

install_fonts() {
    # per-user, no root: pull the two .debs and unpack the font files
    local tmp; tmp="$(mktemp -d)"
    ( cd "$tmp" && apt-get download fonts-inter fonts-jetbrains-mono >/dev/null 2>&1 \
        && for d in *.deb; do dpkg-deb -x "$d" x; done )
    if compgen -G "$tmp/x" >/dev/null; then
        mkdir -p "$FONT_DIR"
        find "$tmp/x" \( -name '*.otf' -o -name '*.ttf' \) -exec cp {} "$FONT_DIR/" \;
        fc-cache -f "$FONT_DIR" >/dev/null 2>&1
    fi
    rm -rf "$tmp"
}

set_weather() {
    local town="$1"
    [ -z "$town" ] && return
    python3 - "$town" "$HERE/weather.json" <<'PY'
import json, sys, urllib.parse, urllib.request
town, out = sys.argv[1], sys.argv[2]
url = "https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + urllib.parse.quote(town)
try:
    r = json.load(urllib.request.urlopen(url, timeout=8))["results"][0]
    with open(out, "w") as f:
        json.dump({"lat": r["latitude"], "lon": r["longitude"], "name": r["name"]}, f)
except Exception:
    pass
PY
}

# ---- 1. pick what to set up ----------------------------------------------
CHOICES="$(zen --list --checklist --width=600 --height=460 \
    --title="Install Linux Mint HUD" \
    --text="Choose what to set up. Safe to run again any time to change things." \
    --column="" --column="Option" --column="id" --hide-column=3 --print-column=3 \
    TRUE  "Core components (required to run)"                 core \
    TRUE  "Nicer fonts (Inter + JetBrains Mono)"              fonts \
    TRUE  "Start automatically when you log in"               autostart \
    TRUE  "Add menu entries (Settings, Uninstall)"            menu \
    TRUE  "Show local weather in the top slot"                weather \
    TRUE  "Claude quota from Chrome / Brave / Edge browsers"  chromium \
    TRUE  "Full system power reading (Intel RAPL, optional)"  rapl)" || exit 0

has() { [[ "|$CHOICES|" == *"|$1|"* ]]; }

# weather city up front (it needs a text answer)
CITY=""
if has weather; then
    CITY="$(zen --entry --title="Weather" --width=380 \
        --text="Town or city to show the weather for:")" || CITY=""
fi

# ---- 2. the parts that need root, in a single password prompt ------------
APT=()
has core     && for p in python3-pil python3-gi python3-gi-cairo python3-cairo; do dpkg -s "$p" >/dev/null 2>&1 || APT+=("$p"); done
has chromium && for p in python3-cryptography gir1.2-secret-1;               do dpkg -s "$p" >/dev/null 2>&1 || APT+=("$p"); done
DO_RAPL=no
has rapl && compgen -G "/sys/class/powercap/intel-rapl:*" >/dev/null 2>&1 && DO_RAPL=yes

if [ ${#APT[@]} -gt 0 ] || [ "$DO_RAPL" = yes ]; then
    ROOT="$(mktemp)"
    {
        echo '#!/bin/bash'
        echo 'set -e'
        [ ${#APT[@]} -gt 0 ] && echo "apt-get update -qq || true" && echo "apt-get install -y ${APT[*]}"
        if [ "$DO_RAPL" = yes ]; then
            echo "cp '$HERE/99-rapl-psys.rules' /etc/udev/rules.d/"
            echo "udevadm trigger --subsystem-match=powercap --action=add --settle || true"
        fi
    } > "$ROOT"
    if ! pkexec bash "$ROOT"; then
        rm -f "$ROOT"
        err "The step that needs your password was cancelled or failed.\nNothing else was changed."
        exit 1
    fi
    rm -f "$ROOT"
fi

# ---- 3. the per-user parts, with a progress dialog -----------------------
(
    echo "10";  echo "# Setting things up…"
    if has fonts;     then echo "# Installing fonts…";        install_fonts; fi
    echo "55"
    if has weather;   then echo "# Fetching the weather…";    set_weather "$CITY"; fi
    echo "65"; echo "# Installing the icon…"; install_icons
    echo "72"
    if has autostart; then echo "# Enabling autostart…";      write_autostart; fi
    echo "85"
    if has menu;      then echo "# Adding menu entries…";     write_menu_entries; fi
    chmod +x "$HERE/hud.py" 2>/dev/null || true
    echo "100"; echo "# Done"
) | zen --progress --title="Installing Linux Mint HUD" --width=460 \
        --auto-close --no-cancel --percentage=0

# ---- 4. start it now ------------------------------------------------------
if ! pgrep -f "hud[.]py$" >/dev/null; then
    if zen --question --width=360 --title="Linux Mint HUD" \
           --text="All set. Start the panel now?"; then
        setsid "$HERE/hud.py" >/dev/null 2>&1 </dev/null &
        sleep 2
    fi
fi

zen --info --width=440 --title="Linux Mint HUD" \
    --text="Done. You can open <b>Linux Mint HUD — Settings</b> from the menu to configure it, and re-run this installer any time."
