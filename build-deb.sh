#!/usr/bin/env bash
#
# Build a .deb of linux-mint-hud that installs system-wide under /usr, so it can
# be installed with a double-click (GDebi / Software Installer) and removed
# through the normal package manager. Code goes read-only under /usr; each
# user's settings/cache live in ~/.config/mint-hud.
#
#   ./build-deb.sh            -> dist/linux-mint-hud_<version>_all.deb
#
set -euo pipefail
umask 022                      # so packaged dirs are 0755, not group-writable

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="1.0.6"
PKG="linux-mint-hud"
STAGE="$HERE/build/$PKG"
OUT="$HERE/dist"

rm -rf "$HERE/build"
mkdir -p "$STAGE" "$OUT"

APPDIR="$STAGE/usr/share/mint-hud"
BINDIR="$STAGE/usr/bin"
DESKDIR="$STAGE/usr/share/applications"
AUTODIR="$STAGE/etc/xdg/autostart"
DOCDIR="$STAGE/usr/share/doc/$PKG"
ICONROOT="$STAGE/usr/share/icons/hicolor"
mkdir -p "$APPDIR" "$BINDIR" "$DESKDIR" "$AUTODIR" "$DOCDIR" "$STAGE/DEBIAN"

# ---- application code (read-only) ----------------------------------------
for f in hud.py weather.py claude_quota.py browser_cookie.py chromium_cookies.py \
         99-rapl-psys.rules; do
    install -m 0644 "$HERE/$f" "$APPDIR/$f"
done
chmod 0755 "$APPDIR/hud.py" "$APPDIR/weather.py" "$APPDIR/claude_quota.py"

# ---- launcher ------------------------------------------------------------
cat > "$BINDIR/mint-hud" <<'EOF'
#!/bin/sh
exec python3 /usr/share/mint-hud/hud.py "$@"
EOF
chmod 0755 "$BINDIR/mint-hud"

# ---- icons (into the system hicolor theme + a pixmaps fallback) ----------
for s in 16 24 32 48 64 128 256; do
    [ -f "$HERE/icons/mint-hud-$s.png" ] || continue
    install -D -m 0644 "$HERE/icons/mint-hud-$s.png" \
        "$ICONROOT/${s}x${s}/apps/mint-hud.png"
done
# a plain /usr/share/pixmaps copy resolves Icon=mint-hud even without an icon
# cache, so the menu shows it regardless of theme-cache timing
install -D -m 0644 "$HERE/icons/mint-hud-128.png" "$STAGE/usr/share/pixmaps/mint-hud.png"

# ---- menu entry ----------------------------------------------------------
# One entry only: opening it configures the panel and also starts the panel
# if it isn't running (see run_settings), so there's no need for a separate
# "launch the panel" item. The panel itself autostarts on login.
cat > "$DESKDIR/mint-hud-settings.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Linux Mint HUD — Settings
Comment=Configure the desktop system panel (opening this also starts it)
Exec=mint-hud --settings
Icon=mint-hud
Terminal=false
Categories=Settings;
EOF

# ---- autostart (per login session, runs as the user) ---------------------
cat > "$AUTODIR/mint-hud.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Linux Mint HUD
Comment=Live system-stats panel on the desktop
Exec=mint-hud
Icon=mint-hud
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=3
EOF

# ---- APT source for updates ----------------------------------------------
# Ship our signed repository as an apt source + keyring, so once installed the
# app updates through the normal Update Manager whenever a new version is
# published. The key is our repo-signing public key (armored in packaging/).
mkdir -p "$STAGE/etc/apt/keyrings" "$STAGE/etc/apt/sources.list.d"
gpg --dearmor < "$HERE/packaging/mint-hud-archive-keyring.asc" \
    > "$STAGE/etc/apt/keyrings/linux-mint-hud.gpg"
chmod 0644 "$STAGE/etc/apt/keyrings/linux-mint-hud.gpg"
cat > "$STAGE/etc/apt/sources.list.d/linux-mint-hud.list" <<EOF
# linux-mint-hud updates — signed repo on GitHub Pages
deb [signed-by=/etc/apt/keyrings/linux-mint-hud.gpg] https://versifft.github.io/linux-mint-hud stable main
EOF

# ---- copyright -----------------------------------------------------------
cat > "$DOCDIR/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: linux-mint-hud
Source: https://github.com/Versifft/linux-mint-hud

Files: *
Copyright: 2026 Versifft
License: MIT
EOF

