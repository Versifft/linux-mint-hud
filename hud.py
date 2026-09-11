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
import urllib.request

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HOME = os.path.expanduser("~")
# Code can live read-only under /usr (the .deb) or in a checkout; data always
# lives per-user under ~/.config/mint-hud. For a checkout at that path the two
# coincide, so nothing changes there.
CODE_DIR = os.path.dirname(os.path.abspath(__file__))
CONF_DIR = os.path.join(HOME, ".config", "mint-hud")
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


def _apply_window_icon(Gtk):
    """Give our GTK windows the app logo in the taskbar/title bar (the window
    icon is separate from the .desktop icon). Uses the installed theme icon
    'mint-hud'; a checkout also carries the PNGs under icons/, used as a fallback
    so it works before install too."""
    try:
        Gtk.Window.set_default_icon_name("mint-hud")
        png = os.path.join(CODE_DIR, "icons", "mint-hud-256.png")
        if os.path.exists(png):
            Gtk.Window.set_default_icon_from_file(png)
    except Exception:
        pass

# Every panel section, in the default top-to-bottom order. Single source of
# truth for: the render order, the on/off checkboxes, and the drag-to-reorder
# list in the settings window. "top" is the combined quota/weather slot that
# alternates; "claude" and "weather" are the same content on their own, so a
# user can place either (or both) wherever they like — they default off so the
# stock panel keeps the single alternating slot.
SECTION_DEFS = [
    ("top", "Quota / weather"),
    ("claude", "Quota only"),
    ("weather", "Weather only"),
    ("load", "Uptime & load"),
    ("gauges", "CPU / GPU / RAM"),
    ("cores", "Core strip"),
    ("history", "History"),
    ("thermals", "Thermals"),
    ("memory", "Memory & swap"),
    ("disk", "Disk"),
    ("network", "Network"),
    ("power", "Power"),
    ("battery", "Battery"),
    ("devices", "Devices"),
    ("processes", "Top processes"),
]
SECTION_ORDER = [k for k, _ in SECTION_DEFS]
SECTION_LABELS = dict(SECTION_DEFS)
_DEFAULT_OFF = {"claude", "weather"}

