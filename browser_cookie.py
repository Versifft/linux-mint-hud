#!/usr/bin/env python3
"""Loads the claude.ai session cookie from a running browser.

Tries Firefox and its forks first, then Chromium-family browsers, then the
last cached copy. Firefox first because reading it is a plain sqlite copy;
Chromium needs the desktop keyring and the cryptography library, so it is only
reached when Firefox yields nothing.

Firefox rolls the claude.ai session cookie forward every time the site is
visited (expiry is a rolling ~30 days), so taking it from the live browser
profile means it never goes stale — as long as you stay logged in in Firefox.
That replaces the manual DevTools/HAR export the setup used to need.

Run standalone to write ~/.config/mint-hud/.claude_web_cookie; claude_quota.py
imports load_cookie() and calls it on every fetch.
"""
import configparser
import glob
import os
import shutil
import sqlite3
import sys
import tempfile

HOME = os.path.expanduser("~")

# Firefox and its forks all keep the same profile layout, so they all work.
# Chromium-family browsers do not: their cookie store is encrypted against a
# key in the desktop keyring, which is a different job entirely.
BROWSER_DIRS = (
    ".mozilla/firefox",
    ".librewolf",
    ".waterfox",
    ".floorp",
    ".var/app/org.mozilla.firefox/.mozilla/firefox",     # flatpak
    "snap/firefox/common/.mozilla/firefox",              # snap
)
COOKIE_FILE = os.path.join(HOME, ".config/mint-hud/.claude_web_cookie")

# Cloudflare's cf_clearance is bound to the User-Agent that obtained it, so the
# request has to introduce itself as the same Firefox the cookies came from.
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64; rv:150.0) "
              "Gecko/20100101 Firefox/150.0")


def firefox_profile():
    """A browser profile holding a cookies.sqlite, or None if there is none.

    Tries each known Firefox-family location: the default profile named in
    profiles.ini first, then any *.default-release or *.default beside it."""
    for base in BROWSER_DIRS:
        root = os.path.join(HOME, base)
        if not os.path.isdir(root):
            continue
        ini = os.path.join(root, "profiles.ini")
        if os.path.exists(ini):
            cp = configparser.ConfigParser()
            try:
                cp.read(ini)
                for sec in (x for x in cp.sections() if x.startswith("Install")):
                    path = cp[sec].get("Default")
                    if path:
                        p = path if os.path.isabs(path) else os.path.join(root, path)
                        if os.path.exists(os.path.join(p, "cookies.sqlite")):
                            return p
            except Exception:
                pass
        for pat in ("*.default-release", "*.default"):
            for p in sorted(glob.glob(os.path.join(root, pat))):
                if os.path.exists(os.path.join(p, "cookies.sqlite")):
                    return p
    return None


def read_cookies(profile):
    """Snapshot the store and read it. The -wal file must come along: Firefox
    writes in WAL mode, so recent cookie updates live there and copying only
    cookies.sqlite hands you a stale snapshot. Copying rather than opening in
    place also avoids fighting Firefox for the lock while it's running."""
    src = os.path.join(profile, "cookies.sqlite")
    with tempfile.TemporaryDirectory() as tmp:
        dst = os.path.join(tmp, "cookies.sqlite")
        shutil.copy2(src, dst)
        for ext in ("-wal", "-shm"):
            if os.path.exists(src + ext):
                shutil.copy2(src + ext, dst + ext)
        con = sqlite3.connect(dst)
        try:
            rows = con.execute(
                "select name, value from moz_cookies "
                "where host = 'claude.ai' or host like '%.claude.ai'"
            ).fetchall()
        finally:
            con.close()
    return rows


def build_header(rows):
    pairs = [f"{n}={v}" for n, v in rows if v]
    return "; ".join(pairs)


def _rows_from_chromium():
    try:
        import chromium_cookies
        return chromium_cookies.read_cookies()
    except Exception:
        return []


def load_cookie(write_cache=True):
    """Current cookie header. Returns (cookie, source), source one of
    'firefox', 'chromium', 'cache', 'none'.

    A header without sessionKey is worthless, so a browser that yields cookies
    but no session key is treated as a miss and the next source gets its turn."""
    profile = firefox_profile()
    if profile:
        try:
            rows = read_cookies(profile)
            if any(n == "sessionKey" for n, _ in rows):
                header = build_header(rows)
                if write_cache:
                    save_cookie(header)
                return header, "firefox"
        except Exception:
            pass

    rows = _rows_from_chromium()
    if any(n == "sessionKey" for n, _ in rows):
        header = build_header(rows)
        if write_cache:
            save_cookie(header)
        return header, "chromium"

    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE) as f:
            cached = f.read().strip()
        if cached:
            return cached, "cache"
    return None, "none"


def any_profile():
    """True if any supported browser has a profile at all — used to tell
    'log in' from 'no browser here'."""
    if firefox_profile():
        return True
    try:
        import chromium_cookies
        return chromium_cookies.find_store()[0] is not None
    except Exception:
        return False


def save_cookie(header):
    tmp = f"{COOKIE_FILE}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(header + "\n")
    os.replace(tmp, COOKIE_FILE)


if __name__ == "__main__":
    cookie, source = load_cookie()
    if not cookie:
        print("no claude.ai cookies found — is a browser logged in to claude.ai?",
              file=sys.stderr)
        sys.exit(1)
    names = [p.split("=", 1)[0] for p in cookie.split("; ")]
    print(f"source: {source}")
    print(f"cookies: {len(names)} ({', '.join(sorted(names))})")
    print(f"sessionKey present: {'sessionKey' in names}")
    print(f"written to {COOKIE_FILE}")
