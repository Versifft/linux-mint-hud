#!/usr/bin/env python3
"""A system panel that lives on the desktop.

Reads every metric straight from /proc and /sys, draws the whole panel with
Pillow — letterspacing, rounded bars, gradients, ring gauges, a glass
background, none of which a text-based panel can do — and paints the result
into its own desktop window through GTK and cairo.

Rates are deltas against the previous frame, so no sampling sleep is needed.
Run with --png to write a single frame to cache/hud.png instead.
"""
import fcntl
import glob
import json
import os
import re
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
    try:                       # a renderer killed mid-write leaves these behind
        if time.time() - os.path.getmtime(_stale) > 300:
            os.unlink(_stale)
    except OSError:
        pass

STATE_FILE = os.path.join(CACHE_DIR, "state.json")
HISTORY_FILE = os.path.join(CACHE_DIR, "history.json")
PNG_PATH = os.path.join(CACHE_DIR, "hud.png")   # only written by --png
LOG_FILE = os.path.join(CACHE_DIR, "hud.log")
LOCK_FILE = os.path.join(CACHE_DIR, "hud.lock")

PSUPPLY = "/sys/class/power_supply"
DETECT_TTL = 30         # seconds between re-detections
HIST_MAX_AGE = 3600     # keep an hour of samples
# >1 = more resolution on the recent (right) side. 3.3 over an hour gives the
# last minute the same 29% of the width that 2.2 gave it over fifteen, so
# nothing was lost at the live end in exchange for the extra 45 minutes.
HIST_GAMMA = 3.3

# Type scale. Six steps and nothing in between: cap heights quantise to half a
# pixel at this panel size, so 9.0 and 9.5 render identically and 10.5 and 11.0
# do too. Sizes closer than ~1.5pt buy no hierarchy, they only look unresolved.
T_HERO  = 29            # gauge readings
T_LEAD  = 21            # uptime
T_VALUE = 13            # a section's primary figure
T_BODY  = 11.5          # secondary figures and context text
T_LABEL = 10            # section titles, letterspaced caps
T_MICRO = 8.5           # sub-labels inside a section

SS = 2                  # supersampling factor; everything is drawn at SSx then downscaled
W = 420                 # panel width in final pixels
PAD = 22                # horizontal padding inside the panel
CW = W - 2 * PAD        # content width

# The panel is grown to sit at an equal margin on all sides. MARGIN must match
# the window placement below. The height to fill is the WORK AREA, not the
# screen: this desktop has a 40px taskbar at the bottom, and measuring against
# the full 1200px would tuck the last stretch of the panel behind it.
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
FLEX_MAX = 48           # cap, so a short panel doesn't get absurd gaps

# Space added to every section boundary to reach TARGET_H. Solved from the last
# frame's measurement and carried in state.json: the layout is stable, so this
# converges in one frame and only ever moves when the content changes shape.
FLEX = 0.0
FLEX_POINTS = 0


def gap(base):
    """A section boundary that absorbs part of the leftover vertical space."""
    global FLEX_POINTS
    FLEX_POINTS += 1
    return base + FLEX

# ---------------------------------------------------------------- palette
TEXT      = (233, 238, 245)
DIM       = (150, 161, 177)
MUTE      = (104, 116, 133)
STEEL     = (116, 129, 149)
# quota ramp: green -> amber -> red. Routed through amber because a straight
# green-to-red interpolation goes through mud in the middle.
# Four stops, not three. With a single amber waypoint and CRIT at the end the
# ramp never actually arrived: a battery at 13% drew (250,114,87) and 0% would
# have been (255,84,94) — the whole bottom of the range was one shade of
# salmon. CRIT is deliberately soft because it is a warning tint on text; a bar
# that is supposed to read as empty needs a real red.
RAMP      = ((0.0, (72, 199, 116)),     # full
             (0.50, (240, 185, 70)),    # amber
             (0.78, (246, 105, 64)),    # orange-red
             (1.0, (222, 42, 52)))      # empty
