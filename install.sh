#!/usr/bin/env bash
#
# Guided installer for linux-mint-hud. Asks before it touches anything, skips
# what is already in place, and can be re-run safely.
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOSTART="$HOME/.config/autostart/mint-hud.desktop"
FONT_DIR="$HOME/.local/share/fonts"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '  %s\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }

# ask "question" default(y/n) -> returns 0 for yes
ask() {
    local q="$1" def="${2:-y}" reply hint="[Y/n]"
    [ "$def" = n ] && hint="[y/N]"
    read -r -p "$q $hint " reply </dev/tty
    reply="${reply:-$def}"
    [[ "$reply" =~ ^[Yy] ]]
}

need_sudo() {
    if ! sudo -n true 2>/dev/null; then
        info "The next step needs root; you'll be asked for your password."
    fi
}

bold "linux-mint-hud installer"
info "Installing from: $HERE"
echo

# ---- 1. core dependencies ------------------------------------------------
CORE=(python3-pil python3-gi python3-gi-cairo python3-cairo)
missing=()
for p in "${CORE[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
done

if [ ${#missing[@]} -eq 0 ]; then
    ok "Core dependencies already present."
else
    bold "Core dependencies"
    info "These are required: ${missing[*]}"
    if ask "Install them now?"; then
        need_sudo
        sudo apt-get install -y "${missing[@]}" && ok "Installed."
    else
        info "Skipped — the panel will not run without them."
    fi
fi
echo

# ---- 2. optional: Chromium quota support ---------------------------------
EXTRA=(python3-cryptography gir1.2-secret-1)
extra_missing=()
for p in "${EXTRA[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || extra_missing+=("$p")
done

if [ ${#extra_missing[@]} -gt 0 ]; then
    bold "Claude quota from a Chromium browser (optional)"
    info "The Claude usage bars read a cookie from your browser. Firefox works"
    info "with the core packages above; Chrome, Brave and Edge additionally need:"
    info "  ${extra_missing[*]}"
    info "Skip this if you use Firefox, or don't use the Claude section at all."
    if ask "Install Chromium cookie support?" n; then
        need_sudo
        sudo apt-get install -y "${extra_missing[@]}" && ok "Installed."
    else
        info "Skipped."
    fi
    echo
fi

# ---- 2b. weather location -----------------------------------------------
if [ ! -f "$HERE/weather.json" ]; then
    bold "Weather"
    info "When Claude quota is unavailable the top slot can show local weather"
    info "instead (from open-meteo, no API key). Set a location to enable it."
    if ask "Set a weather location?" n; then
        read -r -p "  Town or city: " town </dev/tty
        if [ -n "$town" ]; then
            # geocode via open-meteo, no key, then store only the coordinates
            python3 - "$town" "$HERE/weather.json" <<'PY'
import json, sys, urllib.parse, urllib.request
town, out = sys.argv[1], sys.argv[2]
url = "https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + urllib.parse.quote(town)
try:
    d = json.load(urllib.request.urlopen(url, timeout=8))
    r = d["results"][0]
    with open(out, "w") as f:
        json.dump({"lat": r["latitude"], "lon": r["longitude"], "name": r["name"]}, f)
    print(f"  set: {r['name']}, {r.get('admin1','')} {r['country_code']}")
except Exception:
    print("  couldn't find that place — weather stays off (edit weather.json later)")
PY
        else
            info "Skipped."
        fi
    else
        info "Skipped — no weather shown."
    fi
    echo
fi

# ---- 3. fonts ------------------------------------------------------------
bold "Fonts"
# capture once and match with here-strings: piping fc-list into grep -q makes
# grep close the pipe early, fc-list dies on SIGPIPE, and under pipefail the
# whole test then reports failure even when the font is present
installed_fonts="$(fc-list)"
if grep -qi "JetBrains Mono" <<<"$installed_fonts" \
   && grep -qi "Inter:" <<<"$installed_fonts"; then
    ok "Inter and JetBrains Mono are already installed."
else
    info "The panel is designed for Inter (labels) and JetBrains Mono (values)."
    info "Without them it falls back to DejaVu — it works, it just looks worse."
    info "They install per-user, no root needed."
    if ask "Download and install them?"; then
        tmp="$(mktemp -d)"
        ( cd "$tmp" \
          && apt-get download fonts-inter fonts-jetbrains-mono \
          && for d in *.deb; do dpkg-deb -x "$d" x; done )
        if compgen -G "$tmp/x" >/dev/null; then
            mkdir -p "$FONT_DIR"
            find "$tmp/x" \( -name '*.otf' -o -name '*.ttf' \) -exec cp {} "$FONT_DIR/" \;
            fc-cache -f "$FONT_DIR" >/dev/null 2>&1
            ok "Fonts installed to $FONT_DIR"
        else
            info "Download failed — leaving fonts as they are."
        fi
        rm -rf "$tmp"
    else
        info "Skipped — DejaVu fallback will be used."
    fi
fi
echo

# ---- 4. system power reading (optional, security tradeoff) ---------------
if compgen -G "/sys/class/powercap/intel-rapl:*" >/dev/null 2>&1; then
    bold "Full system power reading (optional)"
    info "On mains the battery reports nothing, so the panel can't show draw."
    info "Intel RAPL can, but it is root-only by default for a reason: fine-"
    info "grained energy readings are a known side channel (CVE-2020-8694)."
    info "This grants read access to ONLY the whole-board 'psys' domain, which"
    info "is the least sensitive one — only the whole-board psys domain,"
    info "not the fine-grained CPU counters those attacks actually use."
    if ask "Install the udev rule for it?" n; then
        need_sudo
        sudo cp "$HERE/99-rapl-psys.rules" /etc/udev/rules.d/ \
          && sudo udevadm trigger --subsystem-match=powercap --action=add --settle \
          && ok "Rule installed and applied."
    else
        info "Skipped — the panel shows battery draw only."
    fi
    echo
fi

# ---- 5. autostart --------------------------------------------------------
bold "Start on login"
if [ -f "$AUTOSTART" ] && grep -q "$HERE/hud.py" "$AUTOSTART" 2>/dev/null; then
    ok "Autostart already points here."
elif ask "Start the panel automatically on login?"; then
    mkdir -p "$(dirname "$AUTOSTART")"
    cat > "$AUTOSTART" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Linux Mint HUD
Comment=Live system stats panel on the desktop
Exec=$HERE/hud.py
Icon=utilities-system-monitor
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=3
DESKTOP
    ok "Autostart written to $AUTOSTART"
else
    info "Skipped."
fi
echo

# ---- 5b. application menu entries ----------------------------------------
bold "Menu entries"
APPS="$HOME/.local/share/applications"
info "These add 'Linux Mint HUD — Settings' (the graphical settings) and an"
info "uninstaller to your application menu, so you don't need a terminal."
if ask "Add menu entries?"; then
    mkdir -p "$APPS"
    cat > "$APPS/mint-hud-settings.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Linux Mint HUD — Settings
Comment=Configure the desktop system panel
Exec=$HERE/hud.py --settings
Icon=preferences-desktop
Terminal=false
Categories=Settings;
EOF
    cat > "$APPS/mint-hud-install.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Linux Mint HUD — Install / Update
Comment=Set up or update the desktop system panel
Exec=$HERE/install-gui.sh
Icon=system-software-install
Terminal=false
Categories=Settings;
EOF
    cat > "$APPS/mint-hud-uninstall.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Linux Mint HUD — Uninstall
Comment=Remove the desktop system panel
Exec=$HERE/uninstall.sh
Icon=edit-delete
Terminal=false
Categories=Settings;
EOF
    chmod +x "$HERE/install-gui.sh" "$HERE/uninstall.sh" 2>/dev/null || true
    update-desktop-database "$APPS" >/dev/null 2>&1 || true
    ok "Menu entries added."
else
    info "Skipped."
fi
echo

# ---- 6. start now --------------------------------------------------------
if pgrep -f "hud[.]py$" >/dev/null; then
    ok "The panel is already running."
elif ask "Start it now?"; then
    chmod +x "$HERE/hud.py"
    setsid "$HERE/hud.py" >/dev/null 2>&1 </dev/null &
    sleep 2
    pgrep -f "hud[.]py$" >/dev/null && ok "Started." || info "Didn't come up — run $HERE/hud.py by hand to see why."
fi

echo
bold "Done."
info "Started from $HERE"