# ---- control + maintainer scripts ----------------------------------------
INSTALLED_KB=$(du -sk "$STAGE/usr" "$STAGE/etc" 2>/dev/null | awk '{s+=$1} END{print s}')
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Maintainer: Versifft <versifft@users.noreply.github.com>
Depends: python3, python3-pil, python3-gi, python3-gi-cairo, python3-cairo
Recommends: fonts-inter, fonts-jetbrains-mono, python3-cryptography, gir1.2-secret-1
Installed-Size: ${INSTALLED_KB:-2000}
Homepage: https://github.com/Versifft/linux-mint-hud
Description: Live system-stats panel for the Linux Mint desktop
 A translucent panel that lives on the desktop showing CPU/GPU/RAM ring gauges,
 temperatures, memory, disk, network, power and battery, top processes, and an
 optional Claude-usage / weather slot. It is configured from a graphical
 settings window, sits below your windows on every workspace, and clicks fall
 through it to the desktop. Settings and cache are kept per-user under
 ~/.config/mint-hud.
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
    prev="${2:-}"        # previous version on an upgrade; empty on a fresh install
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor >/dev/null 2>&1 || true
    update-desktop-database -q >/dev/null 2>&1 || true
    # Nudge the menu to re-read the entry now that the icon cache is fresh: the
    # Cinnamon/GNOME menus watch the applications dir and can cache "no icon" if
    # they read the .desktop in the moment between dpkg unpacking it and the
    # icon cache updating. Touching it fires the file monitor again, this time
    # with the icon already resolvable.
    touch /usr/share/applications/mint-hud-settings.desktop 2>/dev/null || true
    update-desktop-database -q >/dev/null 2>&1 || true

    # After an upgrade we want the app to immediately run the new code, not the
    # old build still resident in memory. So we stop any running panel (and the
    # Settings window, if open) and relaunch the panel. On a fresh install the
    # panel also opens (its first run opens Settings); on an upgrade we relaunch
    # only if it was already running, so we never pop it open on someone who
    # had deliberately closed it.
    #
    # The target user is the active graphical login session, so it works however
    # the package was installed — GDebi/pkexec, apt/sudo, or the Software Manager
    # (which sets no PKEXEC_UID/SUDO_UID). We copy that session's
    # DISPLAY/XAUTHORITY/DBUS from one of its live processes and launch with
    # systemd-run, which runs the panel in its own transient scope, fully
    # decoupled from dpkg's process tree and pipes — a GUI started directly from
    # a maintainer script inherits apt's status pipe and hangs the whole install.
    # If no live session is found, the autostart entry opens it on next login.
    if command -v systemd-run >/dev/null 2>&1 && command -v loginctl >/dev/null 2>&1; then
        uid=""
        for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do
            [ "$(loginctl show-session "$s" -p Active --value 2>/dev/null)" = yes ] || continue
            case "$(loginctl show-session "$s" -p Type --value 2>/dev/null)" in
                x11|wayland) uid="$(loginctl show-session "$s" -p User --value 2>/dev/null)"; break ;;
            esac
        done
        if [ -n "$uid" ] && [ "$uid" -ge 1000 ] 2>/dev/null; then
            uname="$(getent passwd "$uid" | cut -d: -f1)"
            pid=""
            for p in cinnamon-session nemo-desktop cinnamon mate-session xfce4-session gnome-session; do
                pid="$(pgrep -u "$uid" -x "$p" 2>/dev/null | head -1)"
                [ -n "$pid" ] && break
            done
            [ -n "$pid" ] || pid="$(pgrep -u "$uid" 2>/dev/null | head -1)"
            disp=":0"; xauth="/home/$uname/.Xauthority"; dbus=""
            # A sensible default PATH that includes the user's ~/.local/bin —
            # systemd-run otherwise starts with a bare PATH, and the Claude slot
            # shells out to the `claude` CLI which usually lives there.
            upath="/home/$uname/.local/bin:/usr/local/bin:/usr/bin:/bin"
            if [ -n "$pid" ] && [ -r "/proc/$pid/environ" ]; then
                v="$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n 's/^DISPLAY=//p' | head -1)"; [ -n "$v" ] && disp="$v"
                v="$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n 's/^XAUTHORITY=//p' | head -1)"; [ -n "$v" ] && xauth="$v"
                dbus="$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n 's/^DBUS_SESSION_BUS_ADDRESS=//p' | head -1)"
                v="$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n 's/^PATH=//p' | head -1)"; [ -n "$v" ] && upath="$v"
            fi

            # Stop the running panel + Settings window. The [.] keeps the pattern
            # from matching our own pkill/pgrep command line. Give them a moment
            # to exit (releasing the panel's single-instance lock) before we
            # escalate, so the relaunch below can actually acquire it.
            was_running=no
            if pkill -TERM -u "$uid" -f 'mint-hud/hud[.]py' 2>/dev/null; then
                was_running=yes
                _i=0
                while [ "$_i" -lt 5 ] && pgrep -u "$uid" -f 'mint-hud/hud[.]py' >/dev/null 2>&1; do
                    sleep 1; _i=$((_i + 1))
                done
                pkill -KILL -u "$uid" -f 'mint-hud/hud[.]py' 2>/dev/null || true
                sleep 1
            fi

            # Relaunch the panel: always on a fresh install, on an upgrade only
            # if it had been running.
            if [ -z "$prev" ] || [ "$was_running" = yes ]; then
                set -- --collect --quiet --uid="$uid" \
                       --setenv=DISPLAY="$disp" --setenv=XAUTHORITY="$xauth" \
                       --setenv=XDG_RUNTIME_DIR="/run/user/$uid" \
                       --setenv=PATH="$upath"
                [ -n "$dbus" ] && set -- "$@" --setenv=DBUS_SESSION_BUS_ADDRESS="$dbus"
                systemd-run "$@" /usr/bin/mint-hud >/dev/null 2>&1 || true
            fi
        fi
    fi
fi
exit 0
EOF

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = purge ]; then
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor >/dev/null 2>&1 || true
    update-desktop-database -q >/dev/null 2>&1 || true
fi
exit 0
EOF
chmod 0755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

# ---- build ---------------------------------------------------------------
DEB="$OUT/${PKG}_${VERSION}_all.deb"
if dpkg-deb --build --root-owner-group "$STAGE" "$DEB" 2>/dev/null; then
    :
else
    # older dpkg-deb without --root-owner-group
    dpkg-deb --build "$STAGE" "$DEB"
fi

echo "built: $DEB"
command -v lintian >/dev/null 2>&1 && lintian --no-tag-display-limit "$DEB" 2>/dev/null || true