DEFAULT_SETTINGS = {
    "panels": None,                # list of {monitor, position, offset}; None -> one
    "move": None,                  # index of the panel being placed by hand, or None
    "monitor": 0,                  # legacy single-panel keys (migrated into panels)
    "position": "top-right",
    "margin": 22,                  # legacy single margin, migrated to the edge margins
    "vmargin": 22,                 # legacy symmetric top/bottom gap (migrated to top/bottom)
    "hmargin": 22,                 # legacy near-side gap (migrated to right)
    "top": 22,                     # per-panel edge margins (gap from each work-area edge);
    "bottom": 22,                  # top+bottom set the height, left+right set the width
    "left": None,                  # None -> native width anchored to the right
    "right": 22,
    "location": None,
    "weather_units": "c",          # "c"/"f" for the weather slot (separate from hardware)
    "weather_show_location": True, # show the town name on the weather slot
    "sensor_names": {},            # {sensor id: custom label} for the thermals row
    "units": "c",                  # "c" or "f", for hardware temperatures (per panel)
    "disks": None,                 # mount points to show; None -> just "/"
    "sensors": None,               # temp-sensor ids for thermals; None -> auto
    "peripherals": None,           # peripheral-battery ids to show; None -> none
    "sections": {k: (k not in _DEFAULT_OFF) for k in SECTION_ORDER},
    "order": None,                 # custom section order (list of keys); None -> default
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
FLEX_MAX = 400          # most a gap may stretch (fill a tall work area)
FLEX_MIN = 0            # hard floor: the gaps never shrink below their natural
                        # size, so the content can't be squashed. The panel is
                        # exactly as tall as its content; a config taller than
                        # the screen is scaled to fit by the off-screen guard
                        # (scaled_for), not by compressing the layout.

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
    if Wp < 1 or Hp < 1:                 # a sliver-thin/short row: nothing to draw
        return
    tile = Image.new("RGBA", (Wp, Hp), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)

    def bar(x1, fill):
        # PIL's rounded_rectangle raises ("x1 must be >= x0") when the box is
        # narrower or shorter than roughly twice the corner radius — which a
        # low-fraction bar or a compressed panel can produce. Clamp the radius to
        # the box so each side is at least 2r+2 (radius 0 => a plain rectangle),
        # which keeps the normal look at normal sizes and never crashes.
        x1 = int(x1)
        if x1 < 1:
            return
        r = max(0, min(3 * SS, (x1 - 2) // 2, (Hp - 1 - 2) // 2))
        td.rounded_rectangle([0, 0, x1, Hp - 1], radius=r, fill=fill)

    bar(Wp - 1, (255, 255, 255, 8))
    fw = max(0.0, min(1.0, frac)) * Wp
    if fw > 2 * SS:
        bar(fw, (*color, 46))
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
    """A short, human-readable name for a sensor from its hwmon chip name and
    optional label, checked against the common Linux drivers (coretemp, k10temp,
    nvme, amdgpu, iwlwifi, …). Falls back to the raw label/chip, trimmed."""
    c = (chip or "").lower()
    t = (lab or "").lower()
    # CPU — Intel coretemp / AMD k10temp / zenpower / SoC thermal
    if c in ("coretemp", "cpu_thermal", "x86_pkg_temp") or "package id" in t or "x86_pkg" in t:
        if t.startswith("core "):
            return "CPU Core " + t.split()[-1]
        return "CPU"
    if c in ("k10temp", "k8temp", "zenpower"):
        if "ccd" in t:                          # per-die temps: Tccd1 -> CPU CCD1
            return "CPU " + (lab or "").upper()
        return "CPU"
    if "tctl" in t or "tdie" in t or t == "cpu":
        return "CPU"
    # Storage
    if c == "nvme":
        return "SSD" + (f" {lab}" if t and t != "composite" else "")
    if c == "drivetemp" or "drive" in t or "disk" in t:
        return "Disk"
    # GPU — AMD / NVIDIA
    if c in ("amdgpu", "radeon"):
        if "junction" in t:
            return "GPU Junction"
        if "mem" in t:
            return "GPU Memory"
        return "GPU"
    if c in ("nouveau", "nvidia"):
        return "GPU"
    # Wi-Fi radios
    if c.startswith(("iwlwifi", "ath", "mt79", "mt76", "rtw", "mwifiex", "brcm")) \
            or "wifi" in t or "wlan" in t:
        return "WiFi"
    # Board / chipset / ACPI
    if c == "acpitz" or "acpi" in c:
        return "System"
    if "pch" in c:
        return "Chipset"
    if c.startswith(("nct6", "it87", "it8", "f718", "w836", "nzxt")):
        return (lab or chip)[:12]               # super-I/O: keep its own label
    return (lab or chip)[:12]


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
            short = _sensor_short(chip, lab)
            tech = f"{chip} · {lab}" if lab else chip
            out.append({"id": f"{chip}:{base}", "label": short,
                        "full": f"{short}  ({tech})" if short.lower() != tech.lower() else tech,
                        "path": inp, "warn": warn, "crit": crit})
    for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        ty = read_first(f"{z}/type", default="")
        p = f"{z}/temp"
        if ty and os.path.exists(p):
            warn, crit = _sensor_thresholds(ty)
            short = _sensor_short(ty, "")
            out.append({"id": f"zone:{ty}", "label": short,
                        "full": f"{short}  (zone · {ty})" if short.lower() != ty.lower() else f"zone · {ty}",
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


def cached_cmd(name, argv, ttl, ok_prefix=None, fail_ttl=60, key=None):
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
    # `key` ties the cache to an input (the weather location): when it changes,
    # refetch immediately instead of waiting out the ttl, and don't serve the
    # previous input's result in the meantime.
    key_changed = key is not None and (read_first(f"{path}.key", default="") or "") != str(key)
    if age > ttl or key_changed:
        with _INFLIGHT_LOCK:
            busy = name in _INFLIGHT
            if not busy:
                _INFLIGHT.add(name)
        if not busy:
            if key is not None:
                try:
                    with open(f"{path}.key", "w") as f:
                        f.write(str(key))
                except OSError:
                    pass
            threading.Thread(target=_refresh_cached, daemon=True,
                             args=(name, argv, path, f"{path}.tmp")).start()
        if key_changed:
            return ""            # the cached text is for the old input; hide it
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


# The display settings each panel carries its own copy of (placement —
# monitor and the four edge margins — is per-panel too).
PANEL_DISPLAY_KEYS = ("top", "bottom", "left", "right", "units", "disks",
                      "sensors", "peripherals", "sections", "order",
                      "weather_show_location")


def panel_box(cfg, wa):
    """Resolve a panel's on-screen box from its four edge margins.

    top/bottom/left/right are each a gap from that edge of the work area `wa`.
    top+bottom set the height (and vertical position); left+right, when both
    are given, set the width — so the width is the user's to choose and never
    follows the content. When left/right are not both set the box keeps the
    native panel width, anchored to whichever side is given (right by default).
    Returns (x, y, w, h, margins) with margins the resolved {top,bottom,left,
    right}."""
    top = int(cfg.get("top", cfg.get("vmargin", MARGIN)))
    bottom = int(cfg.get("bottom", cfg.get("vmargin", MARGIN)))
    left = cfg.get("left")
    right = cfg.get("right", cfg.get("hmargin", MARGIN))
    h = max(120, wa.height - top - bottom)
    if left is not None and right is not None:
        left, right = int(left), int(right)
        # Hard floor at the native width: the panel can be widened but never
        # squeezed narrower than its content is designed for.
        w = max(W, wa.width - left - right)
    else:                                  # native width, anchored to a side
        w = W
        if left is not None:
            left = int(left)
            right = wa.width - w - left
        else:
            right = int(right if right is not None else MARGIN)
            left = wa.width - w - right
    x = wa.x + left
    y = wa.y + top
    return x, y, w, h, {"top": top, "bottom": bottom, "left": int(left), "right": int(right)}


def normalize_order(order):
    """A section order as a full list of known keys: drop unknown ones, append
    any the saved order predates (a new key) in its default slot."""
    if isinstance(order, list):
        kept = [k for k in order if k in SECTION_ORDER]
        seen = set(kept)
        return kept + [k for k in SECTION_ORDER if k not in seen]
    return list(SECTION_ORDER)


def normalize_sections(sd):
    """A sections on/off map with every known key present."""
    out = {k: (k not in _DEFAULT_OFF) for k in SECTION_ORDER}
    if isinstance(sd, dict):
        out.update({k: bool(v) for k, v in sd.items() if k in out})
    return out


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
        s.update({k: v for k, v in data.items() if k not in ("sections", "panels")})
        # Top-level display config is kept as the template new panels inherit.
        s["sections"] = normalize_sections(data.get("sections"))
        s["order"] = normalize_order(data.get("order"))
        if "vmargin" not in data:
            s["vmargin"] = data.get("margin", 22)
        if "hmargin" not in data:
            s["hmargin"] = data.get("margin", 22)
        # Each panel carries its own display config; anything a panel doesn't
        # set falls back to the top-level (legacy, pre-per-panel) values, so an
        # old single-panel settings file migrates cleanly into panel 0. Old
        # single monitor/position/offset keys migrate the same way.
        tmpl = {k: s.get(k) for k in PANEL_DISPLAY_KEYS}
        raw_panels = data.get("panels") or [{"monitor": s.get("monitor", 0),
                                             "position": s.get("position", "top-right"),
                                             "offset": s.get("offset")}]
        panels = []
        for p in raw_panels:
            q = dict(p) if isinstance(p, dict) else {}
            q.setdefault("monitor", 0)
            for k in ("units", "disks", "sensors", "peripherals", "weather_show_location"):
                q.setdefault(k, tmpl[k])
            # Four independent edge margins. Migrate from the old symmetric
            # vmargin/hmargin (or a free offset) when a panel predates them.
            vm = q.get("vmargin", tmpl.get("top", 22))
            hm = q.get("hmargin", tmpl.get("right", 22))
            q.setdefault("top", vm)
            q.setdefault("bottom", vm)
            if "right" not in q and q.get("left") is None:
                q["right"] = hm
            q.setdefault("left", None)
            q.setdefault("right", None)
            for k in ("position", "offset", "vmargin", "hmargin"):
                q.pop(k, None)
            q["sections"] = normalize_sections(q.get("sections", tmpl["sections"]))
            q["order"] = normalize_order(q.get("order", tmpl["order"]))
            panels.append(q)
        s["panels"] = panels
        _SETTINGS, _SETTINGS_MTIME = s, m
    return _SETTINGS


def save_settings(s):
    save_json(SETTINGS_FILE, s)
    global _SETTINGS_MTIME
    _SETTINGS_MTIME = -1.0          # force a reload on the next read


def autodetect_location():
    """First-run convenience: if no weather location is configured yet, guess
    one from the machine's public IP and save it into settings.json, so the
    weather slot works out of the box and the guessed town shows up (editable)
    in the settings window. A no-op when a location is already set or the
    lookup fails, and it makes no network call at all for existing users (the
    location check happens before anything is fetched)."""
    try:
        if load_settings().get("location"):
            return
        req = urllib.request.Request("https://ipapi.co/json/",
                                     headers={"User-Agent": "linux-mint-hud"})
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.loads(r.read())
        lat, lon = d.get("latitude"), d.get("longitude")
        if lat is None or lon is None:
            return
        name = d.get("city") or d.get("region") or d.get("country_name") or ""
        s = dict(load_settings())
        if s.get("location"):                 # someone set it while we fetched
            return
        s["location"] = {"lat": float(lat), "lon": float(lon), "name": str(name)}
        save_settings(s)
    except Exception:
        pass


HISTORY = []
_STATE = None
_PERSISTED = 0.0
PERSIST_EVERY = 30


_PANEL_CACHE = {}


def panel_bg(H, width=W):
    """The glass panel behind the content, at a given width and height. Built
    once per (width, height) and reused — a radius-12 Gaussian blur over the
    whole canvas is far too costly to redo every frame for an image that only
    changes when the panel is resized."""
    key = (width, H)
    cached = _PANEL_CACHE.get(key)
    if cached is not None:
        return cached

    panel = Image.new("RGBA", (width * SS, H * SS), (0, 0, 0, 0))
    grad = Image.new("RGBA", (1, H * SS))
    gp = grad.load()
    for i in range(H * SS):
        t = i / max(1, H * SS - 1)
        gp[0, i] = (int(20 - 6 * t), int(23 - 6 * t), int(30 - 7 * t), int(208 + 18 * t))
    grad = grad.resize((width * SS, H * SS))
    mask = Image.new("L", (width * SS, H * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, width * SS - 1, H * SS - 1], radius=18 * SS, fill=255)
    panel.paste(grad, (0, 0), mask)

    ImageDraw.Draw(panel).rounded_rectangle(
        [0, 0, width * SS - 1, H * SS - 1], radius=18 * SS, outline=HAIRLINE, width=max(1, SS))

    strip_h = 90 * SS
    hl = Image.new("RGBA", (width * SS, strip_h), (0, 0, 0, 0))
    ImageDraw.Draw(hl).rounded_rectangle(
        [SS, SS, width * SS - 1 - SS, 60 * SS], radius=17 * SS, fill=(255, 255, 255, 12))
    hl = hl.filter(ImageFilter.GaussianBlur(6 * SS))
    panel.alpha_composite(hl, (0, 0))

    if len(_PANEL_CACHE) > 6:
        _PANEL_CACHE.clear()
    _PANEL_CACHE[key] = panel
    return panel


def gather_frame():
    """Sample every metric once per tick and advance the delta/history state.
    Returns a dict the per-panel draw reads from — the panels differ only in
    which of these values they show, never in the values themselves, so this
    must run exactly once per tick (the rates are deltas against the previous
    frame; running it per panel would zero the elapsed time)."""
    global HISTORY, _STATE, _PERSISTED
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

    claude_quota = cached_cmd("claude_quota", [f"{CODE_DIR}/claude_quota.py"], 300,
                              ok_prefix="Session")
    _loc = load_settings().get("location")
    weather_raw = cached_cmd("weather", [f"{CODE_DIR}/weather.py"], 900, ok_prefix="{",
                             key=json.dumps(_loc, sort_keys=True) if _loc else "none")

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
    _STATE = new_state
    if now - _PERSISTED > PERSIST_EVERY:
        _PERSISTED = now
        save_json(STATE_FILE, new_state)
        save_json(HISTORY_FILE, HISTORY)
    return {
        "cpu_pct": cpu_pct, "core_loads": core_loads, "gpu_pct": gpu_pct,
        "have_gpu": have_gpu, "mem_used": mem_used, "mem_total": mem_total,
        "swap_total": swap_total, "swap_used": swap_used, "disk_used": disk_used,
        "disk_total": disk_total, "rd": rd, "wr": wr, "down": down, "up": up,
        "cap": cap, "bstatus": bstatus, "batt_w": batt_w, "eta": eta, "batt_v": batt_v,
        "watts": watts, "ac_w": ac_w, "power_src": power_src, "cpu_t": cpu_t,
        "nvme_t": nvme_t, "wifi_t": wifi_t, "uptime": uptime, "load": load,
        "sess": sess, "week": week, "have_claude": have_claude, "weather": weather,
        "have_weather": have_weather, "top_cpu": top_cpu, "top_mem": top_mem,
    }


def render(frame=None, cfg=None, target_h=None, flex_in=0.0, width=None, write_png=False):
    """Draw one panel's image from a gathered frame and a panel config, and
    return (image, next_flex). The panel is drawn natively at `width` px wide
    (default the standard width) — so the user's chosen width never scales the
    content and never couples to it — with the flex fill taking the height to
    `target_h`. Each panel picks its own sections, order, units and selections
    out of cfg; the flex converges over two draws and is per-panel state the
    caller keeps."""
    global FLEX, FLEX_POINTS
    # Local width so the whole draw (and its helpers, which take explicit
    # coordinates) works at any panel width; PAD stays fixed, so a wider panel
    # spreads its content out rather than magnifying it.
    W = int(width) if width else globals()["W"]
    CW = W - 2 * PAD
    M = frame if frame is not None else gather_frame()
    _s = load_settings()
    cfg = cfg or {}
    SECTIONS = cfg.get("sections") or _s.get("sections")
    UNITS = cfg.get("units") or _s.get("units", "c")
    WEATHER_UNITS = _s.get("weather_units", "c")          # global (one weather source)
    SHOW_LOC = cfg.get("weather_show_location", _s.get("weather_show_location", True))
    SENSOR_NAMES = _s.get("sensor_names") or {}
    disks_sel = cfg.get("disks", _s.get("disks"))
    sensors_sel = cfg.get("sensors", _s.get("sensors"))
    periph_sel_cfg = cfg.get("peripherals", _s.get("peripherals"))
    order_cfg = cfg.get("order") or _s.get("order") or SECTION_ORDER
    target = target_h if target_h is not None else TARGET_H
    (cpu_pct, core_loads, gpu_pct, have_gpu, mem_used, mem_total, swap_total,
     swap_used, disk_used, disk_total, rd, wr, down, up, cap, bstatus, batt_w,
     eta, batt_v, watts, ac_w, power_src, cpu_t, nvme_t, wifi_t, uptime, load,
     sess, week, have_claude, weather, have_weather, top_cpu, top_mem) = (
        M["cpu_pct"], M["core_loads"], M["gpu_pct"], M["have_gpu"], M["mem_used"],
        M["mem_total"], M["swap_total"], M["swap_used"], M["disk_used"],
        M["disk_total"], M["rd"], M["wr"], M["down"], M["up"], M["cap"],
        M["bstatus"], M["batt_w"], M["eta"], M["batt_v"], M["watts"], M["ac_w"],
        M["power_src"], M["cpu_t"], M["nvme_t"], M["wifi_t"], M["uptime"],
        M["load"], M["sess"], M["week"], M["have_claude"], M["weather"],
        M["have_weather"], M["top_cpu"], M["top_mem"])
    FLEX = float(flex_in or 0.0)
    FLEX_POINTS = 0
    img = Image.new("RGBA", (W * SS, 1400 * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 22
    R = W - PAD

    f_val    = F(MONO_MED, T_VALUE)
    f_val_sm = F(MONO_REG, T_BODY)
    f_big    = F(MONO_LIGHT, T_LEAD)

    SLOT_H = 95
    ALT_PERIOD = 8

    def draw_claude(y):
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

    def draw_weather(y0):
        label(d, PAD, y0, "weather", ACCENT, tracking=2.4)
        if SHOW_LOC and weather.get("name"):
            label_r(d, R, y0, weather["name"], TEXT, size=T_MICRO)
        t = weather["temp"]
        f_temp = F(MONO_LIGHT, T_HERO)
        icon_s = 22
        ttxt = temp_str(t, WEATHER_UNITS)
        tw = measure(f_temp, ttxt) / SS
        iw = icon_s * 2.5
        gap_it = 14
        group_w = iw + gap_it + tw
        gx = PAD + (CW - group_w) / 2.0
        hero_cy = y0 + 46
        draw_weather_icon(img, gx + iw / 2, hero_cy, icon_s, weather.get("code", 3))
        text(d, gx + iw + gap_it, hero_cy - 21, ttxt, f_temp, TEXT)
        dy = y0 + 80
        f_dv = F(MONO_REG, T_VALUE)
        f_dl = F(UI_SEMI, T_MICRO)
        cells = (("min", weather['lo']), ("max", weather['hi']),
                 ("feels", weather['feels']))
        colw = CW / 3.0
        for i, (lab, tval) in enumerate(cells):
            val = temp_str(tval, WEATHER_UNITS)
            vcol = weather_temp_color(tval)
            cx = PAD + colw * (i + 0.5)
            lw = measure(f_dl, lab.upper(), 1.4) / SS
            vw = measure(f_dv, val) / SS
            gap_lv = 7
            x0 = cx - (lw + gap_lv + vw) / 2.0
            text(d, x0, dy + 3, lab.upper(), f_dl, TEXT, tracking=1.4)
            text(d, x0 + lw + gap_lv, dy, val, f_dv, vcol)

    def sec_top(y):
        if have_claude and have_weather:
            which = "weather" if int(time.time() // ALT_PERIOD) % 2 else "claude"
        elif have_claude:
            which = "claude"
        elif have_weather:
            which = "weather"
        else:
            return y
        (draw_claude if which == "claude" else draw_weather)(y)
        return y + SLOT_H + gap(26)

    def sec_claude(y):
        if not have_claude:
            return y
        draw_claude(y)
        return y + SLOT_H + gap(26)

    def sec_weather(y):
        if not have_weather:
            return y
        draw_weather(y)
        return y + SLOT_H + gap(26)

    def sec_load(y):
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
        return y + gap(40)

    def sec_gauges(y):
        gr, gth = 46, 9
        gauges = [("cpu", cpu_pct, ACCENT)]
        if have_gpu:
            gauges.append(("gpu", gpu_pct, VIOLET))
        gauges.append(("ram", mem_used / mem_total * 100, TEAL))
        gwidth = CW / len(gauges)
        for i, (name, pct, hue) in enumerate(gauges):
            cx = PAD + gwidth * (i + 0.5)
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
        return y + 2 * gr + gap(36)

    def sec_cores(y):
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
        return y + 24 + gap(14)

    def sec_history(y):
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
        return y

    def sec_thermals(y):
        sel = sensors_sel
        if sel:
            by_id = {s["id"]: s for s in list_sensors()}
            chosen = [by_id[i] for i in sel if i in by_id][:4]
            therms = [((SENSOR_NAMES.get(s["id"]) or s["label"]).lower(),
                       read_sensor(s["path"]), s["warn"], s["crit"])
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
        if not groups:
            return y
        label(d, PAD, y, "thermals", RED)
        gap_lv, gap_gg = 6, 20
        total = sum(lw + gap_lv + vw for _, lw, _, vw, _ in groups) + gap_gg * (len(groups) - 1)
        gx = R - total
        for labu, lw, val, vw, col in groups:
            text(d, gx, y + 1, labu, f_tl, TEXT, tracking=1.4)
            text(d, gx + lw + gap_lv, y - 1, val, f_tv, col)
            gx += lw + gap_lv + vw + gap_gg
        return y + gap(33)

    def sec_memory(y):
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
        return y + gap(22)

    def sec_disk(y):
        label(d, PAD, y, "disk", PINK)
        infos = []
        for mp in (disks_sel or ["/"]):
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
        return y + gap(30)

    def sec_network(y):
        label(d, PAD, y, "network", ACCENT)
        y += 16
        peak = net_chart(img, PAD, y, CW, 60, ACCENT, CORAL, floor=64 * 1024)
        y += 60 + 6
        text(d, PAD, y, f"↓ {fmt_bytes(down, True)}", f_val_sm, ACCENT)
        text(d, W / 2, y, f"peak {fmt_bytes(peak, True)}", F(MONO_REG, T_LABEL), TEXT, anchor="c")
        text(d, R, y, f"↑ {fmt_bytes(up, True)}", f_val_sm, CORAL, anchor="r")
        return y + gap(30)

    def sec_power(y):
        if not have_power:
            return y
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
        return y + gap(20)

    def sec_battery(y):
        if not have_battery:
            return y
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
        return y + gap(10)

    def sec_devices(y):
        periph_sel = periph_sel_cfg or []
        if not periph_sel:
            return y
        devs = [p for p in peripheral_batteries()
                if p["id"] in periph_sel and p["capacity"] is not None]
        if not devs:
            return y
        label(d, PAD, y, "devices", VIOLET)
        y += 18
        for p in devs:
            dcap = p["capacity"]
            col = GREEN if p["status"] == "Charging" else ramp_rgb(1 - dcap / 100)
            text(d, PAD, y - 2, p["name"][:26], F(UI_MED, T_BODY), TEXT)
            text(d, R, y - 2, f"{dcap}%", f_val, col, anchor="r")
            y += 15
            bar(img, PAD, y, CW, 5, dcap / 100, col)
            y += 13
        return y + gap(20)

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

    def sec_processes(y):
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
        return y + 22

    have_battery = bstatus != "no battery"
    have_power = power_src != "battery" or have_battery
    charging = bstatus == "Charging"
    section_fns = {
        "top": sec_top, "claude": sec_claude, "weather": sec_weather,
        "load": sec_load, "gauges": sec_gauges, "cores": sec_cores,
        "history": sec_history, "thermals": sec_thermals, "memory": sec_memory,
        "disk": sec_disk, "network": sec_network, "power": sec_power,
        "battery": sec_battery, "devices": sec_devices, "processes": sec_processes,
    }
    # Draw the sections in this panel's chosen order (normalised to a full list
    # of known keys), skipping the ones switched off.
    for key in order_cfg:
        fn = section_fns.get(key)
        if fn is not None and SECTIONS.get(key, True):
            y = fn(y)

    H = int(round(y))

    # Stretch the gaps between sections to fill TARGET_H (work area minus the
    # two vertical margins). This fills to the bottom margin and, crucially,
    # only changes the gaps — never the width — so toggling a section changes
    # the height, not the width.
    natural = y - FLEX_POINTS * FLEX
    next_flex = 0.0
    if FLEX_POINTS:
        # The panel is as tall as its content: adding or removing a section
        # grows or shrinks the window by that section. The gaps never *stretch*
        # to fill the box height (that is the ceiling `target`); they only
        # compress, down to FLEX_MIN, when the content would otherwise run past
        # it. Either way the width stays native.
        next_flex = max(FLEX_MIN, min(0.0, (target - natural) / FLEX_POINTS))

    panel = panel_bg(H, W)

    out = Image.alpha_composite(panel, img.crop((0, 0, W * SS, H * SS)))
    out = out.resize((W, H), Image.BOX)

    if write_png:
        tmp = f"{PNG_PATH}.{os.getpid()}.tmp"
        out.save(tmp, "PNG", compress_level=1)
        os.replace(tmp, PNG_PATH)

    return out, next_flex


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

    _apply_window_icon(Gtk)

    disp = Gdk.Display.get_default()
    last_frame = [None]      # most recent gathered metrics, reused for live redraws

    def monitor_of(cfg):
        return (disp.get_monitor(cfg.get("monitor", 0))
                or disp.get_primary_monitor() or disp.get_monitor(0))

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

    GRIP = 30                            # size of the resize handle, in px
    live_box = [None]                    # {"idx": i, "margins": {...}} during a grip drag

    def eff_margins(pw, cfg, wa):
        """The panel's box (x, y, w, h) and resolved four margins, honouring an
        in-flight grip resize of this panel."""
        lb = live_box[0]
        c = {**cfg, **lb["margins"]} if (lb and lb["idx"] == pw["idx"]) else cfg
        return panel_box(c, wa)

    def place(pw, cfg, w, h):
        win = pw["win"]
        wa = monitor_of(cfg).get_workarea()
        x, y, bw, bh, m = eff_margins(pw, cfg, wa)
        x = max(wa.x, min(x, wa.x + wa.width - w))
        y = max(wa.y, min(y, wa.y + wa.height - h))
        win.set_size_request(w, h)
        win.move(x, y)

    panels = []

    def snapped(pw, x, y, w, h):
        """Nudge a dragged window onto tidy targets: a small even gap from the
        work-area edges, and the edges of any other panel."""
        mon = disp.get_monitor_at_point(x + w // 2, y + h // 2) or disp.get_primary_monitor()
        wa = mon.get_workarea()
        SNAP, G = 26, MARGIN
        sx = [wa.x + G, wa.x + wa.width - w - G]
        sy = [wa.y + G, wa.y + wa.height - h - G]
        for other in panels:
            if other is pw:
                continue
            try:
                ox, oy = other["win"].get_position()
                oaw, oah = other["win"].get_allocated_width(), other["win"].get_allocated_height()
            except Exception:
                continue
            sx += [ox, ox + oaw - w]
            sy += [oy, oy + oah - h]
        for t in sx:
            if abs(x - t) <= SNAP:
                x = t
                break
        for t in sy:
            if abs(y - t) <= SNAP:
                y = t
                break
        return x, y

    def scaled_for(pw, cfg):
        """The frame is rendered natively at the panel's box width and height, so
        this returns it unchanged — width and height are both the user's, set
        independently, and no scaling is done at rest. The only guard keeps a
        panel from running past the bottom of its monitor (content that can't
        compress far enough)."""
        img = pw["state"].get("img")
        if img is None:
            return None
        wa = monitor_of(cfg).get_workarea()
        x, y, bw, bh, m = eff_margins(pw, cfg, wa)
        maxh = wa.height - m["top"] - 2
        if img.height > maxh > 0:                 # off-screen guard only
            k = maxh / img.height
            return img.resize((max(120, round(img.width * k)), maxh), Image.LANCZOS)
        return img

    def paint(pw, cfg):
        """Rebuild a panel's surface from its own current native frame."""
        dimg = scaled_for(pw, cfg)
        if dimg is None:
            return None
        st = pw["state"]
        st["surface"], st["buf"] = surface_from(dimg)
        st["natw"], st["nath"] = dimg.width, dimg.height   # true surface size
        st["w"], st["h"] = dimg.width, dimg.height          # window size (== surface at rest)
        return dimg

    def setup(pw):
        win, st = pw["win"], pw["state"]

        def on_draw(_w, cr):
            if st["surface"] is not None:
                aw, ah = win.get_allocated_width(), win.get_allocated_height()
                nw, nh = st.get("natw") or aw, st.get("nath") or ah
                cr.set_operator(cairo.OPERATOR_SOURCE)
                if nw and nh and (aw != nw or ah != nh):
                    # Window is a different size than the rendered frame (a live
                    # grip resize): let cairo scale the surface — cheap, unlike a
                    # per-motion Pillow resize. Re-rendered crisply on release.
                    cr.save()
                    cr.scale(aw / nw, ah / nh)
                    cr.set_source_surface(st["surface"], 0, 0)
                    cr.paint()
                    cr.restore()
                else:
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
            return {"monitor": 0}

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

        def _resize_render():
            """Debounced during a grip drag: re-render this panel natively at its
            live box so the stretched cairo preview snaps crisp when the drag
            pauses. Cheap enough off the last frame, and only fires between
            motions."""
            drag["rr"] = None
            if not st.get("resizing"):
                return False
            cfg = _cfg_of()
            wa = monitor_of(cfg).get_workarea()
            x, y, bw, bh, m = eff_margins(pw, cfg, wa)
            M = last_frame[0] if last_frame[0] is not None else gather_frame()
            img, fl = st.get("img"), st.get("flex", 0.0)
            for _ in range(2):
                img, fl = render(frame=M, cfg=cfg, width=int(bw), target_h=int(bh), flex_in=fl)
            st["img"], st["flex"] = img, fl
            paint(pw, cfg)
            win.set_size_request(st["w"], st["h"])
            win.queue_draw()
            return False

        def on_press(_w, ev):
            if not st.get("moving"):
                return False
            drag["active"] = True
            drag["sx"], drag["sy"] = ev.x_root, ev.y_root
            w, h = win.get_allocated_width(), win.get_allocated_height()
            if ev.x >= w - GRIP - 8 and ev.y >= h - GRIP - 8:
                drag["mode"] = "resize"
                wa = monitor_of(_cfg_of()).get_workarea()
                drag["m0"] = panel_box(_cfg_of(), wa)[4]
                drag["wa"] = wa
                drag["sw0"], drag["sh0"] = max(1, w), max(1, h)   # current size
            else:
                drag["mode"] = "move"
                drag["wx"], drag["wy"] = win.get_position()
            return True

        def on_motion(_w, ev):
            if not (st.get("moving") and drag["active"]):
                return False
            if drag.get("mode") == "resize":
                # bottom-right grip: the horizontal drag sets the width (floored
                # at the native width — it can be widened but never squeezed
                # narrower than the content is designed for). The height is not
                # draggable: it follows the content, so the vertical drag is
                # inert and the panel keeps its natural height.
                m0, wa = drag["m0"], drag["wa"]
                sw0, sh0 = drag["sw0"], drag["sh0"]
                dx = ev.x_root - drag["sx"]
                new_sw = min(max(W, int(sw0 + dx)), wa.width - m0["left"])
                new_sh = sh0
                new_right = int(wa.width - m0["left"] - new_sw)
                new_bottom = int(wa.height - m0["top"] - new_sh)
                live_box[0] = {"idx": pw["idx"],
                               "margins": {**m0, "right": new_right, "bottom": new_bottom}}
                st["resizing"] = True
                # Per motion: just resize the window and let cairo scale the
                # existing surface (fast, but stretched on one axis). A short
                # debounce then re-renders it natively at the new box, so it
                # snaps crisp and un-stretched whenever the drag pauses.
                st["w"], st["h"] = new_sw, new_sh
                win.set_size_request(new_sw, new_sh)
                gw = win.get_window()
                if gw is not None:              # keep the whole grip area clickable
                    gw.input_shape_combine_region(
                        cairo.Region(cairo.RectangleInt(0, 0, new_sw, new_sh)), 0, 0)
                win.queue_draw()
                if drag.get("rr"):
                    GLib.source_remove(drag["rr"])
                drag["rr"] = GLib.timeout_add(80, _resize_render)
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
                cfgs.append({"monitor": 0})
            pcfg = dict(cfgs[pw["idx"]])
            if drag.get("mode") == "resize":
                # lock in the resized box (its left/top were fixed, right/bottom
                # moved) — the width is now the user's, set by left+right.
                if drag.get("rr"):
                    GLib.source_remove(drag["rr"])
                    drag["rr"] = None
                if live_box[0] is not None:
                    pcfg.update(live_box[0]["margins"])
                    live_box[0] = None
                st["resizing"] = False
            else:
                # dropped freely: set all four margins to exactly where it sits,
                # at its current size, and it stays there. Display config kept.
                wx, wy = win.get_position()
                ww = win.get_allocated_width() or W
                wh = win.get_allocated_height() or (st["h"] or 0)
                mon = (disp.get_monitor_at_point(wx + ww // 2, wy + wh // 2)
                       or disp.get_primary_monitor())
                wa = mon.get_workarea()
                pcfg.update({"monitor": mon_index(disp, mon),
                             "left": int(wx - wa.x), "top": int(wy - wa.y),
                             "right": int(wa.width - (wx - wa.x) - ww),
                             "bottom": int(wa.height - (wy - wa.y) - wh)})
            cfgs[pw["idx"]] = pcfg
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
                            "img": None, "flex": 0.0,
                            "drag": {"active": False, "sx": 0, "sy": 0, "wx": 0, "wy": 0},
                            "placekey": None}}
            setup(pw)
            panels.append(pw)
            pw["win"].show_all()
        while len(panels) > n:
            pw = panels.pop()
            pw["closing"] = True
            pw["win"].destroy()

    DEFAULT_CFG = {"monitor": 0}

    def render_all(passes=1):
        """Render each panel's own frame from a single metric sample. The
        panels differ only in their display config, so the metrics are gathered
        once (running the sampler per panel would zero the rate deltas). Each
        panel is rendered natively at its box width and height — no scaling — so
        it stays crisp at any size; it keeps its own flex, which converges over
        two passes, so a settings change asks for passes=2. Touches no windows."""
        # While a panel is actively dragged, skip the whole refresh — gathering
        # metrics and rendering is heavy enough to hitch the drag. The live
        # feedback runs off the last frame; the metrics resume on release.
        if any(pw["state"].get("drag", {}).get("active") for pw in panels):
            return
        M = gather_frame()
        last_frame[0] = M
        cfgs = load_settings().get("panels") or [DEFAULT_CFG]
        for i, pw in enumerate(panels):
            st = pw["state"]
            cfg = cfgs[i] if i < len(cfgs) else {}
            wa = monitor_of(cfg).get_workarea()
            x, y, bw, bh, m = eff_margins(pw, cfg, wa)
            img, fl = st.get("img"), st.get("flex", 0.0)
            for _ in range(max(1, passes)):
                img, fl = render(frame=M, cfg=cfg, width=bw, target_h=bh, flex_in=fl)
            st["img"], st["flex"] = img, fl

    def tick(passes=1):
        try:
            s = load_settings()
            cfgs = s.get("panels") or [DEFAULT_CFG]
            sync_count(max(1, len(cfgs)))
            for i, pw in enumerate(panels):
                pw["idx"] = i
            # Render each panel's frame before placing it. The flex fill
            # converges over two passes, so a settings change (passes=2) sizes
            # and places each panel exactly once, at its final size — no visible
            # jump.
            render_all(passes)
            move_idx = s.get("move")
            if move_idx is True:
                move_idx = 0
            for i, pw in enumerate(panels):
                st = pw["state"]
                cfg = cfgs[i] if i < len(cfgs) else {}
                if move_idx == i and not st.get("moving"):
                    st["enter_move"]()
                elif move_idx != i and st.get("moving"):
                    st["exit_move"]()
                if not st.get("resizing"):       # don't fight a live resize drag
                    paint(pw, cfg)
                if not st.get("moving"):
                    key = (st.get("w"), st.get("h"), cfg.get("monitor"), cfg.get("top"),
                           cfg.get("bottom"), cfg.get("left"), cfg.get("right"))
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

    # Respond to settings changes near-instantly without cranking the (heavy)
    # metric refresh above. Reading the small settings file is cheap, so poll it
    # often and re-render only when its *contents* change — a content hash, not
    # the mtime, because a coarse filesystem mtime can collapse two quick edits
    # (a section toggled off then on) into no visible change.
    def _settings_stamp():
        try:
            with open(SETTINGS_FILE, "rb") as f:
                return hash(f.read())
        except OSError:
            return None

    def watch_settings():
        stamp = _settings_stamp()
        if stamp != watch_settings.stamp:
            watch_settings.stamp = stamp
            # Two render passes converge the flex fill for the new layout, then
            # the panel is placed once at its final size (no "springt hin und
            # her" when sections are toggled).
            tick(passes=2)
        return True

    watch_settings.stamp = _settings_stamp()
    GLib.timeout_add(120, watch_settings)

    log(f"panel started (pid {os.getpid()})")
    Gtk.main()


def run_settings():
    """A small GTK window that reads and writes settings.json. Nothing here is
    hand-edited: this is the graphical front for it, and a running panel picks
    up the saved file within a second."""
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, Gdk, GLib
    import urllib.parse
    import urllib.request

    _apply_window_icon(Gtk)

    # Opening Settings also brings the panel up, so the app works even when
    # autostart never ran — that's why there's no separate "panel" menu entry.
    # The running panel holds an exclusive lock on LOCK_FILE for its whole life;
    # if we can take that lock then none is running, so we release it and start
    # one. A second panel would exit on the same lock, so this never doubles up.
    try:
        _probe = open(LOCK_FILE, "a+")
        try:
            fcntl.flock(_probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(_probe, fcntl.LOCK_UN)
            subprocess.Popen([sys.executable, os.path.abspath(__file__)],
                             start_new_session=True)
        except OSError:
            pass                       # locked -> a panel is already running
        finally:
            _probe.close()
    except Exception:
        pass

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

    /* Drag-to-reorder section list */
    .order-list {
        background-color: #14171c; border: 1px solid #262b33; border-radius: 10px;
    }
    .order-list row { border-bottom: 1px solid #20252d; }
    .order-list row:last-child { border-bottom: none; }
    .order-list row:hover { background-color: rgba(96,176,255,0.10); }
    .order-list row.dragging { background-color: rgba(96,176,255,0.20); }

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

    # ---- Panels ----------------------------------------------------------
    # Each panel is edited on its own. Pick it here and every control below
    # (margins, sections, order, disks, sensors, unit) reads and writes that
    # one panel's config; placement (move/resize) already acts per panel.
    pbox, pg, pc = make_group("Panels")
    disp = Gdk.Display.get_default()
    editing = [0]                 # index of the panel the controls are bound to
    _loading = [False]            # True while load_panel() sets the controls

    def _panels():
        return [dict(c) for c in (load_settings().get("panels") or [{}])]

    def _nmon():
        return disp.get_n_monitors()

    panel_combo = _noscroll(Gtk.ComboBoxText())

    def _panel_label(i, p):
        return (p.get("name") or "").strip() or f"Panel {i + 1}"

    def _refill_combo():
        ps = _panels()
        n = max(1, len(ps))
        editing[0] = max(0, min(editing[0], n - 1))
        panel_combo.handler_block(panel_combo._h)
        panel_combo.remove_all()
        for i in range(n):
            panel_combo.append(str(i), _panel_label(i, ps[i] if i < len(ps) else {}))
        panel_combo.set_active_id(str(editing[0]))
        panel_combo.handler_unblock(panel_combo._h)

    panel_combo.set_hexpand(False)
    panel_combo.set_halign(Gtk.Align.START)
    panel_combo.set_size_request(160, -1)
    field(pg, pc, "Edit panel", panel_combo)

    name_entry = Gtk.Entry()
    name_entry.set_placeholder_text("Panel name")
    name_entry.set_hexpand(False)
    name_entry.set_size_request(200, -1)
    field(pg, pc, "Name", name_entry)

    add_btn = _cls(Gtk.Button(label="Add"), "subtle")
    dup_btn = _cls(Gtk.Button(label="Duplicate"), "subtle")
    del_btn = _cls(Gtk.Button(label="Remove"), "subtle")
    btnrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    for _b in (add_btn, dup_btn, del_btn):
        btnrow.pack_start(_b, False, False, 0)
    field(pg, pc, "", btnrow)

    move1 = _cls(Gtk.Button(label="Move panel…"), "ghost")
    move1.set_halign(Gtk.Align.START)
    move1.set_size_request(240, -1)
    reset_btn = _cls(Gtk.Button(label="Reset positions"), "subtle")
    reset_btn.set_halign(Gtk.Align.START)
    place_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
    place_box.pack_start(move1, False, False, 0)
    place_box.pack_start(reset_btn, False, False, 0)
    field(pg, pc, "", place_box)

    def refresh_move_labels():
        mv = load_settings().get("move")
        move1.set_label("Save position" if mv == editing[0] else "Move panel…")

    def toggle_move(*_):
        idx = editing[0]
        st = dict(load_settings())
        if st.get("move") == idx:                 # second click -> save & stop
            st["move"] = None
            save_settings(st)
            status.set_text("Position saved.")
        else:
            st["move"] = idx
            save_settings(st)
            status.set_text("Drag the panel (its corner grip resizes). Click “Save position” when done.")
        refresh_move_labels()

    def do_add(dup):
        st = dict(load_settings())
        cfgs = [dict(c) for c in (st.get("panels") or [{}])]
        src = cfgs[editing[0]] if editing[0] < len(cfgs) else cfgs[0]
        new = dict(src)                           # inherit the display config
        if dup and (src.get("name") or "").strip():
            # name the copy "<name> 2", "<name> 3", … (next free number)
            base = (src["name"] or "").strip()
            head, _, tail = base.rpartition(" ")
            root = head.strip() if (head and tail.isdigit()) else base
            existing = {(p.get("name") or "").strip() for p in cfgs}
            n = 2
            while f"{root} {n}" in existing:
                n += 1
            new["name"] = f"{root} {n}"
        elif not dup:                             # a plain Add is a fresh, unnamed panel
            new.pop("name", None)
            new["position"], new["offset"] = "top-left", None
        new["monitor"] = 1 if _nmon() > 1 else new.get("monitor", 0)
        cfgs.append(new)
        editing[0] = len(cfgs) - 1
        st["panels"] = cfgs
        save_settings(st)
        _refill_combo()
        load_panel()
        refresh_move_labels()
        status.set_text(("Duplicated" if dup else "Added")
                        + f" — {len(cfgs)} panels. Use “Move panel…” to place it.")

    def do_remove(*_):
        st = dict(load_settings())
        cfgs = [dict(c) for c in (st.get("panels") or [{}])]
        if len(cfgs) <= 1:
            status.set_text("At least one panel is needed.")
            return
        cfgs.pop(editing[0])
        editing[0] = max(0, editing[0] - 1)
        st["panels"] = cfgs
        st["move"] = None
        save_settings(st)
        _refill_combo()
        load_panel()
        refresh_move_labels()
        status.set_text(f"Removed — {len(cfgs)} panels.")

    def do_reset(_b):
        st = dict(load_settings())
        cfgs = [dict(c) for c in (st.get("panels") or [{}])]
        for c in cfgs:
            c["position"], c["offset"] = "top-right", None
        st["panels"] = cfgs
        st["move"] = None
        save_settings(st)
        refresh_move_labels()
        status.set_text("Positions reset to the top-right corner.")

    def _on_panel_switch(_c):
        aid = panel_combo.get_active_id()
        if aid is None:
            return
        editing[0] = int(aid)
        load_panel()
        refresh_move_labels()

    panel_combo._h = panel_combo.connect("changed", _on_panel_switch)
    add_btn.connect("clicked", lambda *_: do_add(False))
    dup_btn.connect("clicked", lambda *_: do_add(True))
    del_btn.connect("clicked", do_remove)
    move1.connect("clicked", toggle_move)
    reset_btn.connect("clicked", do_reset)

    _P0 = (s.get("panels") or [{}])[0]

    def _resolved_margins(pcfg):
        mon = (disp.get_monitor(pcfg.get("monitor", 0))
               or disp.get_primary_monitor() or disp.get_monitor(0))
        return panel_box(pcfg, mon.get_workarea())[4]

    _m0 = _resolved_margins(_P0)
    # Four independent edge margins. Top+bottom set the height and vertical
    # position; left+right set the width and horizontal position.
    margin_spins = {}
    for _key, _lbl in (("top", "Top"), ("bottom", "Bottom"), ("left", "Left"), ("right", "Right")):
        sp = _noscroll(Gtk.SpinButton.new_with_range(0, 4000, 1))
        sp.set_value(_m0.get(_key, 22))
        sp.set_hexpand(False)
        sp.set_halign(Gtk.Align.START)
        sp.set_size_request(110, -1)
        field(pg, pc, _lbl, sp)
        margin_spins[_key] = sp


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

    # Weather has its own °C/°F, separate from the hardware temperatures below,
    # and the location name can be hidden.
    wunit_combo = _noscroll(Gtk.ComboBoxText())
    wunit_combo.append("c", "Celsius (°C)")
    wunit_combo.append("f", "Fahrenheit (°F)")
    wunit_combo.set_active_id(s.get("weather_units", "c"))
    wunit_combo.set_hexpand(False)
    wunit_combo.set_halign(Gtk.Align.START)
    wunit_combo.set_size_request(190, -1)
    field(wg, wc, "Weather unit", wunit_combo)
    showloc_chk = Gtk.CheckButton(label="Show location name (this panel)")
    showloc_chk.set_active(bool(_P0.get("weather_show_location", True)))
    field(wg, wc, "", showloc_chk)

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
            commit_weather()
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
    for idx, (key, txt) in enumerate(SECTION_DEFS):
        cb = Gtk.CheckButton(label=txt)
        cb.set_active((_P0.get("sections") or {}).get(key, True))
        checks[key] = cb
        secgrid.attach(cb, idx % 2, idx // 2, 1, 1)
    field(seg, sec_, "", secgrid)

    # ---- Order (drag to reorder) ----------------------------------------
    # A list of just the enabled sections that the user can drag into any
    # order; that order drives the render top-to-bottom. Toggling a section
    # above adds or removes its row here without disturbing the rest.
    _, og, oc = make_group("Order")
    full_order = list(_P0.get("order") or SECTION_ORDER)
    order_list = Gtk.ListBox()
    order_list.set_selection_mode(Gtk.SelectionMode.NONE)
    _cls(order_list, "order-list")
    ROW_TARGET = [Gtk.TargetEntry.new("HUD_ROW", Gtk.TargetFlags.SAME_APP, 0)]
    drag_src = {"key": None}

    # The drag source/target sits on an EventBox inside each row, not on the
    # GtkListBoxRow itself: the list box claims the row's button-press for its
    # own selection handling, so a source set on the row never sees the motion
    # that would start a drag. The EventBox is a child with its own window and
    # gets the press first.
    def _on_drag_begin(widget, ctx):
        drag_src["key"] = widget.key
        widget.row.get_style_context().add_class("dragging")

    def _on_drag_end(widget, ctx):
        widget.row.get_style_context().remove_class("dragging")
        drag_src["key"] = None

    def _on_drag_get(widget, ctx, sel, info, t):
        sel.set(sel.get_target(), 8, widget.key.encode())

    def _on_drag_received(dest, ctx, x, y, sel, info, t):
        sk, dk = drag_src["key"], dest.key
        if not sk or sk == dk:
            return
        vis = _visible_keys()
        if sk not in vis or dk not in vis:
            return
        vis.remove(sk)
        dest_i = vis.index(dk)
        if y > dest.get_allocated_height() / 2:
            dest_i += 1
        vis.insert(dest_i, sk)
        it = iter(vis)
        full_order[:] = [next(it) if checks[k].get_active() else k for k in full_order]
        _rebuild_order_rows()
        commit()

    def _make_order_row(key):
        row = Gtk.ListBoxRow()
        row.key = key
        ev = Gtk.EventBox()
        ev.key = key
        ev.row = row
        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
        hb.set_margin_start(4)
        hb.set_margin_end(4)
        hb.set_margin_top(5)
        hb.set_margin_bottom(5)
        hb.pack_start(_cls(Gtk.Label(label="≡"), "hint"), False, False, 0)
        hb.pack_start(Gtk.Label(label=SECTION_LABELS.get(key, key), xalign=0), True, True, 0)
        ev.add(hb)
        row.add(ev)
        ev.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, ROW_TARGET, Gdk.DragAction.MOVE)
        ev.drag_dest_set(Gtk.DestDefaults.ALL, ROW_TARGET, Gdk.DragAction.MOVE)
        ev.connect("drag-begin", _on_drag_begin)
        ev.connect("drag-end", _on_drag_end)
        ev.connect("drag-data-get", _on_drag_get)
        ev.connect("drag-data-received", _on_drag_received)
        return row

    def _visible_keys():
        return [k for k in full_order if checks[k].get_active()]

    def _rebuild_order_rows():
        for c in order_list.get_children():
            order_list.remove(c)
        for k in _visible_keys():
            order_list.add(_make_order_row(k))
        order_list.show_all()

    field(og, oc, "", order_list)
    _rebuild_order_rows()

    # ---- Disks -----------------------------------------------------------
    _, dg, dc = make_group("Disks")
    cur_disks = _P0.get("disks") or ["/"]
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
    units_combo.set_active_id(_P0.get("units", "c"))
    field(tg, tc, "Unit", units_combo)
    units_combo.set_hexpand(False)
    units_combo.set_halign(Gtk.Align.START)
    units_combo.set_size_request(190, -1)

    cur_sens = _P0.get("sensors") or []
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

    # Custom display names for the chosen sensors, rebuilt as the selection (or
    # the edited panel) changes. Names are shared across panels — a sensor is
    # the same sensor everywhere; the placeholder is the auto-detected name.
    sensor_names_state = dict(s.get("sensor_names") or {})
    _sensmap = {se["id"]: se for se in sensors}
    names_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

    def rebuild_sensor_names():
        for c in names_box.get_children():
            names_box.remove(c)
        sel = [sid for sid, cb in sens_checks.items() if cb.get_active()][:4]
        if not sel:
            names_box.pack_start(
                _cls(Gtk.Label(label="Tick sensors above to give them panel names.",
                               xalign=0), "hint"), False, False, 0)
        for sid in sel:
            se = _sensmap.get(sid)
            if not se:
                continue
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lab = _cls(Gtk.Label(label=se["label"], xalign=0), "hint")
            lab.set_size_request(120, -1)
            row.pack_start(lab, False, False, 0)
            ent = Gtk.Entry()
            ent.set_placeholder_text(se["label"])
            ent.set_text(sensor_names_state.get(sid, ""))

            def _on_name(e, sid=sid):
                if _loading[0]:
                    return
                sensor_names_state[sid] = e.get_text()
                commit_weather()
            ent.connect("changed", _on_name)
            row.pack_start(ent, True, True, 0)
            names_box.pack_start(row, False, False, 0)
        names_box.show_all()

    tg.attach(_cls(Gtk.Label(label="Names", xalign=0, yalign=0), "field-label"), 0, tc[0], 1, 1)
    names_box.set_hexpand(True)
    tg.attach(names_box, 1, tc[0], 1, 1)
    tc[0] += 1

    # ---- Devices ---------------------------------------------------------
    _, deg, dec = make_group("Devices")
    cur_periph = _P0.get("peripherals") or []
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
    status = _cls(Gtk.Label(label="Changes apply as you make them.", xalign=0), "status-ok")
    status.set_line_wrap(True)
    actionbar.pack_start(status, True, True, 0)
    close = _cls(Gtk.Button(label="Close"), "subtle")
    actionbar.pack_end(close, False, False, 0)
    outer.pack_start(actionbar, False, False, 0)

    def commit(*_):
        if _loading[0]:                      # ignore the signals load_panel() fires
            return
        new = dict(load_settings())
        # Shared weather fields (location/unit/sensor names) are *not* touched
        # here — they have their own commit_weather(), so toggling a section can
        # never clobber the saved location the way it used to.
        cfgs = [dict(c) for c in (new.get("panels") or [{"monitor": 0}])]
        idx = max(0, min(editing[0], len(cfgs) - 1))   # clamp, never append phantoms
        pcfg = dict(cfgs[idx])              # keeps the on-disk margins as they are
        pcfg["weather_show_location"] = showloc_chk.get_active()   # per panel
        pcfg["units"] = units_combo.get_active_id() or "c"
        pcfg["sections"] = {k: cb.get_active() for k, cb in checks.items()}
        pcfg["order"] = list(full_order)
        dsel = [mp for mp, cb in disk_checks.items() if cb.get_active()]
        pcfg["disks"] = dsel or None
        ssel = [sid for sid, cb in sens_checks.items() if cb.get_active()]
        pcfg["sensors"] = ssel or None
        psel = [pid for pid, cb in periph_checks.items() if cb.get_active()]
        pcfg["peripherals"] = psel or None
        cfgs[idx] = pcfg
        new["panels"] = cfgs
        save_settings(new)
        _own_stamp[0] = _file_stamp()      # remember our own write (see watcher)

    def commit_weather(*_):
        # Shared (not per-panel): the weather location + unit and the custom
        # sensor names. Written onto fresh on-disk settings and only from the
        # widgets that actually own these values, so a section toggle elsewhere
        # never rewrites (and used to wipe) the location.
        if _loading[0]:
            return
        new = dict(load_settings())
        if loc_state["data"]:                      # never overwrite with nothing
            new["location"] = loc_state["data"]
        new["weather_units"] = wunit_combo.get_active_id() or "c"
        new["sensor_names"] = {k: v.strip() for k, v in sensor_names_state.items() if v.strip()}
        save_settings(new)
        _own_stamp[0] = _file_stamp()

    def commit_margins(*_):
        # The four margin spinners write only the margins, onto the panel's
        # current on-disk config — so a section toggle never clobbers a
        # hand-dragged position, and vice versa.
        if _loading[0]:
            return
        new = dict(load_settings())
        cfgs = [dict(c) for c in (new.get("panels") or [{"monitor": 0}])]
        idx = max(0, min(editing[0], len(cfgs) - 1))
        pcfg = dict(cfgs[idx])
        for _k, _sp in margin_spins.items():
            pcfg[_k] = int(_sp.get_value())
        cfgs[idx] = pcfg
        new["panels"] = cfgs
        save_settings(new)
        _own_stamp[0] = _file_stamp()

    def commit_name(*_):
        # Writes only the panel's display name (onto its current on-disk config)
        # and refreshes the picker label.
        if _loading[0]:
            return
        new = dict(load_settings())
        cfgs = [dict(c) for c in (new.get("panels") or [{"monitor": 0}])]
        idx = max(0, min(editing[0], len(cfgs) - 1))
        nm = name_entry.get_text().strip()
        if nm:
            cfgs[idx]["name"] = nm
        else:
            cfgs[idx].pop("name", None)
        new["panels"] = cfgs
        save_settings(new)
        _own_stamp[0] = _file_stamp()
        _refill_combo()

    _sens_paths = {se["id"]: se["path"] for se in sensors}

    def load_panel():
        """Point every display control at the currently-edited panel."""
        cfgs = _panels()
        pcfg = cfgs[editing[0]] if editing[0] < len(cfgs) else {}
        _loading[0] = True
        try:
            _rm = _resolved_margins(pcfg)
            for _k, _sp in margin_spins.items():
                _sp.set_value(_rm.get(_k, 22))
            name_entry.set_text(pcfg.get("name", "") or "")
            units_combo.set_active_id(pcfg.get("units", "c"))
            showloc_chk.set_active(bool(pcfg.get("weather_show_location", True)))
            sec = pcfg.get("sections") or {}
            for k, cb in checks.items():
                cb.set_active(bool(sec.get(k, True)))
            dsel = pcfg.get("disks") or ["/"]
            for mp, cb in disk_checks.items():
                cb.set_active(mp in dsel)
            ssel = pcfg.get("sensors") or []
            for sid, cb in sens_checks.items():
                cb.set_active((sid in ssel) if ssel else (_sens_paths.get(sid) in auto_paths))
            psel = pcfg.get("peripherals") or []
            for pid, cb in periph_checks.items():
                cb.set_active(pid in psel)
            full_order[:] = normalize_order(pcfg.get("order"))
            _rebuild_order_rows()
            rebuild_sensor_names()
        finally:
            _loading[0] = False

    # every control applies itself immediately — no Save button
    name_entry.connect("changed", commit_name)
    units_combo.connect("changed", commit)
    wunit_combo.connect("changed", commit_weather)
    showloc_chk.connect("toggled", commit)

    def _on_section_toggle(*_):
        if _loading[0]:
            return
        _rebuild_order_rows()       # add/remove this section's row in the order list
        commit()
    for _cb in checks.values():
        _cb.connect("toggled", _on_section_toggle)

    def _on_sensor_toggle(*_):
        if _loading[0]:
            return
        rebuild_sensor_names()      # add/remove this sensor's name field
        commit()
    for _cb in sens_checks.values():
        _cb.connect("toggled", _on_sensor_toggle)
    for _cb in (list(disk_checks.values()) + list(periph_checks.values())):
        _cb.connect("toggled", commit)
    for _sp in margin_spins.values():
        _sp.connect("value-changed", commit_margins)
    close.connect("clicked", lambda *_: win.close())

    def _file_stamp():
        try:
            with open(SETTINGS_FILE, "rb") as f:
                return hash(f.read())
        except OSError:
            return None

    _own_stamp = [_file_stamp()]

    def _watch_external():
        # If settings.json changed underneath us — the panel saving a hand-drag
        # or grip resize — reload the edited panel's controls, so a later edit
        # doesn't write the stale margins back over the new position/size.
        cur = _file_stamp()
        if cur != _own_stamp[0]:
            _own_stamp[0] = cur
            _refill_combo()
            load_panel()
            refresh_move_labels()
        return True
    GLib.timeout_add(400, _watch_external)

    _refill_combo()
    load_panel()                # sync every control (incl. the name field)
    refresh_move_labels()

    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    if "--settings" in sys.argv:
        run_settings()
    elif "--png" in sys.argv:
        _M = gather_frame()
        _, _fl = render(frame=_M, write_png=False)   # settle the flex fill
        render(frame=_M, flex_in=_fl, write_png=True)
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
        # Guess a weather location from the public IP if none is set yet, off
        # the main thread so it never delays the panel (no-op once configured).
        threading.Thread(target=autodetect_location, daemon=True).start()
        # First run ever: open the settings window once so a new user lands
        # straight in the configuration. A marker keeps it to the first time.
        _welcome = os.path.join(CONF_DIR, ".welcomed")
        if not os.path.exists(_welcome):
            try:
                open(_welcome, "w").close()
                subprocess.Popen([sys.executable, os.path.abspath(__file__), "--settings"],
                                 start_new_session=True)
            except Exception:
                pass
        run_window()
