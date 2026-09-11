#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$HERE/apt"
SUITE="stable"
ORIGIN="linux-mint-hud"
KEYID="$(gpg --list-keys --with-colons 'Linux Mint HUD repository' 2>/dev/null | awk -F: '/^fpr/{print $10; exit}')"

[ -n "$KEYID" ] || { echo "no 'Linux Mint HUD repository' signing key in the keyring" >&2; exit 1; }

"$HERE/build-deb.sh" >/dev/null
DEB="$(ls -t "$HERE"/dist/linux-mint-hud_*_all.deb | head -1)"
echo "packaging $(basename "$DEB")"

rm -rf "$REPO"
mkdir -p "$REPO/pool/main" "$REPO/dists/$SUITE/main/binary-all"
cp "$DEB" "$REPO/pool/main/"
cp "$HERE/packaging/mint-hud-archive-keyring.asc" "$REPO/key.asc"

cd "$REPO"

apt-ftparchive packages pool > dists/$SUITE/main/binary-all/Packages
gzip -9kf dists/$SUITE/main/binary-all/Packages

apt-ftparchive \
    -o APT::FTPArchive::Release::Origin="$ORIGIN" \
    -o APT::FTPArchive::Release::Label="$ORIGIN" \
    -o APT::FTPArchive::Release::Suite="$SUITE" \
    -o APT::FTPArchive::Release::Codename="$SUITE" \
    -o APT::FTPArchive::Release::Components="main" \
    -o APT::FTPArchive::Release::Architectures="all" \
    release "dists/$SUITE" > "dists/$SUITE/Release"

GPG_ARGS=(--batch --yes --local-user "$KEYID")
[ -n "${GPG_PASS:-}" ] && GPG_ARGS+=(--pinentry-mode loopback --passphrase "$GPG_PASS")
gpg "${GPG_ARGS[@]}" --clearsign -o "dists/$SUITE/InRelease" "dists/$SUITE/Release"
gpg "${GPG_ARGS[@]}" -abs      -o "dists/$SUITE/Release.gpg" "dists/$SUITE/Release"

touch "$REPO/.nojekyll"

echo "built signed apt repo at $REPO (key ${KEYID: -8})"
echo "sources line:"
echo "  deb [signed-by=/etc/apt/keyrings/linux-mint-hud.gpg] https://versifft.github.io/linux-mint-hud stable main"

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