# Domain hues, chosen by perceptual distance rather than by eye. CPU and GPU
# share the history chart, so that pair matters most; disk no longer borrows
# the GPU's violet. All four are brighter than they were — GPU by 9 points of
# L* — which is as far as luminance goes before the pairs start closing on
# each other again: the brightest set tested fell to dE 38.
# Pairwise now: CPU-GPU 53, CPU-RAM 51, GPU-DISK 50, and 77+ for the rest.
ACCENT    = (96, 176, 255)
VIOLET    = (180, 120, 255)
PINK      = (255, 96, 180)   # disk, which used to borrow the GPU's violet
TEAL      = (48, 228, 236)
CORAL     = (224, 128, 93)
AMBER     = (240, 176, 80)
GREEN     = (72, 199, 116)
DARKRED   = (186, 66, 66)

# Power direction, stored per history sample. Three states rather than a
# boolean: a full battery on AC is neither charging nor discharging, and
# recording it as "not charging" painted an idle machine as if it were
# running down its battery.
CHG_OUT   = 0.0         # on battery, power coming out
CHG_IDLE  = 0.5         # on AC, nothing flowing
CHG_IN    = 1.0         # charging, power going in
CHG_OUT_MAX = 0.25      # above this a column counts as mains-supplied
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
    return DIM


def state_color(pct, base=ACCENT):
    """Each domain keeps its own hue until it runs hot; red and amber are
    reserved for load, so a colour change always means something."""
    if pct >= 90:
        return CRIT
    if pct >= 75:
        return WARN
    return base


# ---------------------------------------------------------------- fonts
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


# ---------------------------------------------------------------- draw helpers
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
        fw = max(fw, Hp)  # keep the pill readable at tiny values
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
                # dimmer at the start, full accent at the head
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
    # anything past the left edge is dropped: render() trims to HIST_MAX_AGE + 5,
    # so a couple of samples always sit just outside it
    pts = [(now - s.get("t", now), s[key]) for s in HISTORY
           if s.get(key) is not None and now - s.get("t", now) < HIST_MAX_AGE]
    if not pts:
        return [0.0] * n
    denom = max(1, n - 1)
    ages = [((denom - i) / denom) ** HIST_GAMMA * HIST_MAX_AGE for i in range(n)]

    cols = []
    last = pts[0][1]          # oldest sample, carried into any empty column
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
            bh = 1.2 * SS          # keep a baseline tick so the axis stays visible
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
        # second series as a line over the columns: two metrics, one axis, and
        # you can see whether GPU load tracks CPU load or moves on its own
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
                bh = 1.2 * SS if col is not GREEN else 0    # keep an axis tick
            if bh <= 0:
                continue
            td.rectangle([bx, base - bh, bx + bw, base], fill=(*col, int(55 + 193 * frac)))
            base -= bh
        tops.append((bx + bw / 2, base, dirs[i] > CHG_OUT_MAX))

    img.alpha_composite(tile.filter(ImageFilter.GaussianBlur(1.8 * SS)), (X - pad, Y - pad))
    img.alpha_composite(tile, (X - pad, Y - pad))

    # Trace across the tops of the stacks: reading a total off two stacked
    # segments means judging where one ends and estimating the sum, and the
    # line states it directly. Drawn after the glow so it stays crisp.
    #
    # It stays continuous but changes colour: white where the wall supplies
    # the total, red where the pack does. Breaking it instead was honest but
    # chopped the trace up. The red is the brighter CRIT rather than the
    # columns' own DARKRED, which would vanish against them.
    # A dark pass slightly wider than the line goes down first. The trace runs
    # over columns of every colour, and without that separation it reads as
    # part of whatever it happens to cross.
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
    # segments are drawn opaque and each layer faded once afterwards, so
    # overlapping joints do not stack up into darker blobs
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
    split = int(7 * SS)                  # dead space between the two bands
    half = (Hp - split) // 2
    pad = 4 * SS
    top_y, bot_y = pad, pad + half + split
    size = (Wp + 2 * pad, Hp + 2 * pad)

    # bands and baselines are flat backdrop, drawn separately from the data so
    # the glow pass below doesn't smear their edges into halos
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


# ---------------------------------------------------------------- formatting
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


