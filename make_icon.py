#!/usr/bin/env python3
"""
Icon generator for Linux Mint HUD -- Settings menu icon.

Concept
-------
A crafted settings COG that is also one of the app's ring gauges. An angular
meter sweeps around the gear with exactly hud.py's gauge geometry (Pillow
start=135 deg, sweep=270 deg): it begins at ~8 o'clock, runs clockwise up the
left, over the top and down the right, and is open at the bottom.

It is a *rising* meter, filled to ~80% of that sweep: the filled part carries
the gauge gradient -- violet at the 8 o'clock start, blue over the top,
brightening to cyan with a hot glowing leading edge on the right (the hero
highlight). The remaining track and the bottom gap are a dim slate -- the
"empty" part of the meter, kept visible so the whole cog silhouette still reads
on dark menus.

The centre is a clean, solid dark hole (no glow, no dot), ringed by the gear
body -- like the app's ring gauges. A thin dark rim gives definition on any
background, and the gear stands on its own (no tile).

Palette from hud.py: VIOLET #b478ff, ACCENT/brand blue, TEAL/cyan #30e4ec.
Pure Pillow. One high-res master (4x supersample) LANCZOS-downsampled to size.
"""

import math
import os
from PIL import Image, ImageDraw, ImageFilter

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
SIZES   = [16, 24, 32, 48, 64, 128, 256]

MASTER = 1024
SS     = 4
N      = MASTER * SS

# gauge geometry (identical to hud.py gauge())
START = 135.0
SWEEP = 270.0
FRAC  = 1.0                  # fill the whole arc; only the bottom gap stays dim

# vivid cool->hot ramp along the filled sweep: violet -> blue -> cyan -> amber
# -> orange -> red (a gauge running cool to hot, ending red before the gap)
VIOLET  = (150, 58, 252)
BLUE    = (36, 120, 255)
CYAN    = (0, 224, 238)
AMBER   = (255, 190, 48)
ORANGE  = (255, 104, 40)
RED     = (246, 36, 58)
DIMTRK  = (96, 103, 132)    # unfilled meter track (unused at full fill)
DIMGAP  = (78, 84, 112)     # bottom gap wedge (kept visible on dark menus)
RIM     = (20, 24, 44)      # deep indigo edge for definition on light menus
HOLECOL = (11, 13, 20)      # solid dark centre hole

_STOPS = [(0.00, VIOLET), (0.20, BLUE), (0.42, CYAN),
          (0.66, AMBER), (0.84, ORANGE), (1.00, RED)]

TEETH   = 8


def px(v):
    return v * SS


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def grad3(t):
    """position 0..1 along the filled sweep -> colour on the cool->hot ramp"""
    t = max(0.0, min(1.0, t))
    for i in range(len(_STOPS) - 1):
        t0, c0 = _STOPS[i]
        t1, c1 = _STOPS[i + 1]
        if t <= t1:
            return lerp(c0, c1, (t - t0) / (t1 - t0))
    return _STOPS[-1][1]


# ----------------------------------------------------------------------------
# gear silhouette -- smoothed radial tooth profile
# ----------------------------------------------------------------------------
def _profile(p, r_tip, r_root):
    """radius for phase p in [0,1) within one tooth period: a trapezoid tooth
    (flat top on r_tip, straight flanks, flat valley on r_root). Only the four
    junction corners get rounded afterwards (light smoothing), so the flat
    tops/valleys survive and it reads as a crisp cog, not a flower."""
    V, FU, TT, FD = 0.13, 0.35, 0.65, 0.87   # valley/flank-up/tooth/flank-down
    if p < V:
        return r_root
    if p < FU:
        return r_root + (r_tip - r_root) * (p - V) / (FU - V)
    if p < TT:
        return r_tip
    if p < FD:
        return r_tip - (r_tip - r_root) * (p - TT) / (FD - TT)
    return r_root


def _smooth(a, w, passes=2):
    n = len(a)
    for _ in range(passes):
        b = [0.0] * n
        for i in range(n):
            s = 0.0
            for k in range(-w, w + 1):
                s += a[(i + k) % n]
            b[i] = s / (2 * w + 1)
        a = b
    return a


