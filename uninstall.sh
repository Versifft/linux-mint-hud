#!/usr/bin/env bash
#
# Uninstaller for linux-mint-hud. Runs graphically (zenity) when launched from
# the menu, and as a plain terminal prompt otherwise. It stops the panel and
# removes what the installer added; deleting the app folder (with your settings)
# is offered separately and off by default.
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="$HOME/.local/share/applications"
AUTOSTART="$HOME/.config/autostart/mint-hud.desktop"
RULE="/etc/udev/rules.d/99-rapl-psys.rules"

GUI=no
[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v zenity >/dev/null 2>&1 && GUI=yes

stop_panel()   { pkill -f "hud[.]py$" 2>/dev/null; pkill -f "hud[.]py --setting" 2>/dev/null; true; }
rm_autostart() { rm -f "$AUTOSTART"; }
rm_menu()      { rm -f "$APPS"/mint-hud-*.desktop; update-desktop-database "$APPS" >/dev/null 2>&1 || true; }
rm_icons()     { rm -f "$HOME/.local/share/icons/hicolor/"*/apps/mint-hud.png 2>/dev/null; gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true; }
rm_rule()      { [ -f "$RULE" ] && pkexec rm -f "$RULE"; }
rm_app()       { rm -rf "$HERE"; }

# ------------------------------------------------------------------ graphical
if [ "$GUI" = yes ]; then
    zenity --question --width=420 --title="Uninstall Linux Mint HUD" \
        --text="Remove Linux Mint HUD?\n\nThis stops the panel and removes its autostart and menu entries." \
        2>/dev/null || exit 0

    CHOICES="$(zenity --list --checklist --width=560 --height=340 \
        --title="Uninstall Linux Mint HUD" \
        --text="Extra things to remove (optional):" \
        --column="" --column="Also remove" --column="id" --hide-column=3 --print-column=3 \
        FALSE "The system-power udev rule (needs your password)"        rule \
        FALSE "The app folder and your settings (cannot be undone)"     app \
        2>/dev/null)" || CHOICES=""
    has() { [[ "|$CHOICES|" == *"|$1|"* ]]; }

    stop_panel
    rm_autostart
    rm_menu
    rm_icons
    has rule && rm_rule
    msg="Linux Mint HUD has been removed.\n\nThe fonts and any installed packages were left in place."
    if has app; then
        rm_app
        zenity --info --width=420 --title="Linux Mint HUD" \
            --text="$msg\n\nThe app folder was deleted." 2>/dev/null
    else
        zenity --info --width=440 --title="Linux Mint HUD" \
            --text="$msg\n\nThe app folder is still at:\n$HERE\nDelete it by hand if you want it gone." 2>/dev/null
    fi
    exit 0
fi

# ------------------------------------------------------------------- terminal
bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ask()  { local r; read -r -p "$1 [y/N] " r </dev/tty; [[ "$r" =~ ^[Yy] ]]; }

bold "Uninstall linux-mint-hud"
ask "Stop the panel and remove autostart + menu entries?" || exit 0
stop_panel; rm_autostart; rm_menu; rm_icons
echo "  removed autostart, menu entries and icon."
[ -f "$RULE" ] && ask "Remove the system-power udev rule (needs sudo)?" && rm_rule
if ask "Also delete the app folder and your settings ($HERE)?"; then
    rm_app; echo "  deleted $HERE"
else
    echo "  left the app folder at $HERE"
fi
bold "Done."