# ---------------------------------------------------------------- metrics
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
        return val                      # keep the last good one over a blip
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
    """First power_supply device of type Battery, or None on a desktop."""
    def find():
        try:
            for name in sorted(os.listdir(PSUPPLY)):
                d = f"{PSUPPLY}/{name}"
                if read_first(f"{d}/type", default="") == "Battery":
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
    """Sums drm-engine-* busy-time (ns) from /proc/*/fdinfo, deduped by
    (pid, drm-client-id) since one process can hold several fds per client."""
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
            cid, engines = None, {}
            for line in content.splitlines():
                if line.startswith("drm-client-id:"):
                    cid = line.split(":", 1)[1].strip()
                elif line.startswith("drm-engine-") and "capacity" not in line:
                    k, _, v = line.partition(":")
                    try:
                        engines[k.strip()] = int(v.strip().split()[0])
                    except (ValueError, IndexError):
                        pass
            if cid and engines:
                clients[(pid_dir, cid)] = engines
    totals = {}
    for engines in clients.values():
        for k, v in engines.items():
            totals[k] = totals.get(k, 0) + v
    return totals


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


def diskio_sectors():
    r = w = 0
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                fl = line.split()
                name = fl[2]
                # physical devices only; skip partitions and virtual devices
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


def temps():
    """CPU package, NVMe and wifi radio temperatures, in degrees C.

    The wifi sensor has no temp*_label, so it's addressed by hwmon name and
    fixed input rather than by looking a label up."""
    cpu = nvme = wifi = None
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read_first(os.path.join(h, "name"), default="")
        if name == "coretemp":
            for lab in glob.glob(os.path.join(h, "temp*_label")):
                if read_first(lab, default="") == "Package id 0":
                    cpu = read_first(lab.replace("_label", "_input"), int)
        elif name == "nvme":
            for lab in glob.glob(os.path.join(h, "temp*_label")):
                if read_first(lab, default="") == "Composite":
                    nvme = read_first(lab.replace("_label", "_input"), int)
        elif name.startswith("iwlwifi"):
            wifi = read_first(os.path.join(h, "temp1_input"), int)
    return tuple(v / 1000 if v else None for v in (cpu, nvme, wifi))


