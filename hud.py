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
    "monitor": 0,
    "position": "top-right",
    "margin": 22,
    "location": None,
    "units": "c",                  # "c" or "f", for every temperature shown
    "disks": None,                 # mount points to show; None -> just "/"
    "sensors": None,               # temp-sensor ids for thermals; None -> auto
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

    natural = y - FLEX_POINTS * FLEX
    next_flex = 0.0
    if FLEX_POINTS:
        next_flex = max(0.0, min(FLEX_MAX, (TARGET_H - natural) / FLEX_POINTS))

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
    """Put the panel on the desktop and keep it painted.

    A top-level ARGB window with four properties on it, redrawn on a timer.
    Handing cairo the pixels costs 4ms a frame against 21ms to encode a PNG
    first, so nothing here touches the disk."""
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, Gdk, GLib
    import cairo

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

    visual = win.get_screen().get_rgba_visual()
    if visual is None:
        log("no RGBA visual; the panel will not be translucent")
    else:
        win.set_visual(visual)

    state = {"surface": None, "buf": None, "h": 0, "fails": 0}

    def place(h):
        """Put the panel in the configured corner of the configured monitor,
        measured off the work area so the taskbar is respected, and size the
        flex target to that monitor's height."""
        global TARGET_H
        s = load_settings()
        disp = Gdk.Display.get_default()
        mon = (disp.get_monitor(s.get("monitor", 0))
               or disp.get_primary_monitor() or disp.get_monitor(0))
        wa = mon.get_workarea()
        margin = s.get("margin", MARGIN)
        pos = s.get("position", "top-right")
        TARGET_H = max(200, wa.height - 2 * margin)
        off = s.get("offset")
        if pos == "free" and isinstance(off, (list, tuple)) and len(off) == 2:
            x, yy = wa.x + int(off[0]), wa.y + int(off[1])
        else:
            x = wa.x + (wa.width - W - margin if pos.endswith("right") else margin)
            yy = wa.y + (wa.height - h - margin if pos.startswith("bottom") else margin)
        x = max(wa.x, min(x, wa.x + wa.width - W))
        yy = max(wa.y, min(yy, wa.y + wa.height - h))
        win.set_size_request(W, h)
        win.move(x, yy)

    def on_draw(_w, cr):
        if state["surface"] is not None:
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_surface(state["surface"], 0, 0)
            cr.paint()
        if state.get("moving"):
            cr.set_operator(cairo.OPERATOR_OVER)
            cr.set_source_rgba(0.23, 0.51, 0.96, 0.95)
            cr.rectangle(0, 0, win.get_allocated_width(), 26)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 1)
            cr.select_font_face("sans")
            cr.set_font_size(12)
            cr.move_to(12, 17)
            cr.show_text("drag to move — release to place")
        return False

    def on_realize(_w):
        win.get_window().input_shape_combine_region(cairo.Region(), 0, 0)

    drag = {"active": False, "sx": 0, "sy": 0, "wx": 0, "wy": 0}

    def enter_move():
        gw = win.get_window()
        if gw is None:
            return
        w = win.get_allocated_width() or W
        h = win.get_allocated_height() or state.get("h") or 1000
        gw.input_shape_combine_region(          # whole window accepts the pointer
            cairo.Region(cairo.RectangleInt(0, 0, w, h)), 0, 0)
        win.set_keep_below(False)
        win.set_keep_above(True)
        state["moving"] = True
        win.queue_draw()

    def exit_move():
        gw = win.get_window()
        if gw is not None:
            gw.input_shape_combine_region(cairo.Region(), 0, 0)   # click-through
        win.set_keep_above(False)
        win.set_keep_below(True)
        state["moving"] = False
        win.queue_draw()

    def on_press(_w, ev):
        if not state.get("moving"):
            return False
        drag["active"] = True
        drag["sx"], drag["sy"] = ev.x_root, ev.y_root
        drag["wx"], drag["wy"] = win.get_position()
        return True

    def on_motion(_w, ev):
        if state.get("moving") and drag["active"]:
            win.move(int(drag["wx"] + (ev.x_root - drag["sx"])),
                     int(drag["wy"] + (ev.y_root - drag["sy"])))
        return False

    def on_release(_w, ev):
        if not state.get("moving"):
            return False
        drag["active"] = False
        wx, wy = win.get_position()
        disp = Gdk.Display.get_default()
        cx, cy = wx + W // 2, wy + (state["h"] or 0) // 2
        mon = disp.get_monitor_at_point(cx, cy) or disp.get_primary_monitor()
        wa = mon.get_workarea()
        mg = mon.get_geometry()
        idx = 0
        for i in range(disp.get_n_monitors()):
            g = disp.get_monitor(i).get_geometry()
            if (g.x, g.y, g.width, g.height) == (mg.x, mg.y, mg.width, mg.height):
                idx = i
                break
        s = dict(load_settings())
        s["monitor"] = idx
        s["position"] = "free"
        s["offset"] = [wx - wa.x, wy - wa.y]
        s["move"] = False
        save_settings(s)
        state["placekey"] = None
        exit_move()
        return True

    def keep_above_desktop():
        """A DESKTOP-type window shares the bottom layer with nemo-desktop, and
        their order inside it is not fixed: a Cinnamon restart left the panel
        underneath a 1920x1160 desktop window, still mapped and completely
        invisible. Landing at the very bottom of the stack is the signature of
        that, and re-mapping lifts us back to the top of the layer.

        Not solvable by raise_(), which Muffin ignores for this window type,
        nor by dropping the type hint: a NORMAL window is then swept away by
        "show desktop" along with the real applications."""
        gw = win.get_window()
        if gw is None:
            return
        stack = win.get_screen().get_window_stack() or []
        if len(stack) < 2:
            return
        xids = [w.get_xid() for w in stack]
        if xids and xids[0] == gw.get_xid():
            log("panel had sunk below the desktop window, re-mapping")
            win.hide()
            win.show()

    def tick():
        try:
            img = render(write_png=False)
            state["surface"], state["buf"] = surface_from(img)
            state["h"] = img.height
            s = load_settings()
            if s.get("move") and not state.get("moving"):
                enter_move()
            elif not s.get("move") and state.get("moving"):
                exit_move()
            if not state.get("moving"):
                key = (img.height, s.get("monitor"), s.get("position"),
                       s.get("margin"), tuple(s.get("offset") or ()))
                if key != state.get("placekey"):
                    state["placekey"] = key
                    place(img.height)
                keep_above_desktop()
            win.queue_draw()
            state["fails"] = 0
        except Exception:
            state["fails"] += 1
            if state["fails"] <= 3:
                log(f"render failed:\n{traceback.format_exc().rstrip()}")
                if state["fails"] == 3:
                    log("further identical failures will not be logged")
        return True

    win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                   | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON1_MOTION_MASK)
    win.connect("draw", on_draw)
    win.connect("realize", on_realize)
    win.connect("button-press-event", on_press)
    win.connect("motion-notify-event", on_motion)
    win.connect("button-release-event", on_release)
    win.connect("destroy", Gtk.main_quit)
    tick()
    place(state["h"] or 900)
    win.show_all()
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
    window { background-color: #16181c; }
    label { color: #cfd6e0; }
    entry, spinbutton, spinbutton entry, combobox button, button {
        background-image: none; background-color: #23262c; color: #e9eef5;
        border: 1px solid #333a44; border-radius: 6px;
    }
    button:hover { background-color: #2c313a; }
    checkbutton { color: #cfd6e0; }
    .accent, .accent:hover { background-color: #3b82f6; color: #ffffff; border-color: #3b82f6; }
    .hint { color: #8a94a4; font-size: 11px; }
    """
    prov = Gtk.CssProvider()
    prov.load_from_data(css)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    s = load_settings()
    win = Gtk.Window(title="Linux Mint HUD — Settings")
    win.set_border_width(16)
    grid = Gtk.Grid(row_spacing=10, column_spacing=12)
    win.add(grid)
    row = [0]

    def add_row(label_text, widget):
        if label_text:
            grid.attach(Gtk.Label(label=label_text, xalign=0), 0, row[0], 1, 1)
        grid.attach(widget, 1 if label_text else 0, row[0],
                    1 if label_text else 2, 1)
        row[0] += 1

    disp = Gdk.Display.get_default()
    n = disp.get_n_monitors()
    mon_combo = Gtk.ComboBoxText()
    for i in range(n):
        g = disp.get_monitor(i).get_geometry()
        prim = " (primary)" if disp.get_monitor(i).is_primary() else ""
        mon_combo.append(str(i), f"{i}:  {g.width}×{g.height}{prim}")
    mid = s.get("monitor", 0)
    mon_combo.set_active_id(str(mid if 0 <= mid < n else 0))
    add_row("Monitor", mon_combo)

    pos_combo = Gtk.ComboBoxText()
    for key, txt in (("top-left", "Top left"), ("top-right", "Top right"),
                     ("bottom-left", "Bottom left"), ("bottom-right", "Bottom right"),
                     ("free", "Custom (dragged)")):
        pos_combo.append(key, txt)
    pos_combo.set_active_id(s.get("position", "top-right"))
    add_row("Position", pos_combo)

    move_btn = Gtk.Button(label="Move panel on screen…")
    add_row("", move_btn)

    margin_spin = Gtk.SpinButton.new_with_range(0, 200, 1)
    margin_spin.set_value(s.get("margin", 22))
    add_row("Edge margin (px)", margin_spin)

    units_combo = Gtk.ComboBoxText()
    units_combo.append("c", "Celsius (°C)")
    units_combo.append("f", "Fahrenheit (°F)")
    units_combo.set_active_id(s.get("units", "c"))
    add_row("Temperature", units_combo)

    loc = s.get("location") or {}
    loc_state = {"data": loc or None}
    town = Gtk.Entry()
    town.set_placeholder_text("town or city")
    lookup = Gtk.Button(label="Look up")
    locbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    locbox.pack_start(town, True, True, 0)
    locbox.pack_start(lookup, False, False, 0)
    add_row("Weather", locbox)
    loc_label = Gtk.Label(label=f"→ {loc['name']}" if loc.get("name") else "→ none (weather off)",
                          xalign=0)
    add_row("", loc_label)

    def do_lookup(_b):
        q = town.get_text().strip()
        if not q:
            return
        try:
            url = "https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + urllib.parse.quote(q)
            res = json.load(urllib.request.urlopen(url, timeout=8))["results"][0]
            loc_state["data"] = {"lat": res["latitude"], "lon": res["longitude"], "name": res["name"]}
            loc_label.set_text(f"→ {res['name']}, {res.get('admin1', '')} {res['country_code']}")
        except Exception:
            loc_label.set_text("→ couldn't find that place")
    lookup.connect("clicked", do_lookup)

    secbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    checks = {}
    for key, txt in (("thermals", "Thermals"), ("network", "Network"),
                     ("power", "Power"), ("processes", "Top processes")):
        cb = Gtk.CheckButton(label=txt)
        cb.set_active(s["sections"].get(key, True))
        checks[key] = cb
        secbox.pack_start(cb, False, False, 0)
    add_row("Sections", secbox)

    cur_disks = s.get("disks") or ["/"]
    disk_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    disk_checks = {}
    for mp, dev in disk_mounts():
        tag = f"  ({os.path.basename(dev)})" if dev else ""
        cb = Gtk.CheckButton(label=f"{mp}{tag}")
        cb.set_active(mp in cur_disks)
        disk_checks[mp] = cb
        disk_box.pack_start(cb, False, False, 0)
    add_row("Disks", disk_box)

    cur_sens = s.get("sensors") or []
    auto_paths = {p for p in (_find_cpu_temp(), _find_disk_temp(), _find_wifi_temp()) if p}
    sens_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
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
    if len(sensors) > 6:
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_min_content_height(150)
        sw.add(sens_box)
        add_row("Temp sensors", sw)
    else:
        add_row("Temp sensors", sens_box)
    add_row("", Gtk.Label(label="up to 4 sensors are shown in the panel", xalign=0))

    status = Gtk.Label(label="", xalign=0)
    add_row("", status)
    btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    save = Gtk.Button(label="Save")
    close = Gtk.Button(label="Close")
    btns.pack_end(close, False, False, 0)
    btns.pack_end(save, False, False, 0)
    add_row("", btns)

    def do_save(_b):
        new = dict(load_settings())
        new["monitor"] = int(mon_combo.get_active_id() or 0)
        new["position"] = pos_combo.get_active_id() or "top-right"
        new["margin"] = int(margin_spin.get_value())
        new["location"] = loc_state["data"]
        new["units"] = units_combo.get_active_id() or "c"
        new["sections"] = {k: cb.get_active() for k, cb in checks.items()}
        dsel = [mp for mp, cb in disk_checks.items() if cb.get_active()]
        new["disks"] = dsel or None
        ssel = [sid for sid, cb in sens_checks.items() if cb.get_active()]
        new["sensors"] = ssel or None
        save_settings(new)
        status.set_text("Saved — the panel updates within a second.")
    save.connect("clicked", do_save)
    save.get_style_context().add_class("accent")
    close.connect("clicked", lambda *_: win.close())

    def do_move(_b):
        m = dict(load_settings())
        m["move"] = True
        save_settings(m)
        pos_combo.set_active_id("free")
        status.set_text("Drag the panel on your desktop, then release to drop it there.")
    move_btn.connect("clicked", do_move)

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
