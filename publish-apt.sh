#!/usr/bin/env bash
#
# Build a signed APT repository under apt/ from the current .deb, so it can be
# served statically (GitHub Pages) and picked up by the Mint Update Manager.
# Layout (served at the Pages root):
#
#   apt/dists/stable/{Release,InRelease,Release.gpg}
#   apt/dists/stable/main/binary-all/{Packages,Packages.gz}
#   apt/pool/main/linux-mint-hud_<version>_all.deb
#   apt/key.asc                      (public signing key)
#
# The repo is signed with the "Linux Mint HUD repository" GPG key in the local
# keyring. Run build-deb.sh first (this calls it).
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$HERE/apt"
SUITE="stable"
ORIGIN="linux-mint-hud"
KEYID="$(gpg --list-keys --with-colons 'Linux Mint HUD repository' 2>/dev/null | awk -F: '/^fpr/{print $10; exit}')"

[ -n "$KEYID" ] || { echo "no 'Linux Mint HUD repository' signing key in the keyring" >&2; exit 1; }

# 1. build the .deb
"$HERE/build-deb.sh" >/dev/null
DEB="$(ls -t "$HERE"/dist/linux-mint-hud_*_all.deb | head -1)"
echo "packaging $(basename "$DEB")"

# 2. lay out the repo
rm -rf "$REPO"
mkdir -p "$REPO/pool/main" "$REPO/dists/$SUITE/main/binary-all"
cp "$DEB" "$REPO/pool/main/"
cp "$HERE/packaging/mint-hud-archive-keyring.asc" "$REPO/key.asc"

cd "$REPO"

# 3. Packages index (Filename is relative to the repo root, e.g. pool/main/…)
apt-ftparchive packages pool > dists/$SUITE/main/binary-all/Packages
gzip -9kf dists/$SUITE/main/binary-all/Packages

# 4. Release (describe the suite), then sign it
apt-ftparchive \
    -o APT::FTPArchive::Release::Origin="$ORIGIN" \
    -o APT::FTPArchive::Release::Label="$ORIGIN" \
    -o APT::FTPArchive::Release::Suite="$SUITE" \
    -o APT::FTPArchive::Release::Codename="$SUITE" \
    -o APT::FTPArchive::Release::Components="main" \
    -o APT::FTPArchive::Release::Architectures="all" \
    release "dists/$SUITE" > "dists/$SUITE/Release"

# GPG_PASS (optional) lets non-interactive/CI runs sign a passphrase-protected
# key via loopback pinentry; unset locally, gpg-agent handles the passphrase.
GPG_ARGS=(--batch --yes --local-user "$KEYID")
[ -n "${GPG_PASS:-}" ] && GPG_ARGS+=(--pinentry-mode loopback --passphrase "$GPG_PASS")
gpg "${GPG_ARGS[@]}" --clearsign -o "dists/$SUITE/InRelease" "dists/$SUITE/Release"
gpg "${GPG_ARGS[@]}" -abs      -o "dists/$SUITE/Release.gpg" "dists/$SUITE/Release"

# GitHub Pages skips folders that contain a Jekyll-unfriendly name; disable it.
touch "$REPO/.nojekyll"

echo "built signed apt repo at $REPO (key ${KEYID: -8})"
echo "sources line:"
echo "  deb [signed-by=/etc/apt/keyrings/linux-mint-hud.gpg] https://versifft.github.io/linux-mint-hud stable main"

# 5. publish to the gh-pages branch (what GitHub Pages serves). Pass --push.
#    The repo contents become the branch root. We force-push a single fresh
#    commit (the apt index is fully regenerated each time, so history is noise).
#    The GPG *private* key never leaves this machine — signing happened above.
if [ "${1:-}" = --push ]; then
    REMOTE="$(git -C "$HERE" remote get-url origin 2>/dev/null \
              || echo https://github.com/Versifft/linux-mint-hud.git)"
    WT="$(mktemp -d)"
    ( cd "$WT"
      cp -a "$REPO/." .
      git init -q
      git checkout -q -b gh-pages
      git add -A
      git -c user.email="versifft@users.noreply.github.com" \
          -c user.name="Versifft" \
          commit -q -m "apt repo: ${PKG:-linux-mint-hud} $(basename "$DEB" | sed -E 's/.*_([0-9.]+)_.*/\1/')"
      git remote add origin "$REMOTE"
      git push -f -q origin gh-pages )
    rm -rf "$WT"
    echo "pushed gh-pages to $REMOTE — live at https://versifft.github.io/linux-mint-hud"
else
    echo "(dry run — re-run with --push to publish to gh-pages / GitHub Pages)"
fi
