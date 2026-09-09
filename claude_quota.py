#!/usr/bin/env python3

import datetime
import json
import subprocess
import urllib.error
import urllib.request

from browser_cookie import USER_AGENT, any_profile, load_cookie


def get_org_id():
    """Organisation id from the Claude Code CLI, or None if it is not
    installed. None means "this machine has no Claude to report on", which is
    a different thing from a call that failed."""
    try:
        r = subprocess.run(["claude", "auth", "status"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return json.loads(r.stdout)["orgId"]
    except (ValueError, KeyError):
        raise RuntimeError("claude auth status returned nothing usable")


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday")


def fmt_when(resets_at):
    """Absolute local time for a reset that is days out.

    "in 4d21h14m" is a number you have to do arithmetic on; "friday 11:00" is
    something you can plan around. Weekday names come from a fixed list rather
    than strftime %A, which follows LC_TIME and would print German names on a
    panel that is otherwise entirely in English."""
    try:
        dt = datetime.datetime.fromisoformat(resets_at.replace("Z", "+00:00")).astimezone()
    except Exception:
        return "?"
    today = datetime.datetime.now().astimezone().date()
    days = (dt.date() - today).days
    if days <= 0:
        return f"today {dt:%H:%M}"
    if days == 1:
        return f"tomorrow {dt:%H:%M}"
    return f"{WEEKDAYS[dt.weekday()]} {dt:%H:%M}"


def fmt_delta(resets_at):
    try:
        dt = datetime.datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except Exception:
        return "?"
    now = datetime.datetime.now(datetime.timezone.utc)
    secs = max(0, int((dt - now).total_seconds()))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d{h:02d}h{m:02d}m"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects outright.

    urllib's default handler copies every request header except content-length
    and content-type onto the redirected request — including Cookie, and
    including redirects to a different host. A session token is not something
    to hand to whatever a 30x points at, and this endpoint has no legitimate
    reason to redirect, so treat any 30x as a failure."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(org_id, cookie):
    req = urllib.request.Request(
        f"https://claude.ai/api/organizations/{org_id}/usage",
        headers={
            "Cookie": cookie,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with _OPENER.open(req, timeout=6) as resp:
        return json.loads(resp.read())


def main():
    """Prints one quota line, a short reason it could not, or NOTHING.

    Silence means the panel should leave the section out altogether: there is
    no Claude on this machine and no amount of waiting will change that. A
    printed reason means it is worth showing, because it is actionable —
    an expired cookie, an unreachable endpoint."""
    org_id = get_org_id()
    if org_id is None:
        return

    cookie, source = load_cookie()
    if not cookie:
        if any_profile():
            print("web quota: log in to claude.ai in your browser")
        else:
            print("web quota: no browser profile found")
        return

    try:
        data = fetch(org_id, cookie)
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            print(f"web quota: HTTP {e.code}")
            return
        if source == "cache":
            fresh, src2 = load_cookie()
            if fresh and src2 == "firefox" and fresh != cookie:
                try:
                    data = fetch(org_id, fresh)
                except Exception:
                    print("web quota: log in to claude.ai in Firefox")
                    return
            else:
                print("web quota: log in to claude.ai in Firefox")
                return
        else:
            print("web quota: log in to claude.ai in Firefox")
            return
    except Exception:
        print("web quota: unreachable")
        return

    limits = {l["kind"]: l for l in data.get("limits", []) if l}
    session = limits.get("session")
    weekly = limits.get("weekly_all")

    parts = []
    if session:
        parts.append(f"Session {session['percent']:.0f}% (in {fmt_delta(session['resets_at'])})")
    if weekly:
        parts.append(f"Week {weekly['percent']:.0f}% ({fmt_when(weekly['resets_at'])})")

    print("  |  ".join(parts) if parts else "no quota data")


if __name__ == "__main__":
    main()
