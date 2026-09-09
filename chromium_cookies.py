#!/usr/bin/env python3

import os
import sqlite3
import shutil
import tempfile

CHROMIUM_DIRS = (
    ".config/chromium",
    ".config/google-chrome",
    ".config/BraveSoftware/Brave-Browser",
    ".config/microsoft-edge",
    ".config/vivaldi",
    ".var/app/org.chromium.Chromium/config/chromium",
)


def _keyring_password(app_hint):
    """The browser's own encryption password from the keyring, or None."""
    try:
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
    except Exception:
        return None
    schemas = ("chrome_libsecret_os_crypt_password_v2",
               "chrome_libsecret_os_crypt_password_v1",
               "chrome_libsecret_password_v2",
               "chrome_libsecret_password_v1")
    apps = (app_hint, "chromium", "chrome")
    for sname in schemas:
        schema = Secret.Schema.new(sname, Secret.SchemaFlags.DONT_MATCH_NAME,
                                   {"application": Secret.SchemaAttributeType.STRING})
        for app in apps:
            try:
                pw = Secret.password_lookup_sync(schema, {"application": app}, None)
            except Exception:
                pw = None
            if pw:
                return pw.encode("utf-8")
    return None


def _derive(password):
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    return PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt",
                      iterations=1).derive(password)


def _decrypt(blob, key_v10, key_v11):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    prefix = blob[:3]
    key = key_v11 if prefix == b"v11" else key_v10
    if key is None or prefix not in (b"v10", b"v11"):
        return None
    dec = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    plain = dec.update(blob[3:]) + dec.finalize()
    if not plain:
        return None
    pad = plain[-1]
    if 1 <= pad <= 16:
        plain = plain[:-pad]
    if plain and not (0x20 <= plain[0] <= 0x7e):
        plain = plain[32:]
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


def find_store():
    """(cookie-db path, app hint) for the first Chromium profile found."""
    home = os.path.expanduser("~")
    for base in CHROMIUM_DIRS:
        for prof in ("Default", "Profile 1"):
            path = os.path.join(home, base, prof, "Cookies")
            if os.path.exists(path):
                return path, os.path.basename(base).lower()
    return None, None


def read_cookies():
    """[(name, value), ...] for claude.ai, or [] if nothing is readable."""
    store, hint = find_store()
    if not store:
        return []
    key_v11 = None
    pw = _keyring_password(hint)
    if pw is not None:
        try:
            key_v11 = _derive(pw)
        except Exception:
            key_v11 = None
    key_v10 = _derive(b"peanuts")

    with tempfile.TemporaryDirectory() as tmp:
        dst = os.path.join(tmp, "Cookies")
        shutil.copy2(store, dst)
        con = sqlite3.connect(dst)
        try:
            rows = con.execute(
                "select name, encrypted_value from cookies "
                "where host_key = 'claude.ai' or host_key like '%.claude.ai'"
            ).fetchall()
        finally:
            con.close()

    out = []
    for name, blob in rows:
        val = _decrypt(blob, key_v10, key_v11) if blob else None
        if val:
            out.append((name, val))
    return out


if __name__ == "__main__":
    import sys
    rows = read_cookies()
    if not rows:
        print("no readable claude.ai cookies in any Chromium profile",
              file=sys.stderr)
        sys.exit(1)
    names = sorted(n for n, _ in rows)
    print(f"cookies: {len(names)} ({', '.join(names)})")
    print("sessionKey present:", any(n == "sessionKey" for n, _ in rows))
