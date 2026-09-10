#!/usr/bin/env bash
#
# One-line bootstrap: clone the repo and run the guided installer.
#   bash <(curl -fsSL https://raw.githubusercontent.com/Versifft/linux-mint-hud/main/bootstrap.sh)
#
set -uo pipefail

REPO="https://github.com/Versifft/linux-mint-hud.git"
DEST="${1:-$HOME/.config/mint-hud}"

if ! command -v git >/dev/null; then
    echo "git is required. Install it with:  sudo apt install git" >&2
    exit 1
fi

if [ -d "$DEST/.git" ]; then
    echo "Already cloned at $DEST — updating."
    git -C "$DEST" pull --ff-only
else
    echo "Cloning into $DEST"
    git clone --depth 1 "$REPO" "$DEST"
fi

chmod +x "$DEST"/*.sh "$DEST/hud.py" 2>/dev/null || true
# Prefer the graphical installer; it falls back to the terminal one itself when
# there's no desktop session or no zenity.
exec "$DEST/install-gui.sh"