def temp_color(t, warn, crit):
    if t is None:
        return MUTE
    if t >= crit:
        return CRIT
    if t >= warn:
        return WARN
    return DIM


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
    battery."""
    global _RAPL
    if _RAPL is None:
        best = None
        for d in sorted(glob.glob("/sys/class/powercap/intel-rapl:[0-9]")):
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
        delta += rng                    # counter wrapped at max_energy_range_uj
    if delta < 0:
        return None, now_uj, name
    return delta / 1e6 / elapsed, now_uj, name


def battery():
    bat = battery_path()
    if not bat:
        return 0, "no battery", 0.0, None
    cap = read_first(f"{bat}/capacity", int, 0)
    status = read_first(f"{bat}/status", default="Unknown")
    cur = read_first(f"{bat}/current_now", int, 0) or 0
    volt = read_first(f"{bat}/voltage_now", int, 0) or 0
    now = read_first(f"{bat}/charge_now", int, 0) or 0
    full = read_first(f"{bat}/charge_full", int, 0) or 0
    watts = (cur / 1e6) * (volt / 1e6)
    eta = None
    if cur > 0:
        if status == "Discharging":
            eta = now / cur * 3600
        elif status == "Charging" and full > now:
            eta = (full - now) / cur * 3600
    return cap, status, watts, eta


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


# ---------------------------------------------------------------- cached shell-outs
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


# ---------------------------------------------------------------- state
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


HISTORY = []
_STATE = None           # previous sample, kept in memory between frames
_PERSISTED = 0.0        # when state/history last reached disk
PERSIST_EVERY = 30      # seconds


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

    # top highlight, so the panel reads as glass rather than a flat rectangle.
    # Only the top strip is blurred; the rest of the canvas contributed nothing.
    strip_h = 90 * SS
    hl = Image.new("RGBA", (W * SS, strip_h), (0, 0, 0, 0))
    ImageDraw.Draw(hl).rounded_rectangle(
        [SS, SS, W * SS - 1 - SS, 60 * SS], radius=17 * SS, fill=(255, 255, 255, 12))
    hl = hl.filter(ImageFilter.GaussianBlur(6 * SS))
    panel.alpha_composite(hl, (0, 0))

    _PANEL_CACHE.clear()          # only one height is ever live
    _PANEL_CACHE[H] = panel
    return panel


def render(write_png=True):
    global HISTORY, FLEX, FLEX_POINTS, _STATE, _PERSISTED
    now = time.time()
    # The renderer is long-lived, so round-tripping ~420 process counters and
    # 450 history samples through JSON every 2s was pure overhead. Disk is only
    # touched every PERSIST_EVERY seconds, to give a restart a warm start.
    prev = _STATE if _STATE is not None else load_json(STATE_FILE, {})
    elapsed = max(0.001, now - prev.get("t", now - 2))
    if not prev or elapsed > 60:
        elapsed = 2.0

    # ---- sample
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

    gpu_snap = gpu_engine_snapshot()
    gpu_prev = prev.get("gpu", {})
    gpu_pct = 0.0
    for k, v in gpu_snap.items():
        if k in gpu_prev:
            gpu_pct = max(gpu_pct, 100.0 * (v - gpu_prev[k]) / (elapsed * 1e9))
    gpu_pct = max(0.0, min(100.0, gpu_pct))

    iface = net_iface()
    rx, tx = net_bytes(iface) if iface else (0, 0)
    if prev.get("iface") != iface:
        down = up = 0.0          # counters restart on a different interface
    else:
        down = max(0.0, (rx - prev.get("rx", rx)) / elapsed)
        up = max(0.0, (tx - prev.get("tx", tx)) / elapsed)

    dr, dw = diskio_sectors()
    rd = max(0.0, (dr - prev.get("dr", dr)) / elapsed)
    wr = max(0.0, (dw - prev.get("dw", dw)) / elapsed)

    cap, bstatus, batt_w, eta = battery()
    watts = batt_w
    # One consistent basis for the whole chart when RAPL is readable: the
    # battery figure drops to zero on mains, which is a measurement gap, not
    # an idle machine.
    rapl_w, rapl_uj, rapl_name = rapl_watts(prev.get("rapl_uj"), elapsed)
    power_src = rapl_name if rapl_w is not None else "battery"
    # Power into the pack, kept apart from consumption. Only separable when
    # RAPL measures consumption independently — psys does not include the
    # charge current, verified against the battery's own reading. Without RAPL
    # `watts` IS the charge current while charging, so splitting it would
    # count the same energy twice.
    charge_w = 0.0
    if rapl_w is not None:
        if bstatus == "Charging":
            charge_w = batt_w
        watts = rapl_w
    # everything the wall supplies: the machine, plus whatever tops up the pack
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

    # ---- history
    if not HISTORY:
        HISTORY = load_json(HISTORY_FILE, [])
        if not isinstance(HISTORY, list):
            HISTORY = []
    # samples recorded before "chg" existed would otherwise be skipped by the
    # colour series, which carries the next known value backwards and paints
    # historic discharge as charging. Unknown means "on battery".
    for sample in HISTORY:
        sample.setdefault("chg", CHG_OUT)
    # Rounded on the way in. Fifteen decimal places of a CPU percentage are
    # noise no chart can draw, and at an hour of samples it is the difference
    # between a 344KB file and a 208KB one, rewritten every 30 seconds.
    HISTORY.append({"t": round(now, 1), "cpu": round(cpu_pct, 2),
                    "gpu": round(gpu_pct, 2), "down": round(down),
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

    # ---- render
    FLEX = float(prev.get("flex", 0.0))
    FLEX_POINTS = 0
    img = Image.new("RGBA", (W * SS, 1400 * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 22
    R = W - PAD  # right edge for right-aligned text

    f_val    = F(MONO_MED, T_VALUE)
    f_val_sm = F(MONO_REG, T_BODY)
    f_big    = F(MONO_LIGHT, T_LEAD)

    # ============ TOP SLOT: CLAUDE / WEATHER ======================
    # Claude quota when there is a subscription to report on; weather when
    # there is not (no CLI, no Pro/Max, a lapsed login). When BOTH are
    # available the slot alternates between them every ALT_PERIOD seconds —
    # no hover, because the panel takes no mouse events by design; the switch
    # is on a wall-clock timer, so it just cycles on its own.
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

    # Fixed height for the top slot so the panel does not resize as it swaps
    # between Claude and weather (they differ in natural height, which otherwise
    # jogged everything below by a frame on every switch). Both render inside
    # SLOT_H and y is snapped to it.
    SLOT_H = 95
    slot_start = y
    if slot == "claude":
        # heavier title and larger figures than the sections below it
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
                 f"resets {resets}", F(UI_MED, T_BODY), DIM, anchor="r")
            y += 19
            bar(img, PAD, y, CW, 8, pct / 100, ramp=True)
            y += 17
    elif slot == "weather":
        label(d, PAD, y, "weather", ACCENT, tracking=2.4)
        if weather.get("name"):
            label_r(d, R, y, weather["name"], DIM, size=T_MICRO)
        t = weather["temp"]
        icon_cy = slot_start + 50
        draw_weather_icon(img, PAD + 17, icon_cy, 14, weather.get("code", 3))
        tx = PAD + 44
        ttxt = f"{t}°"
        # temperatures all in plain white; the condition sits large next to it
        text(d, tx, icon_cy - 16, ttxt, f_big, TEXT)
        text(d, tx + measure(f_big, ttxt) / SS + 13, icon_cy - 13,
             weather["desc"], F(UI_MED, T_LEAD), DIM)
        detail = f"H {weather['hi']}°    L {weather['lo']}°    feels {weather['feels']}°"
        text(d, PAD, slot_start + 74, detail, F(UI_MED, T_BODY), TEXT)

    if slot:
        y = slot_start + SLOT_H
        y += gap(30)

    # ============ HEADER =========================================
    label(d, PAD, y, "uptime", ACCENT)
    label_r(d, R, y, "load 1·5·15m", ACCENT)
    y += 14
    text(d, PAD, y, fmt_dur(uptime), f_big, TEXT)
    # each average coloured by its own value, so a red-amber-grey run reads as
    # "spiking now, was fine a quarter of an hour ago" at a glance
    f_load = F(MONO_REG, T_VALUE)
    lx = R
    for v in reversed(load):
        text(d, lx, y + 6, v, f_load, load_color(float(v or 0), len(core_loads) or 1),
             anchor="r")
        lx -= measure(f_load, v) / SS + 9
    y += gap(40)

    # ============ GAUGES (hero row) ==============================
    # Three donuts carry the "how loaded is this machine" answer on their own,
    # so the sections below can stay quiet and detailed.
    gr, gth = 46, 9
    gauges = (
        ("cpu", cpu_pct, f"{cpu_pct:.0f}", "%", ACCENT),
        ("gpu", gpu_pct, f"{gpu_pct:.0f}", "%", VIOLET),
        ("ram", mem_used / mem_total * 100, f"{mem_used / mem_total * 100:.0f}", "%", TEAL),
    )
    slot = CW / 3
    for i, (name, pct, big, unit, hue) in enumerate(gauges):
        cx = PAD + slot * (i + 0.5)
        cy = y + gr + 4
        col = state_color(pct, hue)
        gauge(img, cx, cy, gr, gth, pct / 100, col)
        # number and unit set as one centred group, so "%" hangs off the value
        # instead of floating in the gap at the bottom of the ring
        f_g, f_u = F(MONO_LIGHT, T_HERO), F(MONO_REG, T_BODY)
        bw, uw = measure(f_g, big) / SS, measure(f_u, unit) / SS
        x0 = cx - (bw + 2 + uw) / 2
        text(d, x0, cy - 18, big, f_g, TEXT)
        text(d, x0 + bw + 3, cy - 6, unit, f_u, DIM)
        # centring a letterspaced label means measuring it with the tracking in
        lw = measure(F(UI_SEMI, T_LABEL), name.upper(), 1.8) / SS
        label(d, cx - lw / 2, cy + gr + 10, name, hue, tracking=1.8)
    y += 2 * gr + gap(32)

    # ============ PER-CORE =======================================
    label(d, PAD, y, "cores", ACCENT)
    rx_ = R
    if cpu_t:
        t = f"{cpu_t:.0f}°C"
        text(d, rx_, y - 1, t, F(MONO_REG, T_BODY), temp_color(cpu_t, 80, 95), anchor="r")
        rx_ -= measure(F(MONO_REG, T_BODY), t) / SS + 12
    clock = f"{cpu_freq_ghz():.2f} GHz"
    text(d, rx_, y - 1, clock, F(MONO_REG, T_BODY), DIM, anchor="r")
    rx_ -= measure(F(MONO_REG, T_BODY), clock) / SS + 12
    label_r(d, rx_, y, f"{len(core_loads)} threads", DIM)
    y += 15
    core_strip(img, PAD, y, CW, 24, core_loads)
    y += 24 + gap(26)

    # ============ CPU / GPU HISTORY ==============================
    label(d, PAD, y, "history", ACCENT)
    label_r(d, R, y, "60 min", DIM)
    y += 15
    histogram(img, PAD, y, CW, 64, "cpu", ACCENT, floor=10, clamp=100,
              overlay="gpu", overlay_color=VIOLET)
    y += 64 + 7
    lx = PAD
    for col, txt in ((ACCENT, "cpu"), (VIOLET, "gpu")):
        swatch(d, lx, y, col)
        # label in the series colour too, so the pairing survives even where
        # the swatch is small
        text(d, lx + 12, y, txt, F(UI_MED, T_LABEL), col)
        lx += 12 + measure(F(UI_MED, T_LABEL), txt) / SS + 18
    y += gap(24)

    # ============ MEMORY =========================================
    mfrac = mem_used / mem_total
    label(d, PAD, y, "memory", TEAL)
    text(d, R, y - 2, f"{fmt_bytes(mem_used)} / {fmt_bytes(mem_total)}", f_val, TEXT, anchor="r")
    y += 16
    bar(img, PAD, y, CW, 6, mfrac, state_color(mfrac * 100, TEAL))
    y += 16

    if swap_total:
        sfrac = swap_used / swap_total
        label(d, PAD, y, "swap", TEAL, size=T_MICRO)
        text(d, R, y - 2, f"{fmt_bytes(swap_used)} / {fmt_bytes(swap_total)}", f_val_sm, DIM, anchor="r")
        y += 14
        bar(img, PAD, y, CW, 4, sfrac, state_color(sfrac * 100, TEAL))
        y += 8
    y += gap(22)

    # ============ DISK ===========================================
    dfrac = disk_used / disk_total
    label(d, PAD, y, "disk", PINK)
    if nvme_t:
        # sits where the mount point used to; the panel only ever shows /
        text(d, PAD + measure(F(UI_SEMI, T_LABEL), "DISK", 1.6) / SS + 11, y - 1,
             f"{nvme_t:.0f}°C", F(MONO_REG, T_BODY), temp_color(nvme_t, 65, 75))
    text(d, R, y - 2, f"{fmt_bytes(disk_used)} / {fmt_bytes(disk_total)}", f_val, TEXT, anchor="r")
    y += 16
    bar(img, PAD, y, CW, 6, dfrac, state_color(dfrac * 100, PINK))
    y += 15
    text(d, PAD, y, "read", F(UI_MED, T_BODY), DIM)
    text(d, PAD + 34, y, fmt_bytes(rd, True), f_val_sm, DIM)
    text(d, R, y, fmt_bytes(wr, True), f_val_sm, DIM, anchor="r")
    text(d, R - measure(f_val_sm, fmt_bytes(wr, True)) / SS - 9, y, "write", F(UI_MED, T_BODY), DIM, anchor="r")
    y += gap(30)

    # ============ NETWORK ========================================
    label(d, PAD, y, "network", ACCENT)
    if wifi_t:
        # radio temperature, placed like the disk one. The interface itself is
        # detected from the default route and no longer spelled out here.
        text(d, PAD + measure(F(UI_SEMI, T_LABEL), "NETWORK", 1.6) / SS + 11, y - 1,
             f"{wifi_t:.0f}°C", F(MONO_REG, T_BODY), temp_color(wifi_t, 75, 85))
    y += 16
    peak = net_chart(img, PAD, y, CW, 60, ACCENT, CORAL, floor=64 * 1024)
    y += 60 + 6
    text(d, PAD, y, f"↓ {fmt_bytes(down, True)}", f_val_sm, ACCENT)
    text(d, W / 2, y, f"peak {fmt_bytes(peak, True)}", F(MONO_REG, T_LABEL), DIM, anchor="c")
    text(d, R, y, f"↑ {fmt_bytes(up, True)}", f_val_sm, CORAL, anchor="r")
    y += gap(30)

    # ============ POWER ==========================================
    charging = bstatus == "Charging"
    label(d, PAD, y, "power", AMBER)

    # While charging the wall feeds two things, so show them as two figures
    # rather than one sum: what the machine draws, and what the wall delivers
    # in total, the difference being whatever goes into the pack.
    ux = R
    if ac_w > 0.05:
        atxt = f"{ac_w:.1f} W"
        # the total, so neutral: it is the sum of the amber consumption and the
        # green charge, and painting it amber made the two figures blur together
        text(d, ux, y - 2, atxt, f_val, TEXT, anchor="r")
        ux -= measure(f_val, atxt) / SS + 6
        label_r(d, ux, y, "ac", DIM, size=T_MICRO)
        ux -= measure(F(UI_SEMI, T_MICRO), "AC", 1.6) / SS + 14
    dtxt = f"{watts:.1f} W"
    text(d, ux, y - 2, dtxt, f_val, consumption_color(1.0 if bstatus != "Discharging" else 0.0)
         if watts > 0.05 else MUTE, anchor="r")
    ux -= measure(f_val, dtxt) / SS + 6
    label_r(d, ux, y, "device", DIM, size=T_MICRO)
    ux -= measure(F(UI_SEMI, T_MICRO), "DEVICE", 1.6) / SS + 14
    # the battery fallback reads 0W on mains, which is a measurement gap rather
    # than an idle machine, and that is worth saying on the panel
    if power_src == "battery":
        label_r(d, ux, y, "battery", DIM, size=T_MICRO)
    y += 16
    power_chart(img, PAD, y, CW, 30, floor=8)
    y += 30

    y += 7
    lx = PAD
    for col, txt, is_line in ((AMBER, "on ac", False), (DARKRED, "on battery", False),
                              (GREEN, "charging", False), (TEXT, "total", True)):
        swatch(d, lx, y, col, line=is_line)
        text(d, lx + (15 if is_line else 12), y, txt, F(UI_MED, T_LABEL), col)
        lx += (15 if is_line else 12) + measure(F(UI_MED, T_LABEL), txt) / SS + 16
    y += 12
    y += gap(20)

    # Charging outranks the level: a battery at 20% that is plugged in is not a
    # problem, so it gets the "gaining" colour rather than a red warning. On
    # battery the fill tracks what is left, green through amber to red.
    # Two different things, so two colours. bcol is about the LEVEL and drives
    # the bar and the percentage; scol is about the DIRECTION and drives the
    # status word and the wattage. Sharing one made "discharging" render green
    # whenever the pack happened to be near full.
    full = bstatus == "Full" or cap >= 100
    if charging or full:
        bcol = GREEN          # gaining, or topped up — a good state, so green
    elif bstatus == "Discharging":
        bcol = ramp_rgb(1 - cap / 100)
    else:
        bcol = STEEL          # on AC, holding below full (e.g. a charge limit)
    scol = GREEN if (charging or full) else (DARKRED if bstatus == "Discharging" else DIM)
    label(d, PAD, y, "battery", AMBER)
    bx = R
    if eta:
        text(d, bx, y - 1, fmt_dur(eta), F(MONO_REG, T_BODY), DIM, anchor="r")
        bx -= measure(F(MONO_REG, T_BODY), fmt_dur(eta)) / SS + 12
    # power crossing the pack's own terminals, which belongs here rather than
    # in the POWER row: that one is about the machine and the wall.
    if batt_w > 0.05 and bstatus in ("Charging", "Discharging"):
        wtxt = f"{batt_w:.1f} W"
        text(d, bx, y - 1, wtxt, F(MONO_REG, T_BODY),
             scol, anchor="r")
        bx -= measure(F(MONO_REG, T_BODY), wtxt) / SS + 12
    text(d, bx, y - 1, bstatus.lower(), F(UI_MED, T_BODY), scol, anchor="r")
    bx -= measure(F(UI_MED, T_BODY), bstatus.lower()) / SS + 12
    text(d, bx, y - 2, f"{cap}%", f_val, bcol, anchor="r")
    y += 16
    bar(img, PAD, y, CW, 6, cap / 100, bcol)
    y += 18

    y += gap(10)

    # ============ TOP PROCESSES ==================================
    # A usage bar behind each row turns two flat lists into something you can
    # read the shape of without parsing the numbers. The leader of each list
    # gets the bright name and a heavier figure, so the ranking reads at a
    # glance without comparing values.
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
            label_r(d, R, y, right, DIM, size=T_MICRO)
        y += 18
        peak = total if total else max([value_of(r) for r in rows] + [1e-9])
        for i, r in enumerate(rows):
            col = colour_of(r)
            row_bar(img, PAD, y - 4, CW, 20,
                    max(0.0, min(1.0, value_of(r) / peak)) ** curve, col)
            text(d, PAD + 7, y, r[0][:22], F(UI_MED, T_BODY), TEXT if i == 0 else DIM)
            text(d, R - 7, y, fmt_of(r),
                 F(MONO_MED, T_BODY) if i == 0 else F(MONO_REG, T_BODY), col, anchor="r")
            y += 22
        return y

    y = proc_list(y, "top cpu", ACCENT, top_cpu,
                  value_of=lambda r: r[1],
                  fmt_of=lambda r: f"{r[1]:.1f}%",
                  # No state escalation here: these are percentages of ONE
                  # core, and the warn/crit thresholds were written for
                  # system-wide load. A video decoder pegging a single core out
                  # of twenty is routine, and painting that row red is a false
                  # alarm. The bar length already carries the ranking.
                  colour_of=lambda r: ACCENT if r[1] > 1 else MUTE,
                  # The bar measures against the WHOLE machine while the
                  # figure beside it stays per-core. Against one core, every
                  # multi-threaded process pinned the bar at full and 195%
                  # looked identical to 150%. Same split as TOP MEMORY, where
                  # the figure is absolute and the bar is a share of RAM:
                  # the number says how hard one process is working, the bar
                  # says how much of the machine that costs.
                  right="% of one core", total=100.0 * max(1, len(core_loads)),
                  curve=0.5)
    y += gap(14)

    y = proc_list(y, "top memory", TEAL, top_mem,
                  value_of=lambda r: r[2],
                  fmt_of=lambda r: fmt_bytes(r[2]),
                  colour_of=lambda r: TEAL,
                  # same curve as TOP CPU: the two lists sit one above the
                  # other and invite comparison, so they have to share a scale
                  right="share of ram", total=mem_total, curve=0.5)

    y += 22
    H = int(round(y))

    # Height without any flex, i.e. what the content genuinely needs. Spreading
    # (TARGET_H - natural) over the section boundaries lands the next frame on
    # TARGET_H exactly, whatever the content is doing.
    natural = y - FLEX_POINTS * FLEX
    next_flex = 0.0
    if FLEX_POINTS:
        next_flex = max(0.0, min(FLEX_MAX, (TARGET_H - natural) / FLEX_POINTS))

    # ---- glass panel behind everything ----------------------------
    panel = panel_bg(H)

    out = Image.alpha_composite(panel, img.crop((0, 0, W * SS, H * SS)))
    # Exactly 2:1, so BOX is a true 2x2 average — textbook supersampling, and
    # ~19ms/frame cheaper than LANCZOS, which at this ratio only adds ringing.
    out = out.resize((W, H), Image.BOX)

    if write_png:
        # Only for screenshots and debugging now. Encoding the panel to PNG
        # costs 21ms a frame against 4ms to hand the pixels straight to cairo,
        # so the window path does not go through a file at all.
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
    return surf, buf          # the buffer must outlive the surface


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
    # below everything, on every workspace, invisible to the taskbar and the
    # window switcher, and never taking focus
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
        """Right edge and top at MARGIN, measured off the work area so the
        taskbar is respected."""
        mon = (Gdk.Display.get_default().get_primary_monitor()
               or Gdk.Display.get_default().get_monitor(0))
        wa = mon.get_workarea()
        # set_size_request, not resize: the window is set non-resizable, and GTK
        # then sizes it from the content request and ignores resize() outright
        win.set_size_request(W, h)
        win.move(wa.x + wa.width - W - MARGIN, wa.y + MARGIN)

    def on_draw(_w, cr):
        if state["surface"] is not None:
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_surface(state["surface"], 0, 0)
            cr.paint()
        return False

    def on_realize(_w):
        # clicks fall through to the desktop underneath; the panel has nothing
        # to click and should not swallow a drag on the wallpaper
        win.get_window().input_shape_combine_region(cairo.Region(), 0, 0)

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
            if img.height != state["h"]:
                state["h"] = img.height
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

    win.connect("draw", on_draw)
    win.connect("realize", on_realize)
    win.connect("destroy", Gtk.main_quit)
    tick()
    place(state["h"] or 900)
    win.show_all()
    GLib.timeout_add(int(interval * 1000), tick)
    log(f"panel started (pid {os.getpid()})")
    Gtk.main()


if __name__ == "__main__":
    if "--png" in sys.argv:
        # one frame to cache/hud.png, for screenshots and for checking a change
        # without disturbing the running panel
        render()
        for _t in threading.enumerate():
            if _t is not threading.main_thread():
                _t.join(timeout=15)
    else:
        # flock so a second copy exits instead of stacking a duplicate panel on
        # the desktop. "a+" and not "w": opening for write TRUNCATES, and the
        # losing copy would do that before it ever reaches the flock.
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
