#!/usr/bin/env python3

import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HOME = os.path.expanduser("~")
CONF_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(CONF_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

for _stale in glob.glob(os.path.join(CACHE_DIR, "*.tmp")):
    try:
        if time.time() - os.path.getmtime(_stale) > 300:
            os.unlink(_stale)
    except OSError:
        pass

STATE_FILE = os.path.join(CACHE_DIR, "state.json")
HISTORY_FILE = os.path.join(CACHE_DIR, "history.json")
PNG_PATH = os.path.join(CACHE_DIR, "hud.png")
LOG_FILE = os.path.join(CACHE_DIR, "hud.log")
LOCK_FILE = os.path.join(CACHE_DIR, "hud.lock")

SETTINGS_FILE = os.path.join(CONF_DIR, "settings.json")
WEATHER_FILE = os.path.join(CONF_DIR, "weather.json")

DEFAULT_SETTINGS = {
    "panels": None,                # list of {monitor, position, offset}; None -> one
    "move": None,                  # index of the panel being placed by hand, or None
    "monitor": 0,                  # legacy single-panel keys (migrated into panels)
    "position": "top-right",
    "margin": 22,
    "location": None,
    "units": "c",                  # "c" or "f", for every temperature shown
    "disks": None,                 # mount points to show; None -> just "/"
    "sensors": None,               # temp-sensor ids for thermals; None -> auto
    "peripherals": None,           # peripheral-battery ids to show; None -> none
    "sections": {"thermals": True, "network": True, "power": True, "processes": True},
}


def temp_str(c, units, decimals=0):
    """A temperature in degrees C formatted for display, converted to Fahrenheit
    when units == 'f'. Colour thresholds stay in Celsius; only the text changes."""
    if c is None:
        return "—"
    v = c * 9 / 5 + 32 if units == "f" else c
    return f"{v:.{decimals}f}°"

PSUPPLY = "/sys/class/power_supply"
DETECT_TTL = 30
HIST_MAX_AGE = 3600
HIST_GAMMA = 3.3

T_HERO  = 29
T_LEAD  = 21
T_VALUE = 13
T_BODY  = 11.5
T_LABEL = 10
T_MICRO = 8.5

SS = 2
W = 420
PAD = 22
CW = W - 2 * PAD

MARGIN = 22


def workarea_height(default=1160):
    """Usable desktop height from _NET_WORKAREA, so a moved or resized taskbar
    is picked up. Read once per process; restart the renderer after changing
    the panel. Falls back to a measured constant with no X available."""
    try:
        out = subprocess.run(["xprop", "-root", "_NET_WORKAREA"],
                             capture_output=True, text=True, timeout=2).stdout
        nums = [int(n) for n in re.findall(r"\d+", out)]
        if len(nums) >= 4 and nums[3] > 200:
            return nums[3]
    except Exception:
        pass
    return default


TARGET_H = workarea_height() - 2 * MARGIN
FLEX_MAX = 48

FLEX = 0.0
FLEX_POINTS = 0


def gap(base):
    """A section boundary that absorbs part of the leftover vertical space."""
    global FLEX_POINTS
    FLEX_POINTS += 1
    return base + FLEX

TEXT      = (233, 238, 245)
MUTE      = (104, 116, 133)
STEEL     = (116, 129, 149)
RAMP      = ((0.0, (72, 199, 116)),
             (0.50, (240, 185, 70)),
             (0.78, (246, 105, 64)),
             (1.0, (222, 42, 52)))
ACCENT    = (96, 176, 255)
VIOLET    = (180, 120, 255)
PINK      = (255, 96, 180)
TEAL      = (48, 228, 236)
CORAL     = (224, 128, 93)
AMBER     = (240, 176, 80)
GREEN     = (72, 199, 116)
DARKRED   = (186, 66, 66)
RED       = (233, 84, 82)

CHG_OUT   = 0.0
CHG_IDLE  = 0.5
CHG_IN    = 1.0
CHG_OUT_MAX = 0.25
WARN      = (255, 181, 84)
CRIT      = (255, 95, 109)
TRACK     = (255, 255, 255, 22)
RING      = (255, 255, 255, 20)
HAIRLINE  = (255, 255, 255, 24)


def ramp_rgb(t):
    """Colour for a 0..1 position on the green-amber-red ramp."""
    t = max(0.0, min(1.0, t))
    for (t0, c0), (t1, c1) in zip(RAMP, RAMP[1:]):
        if t <= t1:
            k = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return tuple(int(a + (b - a) * k) for a, b in zip(c0, c1))
    return RAMP[-1][1]


WEATHER_TEMP_STOPS = (
    (-15, (128, 158, 255)),
    (-5,  (108, 178, 255)),
    (4,   (168, 206, 248)),
    (13,  TEXT),
    (25,  TEXT),
    (31,  (240, 176, 80)),
    (37,  (246, 105, 64)),
    (43,  (222, 42, 52)),
)


def weather_temp_color(c):
    """Colour for an air temperature in degrees Celsius (see WEATHER_TEMP_STOPS)."""
    stops = WEATHER_TEMP_STOPS
    if c <= stops[0][0]:
        return stops[0][1]
    if c >= stops[-1][0]:
        return stops[-1][1]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if c <= t1:
            k = (c - t0) / (t1 - t0) if t1 > t0 else 0.0
            return tuple(int(a + (b - a) * k) for a, b in zip(c0, c1))
    return stops[-1][1]


def load_color(v, ncpu):
    """Load average against the thread count. Below it there is headroom and
    everything runs when it wants to; at it the machine is saturated; above it
    work is queuing. Stays neutral until that actually happens.

    Note this is not a pure CPU figure on Linux: the run queue it averages also
    counts processes blocked in uninterruptible I/O, so a stalled disk drives
    it up as surely as a busy core does."""
    if v >= ncpu * 1.5:
        return CRIT
    if v >= ncpu:
        return WARN
    return TEXT


def state_color(pct, base=ACCENT):
    """Each domain keeps its own hue until it runs hot; red and amber are
    reserved for load, so a colour change always means something."""
    if pct >= 90:
        return CRIT
    if pct >= 75:
        return WARN
    return base


FONT_DIR = os.path.join(HOME, ".local/share/fonts")
_FONT_CACHE = {}


def _font_file(*candidates):
    for name in candidates:
        p = os.path.join(FONT_DIR, name)
        if os.path.exists(p):
            return p
    for name in candidates:
        for root in ("/usr/share/fonts/truetype", "/usr/share/fonts/opentype"):
            hits = glob.glob(os.path.join(root, "**", name), recursive=True)
            if hits:
                return hits[0]
    return None


UI_MED    = _font_file("Inter-Medium.otf", "Inter-Medium.ttf", "DejaVuSans.ttf")
UI_SEMI   = _font_file("Inter-SemiBold.otf", "Inter-SemiBold.ttf", "DejaVuSans-Bold.ttf")
MONO_REG  = _font_file("JetBrainsMono-Regular.ttf", "DejaVuSansMono.ttf")
MONO_MED  = _font_file("JetBrainsMono-Medium.ttf", "DejaVuSansMono.ttf")
MONO_LIGHT= _font_file("JetBrainsMono-Light.ttf", "DejaVuSansMono.ttf")


def F(path, size):
    key = (path, size)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = ImageFont.truetype(path, int(size * SS))
    return _FONT_CACHE[key]


def measure(f, s, tracking=0):
    if not s:
        return 0
    w = f.getlength(s)
    return w + tracking * SS * (len(s) - 1)


def text(d, x, y, s, f, fill, anchor="l", tracking=0):
    """anchor: 'l' left, 'r' right (x is the right edge), 'c' centred on x."""
    if s is None:
        s = ""
    s = str(s)
    if not s:
        return
    x, y = x * SS, y * SS
    if anchor == "r":
        x -= measure(f, s, tracking)
    elif anchor == "c":
        x -= measure(f, s, tracking) / 2
    if tracking:
        for ch in s:
            d.text((x, y), ch, font=f, fill=fill)
            x += f.getlength(ch) + tracking * SS
    else:
        d.text((x, y), s, font=f, fill=fill)


def label(d, x, y, s, fill=MUTE, size=T_LABEL, tracking=1.6):
    """Small letterspaced all-caps section label."""
    text(d, x, y, s.upper(), F(UI_SEMI, size), fill, tracking=tracking)


def label_r(d, x, y, s, fill=MUTE, size=T_LABEL, tracking=1.6):
    text(d, x, y, s.upper(), F(UI_SEMI, size), fill, anchor="r", tracking=tracking)


def bar(img, x, y, w, h, frac, color=None, track=TRACK, ramp=False):
    """Rounded progress bar with a track and a soft gradient fill. Drawn into a
    tile the size of the bar (not the full canvas) — full-canvas glow layers
    were the single biggest cost in the render loop.

    ramp=True colours the fill from the green-amber-red ramp. The ramp is
    mapped across the FULL track, not across the fill, so the colour at the
    bar's head tells you the percentage; stretching the whole ramp over every
    bar would make a 5% bar and a 95% bar look identical."""
    X, Y = int(x * SS), int(y * SS)
    Wp, Hp = int(w * SS), int(h * SS)
    pad = int(3 * SS)
    tile = Image.new("RGBA", (Wp + 2 * pad, Hp + 2 * pad), (0, 0, 0, 0))
    r = Hp / 2
    ImageDraw.Draw(tile).rounded_rectangle(
        [pad, pad, pad + Wp, pad + Hp], radius=r, fill=track)

    frac = max(0.0, min(1.0, frac))
    fw = frac * Wp
    if fw >= 1:
        fw = max(fw, Hp)
        shape = Image.new("L", tile.size, 0)
        ImageDraw.Draw(shape).rounded_rectangle(
            [pad, pad, pad + fw, pad + Hp], radius=r, fill=255)

        strip = Image.new("RGBA", (max(1, int(Wp)), 1))
        sp = strip.load()
        for i in range(strip.width):
            t = i / max(1, strip.width - 1)
            if ramp:
                sp[i, 0] = (*ramp_rgb(t), int(215 + 40 * t))
            else:
                sp[i, 0] = (*color, int(120 + 135 * t))
        fill = Image.new("RGBA", tile.size, (0, 0, 0, 0))
        fill.paste(strip.resize((int(Wp), int(Hp) + 1)), (pad, pad))
        fill.putalpha(ImageChops.multiply(fill.getchannel("A"), shape))

        tile.alpha_composite(fill.filter(ImageFilter.GaussianBlur(1.5 * SS)))
        tile.alpha_composite(fill)

    img.alpha_composite(tile, (X - pad, Y - pad))


def _pctl(vals, q):
    if not vals:
        return 0.0
    sv = sorted(vals)
    return sv[min(len(sv) - 1, int(len(sv) * q))]


def draw_weather_icon(img, cx, cy, s, code):
    """Small hand-drawn weather glyph, in the same supersampled-Pillow idiom as
    the gauges and bars — no icon font to depend on. Shape follows the WMO
    weather code: sun, sun-behind-cloud, cloud, fog, rain, snow, thunder."""
    gs = SS * 3
    box = int(s * 3.4)
    C = box * gs / 2.0
    R = s * gs
    tile = Image.new("RGBA", (box * gs, box * gs), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    SUN, CLOUD, RAIN, SNOW = (240, 185, 70), (200, 208, 222), (96, 176, 255), (214, 226, 240)

    def disc(x, y, r, fill):
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)

    def sun(x, y, r, rays=True):
        if rays:
            import math
            for k in range(8):
                a = k * math.pi / 4
                x0, y0 = x + math.cos(a) * r * 1.35, y + math.sin(a) * r * 1.35
                x1, y1 = x + math.cos(a) * r * 1.9, y + math.sin(a) * r * 1.9
                d.line([(x0, y0), (x1, y1)], fill=(*SUN, 255), width=int(gs * 1.4))
        disc(x, y, r, (*SUN, 255))

    def cloud(x, y, r, col=CLOUD):
        disc(x - r * 0.95, y, r * 0.72, (*col, 255))
        disc(x + r * 0.95, y, r * 0.78, (*col, 255))
        disc(x - r * 0.1, y - r * 0.55, r * 0.9, (*col, 255))
        d.rounded_rectangle([x - r * 1.7, y - r * 0.1, x + r * 1.7, y + r * 0.75],
                            radius=r * 0.55, fill=(*col, 255))

    grp = ("clear" if code in (0, 1) else
           "part" if code == 2 else
           "fog" if code in (45, 48) else
           "snow" if code in (71, 73, 75, 77, 85, 86) else
           "thunder" if code in (95, 96, 99) else
           "rain" if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82) else
           "cloud")

    if grp == "clear":
        sun(C, C, R * 0.62)
    elif grp == "part":
        sun(C - R * 0.55, C - R * 0.5, R * 0.44)
        cloud(C + R * 0.25, C + R * 0.35, R * 0.72)
    elif grp == "fog":
        cloud(C, C - R * 0.35, R * 0.8)
        for i in range(3):
            yy = C + R * (0.7 + i * 0.42)
            d.line([(C - R * 1.4, yy), (C + R * 1.4, yy)], fill=(*CLOUD, 200), width=int(gs * 1.3))
    elif grp == "cloud":
        cloud(C, C, R * 0.85)
    else:
        cloud(C, C - R * 0.35, R * 0.85)
        base = C + R * 0.7
        if grp == "rain":
            for dx in (-R * 0.7, 0, R * 0.7):
                d.line([(C + dx, base), (C + dx - R * 0.3, base + R * 0.7)],
                       fill=(*RAIN, 255), width=int(gs * 1.6))
        elif grp == "snow":
            for dx in (-R * 0.7, 0, R * 0.7):
                disc(C + dx, base + R * 0.35, gs * 1.6, (*SNOW, 255))
        elif grp == "thunder":
            b = [(C - R * 0.15, base - R * 0.1), (C - R * 0.55, base + R * 0.6),
                 (C - R * 0.1, base + R * 0.55), (C - R * 0.45, base + R * 1.25),
                 (C + R * 0.5, base + R * 0.3), (C + R * 0.05, base + R * 0.35),
                 (C + R * 0.35, base - R * 0.1)]
            d.polygon(b, fill=(*SUN, 255))

    tile = tile.resize((box * SS, box * SS), Image.LANCZOS)
    img.alpha_composite(tile, (int(cx * SS - box * SS / 2), int(cy * SS - box * SS / 2)))


