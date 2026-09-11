#!/usr/bin/env bash
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
exec "$DEST/install-gui.sh"
