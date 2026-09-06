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

exec "$DEST/install.sh"
