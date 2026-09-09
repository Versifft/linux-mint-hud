#!/usr/bin/env python3
"""Renders one frame and writes it to docs/ as a standalone panel image.

The panel is translucent dark glass, so it cannot simply be saved with its
alpha channel: GitHub renders a README image on white in light mode, and light
text on white is unreadable. It goes onto a flat dark ground instead — neutral,
and no wallpaper, so a screenshot shows the app and nothing personal.
"""
import subprocess
import sys
from PIL import Image

GROUND = (24, 26, 30)
PAD = 26

name = sys.argv[1] if len(sys.argv) > 1 else "panel"
subprocess.run(["./hud.py", "--png"], check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
hud = Image.open("cache/hud.png").convert("RGBA")
out = Image.new("RGBA", (hud.width + 2 * PAD, hud.height + 2 * PAD), (*GROUND, 255))
out.alpha_composite(hud, (PAD, PAD))
path = f"docs/{name}.png"
out.convert("RGB").save(path, optimize=True)
print(f"{path}  {out.width}x{out.height}")