def gear_outline(cx, cy, r_tip, r_root, teeth=TEETH):
    K = teeth * 240
    raw = [_profile(((i / K) * teeth + 0.5) % 1.0, r_tip, r_root)
           for i in range(K)]
    sm = _smooth(raw, max(2, int(K * 0.006)), passes=2)   # light corner fillet
    pts = []
    for i in range(K):
        ang = math.radians(-90.0 + 360.0 * i / K)   # a tooth centred at top
        pts.append((cx + sm[i] * math.cos(ang), cy + sm[i] * math.sin(ang)))
    return pts


def gear_mask(size, r_tip, r_root, hole_r):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    c = size / 2.0
    d.polygon(gear_outline(c, c, r_tip, r_root), fill=255)
    if hole_r > 0:
        d.ellipse([c - hole_r, c - hole_r, c + hole_r, c + hole_r], fill=0)
    return m


# ----------------------------------------------------------------------------
# conic gauge sweep (built at moderate res, upscaled)
# ----------------------------------------------------------------------------
def conic_sweep(size):
    col = Image.new("RGB", (size, size), DIMGAP)
    fill = Image.new("L", (size, size), 0)
    cp = col.load()
    fp = fill.load()
    c = (size - 1) / 2.0
    for y in range(size):
        dy = y - c
        for x in range(size):
            dx = x - c
            a = math.degrees(math.atan2(dy, dx)) % 360.0
            delta = (a - START) % 360.0
            if delta <= SWEEP:
                t = delta / SWEEP
                if t <= FRAC:
                    cp[x, y] = grad3(t / FRAC)
                    fp[x, y] = 255
                else:
                    cp[x, y] = DIMTRK
    return col, fill


# ----------------------------------------------------------------------------
# compose
# ----------------------------------------------------------------------------
def render():
    R_TIP  = px(488)
    R_ROOT = px(384)
    HOLE   = px(150)
    RIMW   = px(4)
    c      = N / 2.0

    canvas = Image.new("RGBA", (N, N), (0, 0, 0, 0))

    body_mask = gear_mask(N, R_TIP, R_ROOT, HOLE)
    rim_mask  = gear_mask(N, R_TIP + RIMW, R_ROOT + RIMW, HOLE - RIMW)

    M = 820
    col_s, _ = conic_sweep(M)
    col = col_s.resize((N, N), Image.BICUBIC)

    # ---- dark rim (definition on any background) ----
    rim = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    rim.paste((*RIM, 255), (0, 0), rim_mask)
    canvas.alpha_composite(rim)

    # ---- gear body coloured by the conic sweep ----
    body = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    body.paste(col, (0, 0), body_mask)
    canvas.alpha_composite(body)

    # ---- solid dark centre hole (no glow, no dot) ----
    # soft recess shadow just inside the body, then the flat dark hole
    shadow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        [c - HOLE - px(14), c - HOLE - px(14),
         c + HOLE + px(14), c + HOLE + px(14)], fill=(0, 0, 0, 130))
    shadow = shadow.filter(ImageFilter.GaussianBlur(px(10)))
    shadow = Image.composite(shadow, Image.new("RGBA", (N, N), (0, 0, 0, 0)),
                             body_mask)
    canvas.alpha_composite(shadow)

    hole = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(hole).ellipse(
        [c - HOLE, c - HOLE, c + HOLE, c + HOLE], fill=(*HOLECOL, 255))
    canvas.alpha_composite(hole)

    # ---- top sheen: soft light across the upper body ----
    sheen = Image.new("L", (N, N), 0)
    ImageDraw.Draw(sheen).ellipse([px(120), px(-260), px(904), px(470)], fill=34)
    sheen = sheen.filter(ImageFilter.GaussianBlur(px(70)))
    sheen_l = Image.composite(sheen, Image.new("L", (N, N), 0), body_mask)
    white = Image.new("RGBA", (N, N), (255, 255, 255, 0))
    white.putalpha(sheen_l)
    canvas.alpha_composite(white)

    return canvas


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    master = render()
    for s in SIZES:
        master.resize((s, s), Image.LANCZOS).save(
            os.path.join(OUT_DIR, f"mint-hud-{s}.png"))
    master.resize((256, 256), Image.LANCZOS).save(
        os.path.join(OUT_DIR, "mint-hud.png"))
    print("wrote:", ", ".join(f"mint-hud-{s}.png" for s in SIZES), "+ mint-hud.png")


if __name__ == "__main__":
    main()