def gauge(img, cx, cy, r, thick, frac, color):
    """270-degree donut gauge. Arcs are drawn into a locally supersampled tile
    because Pillow's arc() has no antialiasing of its own."""
    gs = SS * 2
    pad = thick + 5
    size = int(2 * r + 2 * pad)
    tile = Image.new("RGBA", (size * gs, size * gs), (0, 0, 0, 0))
    box = [pad * gs, pad * gs, (size - pad) * gs, (size - pad) * gs]
    start, sweep = 135, 270

    ImageDraw.Draw(tile).arc(box, start, start + sweep, fill=RING, width=int(thick * gs))

    frac = max(0.0, min(1.0, frac))
    if frac > 0.004:
        end = start + sweep * frac
        glow = Image.new("RGBA", tile.size, (0, 0, 0, 0))
        ImageDraw.Draw(glow).arc(box, start, end, fill=(*color, 190), width=int(thick * gs))
        tile.alpha_composite(glow.filter(ImageFilter.GaussianBlur(1.8 * gs)))
        ImageDraw.Draw(tile).arc(box, start, end, fill=(*color, 255), width=int(thick * gs))

    tile = tile.resize((size * SS, size * SS), Image.BOX)
    img.alpha_composite(tile, (int((cx - size / 2) * SS), int((cy - size / 2) * SS)))


def core_strip(img, x, y, w, h, loads):
    """One mini column per logical core. Twenty of them make a texture that a
    single averaged CPU line can't: you can see one pinned core vs. an even
    spread at a glance."""
    n = max(1, len(loads))
    gap = 2.0
    bw = (w - gap * (n - 1)) / n
    X, Y = int(x * SS), int(y * SS)
    Wp, Hp = int(w * SS), int(h * SS)
    tile = Image.new("RGBA", (Wp + 4 * SS, Hp + 4 * SS), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    o = 2 * SS
    rad = min(bw, 3) * SS / 2

    for i, pct in enumerate(loads):
        bx = o + i * (bw + gap) * SS
        td.rounded_rectangle([bx, o, bx + bw * SS, o + Hp], radius=rad, fill=(255, 255, 255, 10))
        fh = max(0.0, min(1.0, pct / 100)) * Hp
        if fh < 1:
            continue
        fh = max(fh, bw * SS * 0.8)
        col = state_color(pct, ACCENT)
        td.rounded_rectangle([bx, o + Hp - fh, bx + bw * SS, o + Hp],
                             radius=rad, fill=(*col, 235))

    img.alpha_composite(tile.filter(ImageFilter.GaussianBlur(1.6 * SS)), (X - o, Y - o))
    img.alpha_composite(tile, (X - o, Y - o))


def swatch(d, x, y, color, size=8, line=False):
    """Legend marker. These were 2px-tall bars that read as dashes and gave the
    colour almost no area to show in; a square is small but actually legible.

    line=True draws a rule instead, for a series that IS a line — a square
    there would have the reader looking for columns in that colour."""
    if line:
        d.rounded_rectangle([x * SS, (y + 4.5) * SS, (x + size + 3) * SS, (y + 7) * SS],
                            radius=1.2 * SS, fill=(*color, 235))
    else:
        d.rounded_rectangle([x * SS, (y + 1.5) * SS, (x + size) * SS, (y + 1.5 + size) * SS],
                            radius=1.5 * SS, fill=(*color, 235))


def row_bar(img, x, y, w, h, frac, color):
    """A dim proportional block behind a list row — a heat bar, not a rule."""
    X, Y = int(x * SS), int(y * SS)
    Wp, Hp = int(w * SS), int(h * SS)
    tile = Image.new("RGBA", (Wp, Hp), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    td.rounded_rectangle([0, 0, Wp - 1, Hp - 1], radius=3 * SS, fill=(255, 255, 255, 8))
    fw = max(0.0, min(1.0, frac)) * Wp
    if fw > 2 * SS:
        td.rounded_rectangle([0, 0, fw, Hp - 1], radius=3 * SS, fill=(*color, 46))
    img.alpha_composite(tile, (X, Y))


def _series_cols(key, n):
    """Aggregate a history series into n columns on the compressed time axis.

    One pass over the samples, not one pass per column. HISTORY is chronological
    and the column age bounds descend monotonically, so the two can be walked
    together; rescanning every sample for every column made this O(columns x
    samples) and cost 17ms a frame once the window grew to an hour."""
    now = time.time()
    pts = [(now - s.get("t", now), s[key]) for s in HISTORY
           if s.get(key) is not None and now - s.get("t", now) < HIST_MAX_AGE]
    if not pts:
        return [0.0] * n
    denom = max(1, n - 1)
    ages = [((denom - i) / denom) ** HIST_GAMMA * HIST_MAX_AGE for i in range(n)]

    cols = []
    last = pts[0][1]
    j = 0
    for i in range(n):
        lo = ages[i + 1] if i + 1 < n else 0.0
        total = 0.0
        cnt = 0
        while j < len(pts) and pts[j][0] >= lo:
            total += pts[j][1]
            cnt += 1
            j += 1
        if cnt:
            last = total / cnt
        cols.append(last)
    return cols


def _scale(cols, floor=None, clamp=None):
    ymax = max(_pctl(cols, 0.95) * 1.35, floor or 1.0)
    return min(ymax, clamp) if clamp else ymax


def _cols_into(tile, ox, oy, w, h, cols, ymax, color, colw, gap, up=True, colors=None):
    """Column bars, brightness scaled by height so peaks read and idle noise
    recedes — a flat hairline at 2% conveyed nothing at a glance."""
    td = ImageDraw.Draw(tile)
    for i, v in enumerate(cols):
        frac = max(0.0, min(1.0, v / ymax))
        bx = ox + i * (colw + gap) * SS
        bh = frac * h
        if bh < 1:
            bh = 1.2 * SS
        a = int(55 + 193 * frac)
        c = colors[i] if colors else color
        box = ([bx, oy + h - bh, bx + colw * SS, oy + h] if up
               else [bx, oy, bx + colw * SS, oy + bh])
        td.rectangle(box, fill=(*c, a))


def consumption_color(t):
    """Colour of the consumption segment for a power-direction value: dark red
    while the pack is supplying it, amber once the wall is — charging or not,
    because either way the machine is running off mains."""
    k = min(1.0, max(0.0, t) / CHG_IDLE)
    return tuple(int(a + (b - a) * k) for a, b in zip(DARKRED, AMBER))


def histogram(img, x, y, w, h, key, color, floor=None, clamp=None, colw=3, gap=1.6,
              overlay=None, overlay_color=None):
    n = max(1, int((w + gap) // (colw + gap)))
    cols = _series_cols(key, n)
    ocols = _series_cols(overlay, n) if overlay else None
    ccols = None
    ymax = _scale(cols, floor, clamp)
    if ocols:
        ymax = max(ymax, _scale(ocols, floor, clamp))

    X, Y = int(x * SS), int(y * SS)
    Wp, Hp = int(w * SS), int(h * SS)
    pad = 4 * SS
    tile = Image.new("RGBA", (Wp + 2 * pad, Hp + 2 * pad), (0, 0, 0, 0))
    _cols_into(tile, pad, pad, Wp, Hp, cols, ymax, color, colw, gap, colors=ccols)

    if ocols:
        pts = [(pad + i * (colw + gap) * SS + colw * SS / 2,
                pad + Hp - max(0.0, min(1.0, v / ymax)) * Hp) for i, v in enumerate(ocols)]
        if len(pts) > 1:
            glow = Image.new("RGBA", tile.size, (0, 0, 0, 0))
            ImageDraw.Draw(glow).line(pts, fill=(*overlay_color, 200), width=int(2.2 * SS))
            tile.alpha_composite(glow.filter(ImageFilter.GaussianBlur(1.4 * SS)))
            ImageDraw.Draw(tile).line(pts, fill=(*overlay_color, 245), width=max(1, int(1.4 * SS)))

    img.alpha_composite(tile.filter(ImageFilter.GaussianBlur(1.8 * SS)), (X - pad, Y - pad))
    img.alpha_composite(tile, (X - pad, Y - pad))
    return ymax


def power_chart(img, x, y, w, h, floor=None):
    """Consumption and battery charging, stacked.

    Total height is the power actually crossing the boundary: out of the pack
    when discharging, out of the wall otherwise — and while charging the wall
    feeds both the machine and the pack, which is exactly the split worth
    seeing. The lower segment carries the direction colour (amber on battery,
    steel on mains), the upper one is always green because it can only ever be
    energy going in."""
    colw, gap = 3, 1.6
    n = max(1, int((w + gap) // (colw + gap)))
    use = _series_cols("power", n)
    chg = _series_cols("chg_w", n)
    dirs = _series_cols("chg", n)
    ymax = _scale([u + c for u, c in zip(use, chg)], floor)

    X, Y = int(x * SS), int(y * SS)
    Wp, Hp = int(w * SS), int(h * SS)
    pad = 4 * SS
    tile = Image.new("RGBA", (Wp + 2 * pad, Hp + 2 * pad), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)

    tops = []
    for i in range(n):
        bx = pad + i * (colw + gap) * SS
        bw = colw * SS
        base = pad + Hp
        for value, col in ((use[i], consumption_color(dirs[i])),
                           (chg[i], GREEN)):
            frac = max(0.0, min(1.0, value / ymax))
            bh = frac * Hp
            if bh < 1:
                bh = 1.2 * SS if col is not GREEN else 0
            if bh <= 0:
                continue
            td.rectangle([bx, base - bh, bx + bw, base], fill=(*col, int(55 + 193 * frac)))
            base -= bh
        tops.append((bx + bw / 2, base, dirs[i] > CHG_OUT_MAX))

    img.alpha_composite(tile.filter(ImageFilter.GaussianBlur(1.8 * SS)), (X - pad, Y - pad))
    img.alpha_composite(tile, (X - pad, Y - pad))

    lw = max(1, int(1.9 * SS))
    shadow = Image.new("RGBA", tile.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for (x0, y0, _), (x1, y1, _) in zip(tops, tops[1:]):
        sd.line([(x0, y0), (x1, y1)], fill=(6, 8, 12, 255), width=lw + 2 * SS)
    shadow.putalpha(shadow.getchannel("A").point(lambda v: v * 150 // 255))
    img.alpha_composite(shadow, (X - pad, Y - pad))

    line = Image.new("RGBA", tile.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(line)
    for (x0, y0, ac0), (x1, y1, _) in zip(tops, tops[1:]):
        ld.line([(x0, y0), (x1, y1)], fill=(*(TEXT if ac0 else CRIT), 255), width=lw)
    line.putalpha(line.getchannel("A").point(lambda v: v * 240 // 255))
    img.alpha_composite(line, (X - pad, Y - pad))
    return ymax


def net_chart(img, x, y, w, h, color_down, color_up, floor=None):
    """Down and up mirrored around a shared axis — one chart that shows the
    balance between the two, instead of two disconnected little boxes.

    Each direction gets its own tinted band and its own coloured baseline. With
    little traffic both series sit at their baseline, and without the bands the
    whole thing collapsed into one thin striped line in the middle where you
    could no longer tell the two directions apart."""
    colw, gap = 3, 1.6
    n = max(1, int((w + gap) // (colw + gap)))
    dn, up_s = _series_cols("down", n), _series_cols("up", n)
    ymax = max(_scale(dn, floor), _scale(up_s, floor))

    X, Y = int(x * SS), int(y * SS)
    Wp, Hp = int(w * SS), int(h * SS)
    split = int(7 * SS)
    half = (Hp - split) // 2
    pad = 4 * SS
    top_y, bot_y = pad, pad + half + split
    size = (Wp + 2 * pad, Hp + 2 * pad)

    bands = Image.new("RGBA", size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(bands)
    bd.rectangle([pad, top_y, pad + Wp, top_y + half], fill=(*color_down, 15))
    bd.rectangle([pad, bot_y, pad + Wp, bot_y + half], fill=(*color_up, 15))
    lw = max(1, SS // 2)
    bd.line([(pad, top_y + half), (pad + Wp, top_y + half)], fill=(*color_down, 110), width=lw)
    bd.line([(pad, bot_y), (pad + Wp, bot_y)], fill=(*color_up, 110), width=lw)
    img.alpha_composite(bands, (X - pad, Y - pad))

    tile = Image.new("RGBA", size, (0, 0, 0, 0))
    _cols_into(tile, pad, top_y, Wp, half, dn, ymax, color_down, colw, gap, up=True)
    _cols_into(tile, pad, bot_y, Wp, half, up_s, ymax, color_up, colw, gap, up=False)

    img.alpha_composite(tile.filter(ImageFilter.GaussianBlur(1.8 * SS)), (X - pad, Y - pad))
    img.alpha_composite(tile, (X - pad, Y - pad))
    return ymax


def fmt_bytes(n, per_sec=False):
    n = float(n)
    for unit, div in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if n >= div:
            v = n / div
            s = f"{v:.1f}{unit}" if v < 100 else f"{v:.0f}{unit}"
            return s + ("/s" if per_sec else "")
    return f"{n:.0f}B" + ("/s" if per_sec else "")


def fmt_dur(secs):
    secs = int(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h:02d}h {m:02d}m"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


_DETECTED = {}


def _detect(key, finder):
    """Cache a hardware name for DETECT_TTL seconds. Both the network interface
    and the battery used to be hardcoded, which failed silently: a renamed
    interface produced a chart flat at zero rather than an error."""
    ts, val = _DETECTED.get(key, (0.0, None))
    now = time.time()
    if val is not None and now - ts < DETECT_TTL:
        return val
    found = finder()
    if found is None and val is not None:
        return val
    _DETECTED[key] = (now, found)
    return found


def net_iface():
    """Whichever interface carries the default route — ethernet when it is
    plugged in, wifi otherwise, and it follows a rename on its own."""
    def find():
        try:
            with open("/proc/net/route") as f:
                next(f)
                for line in f:
                    fl = line.split()
                    if fl[1] == "00000000" and int(fl[3], 16) & 2:
                        return fl[0]
        except (OSError, StopIteration, IndexError, ValueError):
            pass
        return None
    return _detect("iface", find)


def battery_path():
    """The system battery's power_supply device, or None on a desktop.

    Peripheral batteries (a wireless mouse or keyboard, e.g. via Solaar) also
    show up here as type Battery; they carry scope=Device, so they are skipped
    — otherwise a desktop would show its keyboard's charge as the machine's."""
    def find():
        try:
            for name in sorted(os.listdir(PSUPPLY)):
                d = f"{PSUPPLY}/{name}"
                if read_first(f"{d}/type", default="") != "Battery":
                    continue
                if read_first(f"{d}/scope", default="") == "Device":
                    continue
                return d
        except OSError:
            pass
        return None
    return _detect("battery", find)


_UPOWER = {"t": -1e9, "data": []}


def _upower_peripherals():
    """Peripheral batteries UPower knows about — this reaches devices that
    report only over Bluez/UPower (many Bluetooth headsets) and never appear
    under /sys/class/power_supply. `power supply: no` filters out the machine's
    own battery and the AC line. Cached for a few seconds; upower -d is quick
    but not worth running every frame."""
    now = time.time()
    if now - _UPOWER["t"] < 12:
        return _UPOWER["data"]
    _UPOWER["t"] = now
    if not shutil.which("upower"):
        _UPOWER["data"] = []
        return []
    try:
        out = subprocess.run(["upower", "-d"], capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return _UPOWER["data"]
    devs = []
    for block in out.split("Device:")[1:]:
        d, psupply = {}, None
        for line in block.splitlines():
            ln = line.strip()
            if ln.startswith("model:"):
                d["name"] = ln.split(":", 1)[1].strip()
            elif ln.startswith("power supply:"):
                psupply = ln.split(":", 1)[1].strip()
            elif ln.startswith("percentage:"):
                try:
                    d["cap"] = int(round(float(ln.split(":", 1)[1].strip().rstrip("%"))))
                except ValueError:
                    pass
            elif ln.startswith("state:"):
                d["state"] = ln.split(":", 1)[1].strip()
        if psupply == "no" and "cap" in d:
            name = d.get("name") or "device"
            devs.append({"id": "upower:" + name, "name": name, "capacity": d["cap"],
                         "status": "Charging" if d.get("state") == "charging" else "Discharging"})
    _UPOWER["data"] = devs
    return devs


def peripheral_batteries():
    """Wireless mouse/keyboard/headset/etc. batteries. Two sources merged:
    /sys/class/power_supply devices with scope=Device (the ones battery_path
    skips), plus whatever UPower reports that isn't a system battery — the
    latter catches Bluetooth devices that never show up in sysfs. Returns
    [{id, name, capacity, status}]."""
    out = []
    try:
        for name in sorted(os.listdir(PSUPPLY)):
            d = f"{PSUPPLY}/{name}"
            if read_first(f"{d}/type", default="") != "Battery":
                continue
            if read_first(f"{d}/scope", default="") != "Device":
                continue
            out.append({"id": name,
                        "name": read_first(f"{d}/model_name", default="") or name,
                        "capacity": read_first(f"{d}/capacity", int),
                        "status": read_first(f"{d}/status", default="")})
    except OSError:
        pass
    seen = {p["name"].lower() for p in out if p.get("name")}
    for u in _upower_peripherals():
        if u["name"].lower() not in seen:
            out.append(u)
    return out


def read_first(path, cast=str, default=None):
    try:
        with open(path) as f:
            return cast(f.read().strip())
    except Exception:
        return default


def cpu_jiffies():
    with open("/proc/stat") as f:
        parts = list(map(int, f.readline().split()[1:8]))
    return parts[3] + parts[4], sum(parts)


def cpu_jiffies_per_core():
    cores = []
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu") or line[3] == " ":
                continue
            v = list(map(int, line.split()[1:8]))
            cores.append((v[3] + v[4], sum(v)))
    return cores


def meminfo():
    d = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            d[k] = int(v.split()[0]) * 1024
    return d


def cpu_freq_ghz():
    vals = []
    for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
        v = read_first(p, int)
        if v:
            vals.append(v)
    if vals:
        return sum(vals) / len(vals) / 1e6
    try:
        with open("/proc/cpuinfo") as f:
            mhz = [float(l.split(":")[1]) for l in f if l.startswith("cpu MHz")]
        return sum(mhz) / len(mhz) / 1000 if mhz else 0.0
    except Exception:
        return 0.0


def gpu_engine_snapshot():
    """Per-engine GPU busy counters from /proc/*/fdinfo, deduped by
    (pid, drm-client-id) since one process holds several fds per client.

    Two counter shapes, because the drivers differ:
      drm-engine-<c>:        busy nanoseconds (i915, amdgpu, most drivers)
      drm-cycles-<c> +
      drm-total-cycles-<c>:  busy vs. elapsed cycles (Intel xe — Arc, Lunar
                             Lake and up)
    Returned as one flat dict. Engine-ns keys keep their 'drm-engine-*' name;
    the cycle model becomes 'cyc:<c>' (busy, summed over clients) and 'tot:<c>'
    (the elapsed-cycles reference, the max over clients — it is a free-running
    per-engine counter, so summing it would inflate the denominator). The
    caller reads whichever model is present."""
    clients = {}
    with os.scandir("/proc") as it:
        pids = [e.path for e in it if e.name.isdigit()]
    for pid_dir in pids:
        fd_dir = pid_dir + "/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if not os.readlink(fd_dir + "/" + fd).startswith("/dev/dri/"):
                    continue
                with open(pid_dir + "/fdinfo/" + fd) as f:
                    content = f.read()
            except OSError:
                continue
            if "drm-driver:" not in content:
                continue
            cid, counters = None, {}
            for line in content.splitlines():
                if line.startswith("drm-client-id:"):
                    cid = line.split(":", 1)[1].strip()
                    continue
                if "capacity" in line:
                    continue
                for pfx, tag in (("drm-engine-", "eng"),
                                 ("drm-total-cycles-", "tot"),
                                 ("drm-cycles-", "cyc")):
                    if line.startswith(pfx):
                        k, _, v = line.partition(":")
                        try:
                            counters[(tag, k[len(pfx):].strip())] = int(v.strip().split()[0])
                        except (ValueError, IndexError):
                            pass
                        break
            if cid and counters:
                clients[(pid_dir, cid)] = counters
    totals = {}
    for counters in clients.values():
        for (tag, cls), v in counters.items():
            if tag == "eng":
                key = "drm-engine-" + cls
                totals[key] = totals.get(key, 0) + v
            elif tag == "cyc":
                totals["cyc:" + cls] = totals.get("cyc:" + cls, 0) + v
            else:
                totals["tot:" + cls] = max(totals.get("tot:" + cls, 0), v)
    return totals


def _gpu_drivers():
    """Kernel driver name behind each DRM card (i915, xe, amdgpu, nvidia,
    nouveau, ...)."""
    drv = set()
    for c in glob.glob("/sys/class/drm/card[0-9]*/device/driver"):
        try:
            drv.add(os.path.basename(os.readlink(c)))
        except OSError:
            pass
    return drv


def gpu_source():
    """How to read GPU utilisation on this machine, or None if there is no way.

      ("busy", path)  amdgpu's gpu_busy_percent — an instantaneous 0..100
      ("fdinfo",)     drm-engine busy-time deltas (Intel i915/xe, amdgpu, nouveau)
      ("nvsmi",)      nvidia-smi, for the proprietary NVIDIA driver

    Cached: probed once, since the GPU does not change under the running panel.
    A machine with no usable source (headless, or NVIDIA with no nvidia-smi)
    gets no GPU gauge rather than a dead one stuck at zero."""
    def find():
        drv = _gpu_drivers()
        if "amdgpu" in drv:
            for p in sorted(glob.glob("/sys/class/drm/card[0-9]*/device/gpu_busy_percent")):
                if read_first(p, int) is not None:
                    return ("busy", p)
        if drv & {"i915", "xe", "amdgpu", "nouveau"}:
            return ("fdinfo",)
        if "nvidia" in drv and shutil.which("nvidia-smi"):
            return ("nvsmi",)
        if glob.glob("/dev/dri/renderD*") and gpu_engine_snapshot():
            return ("fdinfo",)
        return ""
    return _detect("gpu_src", find) or None


def net_bytes(iface):
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if ":" not in line:
                    continue
                name, rest = line.split(":", 1)
                if name.strip() == iface:
                    fl = rest.split()
                    return int(fl[0]), int(fl[8])
    except Exception:
        pass
    return 0, 0


REAL_FS = {"ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs", "vfat", "exfat",
           "ntfs", "ntfs3", "zfs", "reiserfs", "jfs", "udf", "bcachefs"}


def disk_mounts():
    """Mounted real (block-backed) filesystems as (mountpoint, device), one per
    device, so the settings window can offer every drive in the machine rather
    than only the root filesystem."""
    def find():
        seen, out = set(), []
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    dev, mp, fs = parts[0], parts[1], parts[2]
                    if not dev.startswith("/dev/") or fs not in REAL_FS or dev in seen:
                        continue
                    seen.add(dev)
                    mp = mp.replace("\\040", " ")
                    out.append((mp, dev))
        except OSError:
            pass
        return out or [("/", "")]
    return _detect("mounts", find)


def diskio_sectors():
    r = w = 0
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                fl = line.split()
                name = fl[2]
                if name.startswith(("loop", "ram", "dm-", "zram")):
                    continue
                if name[-1].isdigit() and not name.startswith("nvme"):
                    continue
                if name.startswith("nvme") and "p" in name:
                    continue
                r += int(fl[5])
                w += int(fl[9])
    except Exception:
        pass
    return r * 512, w * 512


WIFI_DRIVERS = ("iwlwifi", "iwlmvm", "ath9k", "ath10k", "ath11k", "ath12k",
                "mt7921", "mt7922", "mt7915", "mt7925", "rtw88", "rtw89",
                "mwifiex", "brcmfmac")


def _hwmon_list():
    """(name, dir) for every hwmon node."""
    return [(read_first(f"{h}/name", default=""), h)
            for h in glob.glob("/sys/class/hwmon/hwmon*")]


def _hwmon_temp_input(h, labels=()):
    """Path to a temp*_input under hwmon dir h: one whose *_label matches a
    wanted label, else the lowest-numbered temp input; "" if it has none."""
    have = {}
    for lab in glob.glob(f"{h}/temp*_label"):
        have[read_first(lab, default="")] = lab.replace("_label", "_input")
    for w in labels:
        p = have.get(w)
        if p and os.path.exists(p):
            return p
    ins = sorted(glob.glob(f"{h}/temp*_input"))
    return ins[0] if ins else ""


def _zone_temp(match):
    """A thermal_zone temp path whose type (lowercased) satisfies match()."""
    for z in glob.glob("/sys/class/thermal/thermal_zone*"):
        t = read_first(f"{z}/type", default="").lower()
        if t and match(t) and os.path.exists(f"{z}/temp"):
            return f"{z}/temp"
    return ""


def _find_cpu_temp():
    hw = _hwmon_list()
    for name, h in hw:
        if name == "coretemp":
            p = _hwmon_temp_input(h, ("Package id 0",))
            if p:
                return p
    for name, h in hw:
        if name == "k10temp":
            p = _hwmon_temp_input(h, ("Tdie", "Tctl"))
            if p:
                return p
    for name, h in hw:
        if "cpu" in name.lower() or name in ("soc", "soc_thermal"):
            p = _hwmon_temp_input(h)
            if p:
                return p
    return (_zone_temp(lambda t: t in ("x86_pkg_temp", "cpu-thermal", "cpu_thermal")
                       or "cpu" in t or "pkg" in t or "tctl" in t
                       or "soc" in t or "cluster" in t or "bigcore" in t)
            or _zone_temp(lambda t: t == "acpitz"))


def _find_disk_temp():
    hw = _hwmon_list()
    for name, h in hw:
        if name == "nvme":
            p = _hwmon_temp_input(h, ("Composite",))
            if p:
                return p
    for name, h in hw:
        if name == "drivetemp":
            p = _hwmon_temp_input(h)
            if p:
                return p
    return ""


def _find_wifi_temp():
    for name, h in _hwmon_list():
        nl = name.lower()
        if any(nl == d or nl.startswith(d) for d in WIFI_DRIVERS):
            p = _hwmon_temp_input(h)
            if p:
                return p
    return _zone_temp(lambda t: any(d in t for d in ("iwlwifi", "wifi", "wlan", "ath")))


def temps():
    """CPU, drive and wifi-radio temperatures in degrees C, each None when the
    machine exposes no matching sensor. Detection is cached — its absence too —
    so it costs one hwmon scan per DETECT_TTL, not one per frame, and it spans
    vendors: Intel coretemp / AMD k10temp / a CPU thermal zone; NVMe or a SATA
    drivetemp; any known wireless chip."""
    paths = (_detect("cpu_temp", _find_cpu_temp),
             _detect("disk_temp", _find_disk_temp),
             _detect("wifi_temp", _find_wifi_temp))
    out = []
    for p in paths:
        v = read_first(p, int) if p else None
        out.append(v / 1000 if v is not None else None)
    return tuple(out)


def _sensor_thresholds(text):
    t = text.lower()
    if any(k in t for k in ("coretemp", "k10temp", "cpu", "package", "tctl", "tdie", "x86_pkg")):
        return 80, 95
    if any(k in t for k in ("nvme", "drivetemp", "composite", "ssd", "disk")):
        return 60, 75
    if any(k in t for k in ("wifi", "iwlwifi", "ath", "mt79", "wlan")):
        return 75, 85
    if any(k in t for k in ("amdgpu", "gpu", "edge", "junction")):
        return 80, 95
    return 70, 90


def _sensor_short(chip, lab):
    t = (lab or "").lower()
    c = chip.lower()
    if "package id 0" in t or (c == "coretemp" and not lab):
        return "CPU"
    if c == "k10temp" and t in ("tdie", "tctl", ""):
        return "CPU"
    if c == "nvme" and t == "composite":
        return "SSD"
    if c.startswith(("iwlwifi", "ath", "mt79", "rtw", "mwifiex", "brcm")):
        return "WIFI"
    return (lab or chip)[:10]


def list_sensors():
    """Every readable temperature sensor as {id, label, full, path, warn, crit}.
    id (chip + input) is stable across runs so a selection can be saved; label
    is short for the panel, full is descriptive for the settings window."""
    out = []
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        chip = read_first(f"{h}/name", default="") or os.path.basename(h)
        labels = {}
        for lab in glob.glob(f"{h}/temp*_label"):
            labels[lab.replace("_label", "_input")] = read_first(lab, default="")
        for inp in sorted(glob.glob(f"{h}/temp*_input")):
            base = os.path.basename(inp).replace("_input", "")
            lab = labels.get(inp, "")
            warn, crit = _sensor_thresholds(f"{chip} {lab or base}")
            out.append({"id": f"{chip}:{base}", "label": _sensor_short(chip, lab),
                        "full": f"{chip} · {lab}" if lab else chip,
                        "path": inp, "warn": warn, "crit": crit})
    for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        ty = read_first(f"{z}/type", default="")
        p = f"{z}/temp"
        if ty and os.path.exists(p):
            warn, crit = _sensor_thresholds(ty)
            out.append({"id": f"zone:{ty}", "label": ty[:10], "full": f"zone · {ty}",
                        "path": p, "warn": warn, "crit": crit})
    return out


def read_sensor(path):
    v = read_first(path, int)
    return v / 1000 if v is not None else None


def temp_gradient(t, warn, crit):
    """Smooth green->amber->red for a device temperature, on the app's own
    quota/battery ramp. warn lands on amber and crit on red, with an equal span
    below warn fading down to green, so a cool part reads calm rather than
    stepping between flat colours."""
    if t is None:
        return MUTE
    pos = 0.5 + 0.5 * (t - warn) / (crit - warn)
    return ramp_rgb(pos)


_RAPL = None


def rapl_source():
    """Best readable RAPL energy counter, or None.

    The battery only reports current while it is actually charging or
    discharging, so on mains with a full pack it reads 0W even though the
    machine is obviously drawing power — that comes from the wall, and neither
    ADP1 nor the USB-C source node on this board reports it.

    RAPL does. "psys" is the platform domain and covers essentially the whole
    board, which is what we want; "package-0" is only the CPU package and is
    the fallback. Both are root-only by default since CVE-2020-8694, so this
    returns None until read access is granted and the code falls back to the
    battery.

    Any powercap provider is accepted, not just intel-rapl, so an AMD box that
    exposes the same domains through a differently-named zone works too; only
    top-level zones are considered, never the dram/core sub-zones."""
    global _RAPL
    if _RAPL is None:
        best = None
        for d in sorted(glob.glob("/sys/class/powercap/*:[0-9]")):
            if os.path.basename(d).count(":") != 1:
                continue
            try:
                with open(f"{d}/energy_uj") as f:
                    f.read()
            except OSError:
                continue
            name = read_first(f"{d}/name", default="")
            rank = {"psys": 0, "package-0": 1}.get(name, 2)
            if best is None or rank < best[0]:
                best = (rank, d, read_first(f"{d}/max_energy_range_uj", int, 0), name)
        _RAPL = (best[1], best[2], best[3]) if best else False
    return _RAPL or None


def rapl_watts(prev_uj, elapsed):
    """(watts, energy_uj, domain name). Counter is cumulative and wraps."""
    src = rapl_source()
    if not src or elapsed <= 0:
        return None, None, None
    path, rng, name = src
    now_uj = read_first(f"{path}/energy_uj", int)
    if now_uj is None:
        return None, None, name
    if prev_uj is None:
        return None, now_uj, name
    delta = now_uj - prev_uj
    if delta < 0:
        delta += rng
    if delta < 0:
        return None, now_uj, name
    return delta / 1e6 / elapsed, now_uj, name


def battery():
    """(capacity%, status, watts, eta_s, volts). Handles both power_supply
    models: the charge model (current_now µA, charge_now/charge_full µAh) and
    the energy model (power_now µW, energy_now/energy_full µWh) that a large
    share of laptops expose *instead* — reading only the charge model there gave
    0 W and no ETA on a battery that was plainly discharging. current_now can
    also be signed (negative while discharging), so magnitudes are used."""
    bat = battery_path()
    if not bat:
        return 0, "no battery", 0.0, None, 0.0
    cap = read_first(f"{bat}/capacity", int, 0)
    status = read_first(f"{bat}/status", default="Unknown")
    volt = read_first(f"{bat}/voltage_now", int, 0) or 0
    power_uw = read_first(f"{bat}/power_now", int)
    cur = read_first(f"{bat}/current_now", int)
    if power_uw is not None:
        watts = abs(power_uw) / 1e6
    elif cur is not None and volt:
        watts = abs(cur) / 1e6 * (volt / 1e6)
    else:
        watts = 0.0
    now = read_first(f"{bat}/charge_now", int)
    full = read_first(f"{bat}/charge_full", int)
    rate = abs(cur) if cur is not None else None
    if now is None:
        now = read_first(f"{bat}/energy_now", int)
        full = read_first(f"{bat}/energy_full", int)
        rate = abs(power_uw) if power_uw is not None else None
    eta = None
    if rate and now is not None and full is not None:
        if status == "Discharging":
            eta = now / rate * 3600
        elif status == "Charging" and full > now:
            eta = (full - now) / rate * 3600
    return cap, status, watts, eta, volt / 1e6


def top_procs(prev, elapsed):
    """Per-process CPU% (of one core, like top) and RSS, from /proc."""
    clk = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    scale = 100.0 / (clk * elapsed) if elapsed > 0 else 0.0
    cur, rows = {}, []
    try:
        entries = os.scandir("/proc")
    except OSError:
        return cur, rows
    with entries as it:
        for e in it:
            pid = e.name
            if not pid.isdigit():
                continue
            base = e.path
            try:
                with open(base + "/stat") as f:
                    st = f.read()
                rp = st.rfind(")")
                comm = st[st.find("(") + 1:rp]
                fields = st[rp + 2:].split()
                jiff = int(fields[11]) + int(fields[12])
                with open(base + "/statm") as f:
                    rss = int(f.read().split(" ", 2)[1]) * page
            except (OSError, ValueError, IndexError):
                continue
            cur[pid] = jiff
            pj = prev.get(pid)
            rows.append((comm, max(0.0, (jiff - pj) * scale) if pj is not None else 0.0, rss))
    return cur, rows


_INFLIGHT = set()
_INFLIGHT_LOCK = threading.Lock()


def _refresh_cached(name, argv, path, tmp):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30).stdout
        with open(tmp, "w") as f:
            f.write(out)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(name)


def cached_cmd(name, argv, ttl, ok_prefix=None, fail_ttl=60):
    """Returns the last cached stdout, refreshing it on a worker thread when
    stale so the render itself never blocks on network or disk.

    The refresh runs argv directly — no shell. It used to build a command
    string and hand it to /bin/sh for the `> tmp && mv` dance; a home directory
    containing a space or a shell metacharacter would have been enough to break
    that, and there is no reason to involve a shell at all.

    A cached FAILURE gets a much shorter ttl: the quota endpoint can reject a
    cookie for a moment while Firefox rotates the session, and without this the
    error message sits on the panel for the full five minutes even though the
    next attempt would already succeed."""
    path = os.path.join(CACHE_DIR, f"{name}.txt")
    cached = read_first(path, default="") or ""
    if ok_prefix and cached and not cached.startswith(ok_prefix):
        ttl = min(ttl, fail_ttl)
    age = time.time() - os.path.getmtime(path) if os.path.exists(path) else 1e9
    if age > ttl:
        with _INFLIGHT_LOCK:
            busy = name in _INFLIGHT
            if not busy:
                _INFLIGHT.add(name)
        if not busy:
            threading.Thread(target=_refresh_cached, daemon=True,
                             args=(name, argv, path, f"{path}.tmp")).start()
    return cached


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


_SETTINGS = None
_SETTINGS_MTIME = -1.0


def load_settings():
    """Current settings merged onto the defaults, re-read whenever the file
    changes so the settings window's edits apply without a restart. A pre-
    settings weather.json is folded in once as the location."""
    global _SETTINGS, _SETTINGS_MTIME
    try:
        m = os.path.getmtime(SETTINGS_FILE)
    except OSError:
        m = 0.0
    if _SETTINGS is None or m != _SETTINGS_MTIME:
        data = load_json(SETTINGS_FILE, {})
        if not isinstance(data, dict):
            data = {}
        if "location" not in data:
            legacy = load_json(WEATHER_FILE, None)
            if isinstance(legacy, dict) and "lat" in legacy:
                data["location"] = legacy
        s = dict(DEFAULT_SETTINGS)
        s.update({k: v for k, v in data.items() if k != "sections"})
        sec = dict(DEFAULT_SETTINGS["sections"])
        if isinstance(data.get("sections"), dict):
            sec.update(data["sections"])
        s["sections"] = sec
        # placement moved from single monitor/position/offset keys to a list of
        # panels; migrate the old ones into panel 0 so nothing is lost.
        if not s.get("panels"):
            s["panels"] = [{"monitor": s.get("monitor", 0),
                            "position": s.get("position", "top-right"),
                            "offset": s.get("offset")}]
        _SETTINGS, _SETTINGS_MTIME = s, m
    return _SETTINGS


def save_settings(s):
    save_json(SETTINGS_FILE, s)
    global _SETTINGS_MTIME
    _SETTINGS_MTIME = -1.0          # force a reload on the next read


HISTORY = []
_STATE = None
_PERSISTED = 0.0
PERSIST_EVERY = 30


_PANEL_CACHE = {}


def panel_bg(H):
    """The glass panel behind the content. Depends on nothing but the height,
    so it is built once and reused: rebuilding it per frame meant a radius-12
    Gaussian blur over the whole 840x2232 canvas 30 times a minute for an image
    that never changed."""
    cached = _PANEL_CACHE.get(H)
    if cached is not None:
        return cached

    panel = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    grad = Image.new("RGBA", (1, H * SS))
    gp = grad.load()
    for i in range(H * SS):
        t = i / max(1, H * SS - 1)
        gp[0, i] = (int(20 - 6 * t), int(23 - 6 * t), int(30 - 7 * t), int(208 + 18 * t))
    grad = grad.resize((W * SS, H * SS))
    mask = Image.new("L", (W * SS, H * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, W * SS - 1, H * SS - 1], radius=18 * SS, fill=255)
    panel.paste(grad, (0, 0), mask)

    ImageDraw.Draw(panel).rounded_rectangle(
        [0, 0, W * SS - 1, H * SS - 1], radius=18 * SS, outline=HAIRLINE, width=max(1, SS))

    strip_h = 90 * SS
    hl = Image.new("RGBA", (W * SS, strip_h), (0, 0, 0, 0))
    ImageDraw.Draw(hl).rounded_rectangle(
        [SS, SS, W * SS - 1 - SS, 60 * SS], radius=17 * SS, fill=(255, 255, 255, 12))
    hl = hl.filter(ImageFilter.GaussianBlur(6 * SS))
    panel.alpha_composite(hl, (0, 0))

    _PANEL_CACHE.clear()
    _PANEL_CACHE[H] = panel
    return panel


def render(write_png=True):
    global HISTORY, FLEX, FLEX_POINTS, _STATE, _PERSISTED
    _s = load_settings()
    SECTIONS = _s["sections"]
    UNITS = _s.get("units", "c")
    now = time.time()
    prev = _STATE if _STATE is not None else load_json(STATE_FILE, {})
    elapsed = max(0.001, now - prev.get("t", now - 2))
    if not prev or elapsed > 60:
        elapsed = 2.0

    idle, total = cpu_jiffies()
    di = idle - prev.get("cpu_idle", idle)
    dt = total - prev.get("cpu_total", total)
    cpu_pct = max(0.0, min(100.0, 100.0 * (dt - di) / dt)) if dt > 0 else 0.0

    cores_now = cpu_jiffies_per_core()
    cores_prev = prev.get("cores") or []
    core_loads = []
    for i, (ci, ct) in enumerate(cores_now):
        if i < len(cores_prev):
            pi, pt = cores_prev[i]
            cdt, cdi = ct - pt, ci - pi
            core_loads.append(max(0.0, min(100.0, 100.0 * (cdt - cdi) / cdt)) if cdt > 0 else 0.0)
        else:
            core_loads.append(0.0)

    gsrc = gpu_source()
    gpu_snap = {}
    gpu_pct = None
    if gsrc and gsrc[0] == "busy":
        v = read_first(gsrc[1], int)
        gpu_pct = float(v) if v is not None else None
    elif gsrc and gsrc[0] == "nvsmi":
        raw = cached_cmd("gpu", ["nvidia-smi", "--query-gpu=utilization.gpu",
                                 "--format=csv,noheader,nounits"], 2)
        try:
            gpu_pct = float(raw.strip().splitlines()[0])
        except (ValueError, IndexError):
            gpu_pct = None
    elif gsrc:
        gpu_snap = gpu_engine_snapshot()
        gpu_prev = prev.get("gpu", {})
        g = 0.0
        ns_keys = [k for k in gpu_snap if k.startswith("drm-engine-")]
        if ns_keys:
            for k in ns_keys:
                if k in gpu_prev:
                    g = max(g, 100.0 * (gpu_snap[k] - gpu_prev[k]) / (elapsed * 1e9))
        else:
            for k in gpu_snap:
                if not k.startswith("cyc:"):
                    continue
                tk = "tot:" + k[4:]
                if k in gpu_prev and tk in gpu_prev and tk in gpu_snap:
                    dtot = gpu_snap[tk] - gpu_prev[tk]
                    if dtot > 0:
                        g = max(g, 100.0 * (gpu_snap[k] - gpu_prev[k]) / dtot)
        gpu_pct = g
    if gpu_pct is not None:
        gpu_pct = max(0.0, min(100.0, gpu_pct))
    have_gpu = gpu_pct is not None

    iface = net_iface()
    rx, tx = net_bytes(iface) if iface else (0, 0)
    if prev.get("iface") != iface:
        down = up = 0.0
    else:
        down = max(0.0, (rx - prev.get("rx", rx)) / elapsed)
        up = max(0.0, (tx - prev.get("tx", tx)) / elapsed)

    dr, dw = diskio_sectors()
    rd = max(0.0, (dr - prev.get("dr", dr)) / elapsed)
    wr = max(0.0, (dw - prev.get("dw", dw)) / elapsed)

    cap, bstatus, batt_w, eta, batt_v = battery()
    watts = batt_w
    rapl_w, rapl_uj, rapl_name = rapl_watts(prev.get("rapl_uj"), elapsed)
    power_src = rapl_name if rapl_w is not None else "battery"
    charge_w = 0.0
    if rapl_w is not None:
        if bstatus == "Charging":
            charge_w = batt_w
        watts = rapl_w
    ac_w = (watts + charge_w) if bstatus != "Discharging" else 0.0
    mi = meminfo()
    mem_used = mi["MemTotal"] - mi.get("MemAvailable", mi["MemFree"])
    mem_total = mi["MemTotal"]
    swap_total = mi.get("SwapTotal", 0)
    swap_used = swap_total - mi.get("SwapFree", 0)

    vfs = os.statvfs("/")
    disk_total = vfs.f_blocks * vfs.f_frsize
    disk_used = disk_total - vfs.f_bfree * vfs.f_frsize

    proc_cur, proc_rows = top_procs(prev.get("procs", {}), elapsed)
    top_cpu = sorted(proc_rows, key=lambda r: -r[1])[:3]
    top_mem = sorted(proc_rows, key=lambda r: -r[2])[:3]

    cpu_t, nvme_t, wifi_t = temps()
    uptime = read_first("/proc/uptime", lambda s: float(s.split()[0]), 0)
    load = read_first("/proc/loadavg", lambda s: s.split()[:3], ["?", "?", "?"])

    claude_quota = cached_cmd("claude_quota", [f"{CONF_DIR}/claude_quota.py"], 300,
                              ok_prefix="Session")
    weather_raw = cached_cmd("weather", [f"{CONF_DIR}/weather.py"], 900, ok_prefix="{")

    if not HISTORY:
        HISTORY = load_json(HISTORY_FILE, [])
        if not isinstance(HISTORY, list):
            HISTORY = []
    for sample in HISTORY:
        sample.setdefault("chg", CHG_OUT)
    HISTORY.append({"t": round(now, 1), "cpu": round(cpu_pct, 2),
                    "gpu": round(gpu_pct if have_gpu else 0.0, 2), "down": round(down),
                    "up": round(up), "power": round(watts, 2),
                    "chg_w": round(charge_w, 2),
                    "chg": CHG_IN if bstatus == "Charging" else
                           (CHG_OUT if bstatus == "Discharging" else CHG_IDLE)})
    HISTORY = [s for s in HISTORY if now - s.get("t", 0) <= HIST_MAX_AGE + 5]

    new_state = {
        "t": now, "cpu_idle": idle, "cpu_total": total, "cores": cores_now, "gpu": gpu_snap,
        "rx": rx, "tx": tx, "dr": dr, "dw": dw, "procs": proc_cur, "iface": iface,
        "rapl_uj": rapl_uj,
    }

    FLEX = float(prev.get("flex", 0.0))
    FLEX_POINTS = 0
    img = Image.new("RGBA", (W * SS, 1400 * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 22
    R = W - PAD

    f_val    = F(MONO_MED, T_VALUE)
    f_val_sm = F(MONO_REG, T_BODY)
    f_big    = F(MONO_LIGHT, T_LEAD)

    sess = week = None
    if "Session" in claude_quota:
        try:
            for part in [p.strip() for p in claude_quota.split("|")]:
                pct = int(part.split("%")[0].split()[-1])
                rest = part.split("(")[1].rstrip(")") if "(" in part else ""
                if part.startswith("Session"):
                    sess = (pct, rest)
                elif part.startswith("Week"):
                    week = (pct, rest)
        except Exception:
            pass
    have_claude = bool(sess or week)

    weather = None
    if weather_raw.strip().startswith("{"):
        try:
            weather = json.loads(weather_raw)
        except Exception:
            pass
    have_weather = weather is not None

    ALT_PERIOD = 8
    if have_claude and have_weather:
        slot = "weather" if int(time.time() // ALT_PERIOD) % 2 else "claude"
    elif have_claude:
        slot = "claude"
    elif have_weather:
        slot = "weather"
    else:
        slot = None

    SLOT_H = 95
    slot_start = y
    if slot == "claude":
        label(d, PAD, y, "claude", CORAL, tracking=2.4)
        y += 23
        for name, item in (("session", sess), ("week", week)):
            if not item:
                continue
            pct, resets = item
            f_pct = F(MONO_MED, T_VALUE)
            label(d, PAD, y + 3, name, CORAL, size=T_MICRO)
            text(d, R, y, f"{pct}%", f_pct, CORAL, anchor="r")
            text(d, R - measure(f_pct, f"{pct}%") / SS - 11, y + 3,
                 f"resets {resets}", F(UI_MED, T_BODY), TEXT, anchor="r")
            y += 19
            bar(img, PAD, y, CW, 8, pct / 100, ramp=True)
            y += 17
    elif slot == "weather":
        label(d, PAD, y, "weather", ACCENT, tracking=2.4)
        if weather.get("name"):
            label_r(d, R, y, weather["name"], TEXT, size=T_MICRO)
        t = weather["temp"]
        f_temp = F(MONO_LIGHT, T_HERO)
        icon_s = 22
        ttxt = temp_str(t, UNITS)
        tw = measure(f_temp, ttxt) / SS
        iw = icon_s * 2.5
        gap_it = 14
        group_w = iw + gap_it + tw
        gx = PAD + (CW - group_w) / 2.0
        hero_cy = slot_start + 46
        draw_weather_icon(img, gx + iw / 2, hero_cy, icon_s, weather.get("code", 3))
        text(d, gx + iw + gap_it, hero_cy - 21, ttxt, f_temp, TEXT)
        dy = slot_start + 80
        f_dv = F(MONO_REG, T_VALUE)
        f_dl = F(UI_SEMI, T_MICRO)
        cells = (("min", weather['lo']), ("max", weather['hi']),
                 ("feels", weather['feels']))
        colw = CW / 3.0
        for i, (lab, tval) in enumerate(cells):
            val = temp_str(tval, UNITS)
            vcol = weather_temp_color(tval)
            cx = PAD + colw * (i + 0.5)
            lw = measure(f_dl, lab.upper(), 1.4) / SS
            vw = measure(f_dv, val) / SS
            gap_lv = 7
            x0 = cx - (lw + gap_lv + vw) / 2.0
            text(d, x0, dy + 3, lab.upper(), f_dl, TEXT, tracking=1.4)
            text(d, x0 + lw + gap_lv, dy, val, f_dv, vcol)

    if slot:
        y = slot_start + SLOT_H
        y += gap(26)

    label(d, PAD, y, "uptime", ACCENT)
    label_r(d, R, y, "load 1·5·15m", ACCENT)
    y += 14
    text(d, PAD, y, fmt_dur(uptime), f_big, TEXT)
    f_load = F(MONO_REG, T_VALUE)
    lx = R
    for v in reversed(load):
        text(d, lx, y + 6, v, f_load, load_color(float(v or 0), len(core_loads) or 1),
             anchor="r")
        lx -= measure(f_load, v) / SS + 9
    y += gap(40)

    gr, gth = 46, 9
    gauges = [("cpu", cpu_pct, ACCENT)]
    if have_gpu:
        gauges.append(("gpu", gpu_pct, VIOLET))
    gauges.append(("ram", mem_used / mem_total * 100, TEAL))
    slot = CW / len(gauges)
    for i, (name, pct, hue) in enumerate(gauges):
        cx = PAD + slot * (i + 0.5)
        cy = y + gr + 4
        col = state_color(pct, hue)
        gauge(img, cx, cy, gr, gth, pct / 100, col)
        big, unit = f"{pct:.0f}", "%"
        f_g, f_u = F(MONO_LIGHT, T_HERO), F(MONO_REG, T_BODY)
        bw, uw = measure(f_g, big) / SS, measure(f_u, unit) / SS
        x0 = cx - (bw + 2 + uw) / 2
        text(d, x0, cy - 18, big, f_g, TEXT)
        text(d, x0 + bw + 3, cy - 6, unit, f_u, TEXT)
        lw = measure(F(UI_SEMI, T_LABEL), name.upper(), 1.8) / SS
        label(d, cx - lw / 2, cy + gr + 10, name, hue, tracking=1.8)
    y += 2 * gr + gap(36)

    label(d, PAD, y, "cores", ACCENT)
    rx_ = R
    ghz = cpu_freq_ghz()
    if ghz > 0:
        clock = f"{ghz:.2f} GHz"
        text(d, rx_, y - 1, clock, F(MONO_REG, T_BODY), TEXT, anchor="r")
        rx_ -= measure(F(MONO_REG, T_BODY), clock) / SS + 12
    label_r(d, rx_, y, f"{len(core_loads)} threads", TEXT)
    y += 15
    core_strip(img, PAD, y, CW, 24, core_loads)
    y += 24 + gap(14)

    label(d, PAD, y, "history", ACCENT)
    label_r(d, R, y, "60 min", TEXT)
    y += 15
    histogram(img, PAD, y, CW, 64, "cpu", ACCENT, floor=10, clamp=100,
              overlay="gpu" if have_gpu else None, overlay_color=VIOLET)
    y += 64 + 7
    if have_gpu:
        lx = PAD
        for col, txt in ((ACCENT, "cpu"), (VIOLET, "gpu")):
            swatch(d, lx, y, col)
            text(d, lx + 12, y, txt, F(UI_MED, T_LABEL), col)
            lx += 12 + measure(F(UI_MED, T_LABEL), txt) / SS + 18
        y += 24
    else:
        y += 6

    sel = _s.get("sensors")
    if sel:
        by_id = {s["id"]: s for s in list_sensors()}
        chosen = [by_id[i] for i in sel if i in by_id][:4]
        therms = [(s["label"].lower(), read_sensor(s["path"]), s["warn"], s["crit"])
                  for s in chosen]
    else:
        therms = (("cpu", cpu_t, 80, 95), ("ssd", nvme_t, 65, 75),
                  ("wifi", wifi_t, 75, 85))
    f_tl, f_tv = F(UI_SEMI, T_MICRO), F(MONO_REG, T_BODY)
    groups = []
    for lab, tv, warn, crit in therms:
        if tv is None:
            continue
        val = temp_str(tv, UNITS)
        lw = measure(f_tl, lab.upper(), 1.4) / SS
        vw = measure(f_tv, val) / SS
        groups.append((lab.upper(), lw, val, vw, temp_gradient(tv, warn, crit)))
    if groups and SECTIONS.get("thermals", True):
        label(d, PAD, y, "thermals", RED)
        gap_lv, gap_gg = 6, 20
        total = sum(lw + gap_lv + vw for _, lw, _, vw, _ in groups) + gap_gg * (len(groups) - 1)
        gx = R - total
        for labu, lw, val, vw, col in groups:
            text(d, gx, y + 1, labu, f_tl, TEXT, tracking=1.4)
            text(d, gx + lw + gap_lv, y - 1, val, f_tv, col)
            gx += lw + gap_lv + vw + gap_gg
        y += gap(33)

    mfrac = mem_used / mem_total
    label(d, PAD, y, "memory", TEAL)
    text(d, R, y - 2, f"{fmt_bytes(mem_used)} / {fmt_bytes(mem_total)}", f_val, TEXT, anchor="r")
    y += 16
    bar(img, PAD, y, CW, 6, mfrac, state_color(mfrac * 100, TEAL))
    y += 16

    if swap_total:
        sfrac = swap_used / swap_total
        label(d, PAD, y, "swap", TEAL, size=T_MICRO)
        text(d, R, y - 2, f"{fmt_bytes(swap_used)} / {fmt_bytes(swap_total)}", f_val_sm, TEXT, anchor="r")
        y += 14
        bar(img, PAD, y, CW, 4, sfrac, state_color(sfrac * 100, TEAL))
        y += 8
    y += gap(22)

    label(d, PAD, y, "disk", PINK)
    infos = []
    for mp in (_s.get("disks") or ["/"]):
        try:
            vfs2 = os.statvfs(mp)
            tot = vfs2.f_blocks * vfs2.f_frsize
            usd = tot - vfs2.f_bfree * vfs2.f_frsize
            if tot:
                infos.append((mp, usd, tot))
        except OSError:
            pass
    if not infos:
        infos = [("/", disk_used, disk_total)]
    if len(infos) == 1:
        mp, usd, tot = infos[0]
        text(d, R, y - 2, f"{fmt_bytes(usd)} / {fmt_bytes(tot)}", f_val, TEXT, anchor="r")
        y += 16
        bar(img, PAD, y, CW, 6, usd / tot, state_color(usd / tot * 100, PINK))
        y += 15
    else:
        y += 18
        for mp, usd, tot in infos:
            label(d, PAD, y, mp, PINK, size=T_MICRO)
            text(d, R, y - 2, f"{fmt_bytes(usd)} / {fmt_bytes(tot)}", f_val_sm, TEXT, anchor="r")
            y += 14
            bar(img, PAD, y, CW, 5, usd / tot, state_color(usd / tot * 100, PINK))
            y += 13
    text(d, PAD, y, "read", F(UI_MED, T_BODY), TEXT)
    text(d, PAD + 34, y, fmt_bytes(rd, True), f_val_sm, TEXT)
    text(d, R, y, fmt_bytes(wr, True), f_val_sm, TEXT, anchor="r")
    text(d, R - measure(f_val_sm, fmt_bytes(wr, True)) / SS - 9, y, "write", F(UI_MED, T_BODY), TEXT, anchor="r")
    y += gap(30)

    if SECTIONS.get("network", True):
        label(d, PAD, y, "network", ACCENT)
        y += 16
        peak = net_chart(img, PAD, y, CW, 60, ACCENT, CORAL, floor=64 * 1024)
        y += 60 + 6
        text(d, PAD, y, f"↓ {fmt_bytes(down, True)}", f_val_sm, ACCENT)
        text(d, W / 2, y, f"peak {fmt_bytes(peak, True)}", F(MONO_REG, T_LABEL), TEXT, anchor="c")
        text(d, R, y, f"↑ {fmt_bytes(up, True)}", f_val_sm, CORAL, anchor="r")
        y += gap(30)

    charging = bstatus == "Charging"
    have_battery = bstatus != "no battery"
    have_power = power_src != "battery" or have_battery
    if have_power and SECTIONS.get("power", True):
        label(d, PAD, y, "power", AMBER)
        ux = R
        if ac_w > 0.05 and have_battery:
            atxt = f"{ac_w:.1f} W"
            text(d, ux, y - 2, atxt, f_val, TEXT, anchor="r")
            ux -= measure(f_val, atxt) / SS + 6
            label_r(d, ux, y, "ac", TEXT, size=T_MICRO)
            ux -= measure(F(UI_SEMI, T_MICRO), "AC", 1.6) / SS + 14
        dtxt = f"{watts:.1f} W"
        text(d, ux, y - 2, dtxt, f_val, consumption_color(1.0 if bstatus != "Discharging" else 0.0)
             if watts > 0.05 else MUTE, anchor="r")
        ux -= measure(f_val, dtxt) / SS + 6
        label_r(d, ux, y, "device", TEXT, size=T_MICRO)
        ux -= measure(F(UI_SEMI, T_MICRO), "DEVICE", 1.6) / SS + 14
        if power_src == "battery":
            label_r(d, ux, y, "battery", TEXT, size=T_MICRO)
        y += 16
        power_chart(img, PAD, y, CW, 30, floor=8)
        y += 30
        if have_battery:
            y += 7
            lx = PAD
            for col, txt, is_line in ((AMBER, "on ac", False), (DARKRED, "on battery", False),
                                      (GREEN, "charging", False), (TEXT, "total", True)):
                swatch(d, lx, y, col, line=is_line)
                text(d, lx + (15 if is_line else 12), y, txt, F(UI_MED, T_LABEL), col)
                lx += (15 if is_line else 12) + measure(F(UI_MED, T_LABEL), txt) / SS + 16
            y += 12
        y += gap(20)

    if have_battery:
        full = bstatus == "Full" or cap >= 100
        if charging or full:
            bcol = GREEN
        elif bstatus == "Discharging":
            bcol = ramp_rgb(1 - cap / 100)
        else:
            bcol = STEEL
        scol = GREEN if (charging or full) else (DARKRED if bstatus == "Discharging" else TEXT)
        label(d, PAD, y, "battery", AMBER)
        if batt_v > 0.05:
            text(d, PAD + measure(F(UI_SEMI, T_LABEL), "BATTERY", 1.6) / SS + 11, y - 1,
                 f"{batt_v:.1f} V", F(MONO_REG, T_BODY), TEXT)
        bx = R
        if eta:
            text(d, bx, y - 1, fmt_dur(eta), F(MONO_REG, T_BODY), TEXT, anchor="r")
            bx -= measure(F(MONO_REG, T_BODY), fmt_dur(eta)) / SS + 12
        if batt_w > 0.05 and bstatus in ("Charging", "Discharging"):
            wtxt = f"{batt_w:.1f} W"
            text(d, bx, y - 1, wtxt, F(MONO_REG, T_BODY), scol, anchor="r")
            bx -= measure(F(MONO_REG, T_BODY), wtxt) / SS + 12
        text(d, bx, y - 1, bstatus.lower(), F(UI_MED, T_BODY), scol, anchor="r")
        bx -= measure(F(UI_MED, T_BODY), bstatus.lower()) / SS + 12
        text(d, bx, y - 2, f"{cap}%", f_val, bcol, anchor="r")
        y += 16
        bar(img, PAD, y, CW, 6, cap / 100, bcol)
        y += 18
        y += gap(10)

    # ============ DEVICES (peripheral batteries) =================
    periph_sel = _s.get("peripherals") or []
    if periph_sel:
        devs = [p for p in peripheral_batteries()
                if p["id"] in periph_sel and p["capacity"] is not None]
        if devs:
            label(d, PAD, y, "devices", VIOLET)
            y += 18
            for p in devs:
                cap = p["capacity"]
                col = GREEN if p["status"] == "Charging" else ramp_rgb(1 - cap / 100)
                text(d, PAD, y - 2, p["name"][:26], F(UI_MED, T_BODY), TEXT)
                text(d, R, y - 2, f"{cap}%", f_val, col, anchor="r")
                y += 15
                bar(img, PAD, y, CW, 5, cap / 100, col)
                y += 13
            y += gap(20)

    def proc_list(y, title, hue, rows, value_of, fmt_of, colour_of,
                  right=None, total=None, curve=1.0):
        """total: denominator for the bars. Given one, a bar shows the share of
        that whole — an absolute figure. Without one it falls back to the
        largest row, which only restates the sort order the rows already
        carry.

        curve: exponent on that share. 1.0 is linear. 0.5 takes the square
        root, which lifts small values off the floor — against the whole
        machine a single saturated core is 5% of the width, and an idle
        process a couple of pixels. The cost is that lengths stop being
        proportional: twice the load draws 1.4x the bar, not 2x. Acceptable
        because the figure beside it is exact and the rows are sorted, so the
        bar is doing shape rather than measurement."""
        label(d, PAD, y, title, hue, tracking=1.7)
        if right:
            label_r(d, R, y, right, TEXT, size=T_MICRO)
        y += 18
        peak = total if total else max([value_of(r) for r in rows] + [1e-9])
        for i, r in enumerate(rows):
            col = colour_of(r)
            row_bar(img, PAD, y - 4, CW, 20,
                    max(0.0, min(1.0, value_of(r) / peak)) ** curve, col)
            text(d, PAD + 7, y, r[0][:22], F(UI_MED, T_BODY), TEXT)
            text(d, R - 7, y, fmt_of(r),
                 F(MONO_MED, T_BODY) if i == 0 else F(MONO_REG, T_BODY), col, anchor="r")
            y += 22
        return y

    if SECTIONS.get("processes", True):
        y = proc_list(y, "top cpu", ACCENT, top_cpu,
                      value_of=lambda r: r[1],
                      fmt_of=lambda r: f"{r[1]:.1f}%",
                      colour_of=lambda r: ACCENT if r[1] > 1 else MUTE,
                      right="% of one core", total=100.0 * max(1, len(core_loads)),
                      curve=0.5)
        y += gap(14)
        y = proc_list(y, "top memory", TEAL, top_mem,
                      value_of=lambda r: r[2],
                      fmt_of=lambda r: fmt_bytes(r[2]),
                      colour_of=lambda r: TEAL,
                      right="share of ram", total=mem_total, curve=0.5)
        y += 22
    H = int(round(y))

    # The panel renders at its natural content height and no longer stretches
    # its gaps to fill the monitor: that let the resize grip only ever shrink
    # it (it was already full height). Size is the user's to set now, capped by
    # auto-fit so it can grow up to the monitor without overflowing.
    next_flex = 0.0

    panel = panel_bg(H)

    out = Image.alpha_composite(panel, img.crop((0, 0, W * SS, H * SS)))
    out = out.resize((W, H), Image.BOX)

    if write_png:
        tmp = f"{PNG_PATH}.{os.getpid()}.tmp"
        out.save(tmp, "PNG", compress_level=1)
        os.replace(tmp, PNG_PATH)

    new_state["flex"] = next_flex
    _STATE = new_state
    if now - _PERSISTED > PERSIST_EVERY:
        _PERSISTED = now
        save_json(STATE_FILE, new_state)
        save_json(HISTORY_FILE, HISTORY)

    return out


def log(msg):
    """Append one line to cache/hud.log, trimming it when it gets long.

    The watchdog starts the renderer with stderr pointed at /dev/null, so a
    traceback printed there went nowhere: a crash looked exactly like a stopped
    clock, with no way to find out why. Failures go to a file instead."""
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 256 * 1024:
            with open(LOG_FILE) as f:
                tail = f.readlines()[-200:]
            with open(LOG_FILE, "w") as f:
                f.writelines(tail)
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except OSError:
        pass


def surface_from(img):
    """PIL image -> cairo surface, without going through PNG.

    cairo's ARGB32 is premultiplied and byte-ordered BGRA on a little-endian
    machine; Pillow's "RGBa" mode is the premultiplied one. Encoding to PNG and
    decoding again costs 21ms a frame against 4ms for this."""
    import cairo
    prem = img.convert("RGBa")
    b, g, r, a = (prem.getchannel(i) for i in (2, 1, 0, 3))
    buf = bytearray(Image.merge("RGBA", (b, g, r, a)).tobytes())
    surf = cairo.ImageSurface.create_for_data(
        memoryview(buf), cairo.FORMAT_ARGB32, img.width, img.height, img.width * 4)
    return surf, buf


def run_window(interval=2.0):
    """Paint the panel(s) onto the desktop and keep them updated.

    One or two click-through ARGB desktop windows (from settings["panels"]),
    all showing the same rendered frame, each on its own monitor and spot. A
    panel is repositioned by hand: the settings Move button sets
    settings["move"] to a panel index, that window takes the pointer, and
    releasing the drag saves its monitor and offset."""
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, Gdk, GLib
    import cairo
    import math

    def make_window():
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_app_paintable(True)
        win.set_decorated(False)
        win.set_resizable(False)
        win.set_type_hint(Gdk.WindowTypeHint.DESKTOP)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_keep_below(True)
        win.set_accept_focus(False)
        win.set_focus_on_map(False)
        win.stick()
        win.set_title("linux-mint-hud")
        vis = win.get_screen().get_rgba_visual()
        if vis is not None:
            win.set_visual(vis)
        return win

    def mon_index(disp, mon):
        mg = mon.get_geometry()
        for i in range(disp.get_n_monitors()):
            g = disp.get_monitor(i).get_geometry()
            if (g.x, g.y, g.width, g.height) == (mg.x, mg.y, mg.width, mg.height):
                return i
        return 0

    def draw_banner(cr, win):
        w = win.get_allocated_width()
        txt = "drag to place"
        cr.select_font_face("sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(12.5)
        tw = cr.text_extents(txt).width
        pw, ph, py = tw + 62, 30, 12
        px = (w - pw) / 2
        rad = ph / 2

        def rrect(x, y, ww, hh, r):
            cr.new_sub_path()
            cr.arc(x + ww - r, y + r, r, -math.pi / 2, 0)
            cr.arc(x + ww - r, y + hh - r, r, 0, math.pi / 2)
            cr.arc(x + r, y + hh - r, r, math.pi / 2, math.pi)
            cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
            cr.close_path()

        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_source_rgba(0, 0, 0, 0.30)
        rrect(px, py + 2, pw, ph, rad)
        cr.fill()
        cr.set_source_rgba(0.23, 0.51, 0.96, 0.97)
        rrect(px, py, pw, ph, rad)
        cr.fill()
        gx, gy, a, hd = px + 22, py + ph / 2, 7, 3.0
        cr.set_source_rgba(1, 1, 1, 1)
        cr.set_line_width(1.7)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            tx, ty = gx + dx * a, gy + dy * a
            perpx, perpy = -dy, dx
            cr.move_to(gx, gy)
            cr.line_to(tx, ty)
            cr.move_to(tx, ty)
            cr.line_to(tx - dx * hd + perpx * hd, ty - dy * hd + perpy * hd)
            cr.move_to(tx, ty)
            cr.line_to(tx - dx * hd - perpx * hd, ty - dy * hd - perpy * hd)
        cr.stroke()
        cr.move_to(px + 40, py + ph / 2 + 4.5)
        cr.show_text(txt)

    def keep_above_desktop(win):
        gw = win.get_window()
        if gw is None:
            return
        stack = win.get_screen().get_window_stack() or []
        if len(stack) < 2:
            return
        xids = [w.get_xid() for w in stack]
        if xids and xids[0] == gw.get_xid():
            win.hide()
            win.show()

    def place(pw, cfg, w, h):
        global TARGET_H
        win = pw["win"]
        disp = Gdk.Display.get_default()
        mon = (disp.get_monitor(cfg.get("monitor", 0))
               or disp.get_primary_monitor() or disp.get_monitor(0))
        wa = mon.get_workarea()
        margin = load_settings().get("margin", MARGIN)
        pos = cfg.get("position", "top-right")
        off = cfg.get("offset")
        if pw["idx"] == 0:
            TARGET_H = max(200, wa.height - 2 * margin)
        if pos == "free" and isinstance(off, (list, tuple)) and len(off) == 2:
            x, yy = wa.x + int(off[0]), wa.y + int(off[1])
            # a free panel still keeps at least the edge margin from every side
            x = max(wa.x + margin, min(x, wa.x + wa.width - w - margin))
            yy = max(wa.y + margin, min(yy, wa.y + wa.height - h - margin))
        else:
            x = wa.x + (wa.width - w - margin if pos.endswith("right") else margin)
            yy = wa.y + (wa.height - h - margin if pos.startswith("bottom") else margin)
            x = max(wa.x, min(x, wa.x + wa.width - w))
            yy = max(wa.y, min(yy, wa.y + wa.height - h))
        win.set_size_request(w, h)
        win.move(x, yy)

    panels = []

    def snapped(pw, x, y, w, h):
        """Nudge a dragged window onto tidy targets: the monitor's own margins
        (so it clicks into a corner or an even top/bottom/side gap), and the
        edges and top of any other panel (so a second panel lines up to the
        same height and side distance)."""
        disp = Gdk.Display.get_default()
        mon = disp.get_monitor_at_point(x + w // 2, y + h // 2) or disp.get_primary_monitor()
        wa = mon.get_workarea()
        margin = load_settings().get("margin", MARGIN)
        SNAP = 26
        sx = [wa.x + margin, wa.x + wa.width - w - margin]
        sy = [wa.y + margin, wa.y + wa.height - h - margin]
        for other in panels:
            if other is pw:
                continue
            ow = other["win"]
            try:
                ox, oy = ow.get_position()
                oaw, oah = ow.get_allocated_width(), ow.get_allocated_height()
            except Exception:
                continue
            omon = disp.get_monitor_at_point(ox + oaw // 2, oy + oah // 2) or mon
            owa = omon.get_workarea()
            sy.append(wa.y + (oy - owa.y))
            sy.append(wa.y + wa.height - h - ((owa.y + owa.height) - (oy + oah)))
            sx.append(wa.x + (ox - owa.x))
            sx.append(wa.x + wa.width - w - ((owa.x + owa.width) - (ox + oaw)))
        for t in sx:
            if abs(x - t) <= SNAP:
                x = t
                break
        for t in sy:
            if abs(y - t) <= SNAP:
                y = t
                break
        return x, y
    base = {"img": None}                 # the latest native-size rendered frame
    GRIP = 30                            # size of the resize handle, in px

    def scaled_for(cfg):
        """The panel image scaled by cfg['scale'], then clamped so it never
        exceeds the monitor's usable height (auto-fit is the safety net)."""
        img = base["img"]
        if img is None:
            return None
        disp = Gdk.Display.get_default()
        scale = float(cfg.get("scale", 1.0) or 1.0)
        w, h = max(80, round(img.width * scale)), max(80, round(img.height * scale))
        mon = (disp.get_monitor(cfg.get("monitor", 0))
               or disp.get_primary_monitor() or disp.get_monitor(0))
        usable = mon.get_workarea().height - load_settings().get("margin", MARGIN)
        if h > usable > 0:
            k = usable / h
            w, h = max(80, round(w * k)), max(80, round(h * k))
        if (w, h) == (img.width, img.height):
            return img
        return img.resize((w, h), Image.LANCZOS)

    def paint(pw, cfg):
        """Rebuild a panel's surface from the current frame at its scale."""
        dimg = scaled_for(cfg)
        if dimg is None:
            return None
        st = pw["state"]
        st["surface"], st["buf"] = surface_from(dimg)
        st["w"], st["h"] = dimg.width, dimg.height
        return dimg

    def setup(pw):
        win, st = pw["win"], pw["state"]

        def on_draw(_w, cr):
            if st["surface"] is not None:
                cr.set_operator(cairo.OPERATOR_SOURCE)
                cr.set_source_surface(st["surface"], 0, 0)
                cr.paint()
            if st.get("moving"):
                draw_banner(cr, win)
                w, h = win.get_allocated_width(), win.get_allocated_height()
                cx, cy = w - GRIP / 2 - 4, h - GRIP / 2 - 4
                cr.set_operator(cairo.OPERATOR_OVER)
                cr.set_source_rgba(0, 0, 0, 0.30)
                cr.arc(cx, cy + 1.5, GRIP / 2, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgba(0.23, 0.51, 0.96, 0.97)
                cr.arc(cx, cy, GRIP / 2, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgba(1, 1, 1, 0.95)
                cr.set_line_width(1.6)
                cr.set_line_cap(cairo.LINE_CAP_ROUND)
                for d in (1.5, 5.5):
                    cr.move_to(cx - 5 + d, cy + 5)
                    cr.line_to(cx + 5, cy - 5 + d)
                cr.stroke()
            return False

        def _cfg_of():
            cfgs = load_settings().get("panels") or []
            if pw["idx"] < len(cfgs):
                return dict(cfgs[pw["idx"]])
            return {"monitor": 0, "position": "top-right", "offset": None, "scale": 1.0}

        def on_realize(_w):
            win.get_window().input_shape_combine_region(cairo.Region(), 0, 0)

        def enter_move():
            gw = win.get_window()
            if gw is None:
                return
            w = win.get_allocated_width() or W
            h = win.get_allocated_height() or st.get("h") or 1000
            gw.input_shape_combine_region(cairo.Region(cairo.RectangleInt(0, 0, w, h)), 0, 0)
            win.set_keep_below(False)
            win.set_keep_above(True)
            st["moving"] = True
            win.queue_draw()

        def exit_move():
            gw = win.get_window()
            if gw is not None:
                gw.input_shape_combine_region(cairo.Region(), 0, 0)
            win.set_keep_above(False)
            win.set_keep_below(True)
            st["moving"] = False
            win.queue_draw()

        st["enter_move"], st["exit_move"] = enter_move, exit_move
        drag = st["drag"]

        def on_press(_w, ev):
            if not st.get("moving"):
                return False
            drag["active"] = True
            drag["sx"], drag["sy"] = ev.x_root, ev.y_root
            w, h = win.get_allocated_width(), win.get_allocated_height()
            if ev.x >= w - GRIP - 8 and ev.y >= h - GRIP - 8:
                drag["mode"] = "resize"
                drag["sscale"] = float(_cfg_of().get("scale", 1.0) or 1.0)
                st["rscale"] = drag["sscale"]
            else:
                drag["mode"] = "move"
                drag["wx"], drag["wy"] = win.get_position()
            return True

        def on_motion(_w, ev):
            if not (st.get("moving") and drag["active"]):
                return False
            if drag.get("mode") == "resize":
                sc = max(0.5, min(2.0, drag["sscale"] + (ev.x_root - drag["sx"]) / W))
                st["rscale"] = sc
                st["resizing"] = True
                cfg = _cfg_of()
                cfg["scale"] = sc
                dimg = paint(pw, cfg)
                if dimg is not None:
                    win.set_size_request(dimg.width, dimg.height)
                win.queue_draw()
            else:
                nx = int(drag["wx"] + (ev.x_root - drag["sx"]))
                ny = int(drag["wy"] + (ev.y_root - drag["sy"]))
                w, h = win.get_allocated_width(), win.get_allocated_height()
                nx, ny = snapped(pw, nx, ny, w, h)
                win.move(nx, ny)
            return False

        def on_release(_w, ev):
            if not st.get("moving"):
                return False
            drag["active"] = False
            s = dict(load_settings())
            cfgs = [dict(c) for c in (s.get("panels") or [])]
            while len(cfgs) <= pw["idx"]:
                cfgs.append({"monitor": 0, "position": "top-right", "offset": None})
            if drag.get("mode") == "resize":
                cfgs[pw["idx"]]["scale"] = round(st.get("rscale", 1.0), 3)
                st["resizing"] = False
            else:
                wx, wy = win.get_position()
                ww = win.get_allocated_width() or W
                wh = win.get_allocated_height() or (st["h"] or 0)
                disp = Gdk.Display.get_default()
                mon = (disp.get_monitor_at_point(wx + ww // 2, wy + (st["h"] or 0) // 2)
                       or disp.get_primary_monitor())
                wa = mon.get_workarea()
                margin = load_settings().get("margin", MARGIN)
                scale = cfgs[pw["idx"]].get("scale", 1.0)
                mi = mon_index(disp, mon)
                # dropped in a corner (the snap put it exactly at the margin) ->
                # anchor to that corner, so the edge-margin setting then controls
                # its gap. Otherwise keep the exact spot as a free offset.
                left = abs(wx - (wa.x + margin)) <= 6
                right = abs((wx + ww) - (wa.x + wa.width - margin)) <= 6
                top = abs(wy - (wa.y + margin)) <= 6
                bottom = abs((wy + wh) - (wa.y + wa.height - margin)) <= 6
                if (left or right) and (top or bottom):
                    pos = ("bottom" if bottom else "top") + ("-right" if right else "-left")
                    cfgs[pw["idx"]] = {"monitor": mi, "position": pos, "offset": None, "scale": scale}
                else:
                    cfgs[pw["idx"]] = {"monitor": mi, "position": "free",
                                       "offset": [wx - wa.x, wy - wa.y], "scale": scale}
            # stay in move mode after a drag: the settings' "Save position"
            # button (which clears settings["move"]) is what ends it, so the
            # user can nudge or resize repeatedly first.
            s["panels"] = cfgs
            save_settings(s)
            st["placekey"] = None
            return True

        def on_destroy(_w):
            if not pw.get("closing"):
                Gtk.main_quit()

        win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                       | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON1_MOTION_MASK)
        win.connect("draw", on_draw)
        win.connect("realize", on_realize)
        win.connect("button-press-event", on_press)
        win.connect("motion-notify-event", on_motion)
        win.connect("button-release-event", on_release)
        win.connect("destroy", on_destroy)

    def sync_count(n):
        while len(panels) < n:
            pw = {"win": make_window(), "idx": len(panels), "closing": False,
                  "state": {"surface": None, "buf": None, "h": 0, "moving": False,
                            "drag": {"active": False, "sx": 0, "sy": 0, "wx": 0, "wy": 0},
                            "placekey": None}}
            setup(pw)
            panels.append(pw)
            pw["win"].show_all()
        while len(panels) > n:
            pw = panels.pop()
            pw["closing"] = True
            pw["win"].destroy()

    def tick():
        try:
            base["img"] = render(write_png=False)   # native frame; scaled per panel
            s = load_settings()
            cfgs = s.get("panels") or [{"monitor": 0, "position": "top-right", "offset": None}]
            sync_count(max(1, len(cfgs)))
            move_idx = s.get("move")
            if move_idx is True:
                move_idx = 0
            for i, pw in enumerate(panels):
                st = pw["state"]
                pw["idx"] = i
                cfg = cfgs[i] if i < len(cfgs) else {}
                if move_idx == i and not st.get("moving"):
                    st["enter_move"]()
                elif move_idx != i and st.get("moving"):
                    st["exit_move"]()
                if not st.get("resizing"):       # don't fight a live resize drag
                    paint(pw, cfg)
                if not st.get("moving"):
                    key = (st.get("w"), st.get("h"), cfg.get("monitor"), cfg.get("position"),
                           cfg.get("scale"), s.get("margin"), tuple(cfg.get("offset") or ()))
                    if key != st.get("placekey"):
                        st["placekey"] = key
                        place(pw, cfg, st["w"], st["h"])
                    keep_above_desktop(pw["win"])
                pw["win"].queue_draw()
            tick.fails = 0
        except Exception:
            tick.fails = getattr(tick, "fails", 0) + 1
            if tick.fails <= 3:
                log(f"render failed:\n{traceback.format_exc().rstrip()}")
                if tick.fails == 3:
                    log("further identical failures will not be logged")
        return True

    tick()
    GLib.timeout_add(int(interval * 1000), tick)
    log(f"panel started (pid {os.getpid()})")
    Gtk.main()


def run_settings():
    """A small GTK window that reads and writes settings.json. Nothing here is
    hand-edited: this is the graphical front for it, and a running panel picks
    up the saved file within a second."""
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, Gdk
    import urllib.parse
    import urllib.request

    Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
    css = b"""
    window { background-color: #000000; }
    scrolledwindow, scrolledwindow viewport, viewport, .content { background-color: #000000; }
    label { color: #cfd6e0; font-size: 13px; }

    /* Header */
    .app-title { color: #e9eef5; font-size: 18px; font-weight: 800; }
    .app-subtitle { color: #e9eef5; font-size: 12px; }

    /* Grouped cards */
    .group {
        background-color: #191c22;
        border: 1px solid #262b33;
        border-radius: 14px;
    }
    .group-heading {
        color: #60b0ff; font-size: 11px; font-weight: 800;
        letter-spacing: 1.4px;
    }
    .field-label { color: #e9eef5; font-size: 13px; }

    /* Inputs */
    entry, spinbutton, spinbutton entry, combobox button {
        background-image: none; background-color: #23262c; color: #e9eef5;
        border: 1px solid #333a44; border-radius: 9px;
        padding: 6px 10px; caret-color: #60b0ff;
    }
    entry { padding: 7px 11px; }
    entry image { color: #8a94a4; }
    entry:focus, spinbutton:focus, spinbutton entry:focus,
    combobox button:focus, combobox:focus button {
        border-color: #3b82f6;
    }
    spinbutton, spinbutton entry { border-radius: 9px; }
    spinbutton { background-color: #000000; border-color: #000000; padding: 0; }
    spinbutton button { min-height: 0; min-width: 16px; padding: 0 2px; margin: 3px; }
    spinbutton button image { -gtk-icon-transform: scale(0.62); }
    combobox button { padding: 6px 10px; }
    combobox arrow { color: #8a94a4; min-height: 14px; min-width: 14px; }

    /* Buttons */
    button {
        background-image: none; background-color: #23262c; color: #e9eef5;
        border: 1px solid #333a44; border-radius: 9px; padding: 7px 14px;
        font-weight: 600;
    }
    button:hover { background-color: #2c313a; border-color: #414a57; }
    button:active { background-color: #30363f; }
    .ghost {
        background-color: rgba(96,176,255,0.08);
        border: 1px solid #2f4257; color: #cfe0f5;
    }
    .ghost:hover { background-color: rgba(96,176,255,0.16); border-color: #3b82f6; }
    .accent {
        background-color: #3b82f6; color: #ffffff; border-color: #3b82f6;
        font-weight: 700; padding: 7px 20px;
    }
    .accent:hover { background-color: #60b0ff; border-color: #60b0ff; }
    .accent:active { background-color: #2f6fd6; }

    /* Checkboxes */
    checkbutton { color: #cfd6e0; font-size: 13px; }
    checkbutton check {
        background-color: #23262c; border: 1px solid #3a4250;
        border-radius: 5px; min-width: 15px; min-height: 15px;
    }
    checkbutton:hover check { border-color: #4a5568; }
    checkbutton check:checked {
        background-color: #3b82f6; border-color: #3b82f6; color: #ffffff;
    }

    /* Secondary text */
    .hint { color: #e9eef5; font-size: 11px; }
    .result { color: #e9eef5; font-size: 12px; }
    .result-ok { color: #7fc6a0; font-size: 12px; }
    .status-ok { color: #7fc6a0; font-size: 12px; }

    /* Scrolled sensor list */
    .sensor-scroll {
        background-color: #14171c; border: 1px solid #262b33;
        border-radius: 10px;
    }
    scrollbar slider { background-color: #3a4250; border-radius: 8px; min-width: 6px; }
    scrollbar slider:hover { background-color: #4a5568; }
    undershoot.top, undershoot.bottom, undershoot.left, undershoot.right,
    overshoot.top, overshoot.bottom, overshoot.left, overshoot.right {
        background: none; background-image: none;
    }
    scrolledwindow { border: none; box-shadow: none; }
    * { outline: none; -gtk-outline-radius: 0; }

    /* Quiet, borderless secondary action (reset / close) */
    .subtle {
        background: none; background-image: none; border: none;
        box-shadow: none; color: #8a94a4; font-weight: 600; padding: 5px 10px;
    }
    .subtle:hover { color: #dbe3ee; background-color: rgba(255,255,255,0.05); border: none; }
    .subtle:active { background-color: rgba(255,255,255,0.08); }

    /* Bottom action bar */
    .actionbar { background-color: #000000; border-top: 1px solid #262b33; }
    .header { background-color: #000000; border-bottom: 1px solid #262b33; }
    separator { background-color: #262b33; min-height: 1px; min-width: 1px; }
    """
    prov = Gtk.CssProvider()
    prov.load_from_data(css)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _cls(w, *names):
        ctx = w.get_style_context()
        for nm in names:
            ctx.add_class(nm)
        return w

    s = load_settings()
    win = Gtk.Window(title="Linux Mint HUD — Settings")
    win.set_border_width(0)
    win.set_default_size(500, 840)

    # Outer layout: everything scrolls (header included), with only the action
    # bar pinned at the bottom.
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    win.add(outer)

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    outer.pack_start(scroller, True, True, 0)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    _cls(content, "content")
    content.set_border_width(20)
    scroller.add(content)

    header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    header.pack_start(_cls(Gtk.Label(label="Linux Mint HUD", xalign=0), "app-title"), False, False, 0)
    header.pack_start(_cls(Gtk.Label(label="Panel appearance & readouts", xalign=0), "app-subtitle"), False, False, 0)
    content.pack_start(header, False, False, 0)

    _grp = [0]

    def _noscroll(w):
        """Stop the mouse wheel from changing a combo/spin value while the user
        is just scrolling the window past it."""
        w.connect("scroll-event", lambda *a: True)
        return w

    def make_group(title):
        """A labelled group of rows, set off from the previous one by a hairline
        rather than sitting in its own boxed card."""
        if _grp[0] > 0:
            content.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 6)
        _grp[0] += 1
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.pack_start(_cls(Gtk.Label(label=title.upper(), xalign=0), "group-heading"), False, False, 0)
        g = Gtk.Grid(row_spacing=11, column_spacing=14)
        box.pack_start(g, False, False, 0)
        content.pack_start(box, False, False, 0)
        return box, g, [0]

    def field(grid, counter, label_text, widget):
        """Attach a label/control row inside a group grid."""
        if label_text:
            lbl = _cls(Gtk.Label(label=label_text, xalign=0, yalign=0.5), "field-label")
            grid.attach(lbl, 0, counter[0], 1, 1)
            widget.set_hexpand(True)
            widget.set_halign(Gtk.Align.FILL)
            grid.attach(widget, 1, counter[0], 1, 1)
        else:
            widget.set_hexpand(True)
            grid.attach(widget, 0, counter[0], 2, 1)
        counter[0] += 1

    # ---- Placement -------------------------------------------------------
    pbox, pg, pc = make_group("Placement")
    disp = Gdk.Display.get_default()
    panels_now = s.get("panels") or [{}]

    LBL = {0: "Move panel…", 1: "Move second panel…"}
    # The move actions read as one cluster of contained buttons; "Reset
    # positions" sits under them as a quiet, borderless secondary action.
    place_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)

    move1 = _cls(Gtk.Button(label=LBL[0]), "ghost")
    move1.set_halign(Gtk.Align.START)
    move1.set_size_request(240, -1)
    place_box.pack_start(move1, False, False, 0)

    second_chk = Gtk.CheckButton(label="Second panel (drag it to another monitor)")
    second_chk.set_active(len(panels_now) >= 2)
    place_box.pack_start(second_chk, False, False, 0)

    move2 = _cls(Gtk.Button(label=LBL[1]), "ghost")
    move2.set_halign(Gtk.Align.START)
    move2.set_size_request(240, -1)
    move2.set_no_show_all(True)                 # only shown when a second panel exists
    move2.set_visible(len(panels_now) >= 2)
    place_box.pack_start(move2, False, False, 0)

    reset_btn = _cls(Gtk.Button(label="Reset positions"), "subtle")
    reset_btn.set_halign(Gtk.Align.START)
    place_box.pack_start(reset_btn, False, False, 0)

    pbox.pack_start(place_box, False, False, 0)
    pbox.reorder_child(place_box, 1)            # sit above the edge-margin row

    def _nmon():
        return disp.get_n_monitors()

    def refresh_move_labels():
        mv = load_settings().get("move")
        move1.set_label("Save position" if mv == 0 else LBL[0])
        move2.set_label("Save position" if mv == 1 else LBL[1])

    def toggle_move(idx):
        st = dict(load_settings())
        if st.get("move") == idx:                 # second click on the same button
            st["move"] = None
            save_settings(st)
            status.set_text("Position saved.")
        else:
            cfgs = [dict(c) for c in (st.get("panels") or [])] or [{"monitor": 0, "position": "top-right", "offset": None}]
            while len(cfgs) <= idx:
                cfgs.append({"monitor": 1 if _nmon() > 1 else 0, "position": "top-right", "offset": None})
            st["panels"] = cfgs
            st["move"] = idx
            save_settings(st)
            status.set_text("Drag the panel (its corner grip resizes). Click “Save position” when done.")
        refresh_move_labels()

    def set_two(active):
        st = dict(load_settings())
        cfgs = [dict(c) for c in (st.get("panels") or [])] or [{"monitor": 0, "position": "top-right", "offset": None}]
        if active and len(cfgs) < 2:
            cfgs.append({"monitor": 1 if _nmon() > 1 else 0, "position": "top-right", "offset": None})
        st["panels"] = cfgs[:2] if active else cfgs[:1]
        if not active and st.get("move") == 1:
            st["move"] = None
        save_settings(st)
        move2.set_visible(active)
        refresh_move_labels()

    def do_reset(_b):
        st = dict(load_settings())
        k = max(1, len(st.get("panels") or [{}]))
        st["panels"] = [{"monitor": 0, "position": "top-right", "offset": None} for _ in range(k)]
        st["move"] = None
        save_settings(st)
        refresh_move_labels()
        status.set_text("Positions reset to the top-right corner.")

    move1.connect("clicked", lambda *_: toggle_move(0))
    move2.connect("clicked", lambda *_: toggle_move(1))
    second_chk.connect("toggled", lambda cb: set_two(cb.get_active()))
    reset_btn.connect("clicked", do_reset)
    refresh_move_labels()

    margin_spin = _noscroll(Gtk.SpinButton.new_with_range(0, 200, 1))
    margin_spin.set_value(s.get("margin", 22))
    field(pg, pc, "Edge margin (px)", margin_spin)
    margin_spin.set_hexpand(False)
    margin_spin.set_halign(Gtk.Align.START)
    margin_spin.set_size_request(130, -1)

    # ---- Weather ---------------------------------------------------------
    _, wg, wc = make_group("Weather")
    loc = s.get("location") or {}
    loc_state = {"data": loc or None}
    town = Gtk.Entry()
    town.set_placeholder_text("Town or city")
    town.set_text(loc.get("name", ""))          # the chosen city sits in the field
    lookup = Gtk.Button(label="Look up")
    locbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    locbox.pack_start(town, True, True, 0)
    locbox.pack_start(lookup, False, False, 0)
    field(wg, wc, "Location", locbox)
    loc_label = _cls(Gtk.Label(label="", xalign=0), "result")
    loc_label.set_no_show_all(True)             # only appears to report a lookup
    field(wg, wc, "", loc_label)

    def do_lookup(_b):
        q = town.get_text().strip()
        if not q:
            return
        try:
            url = "https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + urllib.parse.quote(q)
            res = json.load(urllib.request.urlopen(url, timeout=8))["results"][0]
            loc_state["data"] = {"lat": res["latitude"], "lon": res["longitude"], "name": res["name"]}
            town.set_text(res["name"])
            loc_label.get_style_context().remove_class("result")
            loc_label.get_style_context().add_class("result-ok")
            loc_label.set_text(f"✓ {res['name']}, {res.get('admin1', '')} {res['country_code']}")
        except Exception:
            loc_label.get_style_context().remove_class("result-ok")
            loc_label.get_style_context().add_class("result")
            loc_label.set_text("couldn't find that place")
        loc_label.set_visible(True)
    lookup.connect("clicked", do_lookup)

    # ---- Panel sections --------------------------------------------------
    _, seg, sec_ = make_group("Panel sections")
    secgrid = Gtk.Grid(row_spacing=8, column_spacing=24)
    checks = {}
    for idx, (key, txt) in enumerate((("thermals", "Thermals"), ("network", "Network"),
                                      ("power", "Power"), ("processes", "Top processes"))):
        cb = Gtk.CheckButton(label=txt)
        cb.set_active(s["sections"].get(key, True))
        checks[key] = cb
        secgrid.attach(cb, idx % 2, idx // 2, 1, 1)
    field(seg, sec_, "", secgrid)

    # ---- Disks -----------------------------------------------------------
    _, dg, dc = make_group("Disks")
    cur_disks = s.get("disks") or ["/"]
    disk_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
    disk_checks = {}
    for mp, dev in disk_mounts():
        tag = f"   ({os.path.basename(dev)})" if dev else ""
        cb = Gtk.CheckButton(label=f"{mp}{tag}")
        cb.set_active(mp in cur_disks)
        disk_checks[mp] = cb
        disk_box.pack_start(cb, False, False, 0)
    field(dg, dc, "", disk_box)

    # ---- Temperature -----------------------------------------------------
    _, tg, tc = make_group("Temperature")
    units_combo = _noscroll(Gtk.ComboBoxText())
    units_combo.append("c", "Celsius (°C)")
    units_combo.append("f", "Fahrenheit (°F)")
    units_combo.set_active_id(s.get("units", "c"))
    field(tg, tc, "Unit", units_combo)
    units_combo.set_hexpand(False)
    units_combo.set_halign(Gtk.Align.START)
    units_combo.set_size_request(190, -1)

    cur_sens = s.get("sensors") or []
    auto_paths = {p for p in (_find_cpu_temp(), _find_disk_temp(), _find_wifi_temp()) if p}
    sens_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
    sens_box.set_border_width(4)
    sens_checks = {}
    sensors = list_sensors()
    for se in sensors:
        val = read_sensor(se["path"])
        vtxt = f"   {val:.0f}°C" if val is not None else ""
        cb = Gtk.CheckButton(label=f"{se['full']}{vtxt}")
        on = se["id"] in cur_sens if cur_sens else (se["path"] in auto_paths)
        cb.set_active(on)
        sens_checks[se["id"]] = cb
        sens_box.pack_start(cb, False, False, 0)
    tg.attach(_cls(Gtk.Label(label="Sensors", xalign=0, yalign=0), "field-label"), 0, tc[0], 1, 1)
    if len(sensors) > 6:
        sw = _cls(Gtk.ScrolledWindow(), "sensor-scroll")
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_min_content_height(150)
        sw.add(sens_box)
        sw.set_hexpand(True)
        tg.attach(sw, 1, tc[0], 1, 1)
    else:
        sens_box.set_hexpand(True)
        tg.attach(sens_box, 1, tc[0], 1, 1)
    tc[0] += 1
    field(tg, tc, "", _cls(Gtk.Label(label="Up to 4 sensors are shown in the panel.", xalign=0), "hint"))

    # ---- Devices ---------------------------------------------------------
    _, deg, dec = make_group("Devices")
    cur_periph = s.get("peripherals") or []
    periph_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
    periph_checks = {}
    periphs = peripheral_batteries()
    for p in periphs:
        cap = p["capacity"]
        cb = Gtk.CheckButton(label=f"{p['name']}" + (f"   {cap}%" if cap is not None else ""))
        cb.set_active(p["id"] in cur_periph)
        periph_checks[p["id"]] = cb
        periph_box.pack_start(cb, False, False, 0)
    if not periphs:
        periph_box.pack_start(
            _cls(Gtk.Label(label="No wireless mouse/keyboard batteries detected.",
                           xalign=0), "hint"), False, False, 0)
    field(deg, dec, "", periph_box)

    # ---- Bottom action bar (fixed) --------------------------------------
    actionbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    _cls(actionbar, "actionbar")
    actionbar.set_border_width(16)
    status = _cls(Gtk.Label(label="", xalign=0), "status-ok")
    status.set_line_wrap(True)
    actionbar.pack_start(status, True, True, 0)
    save = _cls(Gtk.Button(label="Save"), "accent")
    close = _cls(Gtk.Button(label="Close"), "subtle")
    actionbar.pack_end(save, False, False, 0)
    actionbar.pack_end(close, False, False, 0)
    outer.pack_start(actionbar, False, False, 0)

    def do_save(_b):
        new = dict(load_settings())
        new["margin"] = int(margin_spin.get_value())
        new["location"] = loc_state["data"]
        new["units"] = units_combo.get_active_id() or "c"
        new["sections"] = {k: cb.get_active() for k, cb in checks.items()}
        dsel = [mp for mp, cb in disk_checks.items() if cb.get_active()]
        new["disks"] = dsel or None
        ssel = [sid for sid, cb in sens_checks.items() if cb.get_active()]
        new["sensors"] = ssel or None
        psel = [pid for pid, cb in periph_checks.items() if cb.get_active()]
        new["peripherals"] = psel or None
        save_settings(new)
        status.set_text("Saved — the panel updates within a second.")
    save.connect("clicked", do_save)
    save.get_style_context().add_class("accent")
    close.connect("clicked", lambda *_: win.close())

    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    if "--settings" in sys.argv:
        run_settings()
    elif "--png" in sys.argv:
        render()
        for _t in threading.enumerate():
            if _t is not threading.main_thread():
                _t.join(timeout=15)
    else:
        _lock = open(LOCK_FILE, "a+")
        try:
            fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit(0)
        _lock.seek(0)
        _lock.truncate()
        _lock.write(str(os.getpid()))
        _lock.flush()
        run_window()
