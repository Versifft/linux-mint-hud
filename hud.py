#!/usr/bin/env python3

import fcntl
import glob
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import io
import threading
import datetime
import time
import traceback
import types
import urllib.parse
import urllib.request

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HOME = os.path.expanduser("~")
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

__version__ = "2.0.1"        # keep in sync with build-deb.sh VERSION
RELEASES_URL = "https://github.com/Versifft/linux-mint-hud/releases/latest"

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

SECTION_DEFS = [
    ("top", "Weather / Claude usage"),
    ("claude", "Claude usage only"),
    ("weather", "Weather only"),
    ("clock", "Clock & date"),
    ("load", "Uptime & load average"),
    ("gauges", "CPU / GPU / RAM gauges"),
    ("cores", "Per-core CPU bars"),
    ("history", "CPU/GPU/RAM history"),
    ("thermals", "Temperatures"),
    ("memory", "Memory & swap"),
    ("disk", "Disk"),
    ("network", "Network"),
    ("power", "Power"),
    ("battery", "Battery"),
    ("devices", "Device batteries"),
    ("spotify", "Spotify / media"),
    ("processes", "Top processes"),
    ("modules", "Custom modules"),
]
SECTION_ORDER = [k for k, _ in SECTION_DEFS]
SECTION_LABELS = dict(SECTION_DEFS)
_DEFAULT_OFF = {"claude", "weather", "spotify", "clock", "modules"}

def price_command(coin, code, sep):
    """Shell command for a CoinGecko crypto price. `code` is the currency code
    (eur/usd/chf/…), shown upper-case after the value. `sep` groups thousands in
    the integer part ("'", ",", "." or "" for none).

    Decimal-aware: a $64,999 coin shows whole units, a $0.058 coin keeps its
    decimals. The value is pulled from the "<code>": price field specifically
    (not any digit in the JSON), so a rate-limit error object yields nothing
    rather than a bogus price. awk runs under LC_ALL=C so a comma-decimal locale
    doesn't mangle the dotted number."""
    if sep == "'":
        separg = "-v s=\\'"
    elif sep in (",", "."):
        separg = f"-v s={sep}"
    else:
        separg = "-v s=''"
    prog = ('{v=$1+0; f=(v>=1000)?"%.0f":(v>=1)?"%.2f":(v>=0.01)?"%.4f":"%.6f"; '
            'n=sprintf(f,v); p=index(n,"."); ip=p?substr(n,1,p-1):n; dp=p?substr(n,p):""; '
            'o=""; L=length(ip); for(i=1;i<=L;i++){o=o substr(ip,i,1); r=L-i; '
            'if(r>0&&r%3==0&&s!="")o=o s} print o dp" "toupper(c)}')
    return ("curl -s --max-time 5 'https://api.coingecko.com/api/v3/simple/"
            f"price?ids={coin}&vs_currencies={code}' | grep -oE '\"{code}\":[0-9.]+' | "
            f"grep -oE '[0-9.]+' | tail -1 | LC_ALL=C awk {separg} -v c={code} '{prog}'")

def _stock_command(symbol):
    """Shell command for a quote from Yahoo Finance's public chart endpoint
    (no key): stocks, indices (^GSPC → %5EGSPC), FX pairs (EURUSD=X) and futures
    (GC=F). Pulls regularMarketPrice; empty on error. A browser User-Agent is
    required — Yahoo's edge answers the default curl agent with HTTP 429."""
    return ("curl -s --max-time 8 -A 'Mozilla/5.0 (X11; Linux x86_64)' "
            "'https://query1.finance.yahoo.com/v8/finance/"
            f"chart/{symbol}?interval=1d&range=1d' | "
            "grep -oE '\"regularMarketPrice\":[0-9.]+' | head -1 | grep -oE '[0-9.]+'")

# A catalog of ready-made custom modules so a newcomer can pick finished ones
# instead of writing shell commands. (category, [(label, command, type, interval,
# note)]). Commands are defensive (timeouts, 2>/dev/null) and degrade to empty
# output rather than erroring when a tool or device is missing.
# Modules the panel already shows natively (CPU/GPU/RAM, cores, history,
# thermals, memory/swap, disk, network incl. SSID/local-IP/VPN, power, battery,
# devices, media/Spotify, top processes, weather, uptime & load) are deliberately
# NOT duplicated here — the catalog is for things the built-in sections don't do.
MODULE_CATALOG = [
    ("System", [
        ("Processes", "ps -e --no-headers | wc -l", "number", 15,
         "Number of running processes"),
        ("Logged-in users", "who | wc -l", "number", 60, "Active login sessions"),
        ("Failed services", "systemctl --failed --no-legend 2>/dev/null | wc -l",
         "number", 120, "systemd units that failed to start"),
        ("Updates pending", "apt-get -s upgrade 2>/dev/null | grep -c '^Inst'",
         "number", 1800, "Upgradable apt packages"),
        ("Reboot needed", "[ -f /var/run/reboot-required ] && echo 'Reboot needed' "
         "|| echo 'Up to date'", "text", 600, "Whether an update wants a reboot"),
        ("CPU governor", "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor "
         "2>/dev/null", "text", 30, "Current CPU frequency-scaling mode"),
        ("Kernel", "uname -r", "text", 3600, "Running kernel version"),
        ("Trash items", "ls \"$HOME/.local/share/Trash/files\" 2>/dev/null | wc -l",
         "number", 120, "Files sitting in the trash"),
        ("Downloads", "ls -1 \"$HOME/Downloads\" 2>/dev/null | wc -l", "number", 120,
         "Files in your Downloads folder"),
    ]),
    ("Network", [
        ("Public IP", "curl -s --max-time 4 ifconfig.me", "text", 600,
         "Your external IP address — needs curl"),
        ("IP country", "curl -s --max-time 5 ipinfo.io/country", "text", 1800,
         "Country your connection exits from — needs curl"),
        ("Ping internet", "ping -c1 -W1 1.1.1.1 | sed -n 's/.*time=\\([0-9.]*\\).*/\\1/p'",
         "number", 20, "Latency to the internet in ms"),
        ("Ping router", "ping -c1 -W1 \"$(ip route|awk '/default/{print $3;exit}')\" "
         "| sed -n 's/.*time=\\([0-9.]*\\).*/\\1/p'", "number", 20, "Latency to your router in ms"),
        ("Connections", "ss -tun state established 2>/dev/null | tail -n +2 | wc -l",
         "number", 30, "Open network connections"),
    ]),
    ("Hardware & power", [
        ("Brightness %", "brightnessctl -m 2>/dev/null | cut -d, -f4 | tr -dc 0-9", "bar", 30,
         "Screen backlight — needs brightnessctl"),
        ("Fan RPM", "cat /sys/class/hwmon/hwmon*/fan1_input 2>/dev/null | head -1", "number", 15,
         "Speed of the first fan"),
        ("Battery health %", "B=/sys/class/power_supply/BAT0; "
         "f=$(cat $B/charge_full 2>/dev/null || cat $B/energy_full 2>/dev/null); "
         "d=$(cat $B/charge_full_design 2>/dev/null || cat $B/energy_full_design 2>/dev/null); "
         "[ -n \"$f\" ] && [ -n \"$d\" ] && [ \"$d\" -gt 0 ] && echo $((100*f/d))", "bar", 1800,
         "Battery capacity vs. when new (wear)"),
    ]),
    ("Audio", [
        ("Volume %", "pamixer --get-volume 2>/dev/null", "bar", 5,
         "Output volume — needs pamixer"),
        ("Muted?", "pamixer --get-mute 2>/dev/null | sed 's/true/muted/;s/false/on/'", "text", 5,
         "Whether output is muted — needs pamixer"),
        ("Mic muted?", "pamixer --default-source --get-mute 2>/dev/null "
         "| sed 's/true/mic off/;s/false/mic on/'", "text", 5,
         "Whether the microphone is muted — needs pamixer"),
    ]),
    ("Crypto", [
        ("Bitcoin", price_command("bitcoin", "eur", "'"), "text", 300, "BTC price — needs curl"),
        ("Ethereum", price_command("ethereum", "eur", "'"), "text", 300, "ETH price — needs curl"),
        ("Solana", price_command("solana", "eur", "'"), "text", 300, "SOL price — needs curl"),
        ("BNB", price_command("binancecoin", "eur", "'"), "text", 300, "BNB price — needs curl"),
        ("XRP", price_command("ripple", "eur", "'"), "text", 300, "XRP price — needs curl"),
        ("Cardano", price_command("cardano", "eur", "'"), "text", 300, "ADA price — needs curl"),
        ("Dogecoin", price_command("dogecoin", "eur", "'"), "text", 300, "DOGE price — needs curl"),
        ("Polkadot", price_command("polkadot", "eur", "'"), "text", 300, "DOT price — needs curl"),
        ("Chainlink", price_command("chainlink", "eur", "'"), "text", 300, "LINK price — needs curl"),
        ("Litecoin", price_command("litecoin", "eur", "'"), "text", 300, "LTC price — needs curl"),
        ("Avalanche", price_command("avalanche-2", "eur", "'"), "text", 300, "AVAX price — needs curl"),
        ("Monero", price_command("monero", "eur", "'"), "text", 300, "XMR price — needs curl"),
        ("Fear & Greed", "curl -s --max-time 6 https://api.alternative.me/fng/ "
         "| grep -oE '\"value\": *\"[0-9]+\"' | head -1 | grep -oE '[0-9]+'", "bar", 3600,
         "Crypto market sentiment 0–100 — needs curl"),
    ]),
    ("Stocks", [
        ("Apple", _stock_command("AAPL"), "text", 900, "AAPL share price, USD — needs curl"),
        ("Microsoft", _stock_command("MSFT"), "text", 900, "MSFT share price, USD — needs curl"),
        ("NVIDIA", _stock_command("NVDA"), "text", 900, "NVDA share price, USD — needs curl"),
        ("Tesla", _stock_command("TSLA"), "text", 900, "TSLA share price, USD — needs curl"),
        ("Amazon", _stock_command("AMZN"), "text", 900, "AMZN share price, USD — needs curl"),
        ("Alphabet", _stock_command("GOOGL"), "text", 900, "GOOGL share price, USD — needs curl"),
        ("Meta", _stock_command("META"), "text", 900, "META share price, USD — needs curl"),
        ("AMD", _stock_command("AMD"), "text", 900, "AMD share price, USD — needs curl"),
    ]),
    ("Indices", [
        ("S&P 500", _stock_command("%5EGSPC"), "text", 900, "US S&P 500 index — needs curl"),
        ("Nasdaq", _stock_command("%5EIXIC"), "text", 900, "US Nasdaq Composite — needs curl"),
        ("Dow Jones", _stock_command("%5EDJI"), "text", 900, "US Dow Jones index — needs curl"),
        ("DAX", _stock_command("%5EGDAXI"), "text", 900, "German DAX index — needs curl"),
        ("FTSE 100", _stock_command("%5EFTSE"), "text", 900, "UK FTSE 100 index — needs curl"),
        ("Euro Stoxx 50", _stock_command("%5ESTOXX50E"), "text", 900, "Euro Stoxx 50 — needs curl"),
    ]),
    ("Forex & metals", [
        ("EUR / USD", _stock_command("EURUSD=X"), "text", 900, "Euro in US dollars — needs curl"),
        ("GBP / USD", _stock_command("GBPUSD=X"), "text", 900, "Pound in US dollars — needs curl"),
        ("USD / JPY", _stock_command("USDJPY=X"), "text", 900, "Dollar in yen — needs curl"),
        ("USD / CHF", _stock_command("USDCHF=X"), "text", 900, "Dollar in Swiss francs — needs curl"),
        ("Gold", _stock_command("GC=F"), "text", 900, "Gold, USD/oz — needs curl"),
        ("Silver", _stock_command("SI=F"), "text", 900, "Silver, USD/oz — needs curl"),
        ("Crude oil", _stock_command("CL=F"), "text", 900, "WTI crude, USD/bbl — needs curl"),
    ]),
    ("Homelab & desktop", [
        ("Docker running", "docker ps -q 2>/dev/null | wc -l", "number", 30,
         "Running containers — needs docker access"),
        ("Docker images", "docker images -q 2>/dev/null | wc -l", "number", 120,
         "Local docker images"),
        ("Kubernetes context", "kubectl config current-context 2>/dev/null", "text", 60,
         "Active kubectl context — needs kubectl"),
        ("Keyboard layout", "setxkbmap -query 2>/dev/null | awk '/layout/{print $2}'",
         "text", 10, "Current keyboard layout"),
        ("Bluetooth devices", "bluetoothctl devices Connected 2>/dev/null | wc -l",
         "number", 30, "Connected Bluetooth devices — needs bluetoothctl"),
        ("Week number", "date +%V", "number", 3600, "ISO week of the year"),
        ("Day of year", "date +%j", "number", 3600, "Which day of the year it is"),
    ]),
]

DEFAULT_SETTINGS = {
    "panels": None,
    "move": None,
    "monitor": 0,
    "position": "top-right",
    "margin": 22,
    "vmargin": 22,
    "hmargin": 22,
    "top": 22,
    "bottom": 22,
    "left": None,
    "right": 22,
    "location": None,
    "weather_units": "c",
    "weather_show_location": True,
    "sensor_names": {},
    "peripheral_names": {},
    "units": "c",
    "disks": None,
    "sensors": None,
    "peripherals": None,
    "sections": {k: (k not in _DEFAULT_OFF) for k in SECTION_ORDER},
    "order": None,
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

# Display server. The whole window layer (type hints, self-positioning, X11
# input-shape, the get_xid() stacking hack) is written for X11/Cinnamon. On a
# native-Wayland session those calls are either no-ops or raise, so the few
# X11-only paths are guarded behind `not IS_WAYLAND`. GTK still runs the panel
# over XWayland in that case; a native layer-shell path is a separate effort.
IS_WAYLAND = (os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
              or bool(os.environ.get("WAYLAND_DISPLAY")))
HIST_MAX_AGE = 3600
HIST_GAMMA = 3.3

T_HERO  = 29
T_LEAD  = 21
T_VALUE = 13
T_BODY  = 11.5
T_LABEL = 10
T_MICRO = 8.5

# Per-panel text-size multiplier applied to every font in F(); set from the
# panel's `text_scale` at the top of render(). 1.0 leaves the base sizes.
TEXT_SCALE = 1.0

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
FLEX_MAX = 400
FLEX_MIN = 0

# Seconds the top slot shows each of Claude / weather before swapping.
ALT_PERIOD = 8

# Fixed per-section column widths (logical px) for the HORIZONTAL bar, so a
# changing value (network rate, watts, %, a new top-process name, a rotated
# weather city…) never re-flows the whole strip. Generous enough for the widest
# normal content; anything longer is clipped on the right. Sections not listed
# fall back to content-width trimming.
_HBAR_WIDTH = {
    "clock": 146, "load": 228, "gauges": 224, "cores": 220, "history": 232,
    "thermals": 302, "memory": 200, "disk": 173, "network": 205, "power": 224,
    "battery": 193, "devices": 153, "spotify": 200, "processes": 104,
    "weather": 216, "claude": 220,
}

FLEX = 0.0
FLEX_POINTS = 0

def gap(base):
    """A section boundary that absorbs part of the leftover vertical space."""
    global FLEX_POINTS
    FLEX_POINTS += 1
    return base + FLEX

CHG_OUT   = 0.0
CHG_IDLE  = 0.5
CHG_IN    = 1.0
CHG_OUT_MAX = 0.25

_WHITE_RAMP = ((0.0, (72, 199, 116)), (0.50, (240, 185, 70)),
               (0.78, (246, 105, 64)), (1.0, (222, 42, 52)))

_BASE_PALETTE = dict(
    TEXT=(233, 238, 245), MUTE=(120, 132, 150), STEEL=(150, 165, 190),
    ACCENT=(96, 176, 255), VIOLET=(180, 120, 255), PINK=(255, 96, 180),
    TEAL=(48, 228, 236), CORAL=(224, 128, 93), AMBER=(240, 176, 80),
    GREEN=(72, 199, 116), DARKRED=(186, 66, 66), RED=(233, 84, 82),
    WARN=(255, 181, 84), CRIT=(255, 95, 109),
    TRACK=(255, 255, 255, 22), RING=(255, 255, 255, 20),
    HAIRLINE=(255, 255, 255, 24), RAMP=_WHITE_RAMP,
    BG=(20, 23, 30), BG2=(14, 16, 23), HILITE=(255, 255, 255),
    MODE="glass", OPACITY=1.0, RADIUS=18, BORDER=1.0, GLOW=1.0,
)

# Each theme carries its own CPU-accent hue family so the dark set is
# distinguishable at a glance (blue · ice-cyan · amber · jade · mono · frost ·
# cyan · violet · rose · copper · aqua · periwinkle · green), and STEEL sits a
# clear tier above the muted-text layer so section headers read as structure.
THEMES = {
    "midnight": dict(BG=(12, 16, 32), BG2=(6, 8, 20), MODE="solid",
                     STEEL=(140, 160, 200), RED=(240, 70, 60)),
    "obsidian": dict(BG=(14, 14, 16), BG2=(4, 4, 6), MODE="solid",
                     TEXT=(240, 232, 220), MUTE=(132, 122, 110), STEEL=(205, 178, 120),
                     ACCENT=(255, 176, 64), VIOLET=(198, 132, 255), PINK=(255, 110, 175),
                     TEAL=(96, 206, 166), CORAL=(240, 120, 60), AMBER=(255, 150, 40),
                     GREEN=(140, 205, 90), RED=(240, 80, 66), CRIT=(255, 80, 70),
                     DARKRED=(180, 60, 50), WARN=(255, 150, 40), GLOW=0.95,
                     TRACK=(255, 255, 255, 20), HAIRLINE=(255, 255, 255, 30)),
    "graphite": dict(BG=(42, 45, 50), BG2=(24, 26, 29), MODE="solid",
                     TEXT=(228, 233, 238), MUTE=(150, 161, 171), STEEL=(196, 206, 214),
                     ACCENT=(40, 205, 150), VIOLET=(185, 130, 245), PINK=(255, 95, 165),
                     TEAL=(90, 200, 235), CORAL=(240, 140, 80), AMBER=(240, 185, 80),
                     GREEN=(150, 208, 84), RED=(240, 80, 80), CRIT=(255, 80, 95),
                     DARKRED=(185, 62, 62), WARN=(255, 180, 80), GLOW=0.8,
                     TRACK=(255, 255, 255, 26), HAIRLINE=(255, 255, 255, 30)),
    "nord": dict(BG=(32, 38, 54), BG2=(22, 26, 40), MODE="solid",
                 TEXT=(229, 233, 240), MUTE=(130, 142, 162), STEEL=(210, 218, 232),
                 ACCENT=(136, 192, 208), VIOLET=(180, 142, 210), PINK=(191, 97, 160),
                 TEAL=(150, 196, 170), CORAL=(208, 135, 112), AMBER=(235, 203, 139),
                 GREEN=(163, 190, 140), RED=(191, 97, 106), CRIT=(216, 96, 108), DARKRED=(150, 72, 80),
                 WARN=(235, 203, 139), GLOW=0.8,
                 TRACK=(255, 255, 255, 24), HAIRLINE=(255, 255, 255, 28)),
    "dracula": dict(BG=(14, 14, 20), BG2=(9, 9, 13), MODE="solid",
                    TEXT=(248, 248, 242), MUTE=(140, 118, 140), STEEL=(230, 110, 130),
                    ACCENT=(255, 60, 72), VIOLET=(189, 147, 249), PINK=(255, 95, 150),
                    TEAL=(139, 233, 253), CORAL=(255, 150, 90), AMBER=(255, 184, 108),
                    GREEN=(80, 250, 123), RED=(255, 60, 72), CRIT=(255, 55, 70),
                    DARKRED=(205, 40, 58), WARN=(255, 120, 90), GLOW=1.0,
                    TRACK=(255, 60, 72, 30), HAIRLINE=(255, 60, 72, 85)),
    "matrix": dict(BG=(4, 10, 6), BG2=(2, 6, 3), MODE="solid",
                   TEXT=(196, 255, 204), MUTE=(92, 158, 106), STEEL=(120, 195, 135),
                   ACCENT=(80, 240, 130), VIOLET=(188, 222, 52), PINK=(190, 250, 150),
                   TEAL=(43, 230, 160), CORAL=(160, 225, 95), AMBER=(224, 232, 120),
                   GREEN=(80, 240, 130), RED=(255, 96, 64), CRIT=(255, 80, 60),
                   WARN=(224, 232, 120), GLOW=1.3,
                   RAMP=((0.0, (80, 240, 130)), (0.55, (200, 255, 120)),
                         (0.80, (235, 190, 90)), (1.0, (255, 96, 64))),
                   TRACK=(120, 255, 150, 22), HAIRLINE=(80, 240, 130, 55)),
    "daylight": dict(BG=(244, 246, 250), BG2=(226, 232, 244), MODE="solid",
                     TEXT=(28, 33, 44), MUTE=(92, 100, 116), STEEL=(64, 72, 88),
                     ACCENT=(18, 98, 196), VIOLET=(140, 78, 220), PINK=(214, 56, 140),
                     TEAL=(16, 150, 160), CORAL=(204, 90, 52), AMBER=(214, 150, 30),
                     GREEN=(22, 140, 72), RED=(214, 64, 64), CRIT=(210, 54, 72),
                     WARN=(196, 110, 10), HILITE=(255, 255, 255), GLOW=0.3,
                     RAMP=((0.0, (22, 140, 72)), (0.50, (196, 142, 18)),
                           (0.78, (214, 104, 38)), (1.0, (196, 40, 50))),
                     TRACK=(0, 0, 0, 54), RING=(0, 0, 0, 52), HAIRLINE=(0, 0, 0, 48)),
    "gruvbox": dict(BG=(40, 40, 40), BG2=(29, 32, 33), MODE="solid",
                    TEXT=(235, 219, 178), MUTE=(158, 142, 124), STEEL=(168, 153, 132),
                    ACCENT=(250, 189, 47), VIOLET=(211, 134, 155), PINK=(177, 98, 134),
                    TEAL=(142, 192, 124), CORAL=(214, 93, 30), AMBER=(254, 128, 25),
                    GREEN=(152, 190, 60), RED=(251, 73, 52), CRIT=(251, 73, 52),
                    DARKRED=(204, 36, 29), WARN=(254, 128, 25),
                    RAMP=((0.0, (152, 190, 60)), (0.50, (250, 189, 47)),
                          (0.78, (254, 128, 25)), (1.0, (251, 73, 52))),
                    TRACK=(255, 255, 255, 22), HAIRLINE=(255, 255, 255, 26)),
    "catppuccin": dict(BG=(30, 30, 46), BG2=(17, 17, 27), MODE="solid",
                       TEXT=(205, 214, 244), MUTE=(136, 141, 165), STEEL=(180, 190, 254),
                       ACCENT=(137, 180, 250), VIOLET=(203, 166, 247), PINK=(237, 150, 205),
                       TEAL=(148, 226, 213), CORAL=(250, 179, 135), AMBER=(249, 226, 175),
                       GREEN=(166, 227, 161), RED=(243, 139, 168), CRIT=(243, 139, 168), DARKRED=(210, 95, 120),
                       WARN=(250, 179, 135), GLOW=0.7,
                       RAMP=((0.0, (166, 227, 161)), (0.50, (249, 226, 175)),
                             (0.78, (250, 179, 135)), (1.0, (243, 139, 168))),
                       TRACK=(255, 255, 255, 22), HAIRLINE=(203, 166, 247, 60)),
    "solarized": dict(BG=(0, 43, 54), BG2=(7, 54, 66), MODE="solid",
                      TEXT=(238, 232, 213), MUTE=(142, 164, 172), STEEL=(147, 161, 163),
                      ACCENT=(42, 161, 152), VIOLET=(108, 113, 196), PINK=(211, 54, 130),
                      TEAL=(38, 139, 210), CORAL=(203, 75, 22), AMBER=(181, 137, 0),
                      GREEN=(133, 153, 0), RED=(220, 50, 47), CRIT=(220, 50, 47),
                      WARN=(181, 137, 0), GLOW=0.6,
                      RAMP=((0.0, (133, 153, 0)), (0.50, (181, 137, 0)),
                            (0.78, (203, 75, 22)), (1.0, (220, 50, 47))),
                      TRACK=(255, 255, 255, 20), HAIRLINE=(255, 255, 255, 24)),
    "solarized-light": dict(BG=(253, 246, 227), BG2=(238, 232, 213), MODE="solid",
                            TEXT=(60, 80, 86), MUTE=(90, 108, 114), STEEL=(78, 100, 107),
                            ACCENT=(20, 100, 170), VIOLET=(108, 113, 196), PINK=(211, 54, 130),
                            TEAL=(32, 150, 142), CORAL=(203, 75, 22), AMBER=(150, 112, 0),
                            GREEN=(96, 112, 0), RED=(214, 50, 47), CRIT=(210, 50, 60),
                            WARN=(150, 112, 0), HILITE=(255, 255, 255), GLOW=0.3,
                            RAMP=((0.0, (96, 112, 0)), (0.50, (170, 128, 0)),
                                  (0.78, (203, 75, 22)), (1.0, (200, 44, 44))),
                            TRACK=(0, 0, 0, 50), RING=(0, 0, 0, 48), HAIRLINE=(0, 0, 0, 44)),
    "onyx": dict(BG=(0, 0, 0), BG2=(0, 0, 0), MODE="solid",
                 TEXT=(240, 248, 255), MUTE=(120, 140, 158), STEEL=(150, 190, 215),
                 ACCENT=(60, 205, 255), VIOLET=(176, 120, 255), PINK=(255, 70, 175),
                 TEAL=(70, 255, 180), CORAL=(255, 135, 70), AMBER=(255, 190, 60),
                 GREEN=(60, 225, 120), RED=(255, 70, 70), CRIT=(255, 65, 90),
                 DARKRED=(195, 55, 55), WARN=(255, 180, 70), HILITE=(255, 255, 255),
                 GLOW=1.15, TRACK=(255, 255, 255, 30), HAIRLINE=(255, 255, 255, 34)),
    "slatemono": dict(BG=(26, 27, 30), BG2=(18, 19, 21), MODE="solid",
                      TEXT=(236, 238, 242), MUTE=(150, 157, 169), STEEL=(206, 212, 222),
                      ACCENT=(202, 208, 218), VIOLET=(120, 128, 142), PINK=(164, 170, 184),
                      TEAL=(150, 178, 208), CORAL=(190, 194, 204), AMBER=(205, 206, 214),
                      GREEN=(150, 178, 208), RED=(232, 84, 84), CRIT=(255, 80, 80),
                      DARKRED=(182, 62, 62), WARN=(245, 170, 80), GLOW=0.6,
                      TRACK=(255, 255, 255, 22), HAIRLINE=(255, 255, 255, 26)),
    "rosedusk": dict(BG=(38, 26, 36), BG2=(20, 14, 24), MODE="solid",
                     TEXT=(245, 228, 236), MUTE=(158, 128, 144), STEEL=(218, 176, 196),
                     ACCENT=(245, 130, 165), VIOLET=(196, 144, 232), PINK=(230, 90, 160),
                     TEAL=(232, 192, 122), CORAL=(255, 150, 120), AMBER=(246, 196, 118),
                     GREEN=(160, 208, 160), RED=(240, 80, 96), CRIT=(255, 72, 92),
                     DARKRED=(170, 60, 72), WARN=(255, 170, 110), GLOW=0.9,
                     TRACK=(255, 255, 255, 22), HAIRLINE=(245, 130, 165, 55)),
    "neonnoir": dict(BG=(10, 6, 24), BG2=(4, 2, 12), MODE="solid",
                     TEXT=(236, 230, 255), MUTE=(142, 122, 178), STEEL=(214, 120, 240),
                     ACCENT=(140, 96, 255), VIOLET=(255, 56, 170), PINK=(255, 80, 150),
                     TEAL=(40, 255, 190), CORAL=(255, 110, 60), AMBER=(255, 228, 80),
                     GREEN=(60, 255, 150), RED=(255, 50, 90), CRIT=(255, 45, 95),
                     DARKRED=(195, 45, 80), WARN=(255, 185, 55), GLOW=1.4,
                     TRACK=(188, 74, 255, 26), HAIRLINE=(0, 235, 255, 55)),
    "umber": dict(BG=(36, 28, 22), BG2=(18, 13, 10), MODE="solid",
                  TEXT=(230, 218, 196), MUTE=(150, 132, 110), STEEL=(196, 164, 124),
                  ACCENT=(212, 112, 52), VIOLET=(176, 138, 198), PINK=(200, 120, 120),
                  TEAL=(110, 185, 175), CORAL=(198, 138, 72), AMBER=(236, 180, 96),
                  GREEN=(150, 175, 95), RED=(200, 78, 60), CRIT=(214, 76, 54),
                  DARKRED=(155, 62, 50), WARN=(215, 150, 70), GLOW=0.8,
                  TRACK=(255, 255, 255, 22), HAIRLINE=(255, 255, 255, 26)),
    "sandstone": dict(BG=(250, 244, 234), BG2=(240, 230, 214), MODE="solid",
                      TEXT=(50, 40, 30), MUTE=(120, 100, 78), STEEL=(118, 98, 76),
                      ACCENT=(180, 70, 45), VIOLET=(132, 104, 164), PINK=(200, 55, 110),
                      TEAL=(32, 138, 124), CORAL=(214, 118, 38), AMBER=(196, 136, 30),
                      GREEN=(60, 140, 80), RED=(198, 52, 44), CRIT=(200, 45, 50),
                      DARKRED=(150, 42, 40), WARN=(190, 120, 20), HILITE=(255, 255, 255),
                      GLOW=0.3,
                      RAMP=((0.0, (60, 140, 80)), (0.50, (190, 135, 20)),
                            (0.78, (214, 110, 38)), (1.0, (200, 44, 44))),
                      TRACK=(0, 0, 0, 54), RING=(0, 0, 0, 52), HAIRLINE=(0, 0, 0, 48)),
    "transparent": dict(MODE="transparent", OPACITY=0.30, BORDER=1.0, GLOW=0.6,
                        RADIUS=20, STEEL=(224, 227, 242), MUTE=(200, 204, 222),
                        HAIRLINE=(255, 255, 255, 56)),
    "wallpaper": dict(MODE="image", OPACITY=0.56, BORDER=0.9, GLOW=0.4,
                      BG=(18, 20, 28), HAIRLINE=(255, 255, 255, 40)),
}

_PALETTE_KEYS = ("TEXT", "MUTE", "STEEL", "ACCENT", "VIOLET", "PINK", "TEAL",
                 "CORAL", "AMBER", "GREEN", "DARKRED", "RED", "WARN", "CRIT",
                 "TRACK", "RING", "HAIRLINE", "RAMP")

# User-facing text/data colour roles (palette key -> label) that the settings
# Appearance section exposes as per-panel colour overrides. ACCENT has its own
# dedicated picker ("Accent colour"), so it is not repeated here. Each role maps
# to a palette global the draw code already reads; an override set here replaces
# that global for the panel (see resolve_theme).
COLOR_ROLES = (
    ("STEEL",   "Titles"),
    ("TEXT",    "Values / data"),
    ("MUTE",    "Subtitles / muted"),
    ("VIOLET",  "GPU / violet"),
    ("TEAL",    "RAM / teal"),
    ("PINK",    "Pink"),
    ("CORAL",   "Upload / coral"),
    ("AMBER",   "Amber"),
    ("GREEN",   "Good / green"),
    ("RED",     "Alerts / red"),
    ("WARN",    "Warning"),
    ("CRIT",    "Critical"),
    ("DARKRED", "Dark red"),
)
_COLOR_ROLE_KEYS = frozenset(k for k, _ in COLOR_ROLES)

for _k, _v in _BASE_PALETTE.items():
    globals()[_k] = _v
_BG = {k: _BASE_PALETTE[k] for k in ("BG", "BG2", "HILITE", "MODE", "OPACITY",
                                     "RADIUS", "BORDER", "GLOW")}

_WALLPAPER = {"t": 0.0, "path": None}

def _kde_dark():
    """True if the KDE/Plasma colour scheme is dark. Reads kdeglobals directly
    so it needs no D-Bus and works over XWayland too."""
    try:
        with open(os.path.join(HOME, ".config/kdeglobals"), encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                if line.strip().startswith("ColorScheme"):
                    return "dark" in line.split("=", 1)[-1].lower()
    except OSError:
        pass
    return False

def _kde_wallpaper():
    """Current Plasma desktop wallpaper, parsed from the appletsrc. Plasma stores
    it as Image=file://... in a [Containments][n][Wallpaper][org.kde.image][...]
    group; the first readable image wins (good enough for a frosted backdrop)."""
    cfg = os.path.join(HOME, ".config/plasma-org.kde.plasma.desktop-appletsrc")
    try:
        import configparser
        cp = configparser.RawConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read(cfg, encoding="utf-8")
    except Exception:
        return None
    for sect in cp.sections():
        if "org.kde.image" not in sect:
            continue
        for key in ("Image", "image"):
            if cp.has_option(sect, key):
                uri = cp.get(sect, key).strip()
                if uri.startswith("file://"):
                    uri = urllib.parse.unquote(uri[7:])
                if uri and os.path.isdir(uri):
                    # a slideshow folder: take the first image inside it
                    for n in sorted(os.listdir(uri)):
                        p = os.path.join(uri, n)
                        if os.path.isfile(p) and n.lower().endswith(
                                (".jpg", ".jpeg", ".png", ".webp", ".bmp")):
                            return p
                elif uri and os.path.isfile(uri):
                    return uri
    return None

def desktop_wallpaper():
    """Path to the current desktop wallpaper, cached briefly so the 'wallpaper'
    theme can frost the real background with no image chosen. Reads Cinnamon/GNOME
    via gsettings and KDE/Plasma from its own config."""
    now = time.time()
    if now - _WALLPAPER["t"] < 30:
        return _WALLPAPER["path"]
    _WALLPAPER["t"] = now

    def _get(schema, key):
        try:
            out = subprocess.run(["gsettings", "get", schema, key],
                                 capture_output=True, text=True, timeout=3).stdout
        except Exception:
            return ""
        return out.strip().strip("'\"")

    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    path = None
    if "KDE" in desktop or "PLASMA" in desktop:
        path = _kde_wallpaper()
    if path is None:
        dark = "dark" in _get("org.gnome.desktop.interface", "color-scheme").lower() \
            or "dark" in _get("org.cinnamon.desktop.interface", "gtk-theme").lower() \
            or _kde_dark()
        # Prefer the dark-mode wallpaper when the desktop is dark, so the frosted
        # panel matches the picture that is actually on screen.
        keys = ("picture-uri-dark", "picture-uri") if dark else ("picture-uri", "picture-uri-dark")
        for schema in ("org.cinnamon.desktop.background", "org.gnome.desktop.background"):
            for key in keys:
                uri = _get(schema, key)
                if uri.startswith("file://"):
                    uri = urllib.parse.unquote(uri[7:])
                if uri and os.path.isfile(uri):
                    path = uri
                    break
            if path:
                break
    if path is None and ("KDE" in desktop or "PLASMA" in desktop):
        path = _kde_wallpaper()
    _WALLPAPER["path"] = path
    return path

def resolve_theme(cfg, s):
    """Build a panel's effective theme dict: a named base theme merged with any
    global and per-panel appearance overrides (accent, opacity, radius, border,
    background mode and image). cfg is the panel config, s the settings."""
    name = cfg.get("theme") or s.get("theme") or "midnight"
    t = dict(_BASE_PALETTE)
    t.update(THEMES.get(name, {}))
    t["_name"] = name
    def pick(key):
        v = cfg.get(key)
        return v if v is not None else s.get(key)
    acc = pick("accent")
    if acc:
        t["ACCENT"] = tuple(acc)
    for key, dest in (("opacity", "OPACITY"), ("radius", "RADIUS"),
                      ("border", "BORDER"), ("glow", "GLOW"), ("bg_mode", "MODE")):
        v = pick(key)
        if v is not None:
            t[dest] = v
    ct = pick("contrast")
    if ct:
        try:
            f = max(0.0, min(1.0, float(ct)))
        except (TypeError, ValueError):
            f = 0.0
        tc = t["TEXT"]
        t["MUTE"] = tuple(round(a + (b - a) * f) for a, b in zip(t["MUTE"], tc))
        t["STEEL"] = tuple(round(a + (b - a) * f) for a, b in zip(t["STEEL"], tc))
    # Per-role text/data colour overrides. Applied last so an explicit choice
    # wins over the theme and over the contrast nudge above. Only known roles
    # with a valid RGB triple are honoured; anything else is ignored.
    cols = pick("colors")
    if isinstance(cols, dict):
        for role, rgb in cols.items():
            if role in _COLOR_ROLE_KEYS and isinstance(rgb, (list, tuple)) and len(rgb) >= 3:
                try:
                    t[role] = tuple(int(c) for c in rgb[:3])
                except (TypeError, ValueError):
                    pass
    t["_image"] = pick("bg_image")
    t["_blur"] = pick("bg_blur")
    t["_scale"] = pick("bg_scale")
    t["_tile"] = pick("bg_tile")
    t["_wall"] = False
    if t["MODE"] == "image" and not t["_image"]:
        t["_image"] = desktop_wallpaper()        # frost the real wallpaper
        t["_wall"] = True                        # crop it at the panel's position
    return t

def apply_theme(t):
    """Set the palette module-globals and the background spec from a resolved
    theme dict, so the existing draw code (which reads these globals) renders in
    that theme. Mirrors how FLEX is a per-render global."""
    global _BG
    g = globals()
    for k in _PALETTE_KEYS:
        g[k] = t[k]
    _BG = {"BG": t["BG"], "BG2": t["BG2"], "HILITE": t["HILITE"], "MODE": t["MODE"],
           "OPACITY": float(t["OPACITY"]), "RADIUS": float(t["RADIUS"]),
           "BORDER": float(t["BORDER"]), "GLOW": float(t["GLOW"]),
           "image": t.get("_image"), "blur": t.get("_blur"), "wall": t.get("_wall"),
           "scale": t.get("_scale"), "tile": t.get("_tile"), "pos": None, "screen": None}

def theme_palette(name):
    """A theme's full palette (base merged with the named theme), for code that
    needs the colours outside the render loop (e.g. the settings window chrome)."""
    b = dict(_BASE_PALETTE)
    b.update(THEMES.get(name, {}))
    return b

def _hx(c):
    return "#%02x%02x%02x" % (int(c[0]), int(c[1]), int(c[2]))

def _mix(c, other, f):
    return tuple(round(a + (b - a) * f) for a, b in zip(c[:3], other))

def theme_chrome(name):
    """Resolve the settings-window chrome colours for a theme: the window
    background and text, plus whether it is a dark chrome. Shared by the CSS and
    the header-bar override."""
    p = theme_palette(name)
    bg, text = p["BG"], p["TEXT"]
    if p["MODE"] in ("transparent", "image"):
        bg = (20, 23, 30)
    dark = (0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]) < 128
    return bg, text, dark

def make_settings_css(name):
    """Build the settings-window stylesheet from a theme, so the configuration
    UI matches the chosen skin (dark themes get dark chrome, light themes light
    chrome; the accent drives highlights, checks and the primary button)."""
    p = theme_palette(name)
    bg, text, mute, accent = p["BG"], p["TEXT"], p["MUTE"], p["ACCENT"]
    steel = p.get("STEEL", mute)
    # Transparent/image themes have no meaningful solid base; use a dark chrome.
    if p["MODE"] in ("transparent", "image"):
        bg = (20, 23, 30)
    W_, K_ = (255, 255, 255), (0, 0, 0)
    dark = (0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]) < 128
    if dark:
        win, card = bg, _mix(bg, W_, 0.06)
        inp, bord, hov = _mix(bg, W_, 0.12), _mix(bg, W_, 0.18), _mix(bg, W_, 0.16)
        sub_hov = "rgba(255,255,255,0.06)"
        # Tint the body text toward the accent so every dark theme reads as
        # "themed" (like Matrix's green), not a generic white-on-dark chrome.
        # Themes whose TEXT already carries the accent hue (Matrix) barely move.
        text = _mix(text, accent, 0.26)
    else:
        win, card = bg, _mix(bg, W_, 0.5)
        inp, bord, hov = _mix(bg, W_, 0.82), _mix(bg, K_, 0.14), _mix(bg, K_, 0.05)
        sub_hov = "rgba(0,0,0,0.06)"
    atext = "#ffffff" if (0.2126 * accent[0] + 0.7152 * accent[1] + 0.0722 * accent[2]) < 150 else "#101010"
    ah = _mix(accent, W_, 0.18)
    arA = "rgba(%d,%d,%d," % accent
    lab = _mix(text, bg, 0.26)   # field labels/subtitle dimmer than the values they front
    css = """
    window {{ background-color: {win}; }}
    scrolledwindow, scrolledwindow viewport, viewport, .content {{ background-color: {win}; }}
    label {{ color: {text}; font-size: 13px; }}
    .app-title {{ color: {text}; font-size: 18px; font-weight: 800; }}
    .app-subtitle {{ color: {lab}; font-size: 12px; }}
    .group {{ background-color: {card}; border: 1px solid {bord}; border-radius: 14px; }}
    .group-heading {{ color: {steel}; font-size: 11px; font-weight: 700; letter-spacing: 1.6px; }}
    .scope-divider {{ color: {accent}; font-size: 10px; font-weight: 800; letter-spacing: 2px;
        margin: 26px 0 4px 0; padding-top: 18px; border-top: 3px solid {accent}; }}
    /* The divider goes on the expander node (full app width), not the title node
       (which shrinks to the arrow + heading text). */
    expander {{ border-top: 2px solid {mute}; margin-top: 10px; padding-top: 8px; }}
    expander.first {{ border-top: none; margin-top: 0; padding-top: 0; }}
    expander > title {{ padding: 9px 8px; margin: 0 -8px; border-radius: 8px; }}
    expander > title:hover {{ background-color: {sub_hov}; }}
    expander > title:hover .group-heading, expander > title:hover > arrow {{ color: {text}; }}
    expander > title > arrow {{ color: {mute}; min-width: 12px; min-height: 12px; margin-right: 8px; -gtk-icon-transform: scale(0.85); }}
    expander > title > arrow:checked {{ color: {accent}; }}
    .field-label {{ color: {lab}; font-size: 13px; }}
    entry, spinbutton, spinbutton entry, combobox button, filechooserbutton button {{
        background-image: none; background-color: {inp}; color: {text};
        border: 1px solid {bord}; border-radius: 9px; padding: 6px 10px; min-height: 22px;
        caret-color: {accent}; }}
    entry image {{ color: {mute}; }}
    entry:focus, spinbutton:focus, spinbutton entry:focus,
    combobox button:focus, combobox:focus button {{ border-color: {accent}; }}
    spinbutton, spinbutton entry {{ border-radius: 9px; }}
    spinbutton {{ background-color: {inp}; border-color: {bord}; padding: 0; }}
    spinbutton button {{ min-height: 0; min-width: 20px; padding: 0 2px; margin: 3px 2px; }}
    spinbutton button image {{ -gtk-icon-transform: scale(0.62); color: {text}; }}
    combobox button, filechooserbutton button {{ padding: 6px 10px; font-weight: 400; }}
    combobox arrow {{ color: {mute}; min-height: 14px; min-width: 14px; }}
    button {{ background-image: none; background-color: {inp}; color: {text};
        border: 1px solid {bord}; border-radius: 9px; padding: 7px 14px; font-weight: 600; }}
    button:hover {{ background-color: {hov}; border-color: {accent}; }}
    button:active {{ background-color: {hov}; }}
    .ghost {{ background-color: {arA}0.08); border: 1px solid {arA}0.35); color: {text}; }}
    .ghost:hover {{ background-color: {arA}0.16); border-color: {accent}; }}
    .accent {{ background-color: {accent}; color: {atext}; border-color: {accent};
        font-weight: 700; padding: 7px 20px; }}
    .accent:hover {{ background-color: {ah}; border-color: {ah}; }}
    checkbutton {{ color: {text}; font-size: 13px; }}
    checkbutton check {{ background-color: {inp}; border: 1px solid {bord};
        border-radius: 5px; min-width: 15px; min-height: 15px; }}
    checkbutton:hover check {{ border-color: {accent}; }}
    checkbutton check:checked {{ background-color: {accent}; border-color: {accent}; color: {atext}; }}
    .hint {{ color: {mute}; font-size: 11px; }}
    .off-note {{ color: {steel}; font-size: 11px; font-style: italic; }}
    .result {{ color: {text}; font-size: 12px; }}
    .result-ok {{ color: {accent}; font-size: 12px; }}
    .status {{ color: {mute}; font-size: 12px; }}
    .status-ok {{ color: {accent}; font-size: 12px; }}
    .sensor-scroll {{ background-color: {card}; border: 1px solid {bord}; border-radius: 10px; }}
    scrollbar slider {{ background-color: {bord}; border-radius: 8px; min-width: 6px; }}
    scrollbar slider:hover {{ background-color: {mute}; }}
    undershoot.top, undershoot.bottom, undershoot.left, undershoot.right,
    overshoot.top, overshoot.bottom, overshoot.left, overshoot.right {{ background: none; background-image: none; }}
    scrolledwindow {{ border: none; box-shadow: none; }}
    * {{ outline: none; -gtk-outline-radius: 0; }}
    .subtle {{ background: none; background-image: none; border: none; box-shadow: none;
        color: {mute}; font-weight: 600; padding: 5px 10px; }}
    .subtle:hover {{ color: {text}; background-color: {sub_hov}; border: none; }}
    /* Action-bar buttons (Export / Import / Close): visible outline, clear hover. */
    .actionbtn {{ background: none; background-image: none; color: {text};
        border: 1px solid {bord}; border-radius: 9px; padding: 6px 16px;
        font-weight: 600; box-shadow: none; }}
    .actionbtn:hover {{ background-color: {hov}; border-color: {accent}; color: {text}; }}
    .actionbtn:active {{ background-color: {sub_hov}; }}
    .order-list {{ background-color: {card}; border: 1px solid {bord}; border-radius: 10px; }}
    .order-list row {{ border-bottom: 1px solid {bord}; }}
    .order-list row:last-child {{ border-bottom: none; }}
    .order-list row:hover {{ background-color: {arA}0.10); }}
    .order-list row.dragging {{ background-color: {arA}0.20); }}
    .actionbar {{ background-color: {win}; border-top: 1px solid {bord}; padding-top: 14px; }}
    .header {{ background-color: {win}; border-bottom: 1px solid {bord}; }}
    .pagebody {{ padding: 20px; }}   /* inset via padding so the panel colour paints it */
    separator {{ background-color: {bord}; min-height: 1px; min-width: 1px; }}
    scale {{ color: {mute}; padding: 8px 0; }}
    scale trough {{ background-color: {bord}; border-radius: 4px; min-height: 4px; }}
    scale highlight {{ background-color: {accent}; border-radius: 4px; }}
    scale slider {{ background-color: {text}; border-radius: 50%; min-width: 16px; min-height: 16px;
        margin: -8px; }}
    scale value {{ color: {mute}; font-size: 12px; min-width: 30px; margin-left: 10px;
        font-feature-settings: "tnum"; }}
    button.color {{ padding: 5px; min-width: 0; }}
    button.color colorswatch, button.color colorswatch overlay {{ border-radius: 6px;
        min-width: 24px; min-height: 22px; }}
    /* Dropdown popups, menus, list views, tooltips */
    popover, popover.background, combobox window, combobox window.background,
    menu, .menu, .context-menu, .background.menu {{
        background-color: {card}; color: {text};
        border: 1px solid {bord}; border-radius: 10px; padding: 2px; }}
    menuitem, modelbutton, .view, treeview.view, combobox window cellview,
    combobox window treeview.view {{
        background-color: transparent; color: {text}; }}
    menuitem:hover, modelbutton:hover, .view:hover,
    treeview.view:selected, .view:selected, menuitem:selected {{
        background-color: {accent}; color: {atext}; border-radius: 6px; }}
    treeview.view {{ background-color: {card}; }}
    tooltip, tooltip.background {{ background-color: {card}; color: {text};
        border: 1px solid {bord}; }}
    tooltip label {{ color: {text}; }}
    filechooser, filechooser .view, placessidebar, placessidebar .view {{
        background-color: {card}; color: {text}; }}
    """.format(win=_hx(win), card=_hx(card), inp=_hx(inp), bord=_hx(bord), hov=_hx(hov),
               text=_hx(text), mute=_hx(mute), accent=_hx(accent), atext=atext,
               steel=_hx(steel), lab=_hx(lab), ah=_hx(ah), arA=arA, sub_hov=sub_hov)
    return (dark, css.encode())

def menu_css(name):
    """CSS for just the GTK menus/popovers, in a theme's colours.

    The panel process draws its windows itself (click-through ARGB cairo), so it
    must NOT load the full settings CSS — that paints a solid window background
    and would break the transparency. This styles only the tray icon's
    right-click menu (and any popover) so it matches the panel's skin."""
    p = theme_palette(name)
    bg, text, accent = p["BG"], p["TEXT"], p["ACCENT"]
    if p["MODE"] in ("transparent", "image"):
        bg = (20, 23, 30)
    W_, K_ = (255, 255, 255), (0, 0, 0)
    dark = (0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]) < 128
    card = _mix(bg, W_, 0.06) if dark else _mix(bg, W_, 0.5)
    bord = _mix(bg, W_, 0.18) if dark else _mix(bg, K_, 0.14)
    atext = "#ffffff" if (0.2126 * accent[0] + 0.7152 * accent[1] + 0.0722 * accent[2]) < 150 else "#101010"
    css = """
    /* The popup window behind the menu is opaque (theme white) and has no alpha,
       so any rounding on the menu leaves the window's square white corners
       showing ("silver/white corners"). Keep the menu a square dark rectangle so
       its background fills the whole popup — no corner gap to show through — and
       drop the theme border/shadow. */
    window, window.background, window.popup, window.csd, .csd,
    menu decoration, popover decoration, .menu decoration, window.popup decoration {{
        background-color: transparent; border: none; box-shadow: none;
        border-radius: 0; }}
    menu, .menu, .context-menu, .background.menu, menu.background,
    .csd.popup.menu, popover, popover.background, popover contents {{
        background-color: {card}; color: {text};
        border: none; box-shadow: none; border-radius: 0; padding: 4px; }}
    menuitem, modelbutton {{ background-color: {card}; color: {text};
        padding: 6px 12px; border-radius: 0; }}
    menuitem label, modelbutton label {{ color: {text}; }}
    menuitem:hover, menuitem:selected, modelbutton:hover {{
        background-color: {accent}; color: {atext}; }}
    menuitem:hover label, menuitem:selected label {{ color: {atext}; }}
    menu separator, popover separator {{ background-color: {bord};
        min-height: 1px; margin: 4px 2px; }}
    """.format(card=_hx(card), bord=_hx(bord), text=_hx(text),
               accent=_hx(accent), atext=atext)
    return css.encode()

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
    (13,  None),                       # None -> the theme's text colour, resolved
    (25,  None),                       #         live so light themes stay legible
    (31,  (240, 176, 80)),
    (37,  (246, 105, 64)),
    (43,  (222, 42, 52)),
)

def weather_temp_color(c):
    """Colour for an air temperature in degrees Celsius (see WEATHER_TEMP_STOPS).
    The mild band follows the active theme's text colour, so it is readable on
    light themes too."""
    stops = tuple((t, (TEXT if col is None else col)) for t, col in WEATHER_TEMP_STOPS)
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

UI_MED    = _font_file("Inter-Medium.otf", "Inter-Medium.ttf", "DejaVuSans.ttf", "NotoSans-Regular.ttf")
UI_SEMI   = _font_file("Inter-SemiBold.otf", "Inter-SemiBold.ttf", "DejaVuSans-Bold.ttf", "NotoSans-Bold.ttf")
MONO_REG  = _font_file("JetBrainsMono-Regular.ttf", "DejaVuSansMono.ttf", "NotoSansMono-Regular.ttf")
MONO_MED  = _font_file("JetBrainsMono-Medium.ttf", "DejaVuSansMono.ttf", "NotoSansMono-Bold.ttf")
MONO_LIGHT= _font_file("JetBrainsMono-Light.ttf", "DejaVuSansMono.ttf", "NotoSansMono-Regular.ttf")

def F(path, size):
    key = (path, size, TEXT_SCALE)
    if key not in _FONT_CACHE:
        px = int(size * SS * TEXT_SCALE)
        try:
            _FONT_CACHE[key] = ImageFont.truetype(path, px)
        except (OSError, TypeError, AttributeError, ValueError):
            # No Inter/DejaVu on a minimal install (container, KDE-only): keep
            # rendering with Pillow's built-in bitmap font rather than crash.
            try:
                _FONT_CACHE[key] = ImageFont.load_default(px)
            except TypeError:
                _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]

# Text metrics are called ~270x per frame, almost always on the same strings
# (section labels, units, gauge names). Font objects are cached in _FONT_CACHE so
# id(f) is a stable key; memoising getlength turns the hot path into dict lookups.
_MEASURE_CACHE = {}
_CHARW_CACHE = {}

def _charw(f, ch):
    key = (id(f), ch)
    w = _CHARW_CACHE.get(key)
    if w is None:
        w = f.getlength(ch)
        _CHARW_CACHE[key] = w
    return w

def measure(f, s, tracking=0):
    if not s:
        return 0
    key = (id(f), s, tracking)
    v = _MEASURE_CACHE.get(key)
    if v is None:
        v = f.getlength(s) + tracking * SS * (len(s) - 1)
        if len(_MEASURE_CACHE) >= 8192:   # bound it; clear rather than stop caching
            _MEASURE_CACHE.clear()
        _MEASURE_CACHE[key] = v
    return v

_TEXT_TILE_CACHE = {}
_TILE_VPAD = 3

def _text_tile(f, s, fill, tracking):
    """A pre-rendered RGBA tile for a letterspaced label. These are constant
    strings (section headers, units, gauge names) drawn every frame; rendering
    each once and compositing the tile replaces ~130 per-character draws a frame
    with a handful of alpha_composites — the single biggest render saving."""
    key = (id(f), s, fill, tracking)
    tile = _TEXT_TILE_CACHE.get(key)
    if tile is None:
        w = int(measure(f, s, tracking)) + 2
        h = int(getattr(f, "size", 16) * 1.7) + 2 * _TILE_VPAD
        if w < 1 or h < 1:
            return None
        tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        xx = 0.0
        for ch in s:
            td.text((xx, _TILE_VPAD), ch, font=f, fill=fill)
            xx += _charw(f, ch) + tracking * SS
        if len(_TEXT_TILE_CACHE) >= 512:
            _TEXT_TILE_CACHE.clear()
        _TEXT_TILE_CACHE[key] = tile
    return tile

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
        tile = _text_tile(f, s, fill, tracking)
        if tile is not None:
            d._image.alpha_composite(tile, (int(round(x)), int(round(y)) - _TILE_VPAD))
        else:
            xx = x
            for ch in s:
                d.text((xx, y), ch, font=f, fill=fill)
                xx += _charw(f, ch) + tracking * SS
    else:
        d.text((x, y), s, font=f, fill=fill)

def label(d, x, y, s, fill=MUTE, size=T_LABEL, tracking=1.6):
    """Small letterspaced all-caps section label."""
    text(d, x, y, s.upper(), F(UI_SEMI, size), fill, tracking=tracking)

def label_r(d, x, y, s, fill=MUTE, size=T_LABEL, tracking=1.6):
    text(d, x, y, s.upper(), F(UI_SEMI, size), fill, anchor="r", tracking=tracking)

def bar(img, x, y, w, h, frac, color=None, track=None, ramp=False):
    """Rounded progress bar with a track and a soft gradient fill. Drawn into a
    tile the size of the bar (not the full canvas) — full-canvas glow layers
    were the single biggest cost in the render loop.

    ramp=True colours the fill from the green-amber-red ramp. The ramp is
    mapped across the FULL track, not across the fill, so the colour at the
    bar's head tells you the percentage; stretching the whole ramp over every
    bar would make a 5% bar and a 95% bar look identical."""
    if track is None:   # resolve the theme's current track, not midnight's
        track = TRACK
    X, Y = int(x * SS), int(y * SS)
    Wp, Hp = int(w * SS), int(h * SS)
    pad = int(3 * SS)
    tile = Image.new("RGBA", (Wp + 2 * pad, Hp + 2 * pad), (0, 0, 0, 0))
    r = Hp / 2

    def _rad(span):
        # PIL's rounded_rectangle raises when a side is narrower than ~2*radius;
        # clamp so neither axis is degenerate (0 => a plain rectangle).
        return max(0.0, min(r, (span - 2) / 2.0, (Hp - 2) / 2.0))

    ImageDraw.Draw(tile).rounded_rectangle(
        [pad, pad, pad + Wp, pad + Hp], radius=_rad(Wp), fill=track)

    frac = max(0.0, min(1.0, frac))
    fw = frac * Wp
    if fw >= 1:
        fw = max(fw, Hp)
        shape = Image.new("L", tile.size, 0)
        ImageDraw.Draw(shape).rounded_rectangle(
            [pad, pad, pad + fw, pad + Hp], radius=_rad(fw), fill=255)

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
        # Same cloud position as the rain/snow/thunder variants below, so the
        # cloud doesn't jump up or down when precipitation appears or clears.
        cloud(C, C - R * 0.35, R * 0.85)
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
    gsrc = None
    if frac > 0.002:
        import math
        tw = int(thick * gs)
        # A nonzero reading always shows a rounded nib rather than a broken-looking
        # sliver, and both ends of the arc are rounded off like a real dial.
        eff = max(sweep * frac, max(4.0, thick * 0.9))
        end = start + eff
        cxt = cyt = size * gs / 2.0
        Rr = (box[2] - box[0]) / 2.0

        def _cap(a):
            ax = cxt + Rr * math.cos(math.radians(a))
            ay = cyt + Rr * math.sin(math.radians(a))
            return [ax - tw / 2, ay - tw / 2, ax + tw / 2, ay + tw / 2]

        # Glow source drawn at the supersampled scale (same coords), but blurred
        # at OUTPUT resolution below — a 1.8*SS blur on the downscaled image looks
        # identical to a 1.8*gs blur on the 4x image and costs a quarter as much.
        gsrc = Image.new("RGBA", tile.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(gsrc)
        gd.arc(box, start, end, fill=(*color, 190), width=tw)
        gd.ellipse(_cap(start), fill=(*color, 190))
        gd.ellipse(_cap(end), fill=(*color, 190))
        fd = ImageDraw.Draw(tile)
        fd.arc(box, start, end, fill=(*color, 255), width=tw)
        fd.ellipse(_cap(start), fill=(*color, 255))
        fd.ellipse(_cap(end), fill=(*color, 255))

    out = tile.resize((size * SS, size * SS), Image.BOX)
    if gsrc is not None:
        glow = gsrc.resize((size * SS, size * SS), Image.BOX).filter(
            ImageFilter.GaussianBlur(1.8 * SS))
        base = Image.new("RGBA", out.size, (0, 0, 0, 0))
        base.alpha_composite(glow)
        base.alpha_composite(out)
        out = base
    img.alpha_composite(out, (int((cx - size / 2) * SS), int((cy - size / 2) * SS)))

def core_strip(img, x, y, w, h, loads):
    """One mini column per logical core. Twenty of them make a texture that a
    single averaged CPU line can't: you can see one pinned core vs. an even
    spread at a glance."""
    n = max(1, len(loads))
    gap = 2.0 if n <= 64 else 1.0   # tighten so a 128/192-thread CPU still fits
    bw = max(1.0, (w - gap * (n - 1)) / n)
    X, Y = int(x * SS), int(y * SS)
    Wp, Hp = int(w * SS), int(h * SS)
    tile = Image.new("RGBA", (Wp + 4 * SS, Hp + 4 * SS), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    o = 2 * SS
    rad = min(bw, 3) * SS / 2

    for i, pct in enumerate(loads):
        bx = o + i * (bw + gap) * SS
        td.rounded_rectangle([bx, o, bx + bw * SS, o + Hp], radius=rad, fill=TRACK)
        f = max(0.0, min(1.0, pct / 100))
        # Every core keeps a dim stub so an idle strip reads as low load, not as
        # an empty box; it brightens and rises with the core's load.
        # stub floor follows bar width only for thin bars (capped at 25%), so on
        # a few-core CPU the column height still encodes load, not just the alpha
        fh = min(Hp, max(f * Hp, Hp * 0.10, min(bw * SS * 0.8, Hp * 0.25)))
        col = state_color(pct, ACCENT)
        alpha = int(90 + 145 * f)
        td.rounded_rectangle([bx, o + Hp - fh, bx + bw * SS, o + Hp],
                             radius=rad, fill=(*col, alpha))

    img.alpha_composite(tile.filter(ImageFilter.GaussianBlur(1.0 * SS)), (X - o, Y - o))
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
    if Wp < 1 or Hp < 1:
        return
    tile = Image.new("RGBA", (Wp, Hp), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)

    def bar(x1, fill):
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

def fmt_mmss(micros):
    """microseconds -> m:ss."""
    s = max(0, int(micros) // 1_000_000)
    return f"{s // 60}:{s % 60:02d}"

_SPOTIFY_BOX = {"box": None}

def media_glyph(d, cx, cy, kind, r, col):
    """Draw a transport control (prev/play/pause/next) centred on (cx, cy) with
    half-size r, in logical px (scaled by SS). Vector shapes, so no glyph font
    is needed."""
    s = SS
    x, y = cx * s, cy * s
    rr = r * s
    if kind == "play":
        d.polygon([(x - rr * 0.6, y - rr), (x - rr * 0.6, y + rr),
                   (x + rr * 0.9, y)], fill=col)
    elif kind == "pause":
        bw = rr * 0.42
        d.rectangle([x - rr * 0.7, y - rr, x - rr * 0.7 + bw, y + rr], fill=col)
        d.rectangle([x + rr * 0.7 - bw, y - rr, x + rr * 0.7, y + rr], fill=col)
    elif kind in ("next", "previous"):
        sgn = 1 if kind == "next" else -1
        tw = rr * 0.9
        for k in (0, 1):                           # two stacked triangles = skip
            bx = x + sgn * (k * tw - tw * 0.5)
            d.polygon([(bx, y - rr), (bx, y + rr), (bx + sgn * tw, y)], fill=col)
    elif kind == "shuffle":
        w_ = max(2, int(1.7 * s))
        # two crossing arrows pointing right
        d.line([(x - rr, y - rr * 0.6), (x + rr * 0.55, y + rr * 0.6)], fill=col, width=w_)
        d.line([(x - rr, y + rr * 0.6), (x + rr * 0.55, y - rr * 0.6)], fill=col, width=w_)
        for sy in (1, -1):
            ex, ey = x + rr * 0.55, y + sy * rr * 0.6
            d.polygon([(ex + rr * 0.45, ey), (ex - rr * 0.1, ey - sy * rr * 0.5),
                       (ex - rr * 0.2, ey + sy * rr * 0.05)], fill=col)
    elif kind in ("repeat", "repeat_one"):
        import math
        w_ = max(2, int(1.7 * s))
        bbox = [x - rr, y - rr * 0.8, x + rr, y + rr * 0.8]
        d.arc(bbox, 305, 215, fill=col, width=w_)   # open loop ring
        a = math.radians(305)
        ex, ey = x + rr * math.cos(a), y + rr * 0.8 * math.sin(a)
        d.polygon([(ex + rr * 0.3, ey - rr * 0.1), (ex - rr * 0.35, ey - rr * 0.35),
                   (ex - rr * 0.15, ey + rr * 0.3)], fill=col)
        if kind == "repeat_one":
            d.ellipse([x - rr * 0.2, y - rr * 0.2, x + rr * 0.2, y + rr * 0.2], fill=col)

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

def _busiest_iface():
    """The non-loopback, non-virtual interface carrying the most traffic — a
    last-resort guess when there is no IPv4 default route (a tun/wg VPN whose
    default route has no gateway flag, or an IPv6-only host)."""
    best, best_bytes = None, -1
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                name, _, rest = line.partition(":")
                name = name.strip()
                if not rest or name == "lo" or name.startswith(
                        ("docker", "veth", "br-", "virbr", "vnet")):
                    continue
                p = rest.split()
                total = int(p[0]) + int(p[8])
                if total > best_bytes:
                    best, best_bytes = name, total
    except (OSError, ValueError, IndexError):
        pass
    return best

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
        # No IPv4 default route: IPv6 default route, else the busiest iface.
        try:
            with open("/proc/net/ipv6_route") as f:
                for line in f:
                    fl = line.split()
                    if len(fl) >= 10 and fl[1] == "00" and fl[0] == "0" * 32:
                        if fl[9] not in ("lo",):
                            return fl[9]
        except (OSError, IndexError):
            pass
        return _busiest_iface()
    return _detect("iface", find)

def _iface_ipv4(ifname):
    """The interface's own IPv4 address, via SIOCGIFADDR — no external tool."""
    if not ifname:
        return None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            return socket.inet_ntoa(fcntl.ioctl(
                s.fileno(), 0x8915,
                struct.pack("256s", ifname.encode()[:15]))[20:24])
        finally:
            s.close()
    except OSError:
        return None

def _active_vpn_iface():
    """Name of an up tun/wg/ppp/tap interface, if a VPN/tunnel is connected."""
    try:
        for n in sorted(os.listdir("/sys/class/net")):
            if n.startswith(("tun", "wg", "ppp", "tap")):
                if read_first(f"/sys/class/net/{n}/operstate", default="") in (
                        "up", "unknown"):
                    return n
    except OSError:
        pass
    return None

_NETLINK = {"t": -1e9, "data": {}}

def _wifi_signal(ifname):
    """(link_quality_percent, ssid) for a wireless interface. Quality comes from
    /proc/net/wireless (no tool); SSID from iwgetid/nmcli when present."""
    pct = None
    try:
        with open("/proc/net/wireless") as f:
            for line in f.readlines()[2:]:
                if line.split(":", 1)[0].strip() == ifname:
                    link = float(line.split()[2].rstrip("."))
                    pct = max(0, min(100, round(link / 70.0 * 100)))
                    break
    except (OSError, ValueError, IndexError):
        pass
    ssid = None
    for cmd in (["iwgetid", "-r", ifname], ["iwgetid", "-r"]):
        if shutil.which(cmd[0]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=2).stdout.strip()
                if out:
                    ssid = out
                    break
            except Exception:
                pass
    return pct, ssid

def net_link_info():
    """Describes the active connection beyond throughput: {iface, kind, ip, ssid,
    signal, vpn}. kind is 'wifi' | 'ethernet' | 'vpn'. Cached a few seconds so the
    SSID lookup never runs on the render path every tick."""
    now = time.time()
    if now - _NETLINK["t"] < 5:
        return _NETLINK["data"]
    _NETLINK["t"] = now
    iface = net_iface()
    vpn = _active_vpn_iface()
    is_wifi = bool(iface) and os.path.isdir(f"/sys/class/net/{iface}/wireless")
    if iface and iface.startswith(("tun", "wg", "ppp", "tap")):
        kind = "vpn"
    elif is_wifi:
        kind = "wifi"
    else:
        kind = "ethernet"
    ssid = signal = None
    if is_wifi:
        signal, ssid = _wifi_signal(iface)
    info = {"iface": iface, "kind": kind, "ip": _iface_ipv4(iface),
            "ssid": ssid, "signal": signal, "vpn": vpn}
    _NETLINK["data"] = info
    return info

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
            devs.append({"id": "upower:" + _periph_short(name), "name": name,
                         "capacity": d["cap"],
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

def _periph_short(name):
    """A tidier default label for a peripheral: drop the Bluetooth-LE 'LE_'
    advertised-name prefix and a trailing '-battery', so 'LE_WH-1000XM3' shows
    as 'WH-1000XM3'. Custom names (peripheral_names) override this."""
    n = (name or "").strip()
    if n[:3].upper() == "LE_":
        n = n[3:]
    if n.lower().endswith("-battery"):
        n = n[:-len("-battery")]
    return n.strip() or "device"

def _periph_name_from_id(pid):
    """Recover a display name from a peripheral id, so a selected device that is
    currently disconnected (and absent from the live list) can still be shown."""
    return pid[len("upower:"):] if pid.startswith("upower:") else pid

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
    # Entries in /sys/block are whole devices by definition, so counting only
    # those sums each disk once and never double-counts a partition. This also
    # fixes eMMC/SD (mmcblk0), md/RAID (md0) and network block (nbd0/rbd0)
    # disks, whose names end in a digit and so were wrongly skipped as if they
    # were partitions.
    try:
        whole = set(os.listdir("/sys/block"))
    except OSError:
        whole = set()
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                fl = line.split()
                name = fl[2]
                if name.startswith(("loop", "ram", "dm-", "zram")):
                    continue
                if whole:
                    if name not in whole:
                        continue
                else:
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
        if name in ("k10temp", "zenpower"):
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
    if c in ("coretemp", "cpu_thermal", "x86_pkg_temp") or "package id" in t or "x86_pkg" in t:
        if t.startswith("core "):
            return "CPU Core " + t.split()[-1]
        return "CPU"
    if c in ("k10temp", "k8temp", "zenpower"):
        if "ccd" in t:
            return "CPU " + (lab or "").upper()
        return "CPU"
    if "tctl" in t or "tdie" in t or t == "cpu":
        return "CPU"
    if c == "nvme":
        return "SSD" + (f" {lab}" if t and t != "composite" else "")
    if c == "drivetemp" or "drive" in t or "disk" in t:
        return "Disk"
    if c in ("amdgpu", "radeon"):
        if "junction" in t:
            return "GPU Junction"
        if "mem" in t:
            return "GPU Memory"
        return "GPU"
    if c in ("nouveau", "nvidia"):
        return "GPU"
    if c.startswith(("iwlwifi", "ath", "mt79", "mt76", "rtw", "mwifiex", "brcm")) \
            or "wifi" in t or "wlan" in t:
        return "WiFi"
    if c == "acpitz" or "acpi" in c:
        return "System"
    if "pch" in c:
        return "Chipset"
    if c.startswith(("nct6", "it87", "it8", "f718", "w836", "nzxt")):
        return (lab or chip)[:12]
    return (lab or chip)[:12]

_SENSORS_CACHE = {"t": 0.0, "v": None}

def list_sensors():
    """Every readable temperature sensor as {id, label, full, path, warn, crit}.
    id (chip + input) is stable across runs so a selection can be saved; label
    is short for the panel, full is descriptive for the settings window. Cached
    for 30s — the hwmon set never changes while running, and this is called from
    the render path every frame when sensors are configured."""
    now = time.time()
    if _SENSORS_CACHE["v"] is not None and now - _SENSORS_CACHE["t"] < 30:
        return _SENSORS_CACHE["v"]
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
    _SENSORS_CACHE.update(t=now, v=out)
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
_CMD_ATTEMPT = {}

def _refresh_cached(name, argv, path, tmp):
    proc = None
    try:
        # own session so a timeout can kill the whole pipeline — `sh -c
        # 'curl … | jq'` leaves curl running otherwise, piling up every tick.
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True,
                                errors="replace", start_new_session=True)
        out, _ = proc.communicate(timeout=30)
        with open(tmp, "w") as f:
            f.write(out or "")
        os.replace(tmp, path)
    except Exception:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
                proc.communicate(timeout=2)
            except Exception:
                pass
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
    if ok_prefix and not cached.startswith(ok_prefix):
        # empty output ("") is a failure too — retry on the short ttl, not the full one
        ttl = min(ttl, fail_ttl)
    age = time.time() - os.path.getmtime(path) if os.path.exists(path) else 1e9
    key_changed = key is not None and (read_first(f"{path}.key", default="") or "") != str(key)
    if age > ttl or key_changed:
        now = time.time()
        # back off: don't respawn a command that keeps failing (its cache file
        # never updates, so age stays > ttl) on every single tick.
        if not key_changed and now - _CMD_ATTEMPT.get(name, 0.0) < min(ttl, 10):
            return cached
        with _INFLIGHT_LOCK:
            busy = name in _INFLIGHT
            if not busy:
                _INFLIGHT.add(name)
        if not busy:
            _CMD_ATTEMPT[name] = now
            if key is not None:
                try:
                    with open(f"{path}.key", "w") as f:
                        f.write(str(key))
                except OSError:
                    pass
            try:
                threading.Thread(target=_refresh_cached, daemon=True,
                                 args=(name, argv, path, f"{path}.tmp")).start()
            except Exception:
                with _INFLIGHT_LOCK:
                    _INFLIGHT.discard(name)
        if key_changed:
            return ""
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

PANEL_DISPLAY_KEYS = ("top", "bottom", "left", "right", "units", "disks",
                      "sensors", "peripherals", "sections", "order",
                      "weather_show_location", "theme", "accent", "opacity",
                      "radius", "border", "glow", "bg_mode", "bg_image", "bg_blur",
                      "bg_scale", "bg_tile", "horizontal", "contrast", "stretch", "ui_scale",
                      "colors", "text_scale",
                      "clock_24h", "clock_seconds", "clock_date", "clock_date_format")

def panel_box(cfg, wa):
    """Resolve a panel's on-screen box from its four edge margins.

    top/bottom/left/right are each a gap from that edge of the work area `wa`.
    top+bottom set the height (and vertical position); left+right, when both
    are given, set the width — so the width is the user's to choose and never
    follows the content. When left/right are not both set the box keeps the
    native panel width, anchored to whichever side is given (right by default).
    Returns (x, y, w, h, margins) with margins the resolved {top,bottom,left,
    right}."""
    def _int(v, dflt):
        try:
            return int(v)
        except (TypeError, ValueError):
            return int(dflt)
    _vm = cfg.get("vmargin", MARGIN)
    top = _int(cfg.get("top", _vm), MARGIN) if cfg.get("top") is not None else _int(_vm, MARGIN)
    bottom = _int(cfg.get("bottom", _vm), MARGIN) if cfg.get("bottom") is not None else _int(_vm, MARGIN)
    left = cfg.get("left")
    right = cfg.get("right", cfg.get("hmargin", MARGIN))
    h = max(120, wa.height - top - bottom)
    if left is not None and right is not None:
        left, right = int(left), int(right)
        span = wa.width - left - right
        if span >= W:
            w = span
        else:
            w = W
            if right <= left:
                left = max(0, wa.width - w - right)
            else:
                right = max(0, wa.width - w - left)
    else:
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

def _coerce_panel(q):
    """Make a panel config render-safe even if hand-edited or imported with wrong
    types: cast numeric keys, clamp fractions, validate the accent triple. Bad
    values are dropped (fall back to theme/defaults) rather than crashing the
    render tick with int(None)/tuple(str)/float('x')."""
    def _int(k):
        if q.get(k) is None:
            return
        try:
            q[k] = int(q[k])
        except (TypeError, ValueError):
            q.pop(k, None)
    def _frac(k, lo=0.0, hi=3.0):
        if q.get(k) is None:
            return
        try:
            q[k] = max(lo, min(hi, float(q[k])))
        except (TypeError, ValueError):
            q.pop(k, None)
    for k in ("top", "bottom", "left", "right", "stretch", "radius", "bg_blur", "monitor"):
        _int(k)
    if isinstance(q.get("monitor"), int) and q["monitor"] < 0:
        q["monitor"] = 0
    for k in ("opacity", "border", "glow", "contrast", "ui_scale"):
        _frac(k)
    _frac("text_scale", 0.5, 2.0)
    acc = q.get("accent")
    if acc is not None:
        try:
            acc = [int(c) for c in acc]
            q["accent"] = acc[:3] if len(acc) == 3 and all(0 <= c <= 255 for c in acc) else None
        except (TypeError, ValueError):
            q["accent"] = None
    if not isinstance(q.get("theme"), (str, type(None))):
        q.pop("theme", None)
    # Per-role colour overrides: a dict of role -> [r,g,b]. Drop invalid roles
    # and malformed triples rather than letting a bad value reach the palette.
    cols = q.get("colors")
    if cols is not None:
        clean = {}
        if isinstance(cols, dict):
            for role, rgb in cols.items():
                if role not in _COLOR_ROLE_KEYS:
                    continue
                try:
                    trip = [int(c) for c in rgb]
                except (TypeError, ValueError):
                    continue
                if len(trip) == 3 and all(0 <= c <= 255 for c in trip):
                    clean[role] = trip
        q["colors"] = clean or None
    return q

def _coerce_modules(mods):
    """Top-level custom modules, validated so a bad imported entry can't crash."""
    out = []
    if not isinstance(mods, list):
        return out
    for m in mods:
        if not isinstance(m, dict) or not isinstance(m.get("command"), str):
            continue
        try:
            iv = int(m.get("interval") or 10)
        except (TypeError, ValueError):
            iv = 10
        out.append({"id": str(m.get("id") or ("m" + os.urandom(4).hex())),
                    "label": str(m.get("label") or ""),
                    "command": m["command"],
                    "interval": max(1, iv),
                    "type": m.get("type") if m.get("type") in ("text", "number", "bar", "json") else "text"})
    return out

def _coerce_module_ids(mi, valid_ids):
    """A panel's per-panel module selection: an ordered list of module ids.
    Absent/non-list means a pre-feature config, which showed every module — so
    default to all defined ids in definition order (keeps old setups looking the
    same). Otherwise keep the panel's order but drop unknown ids and duplicates."""
    valid = list(valid_ids)
    if not isinstance(mi, list):
        return valid
    vset, seen, out = set(valid), set(), []
    for x in mi:
        x = str(x)
        if x in vset and x not in seen:
            seen.add(x)
            out.append(x)
    return out

def _read_settings_file():
    """(data, corrupt): data is the parsed dict or None; corrupt is True when the
    file exists but could not be parsed (vs simply missing/empty)."""
    try:
        with open(SETTINGS_FILE) as f:
            raw = f.read()
    except OSError:
        return None, False
    if not raw.strip():
        return None, False
    try:
        return json.loads(raw), False
    except Exception:
        return None, True

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
        data, corrupt = _read_settings_file()
        if corrupt:
            # Never silently replace an unparseable config (a stray comma while
            # hand-editing would otherwise lose every panel): back it up first.
            try:
                bak = f"{SETTINGS_FILE}.corrupt-{int(time.time())}"
                if not os.path.exists(bak):
                    shutil.copy2(SETTINGS_FILE, bak)
                log(f"settings.json could not be parsed — backed it up to "
                    f"{os.path.basename(bak)} and loaded defaults")
            except OSError:
                pass
        if not isinstance(data, dict):
            data = {}
        if "location" not in data:
            legacy = load_json(WEATHER_FILE, None)
            if isinstance(legacy, dict) and "lat" in legacy:
                data["location"] = legacy
        s = dict(DEFAULT_SETTINGS)
        s.update({k: v for k, v in data.items() if k not in ("sections", "panels")})
        s["sections"] = normalize_sections(data.get("sections"))
        s["order"] = normalize_order(data.get("order"))
        if "vmargin" not in data:
            s["vmargin"] = data.get("margin", 22)
        if "hmargin" not in data:
            s["hmargin"] = data.get("margin", 22)
        # Modules first, so each panel's per-panel selection can be validated
        # against the defined module ids below.
        s["modules"] = _coerce_modules(s.get("modules"))
        _valid_mod_ids = [mm["id"] for mm in s["modules"]]
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
            q["module_ids"] = _coerce_module_ids(q.get("module_ids"), _valid_mod_ids)
            panels.append(_coerce_panel(q))
        s["panels"] = panels
        _SETTINGS, _SETTINGS_MTIME = s, m
    return _SETTINGS

def save_settings(s):
    save_json(SETTINGS_FILE, s)
    global _SETTINGS_MTIME
    _SETTINGS_MTIME = -1.0

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
        if s.get("location"):
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

_IMG_CACHE = {}

def _load_bg_image(path):
    """A background image loaded and oriented, cached by (path, mtime)."""
    if not path or not os.path.isfile(path):
        return None
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    img = _IMG_CACHE.get(key)
    if img is None:
        try:
            img = Image.open(path).convert("RGBA")
        except Exception:
            return None
        maxd = 1600                              # a frosted backing needs no more;
        if max(img.size) > maxd:                 # keeps a 4K wallpaper from pinning
            k = maxd / max(img.size)             # ~33 MB of RGBA per cached image
            img = img.resize((max(1, round(img.width * k)),
                              max(1, round(img.height * k))), Image.LANCZOS)
        if len(_IMG_CACHE) > 3:
            _IMG_CACHE.clear()
        _IMG_CACHE[key] = img
    return img

def _cover(img, w, h):
    """Scale `img` to cover a w x h box, centre-cropped. Rounds up so the scaled
    image never lands a pixel short of the box (which would leave a seam)."""
    iw, ih = img.size
    k = max(w / iw, h / ih)
    nw, nh = max(w, int(iw * k + 0.999)), max(h, int(ih * k + 0.999))
    img = img.resize((nw, nh), Image.LANCZOS)
    x = (nw - w) // 2
    y = (nh - h) // 2
    return img.crop((x, y, x + w, y + h))

def _fit_scaled(img, w, h, zoom, fill):
    """Cover a w x h box with `img`, then multiply that scale by `zoom` and centre
    it. zoom == 1 is a plain cover; zoom > 1 zooms in (centre-crop); zoom < 1 draws
    the image smaller so more of it shows, on a `fill`-coloured ground."""
    iw, ih = img.size
    k = max(w / iw, h / ih) * zoom
    nw, nh = max(1, round(iw * k)), max(1, round(ih * k))
    scaled = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (fill[0], fill[1], fill[2], 255))
    ox, oy = (w - nw) // 2, (h - nh) // 2
    sx, sy = max(0, -ox), max(0, -oy)
    dx, dy = max(0, ox), max(0, oy)
    cw, ch = min(nw - sx, w - dx), min(nh - sy, h - dy)
    if cw > 0 and ch > 0:
        canvas.alpha_composite(scaled.crop((sx, sy, sx + cw, sy + ch)), (dx, dy))
    return canvas

def _tiled(img, w, h, zoom):
    """Fill a w x h box by repeating `img` at cover-scale x `zoom`. The tile grid
    is centred, so a small `zoom` turns the image into a balanced repeating
    pattern rather than leaving a blank border."""
    iw, ih = img.size
    k = max(w / iw, h / ih) * zoom
    tw, th = max(1, round(iw * k)), max(1, round(ih * k))
    tile = img.resize((tw, th), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    x0 = (w - tw) // 2 % tw - tw          # left-most tile origin (<= 0), centred
    y0 = (h - th) // 2 % th - th
    y = y0
    while y < h:
        x = x0
        while x < w:
            sx, sy = max(0, -x), max(0, -y)
            if sx < tw and sy < th:
                region = tile.crop((sx, sy, tw, th))
                canvas.paste(region, (max(0, x), max(0, y)), region)
            x += tw
        y += th
    return canvas

def _vgrad(Wp, Hp, top, bot, mask):
    """A vertical gradient from RGBA `top` to RGBA `bot`, clipped to `mask`."""
    grad = Image.new("RGBA", (1, Hp))
    gp = grad.load()
    for i in range(Hp):
        t = i / max(1, Hp - 1)
        gp[0, i] = tuple(int(a + (b - a) * t) for a, b in zip(top, bot))
    out = Image.new("RGBA", (Wp, Hp), (0, 0, 0, 0))
    out.paste(grad.resize((Wp, Hp)), (0, 0), mask)
    return out

_WALL_COVER = {}

def _wall_region(img, pos, screen, Wp, Hp):
    """The slice of the wallpaper that sits behind a panel at `pos` on a
    `screen`-sized desktop, so the frosted backing lines up with the real
    wallpaper (it looks embedded). The screen-sized cover is cached, so moving
    the panel only re-crops."""
    sw, sh = max(1, int(screen[0] * SS)), max(1, int(screen[1] * SS))
    key = (id(img), sw, sh)
    covered = _WALL_COVER.get(key)
    if covered is None:
        covered = _cover(img, sw, sh)
        if len(_WALL_COVER) > 2:
            _WALL_COVER.clear()
        _WALL_COVER[key] = covered
    x = max(0, min(int(pos[0] * SS), sw - Wp))
    y = max(0, min(int(pos[1] * SS), sh - Hp))
    return covered.crop((x, y, x + Wp, y + Hp))

def panel_bg(H, width=W):
    """The panel backing behind the content, at a given width and height, in the
    active theme's background mode (glass, solid, transparent or image). Built
    once per (width, height, theme) and reused — the blur and image scaling are
    far too costly to redo every frame for something that only changes on a
    resize or a theme change. The cache lookup does no file I/O; the image is
    only loaded on a miss."""
    bg = _BG
    key = (width, H, bg["MODE"], bg["BG"], bg["BG2"], bg["HILITE"], HAIRLINE,
           round(bg["OPACITY"], 3), round(bg["RADIUS"], 1), round(bg["BORDER"], 3),
           round(bg["GLOW"], 3), bg.get("image"), bg.get("blur"),
           round(bg.get("scale") or 1.0, 3), bool(bg.get("tile")),
           bg.get("pos") if bg.get("wall") else None,
           bg.get("screen") if bg.get("wall") else None)
    cached = _PANEL_CACHE.get(key)
    if cached is not None:
        return cached

    Wp, Hp = width * SS, H * SS
    panel = Image.new("RGBA", (Wp, Hp), (0, 0, 0, 0))
    rad = max(0.0, min(bg["RADIUS"], min(width, H) / 2)) * SS
    op = max(0.0, min(1.0, bg["OPACITY"]))
    r0, g0, b0 = bg["BG"]
    r1, g1, b1 = bg["BG2"]

    mask = Image.new("L", (Wp, Hp), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, Wp - 1, Hp - 1], radius=rad, fill=255)

    mode = bg["MODE"]
    bgimg = _load_bg_image(bg.get("image")) if mode == "image" else None
    if mode == "transparent":
        # A faint veil of the theme's own base colour (dark theme -> dark veil,
        # light theme -> light veil) so the content keeps contrast over any
        # wallpaper, while staying clearly see-through. Opacity drives it.
        if op > 0:
            a0, a1 = int(255 * op * 0.68), int(255 * op)
            panel.alpha_composite(_vgrad(Wp, Hp, (r1, g1, b1, a0), (r1, g1, b1, a1), mask))
    elif mode == "image" and bgimg is not None:
        if bg.get("wall") and bg.get("pos") and bg.get("screen"):
            src = _wall_region(bgimg, bg["pos"], bg["screen"], Wp, Hp)
        else:
            z = bg.get("scale")
            z = 1.0 if z is None else max(0.05, min(3.0, float(z)))
            if bg.get("tile"):
                src = _tiled(bgimg, Wp, Hp, z)
            elif abs(z - 1.0) < 1e-3:
                src = _cover(bgimg, Wp, Hp)
            else:
                src = _fit_scaled(bgimg, Wp, Hp, z, bg["BG"])
        blur = bg.get("blur")
        if blur is None:
            blur = 10
        if blur > 0:
            src = src.filter(ImageFilter.GaussianBlur(float(blur) * SS))
        # Veil of the theme's own colour so text keeps contrast over the image.
        # A light theme needs a minimum light veil or its dark text vanishes on a
        # dark photo; a dark theme has no floor, so opacity 100% shows the image
        # fully (white text reads over most pictures, and the user controls it).
        lum = 0.2126 * r1 + 0.7152 * g1 + 0.0722 * b1
        floor = 150 if lum > 140 else 0
        sa = max(int(255 * (1 - op)), floor)
        if sa > 0:
            src = Image.alpha_composite(src.convert("RGBA"),
                                        Image.new("RGBA", (Wp, Hp), (r1, g1, b1, sa)))
        panel.paste(src, (0, 0), mask)
    else:
        base_a = 236 if mode == "solid" else 208
        span_a = 6 if mode == "solid" else 18
        panel.alpha_composite(_vgrad(
            Wp, Hp, (r0, g0, b0, int(base_a * op)),
            (r1, g1, b1, int((base_a + span_a) * op)), mask))

    border = bg["BORDER"]
    if border > 0:
        hair = HAIRLINE
        ba = int((hair[3] if len(hair) > 3 else 24) * border)
        ImageDraw.Draw(panel).rounded_rectangle(
            [0, 0, Wp - 1, Hp - 1], radius=rad,
            outline=(hair[0], hair[1], hair[2], ba), width=max(1, SS))

    glow = bg["GLOW"]
    if glow > 0 and mode != "transparent":
        hc = bg["HILITE"]
        hl = Image.new("RGBA", (Wp, 90 * SS), (0, 0, 0, 0))
        ImageDraw.Draw(hl).rounded_rectangle(
            [SS, SS, Wp - 1 - SS, 60 * SS], radius=max(1, rad - SS),
            fill=(hc[0], hc[1], hc[2], int(12 * glow)))
        hl = hl.filter(ImageFilter.GaussianBlur(6 * SS))
        panel.alpha_composite(hl, (0, 0))

    if len(_PANEL_CACHE) > 8:
        _PANEL_CACHE.clear()
    _PANEL_CACHE[key] = panel
    return panel

def weather_for(loc):
    """Current weather for one location, cached per-location so each panel can
    watch its own city. `loc` is {lat, lon, name}; returns the parsed dict or
    None. The fetch runs weather.py with the coordinates, on a worker thread when
    stale, and the cache key ties the result to that exact location."""
    if not loc:
        return None
    key = json.dumps(loc, sort_keys=True)
    tag = re.sub(r"[^0-9A-Za-z]", "", f"{loc.get('lat')}_{loc.get('lon')}")[:24] or "x"
    raw = cached_cmd("weather_" + tag,
                     [f"{CODE_DIR}/weather.py", str(loc.get("lat")),
                      str(loc.get("lon")), str(loc.get("name", ""))],
                     900, ok_prefix="{", key=key)
    if raw and raw.strip().startswith("{"):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None

_DBUS = {"bus": None}

def _session_bus():
    """The session D-Bus connection, cached. False once it is known unavailable
    (e.g. no session bus), so we stop retrying."""
    if _DBUS["bus"] is None:
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio
            _DBUS["bus"] = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception:
            _DBUS["bus"] = False
    return _DBUS["bus"] or None

def _mpris_name(bus):
    """The bus name of a media player to show — Spotify preferred, else the
    first MPRIS player that is actually playing, else the first one."""
    from gi.repository import Gio, GLib
    try:
        names = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                              "org.freedesktop.DBus", "ListNames", None,
                              GLib.VariantType.new("(as)"),
                              Gio.DBusCallFlags.NONE, 800, None).unpack()[0]
    except Exception:
        return None
    players = [n for n in names if n.startswith("org.mpris.MediaPlayer2.")]
    if not players:
        return None
    for n in players:
        if "spotify" in n.lower():
            return n
    return players[0]

def spotify_state():
    """Now-playing from an MPRIS player (Spotify first). Returns
    {player, title, artist, status, position, length} or None if nothing is
    playing/available. In-process over D-Bus; no subprocess per frame."""
    bus = _session_bus()
    if not bus:
        return None
    from gi.repository import Gio, GLib
    name = _mpris_name(bus)
    if not name:
        return None
    try:   # one GetAll instead of five Gets — fewer blocking round trips
        props = bus.call_sync(name, "/org/mpris/MediaPlayer2",
                              "org.freedesktop.DBus.Properties", "GetAll",
                              GLib.Variant("(s)", ("org.mpris.MediaPlayer2.Player",)),
                              GLib.VariantType.new("(a{sv})"),
                              Gio.DBusCallFlags.NONE, 800, None).unpack()[0]
    except Exception:
        return None
    meta = props.get("Metadata") or {}
    status = props.get("PlaybackStatus") or ""
    pos = props.get("Position") or 0
    shuffle = props.get("Shuffle")
    loop = props.get("LoopStatus")
    artist = meta.get("xesam:artist") or []
    if isinstance(artist, (list, tuple)):
        artist = ", ".join(artist)
    title = meta.get("xesam:title") or ""
    if not title and not artist:
        return None
    return {"player": "Spotify" if "spotify" in name.lower() else "Media",
            "title": str(title), "artist": str(artist),
            "status": status, "position": int(pos),
            "length": int(meta.get("mpris:length") or 0),
            "art": str(meta.get("mpris:artUrl") or ""),
            "shuffle": bool(shuffle) if shuffle is not None else None,
            "loop": str(loop) if loop is not None else None}

def spotify_control(action):
    """Send a command to the current MPRIS player: 'play_pause', 'next',
    'previous', 'shuffle' (toggle) or 'repeat' (cycle off/all/one)."""
    bus = _session_bus()
    if not bus:
        return
    from gi.repository import Gio, GLib
    name = _mpris_name(bus)
    if not name:
        return
    PLAYER = "org.mpris.MediaPlayer2.Player"

    def _get(p):
        try:
            return bus.call_sync(name, "/org/mpris/MediaPlayer2",
                                 "org.freedesktop.DBus.Properties", "Get",
                                 GLib.Variant("(ss)", (PLAYER, p)),
                                 GLib.VariantType.new("(v)"),
                                 Gio.DBusCallFlags.NONE, 800, None).unpack()[0]
        except Exception:
            return None

    def _set(p, variant):
        try:
            bus.call_sync(name, "/org/mpris/MediaPlayer2",
                          "org.freedesktop.DBus.Properties", "Set",
                          GLib.Variant("(ssv)", (PLAYER, p, variant)), None,
                          Gio.DBusCallFlags.NONE, 800, None)
        except Exception:
            pass

    method = {"play_pause": "PlayPause", "next": "Next",
              "previous": "Previous"}.get(action)
    try:
        if method:
            bus.call_sync(name, "/org/mpris/MediaPlayer2", PLAYER, method, None,
                          None, Gio.DBusCallFlags.NONE, 800, None)
        elif action == "shuffle":
            cur = _get("Shuffle")
            _set("Shuffle", GLib.Variant("b", not bool(cur)))
        elif action == "repeat":
            cur = _get("LoopStatus") or "None"
            nxt = {"None": "Playlist", "Playlist": "Track", "Track": "None"}.get(cur, "Playlist")
            _set("LoopStatus", GLib.Variant("s", nxt))
    except Exception:
        pass

_ART_CACHE = {}
_ART_INFLIGHT = set()

def _fetch_art(url, px):
    try:
        if url.startswith("file://"):
            with open(urllib.parse.unquote(url[7:]), "rb") as f:
                data = f.read()
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "linux-mint-hud"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = r.read()
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        n = px * SS
        im = _cover(im, n, n)
        mask = Image.new("L", (n, n), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, n - 1, n - 1], radius=5 * SS, fill=255)
        out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        out.paste(im, (0, 0), mask)
        if len(_ART_CACHE) > 6:
            _ART_CACHE.clear()
        _ART_CACHE[(url, px)] = out
    except Exception:
        _ART_CACHE[(url, px)] = ("fail", time.time())
    finally:
        _ART_INFLIGHT.discard((url, px))

def album_art(url, px):
    """A rounded square cover thumbnail for an MPRIS artUrl, px logical px wide,
    or None until it is fetched (download happens on a worker thread, cached by
    url+size, so the render never blocks)."""
    if not url:
        return None
    hit = _ART_CACHE.get((url, px))
    if isinstance(hit, tuple):      # a cached failure — don't re-fetch for 5 min
        if time.time() - hit[1] < 300:
            return None
        _ART_CACHE.pop((url, px), None)
        hit = None
    if hit is not None:
        return hit
    if (url, px) not in _ART_INFLIGHT:
        _ART_INFLIGHT.add((url, px))
        threading.Thread(target=_fetch_art, args=(url, px), daemon=True).start()
    return None

_ART_BG_CACHE = {}
_ART_BG_INFLIGHT = set()

def _fetch_art_bg(url, w, h, radius):
    try:
        if url.startswith("file://"):
            with open(urllib.parse.unquote(url[7:]), "rb") as f:
                data = f.read()
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "linux-mint-hud"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = r.read()
        im = _cover(Image.open(io.BytesIO(data)).convert("RGBA"), w, h)
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        out.paste(im, (0, 0), mask)
        if len(_ART_BG_CACHE) > 6:
            _ART_BG_CACHE.clear()
        _ART_BG_CACHE[(url, w, h, radius)] = out
    except Exception:
        _ART_BG_CACHE[(url, w, h, radius)] = ("fail", time.time())
    finally:
        _ART_BG_INFLIGHT.discard((url, w, h, radius))

def album_art_bg(url, w, h, radius):
    """A cover-fit album cover sized to a w x h box (already-supersampled px),
    rounded by `radius`, for use as a module background. None until fetched —
    same threaded, cached, fail-cooldown pattern as album_art."""
    if not url or w < 1 or h < 1:
        return None
    key = (url, w, h, radius)
    hit = _ART_BG_CACHE.get(key)
    if isinstance(hit, tuple):      # cached failure — don't re-fetch for 5 min
        if time.time() - hit[1] < 300:
            return None
        _ART_BG_CACHE.pop(key, None)
        hit = None
    if hit is not None:
        return hit
    if key not in _ART_BG_INFLIGHT:
        _ART_BG_INFLIGHT.add(key)
        threading.Thread(target=_fetch_art_bg, args=key, daemon=True).start()
    return None

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
        # first frame, or stale state from disk after a long gap: the saved
        # counters are hours old, so every delta (net, disk, cpu, procs) would be
        # a huge false spike that then sits in the history for an hour. Drop prev
        # so this frame reports zero rates instead.
        elapsed = 2.0
        prev = {}

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
                              ok_prefix=("Session", "Week"))
    _loc = load_settings().get("location")

    # Each "Label pct% (reset)" item becomes (label, pct, reset) — session, week,
    # and any per-model limit (Fable, Opus, …) claude_quota.py reports.
    quota = []
    if claude_quota and not claude_quota.startswith(("web quota", "no quota")):
        for part in [p.strip() for p in claude_quota.split("|")]:
            try:
                lbl = part.split()[0]
                pct = int(part.split("%")[0].split()[-1])
                rest = part.split("(", 1)[1].rstrip(")") if "(" in part else ""
                quota.append((lbl, pct, rest))
            except Exception:
                pass
    have_claude = bool(quota)

    weather = weather_for(_loc)
    have_weather = weather is not None
    # Only touch the media player over D-Bus when a panel actually shows it — a
    # wedged MPRIS client (Chromium's is notorious) otherwise stalls every tick.
    _want_spotify = any((p.get("sections") or {}).get("spotify", False)
                        for p in (load_settings().get("panels") or []))
    spotify = spotify_state() if _want_spotify else None

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
        "quota": quota, "have_claude": have_claude, "weather": weather,
        "have_weather": have_weather, "top_cpu": top_cpu, "top_mem": top_mem,
        "spotify": spotify,
    }

def render(frame=None, cfg=None, target_h=None, flex_in=0.0, width=None,
           write_png=False, stretch=0.0, pos=None, screen=None, ui_scale=1.0):
    """Draw one panel's image from a gathered frame and a panel config, and
    return (image, next_flex). The panel is drawn natively at `width` px wide
    (default the standard width) — so the user's chosen width never scales the
    content and never couples to it — with the flex fill taking the height to
    `target_h`. Each panel picks its own sections, order, units and selections
    out of cfg; the flex converges over two draws and is per-panel state the
    caller keeps."""
    global FLEX, FLEX_POINTS, TEXT_SCALE
    W = int(width) if width else globals()["W"]
    CW = W - 2 * PAD
    M = frame if frame is not None else gather_frame()
    _s = load_settings()
    cfg = cfg or {}
    # per-panel text-size multiplier (clamped), applied to every font via F()
    _ts = cfg.get("text_scale")
    if _ts is None:
        _ts = _s.get("text_scale")
    try:
        TEXT_SCALE = max(0.75, min(1.4, float(_ts))) if _ts is not None else 1.0
    except (TypeError, ValueError):
        TEXT_SCALE = 1.0
    apply_theme(resolve_theme(cfg, _s))
    SECTIONS = cfg.get("sections") or _s.get("sections")
    UNITS = cfg.get("units") or _s.get("units", "c")
    WEATHER_UNITS = cfg.get("weather_units") or _s.get("weather_units", "c")
    SHOW_LOC = cfg.get("weather_show_location", _s.get("weather_show_location", True))
    SENSOR_NAMES = _s.get("sensor_names") or {}
    PERIPH_NAMES = _s.get("peripheral_names") or {}
    GAUGE_NAMES = _s.get("gauge_names") or {}
    disks_sel = cfg.get("disks", _s.get("disks"))
    sensors_sel = cfg.get("sensors", _s.get("sensors"))
    periph_sel_cfg = cfg.get("peripherals", _s.get("peripherals"))
    order_cfg = cfg.get("order") or _s.get("order") or SECTION_ORDER
    _HORIZ = bool(cfg.get("horizontal"))
    (cpu_pct, core_loads, gpu_pct, have_gpu, mem_used, mem_total, swap_total,
     swap_used, disk_used, disk_total, rd, wr, down, up, cap, bstatus, batt_w,
     eta, batt_v, watts, ac_w, power_src, cpu_t, nvme_t, wifi_t, uptime, load,
     quota, have_claude, weather, have_weather, top_cpu, top_mem, spotify) = (
        M["cpu_pct"], M["core_loads"], M["gpu_pct"], M["have_gpu"], M["mem_used"],
        M["mem_total"], M["swap_total"], M["swap_used"], M["disk_used"],
        M["disk_total"], M["rd"], M["wr"], M["down"], M["up"], M["cap"],
        M["bstatus"], M["batt_w"], M["eta"], M["batt_v"], M["watts"], M["ac_w"],
        M["power_src"], M["cpu_t"], M["nvme_t"], M["wifi_t"], M["uptime"],
        M["load"], M["quota"], M["have_claude"], M["weather"],
        M["have_weather"], M["top_cpu"], M["top_mem"], M.get("spotify"))
    # Weather location(s): a panel can rotate through several cities. Per-panel
    # setting wins, else the global one; a single legacy "location" counts as a
    # list of one. The active city cycles by wall-clock time.
    def _as_locs(c):
        ls = c.get("locations")
        if isinstance(ls, list) and ls:
            return [L for L in ls if isinstance(L, dict) and L.get("lat") is not None]
        one = c.get("location")
        return [one] if isinstance(one, dict) and one.get("lat") is not None else []
    _wlocs = _as_locs(cfg) or _as_locs(_s)
    if _wlocs:
        _rot = max(2, int(cfg.get("weather_rotate") or _s.get("weather_rotate") or 8))
        for _L in _wlocs:
            weather_for(_L)              # warm every city's cache so rotation is instant
        _n = len(_wlocs)
        _now = time.time()
        # When weather has its own section it's shown continuously, so rotate by
        # the chosen interval. When it SHARES the top slot with Claude it's only
        # on screen in alternating windows; rotating by the same clock would alias
        # (the weather window always lands on one city) — advance once per weather
        # window (every 2 x ALT_PERIOD) instead.
        _ded = SECTIONS.get("weather", True) and "weather" in order_cfg
        _cidx = (int(_now / _rot) if (_ded or _n <= 1)
                 else int(_now // (2 * ALT_PERIOD))) % _n
        _active = _wlocs[_cidx]
        weather = weather_for(_active)
        have_weather = weather is not None
        if _active.get("units") in ("c", "f"):   # e.g. US cities default to °F
            WEATHER_UNITS = _active["units"]
    _SPOTIFY_BOX["box"] = None
    FLEX = float(flex_in or 0.0)
    FLEX_POINTS = 0
    surf_h = max(1400, min(int(max(0.0, stretch)) + 1600, 8000))
    img = Image.new("RGBA", (W * SS, surf_h * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 22
    R = W - PAD

    f_val    = F(MONO_MED, T_VALUE)
    f_val_sm = F(MONO_REG, T_BODY)
    f_big    = F(MONO_LIGHT, T_LEAD)

    SLOT_H = 95

    def draw_claude(y):
        if _HORIZ:
            label(d, PAD, y, "claude", STEEL, tracking=2.4)
            y += 16
            items = quota[:3]
            lw = max((measure(F(UI_SEMI, T_MICRO), lbl.upper(), 1.6) / SS
                      for lbl, _, _ in items), default=0)
            for lbl, pct, resets in items:
                label(d, PAD, y + 1, lbl.lower(), CORAL, size=T_MICRO)
                text(d, PAD + lw + 10 + measure(f_val, "100%") / SS, y - 2, f"{pct}%",
                     f_val, CORAL, anchor="r")
                bar(img, PAD + lw + 10 + measure(f_val, "100%") / SS + 12, y + 2, 84, 5,
                    pct / 100, ramp=True)
                y += 16
            return y
        label(d, PAD, y, "claude", STEEL, tracking=2.4)
        y += 22
        # session, week, then any per-model limit (Fable, …) — compact rows so up
        # to three fit the slot. One row if only the weekly figure is available.
        items = quota[:3]
        # two items get roomy rows, three get tight ones, so it always fits SLOT_H
        row = 24 if len(items) >= 3 else 36
        bh = 6 if len(items) >= 3 else 8
        f_pct = F(MONO_MED, T_VALUE)
        for lbl, pct, resets in items:
            label(d, PAD, y + 3, lbl.lower(), CORAL, size=T_MICRO)
            text(d, R, y, f"{pct}%", f_pct, CORAL, anchor="r")
            if resets:
                rt = resets if resets.startswith("in ") else f"resets {resets}"
                text(d, R - measure(f_pct, f"{pct}%") / SS - 11, y + 3,
                     rt, F(UI_MED, T_BODY), TEXT, anchor="r")
            y += row - bh - 10
            bar(img, PAD, y, CW, bh, pct / 100, ramp=True)
            y += bh + 10

    def draw_weather(y0):
        if _HORIZ:
            # Compact: the temperature leads on the left with a small
            # min/max/feels line (↓ low · ↑ high · ≈ feels) below it, and the
            # weather icon sits off to the RIGHT where it has room — clear of the
            # "WEATHER" heading, which it used to clip when it sat top-left.
            # Pinned to a fixed content width (_wbox) so a city/temperature
            # change never reflows the whole bar.
            _wbox = 210
            icon_s = 14
            _ihw = int(icon_s * 1.7)            # icon half-width/height
            icx = _wbox - _ihw - 6              # icon centre, right-hand zone
            label(d, PAD, y0, "weather", STEEL, tracking=2.4)
            if SHOW_LOC and weather.get("name"):
                _nx = PAD + measure(F(UI_SEMI, T_LABEL), "WEATHER", 2.4) / SS + 12
                _navail = (icx - _ihw) - _nx - 6   # keep the name clear of the icon
                _nm, _nf = weather["name"], F(UI_SEMI, T_MICRO)
                if measure(_nf, _nm.upper(), 1.6) / SS > _navail:
                    while len(_nm) > 1 and measure(_nf, (_nm + "…").upper(), 1.6) / SS > _navail:
                        _nm = _nm[:-1]
                    _nm += "…"
                label(d, _nx, y0, _nm, TEXT, size=T_MICRO)
            _top = y0 + 17
            ttxt = temp_str(weather["temp"], WEATHER_UNITS) + WEATHER_UNITS.upper()
            text(d, PAD, _top, ttxt, F(MONO_LIGHT, T_LEAD), TEXT)
            _mmy = _top + 24
            f_dv = F(MONO_REG, T_BODY)
            f_dl = F(UI_SEMI, T_MICRO)
            gx = PAD
            for sym, tval in (("↓", weather['lo']), ("↑", weather['hi']),
                              ("≈", weather['feels'])):
                val = temp_str(tval, WEATHER_UNITS)
                text(d, gx, _mmy, sym, f_dl, MUTE)
                sw = measure(f_dl, sym) / SS
                text(d, gx + sw + 4, _mmy - 1, val, f_dv, weather_temp_color(tval))
                gx += sw + 4 + measure(f_dv, val) / SS + 12
            _bot = _mmy + 14
            # icon on the right, vertically centred over the temp + min/max block
            draw_weather_icon(img, icx, (_top + _bot) / 2.0, icon_s, weather.get("code", 3))
            # near-invisible pixel at the box edge pins the trimmed column width
            d.point((int(_wbox * SS), int(_bot * SS)), fill=(255, 255, 255, 1))
            return _bot
        label(d, PAD, y0, "weather", STEEL, tracking=2.4)
        if SHOW_LOC and weather.get("name"):
            label_r(d, R, y0, weather["name"], TEXT, size=T_MICRO)
        t = weather["temp"]
        f_temp = F(MONO_LIGHT, T_HERO)
        icon_s = 22
        ttxt = temp_str(t, WEATHER_UNITS) + WEATHER_UNITS.upper()
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
        # The top slot alternates Claude quota and weather — but don't repeat a
        # feed that the user has also given its own dedicated section, or it would
        # render twice. Mirror the dispatch loop's SECTIONS.get(key, True).
        show_claude = have_claude and not SECTIONS.get("claude", True)
        show_weather = have_weather and not SECTIONS.get("weather", True)
        if show_claude and show_weather:
            which = "weather" if int(time.time() // ALT_PERIOD) % 2 else "claude"
        elif show_claude:
            which = "claude"
        elif show_weather:
            which = "weather"
        else:
            return y
        ny = (draw_claude if which == "claude" else draw_weather)(y)
        if _HORIZ:
            return ny + gap(8)
        return y + SLOT_H + gap(26)

    def sec_claude(y):
        if not have_claude:
            return y
        ny = draw_claude(y)
        if _HORIZ:
            return ny + gap(8)
        return y + SLOT_H + gap(26)

    def sec_weather(y):
        if not have_weather:
            return y
        ny = draw_weather(y)
        if _HORIZ:
            return ny + gap(8)
        return y + SLOT_H + gap(26)

    def sec_load(y):
        if _HORIZ:
            label(d, PAD, y, "uptime", STEEL)
            text(d, PAD + measure(F(UI_SEMI, T_LABEL), "UPTIME", 1.6) / SS + 12, y - 2,
                 fmt_dur(uptime), f_val, TEXT)
            y += 16
            label(d, PAD, y, "load", STEEL)
            f_load = F(MONO_REG, T_VALUE)
            lx = PAD + measure(F(UI_SEMI, T_LABEL), "LOAD", 1.6) / SS + 12
            for v in load:
                try:
                    lv = float(v)
                except (TypeError, ValueError):
                    lv = 0.0
                text(d, lx, y - 2, v, f_load, load_color(lv, len(core_loads) or 1))
                lx += measure(f_load, v) / SS + 9
            return y + 14 + gap(8)
        label(d, PAD, y, "uptime", STEEL)
        label_r(d, R, y, "load 1·5·15m", STEEL)
        y += 14
        text(d, PAD, y, fmt_dur(uptime), f_big, TEXT)
        f_load = F(MONO_REG, T_VALUE)
        lx = R
        for v in reversed(load):
            try:
                lv = float(v)
            except (TypeError, ValueError):
                lv = 0.0
            text(d, lx, y + 6, v, f_load, load_color(lv, len(core_loads) or 1),
                 anchor="r")
            lx -= measure(f_load, v) / SS + 9
        return y + gap(40)

    def sec_gauges(y):
        if _HORIZ:
            # Compact inline readouts instead of the big rings: a themed label,
            # the percent, and a short thin bar, stacked into a narrow column.
            gs = [("cpu", cpu_pct, ACCENT)]
            if have_gpu:
                gs.append(("gpu", gpu_pct, VIOLET))
            gs.append(("ram", mem_used / mem_total * 100, TEAL))
            f_l = F(UI_SEMI, T_LABEL)
            lw = max(measure(f_l, (GAUGE_NAMES.get(n) or n).upper(), 1.6) / SS
                     for n, _, _ in gs)
            valr = PAD + lw + 10 + measure(f_val, "100%") / SS
            barx = valr + 12
            for name, pct, hue in gs:
                col = state_color(pct, hue)
                disp = GAUGE_NAMES.get(name) or name
                label(d, PAD, y, disp, hue)
                text(d, valr, y - 2, f"{pct:.0f}%", f_val, col, anchor="r")
                bar(img, barx, y + 2, 92, 5, pct / 100, col)
                y += 17
            return y + gap(8)
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
            disp = (GAUGE_NAMES.get(name) or name)
            lw = measure(F(UI_SEMI, T_LABEL), disp.upper(), 1.8) / SS
            label(d, cx - lw / 2, cy + gr + 10, disp, hue, tracking=1.8)
        return y + 2 * gr + gap(36)

    def sec_cores(y):
        if _HORIZ:
            label(d, PAD, y, "cores", STEEL)
            lx = PAD + measure(F(UI_SEMI, T_LABEL), "CORES", 1.6) / SS + 12
            tt = f"{len(core_loads)}t"
            text(d, lx, y, tt, F(MONO_REG, T_BODY), TEXT)
            lx += measure(F(MONO_REG, T_BODY), tt) / SS + 10
            ghz = cpu_freq_ghz()
            if ghz > 0:
                text(d, lx, y, f"{ghz:.2f} GHz", F(MONO_REG, T_BODY), TEXT)
            y += 15
            core_strip(img, PAD, y, 168, 20, core_loads)
            return y + 20 + gap(8)
        label(d, PAD, y, "cores", STEEL)
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
        if _HORIZ:
            label(d, PAD, y, "history", STEEL)
            text(d, PAD + measure(F(UI_SEMI, T_LABEL), "HISTORY", 1.6) / SS + 12, y,
                 "60 min", F(UI_MED, T_MICRO), MUTE)
            y += 15
            histogram(img, PAD, y, 180, 32, "cpu", ACCENT, floor=10, clamp=100,
                      overlay="gpu" if have_gpu else None, overlay_color=VIOLET)
            y += 32
            if have_gpu:
                y += 5
                lx = PAD
                for col, txt in ((ACCENT, "cpu"), (VIOLET, "gpu")):
                    swatch(d, lx, y, col)
                    text(d, lx + 12, y, txt, F(UI_MED, T_LABEL), col)
                    lx += 12 + measure(F(UI_MED, T_LABEL), txt) / SS + 14
                y += 12
            return y + gap(8)
        label(d, PAD, y, "history", STEEL)
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
        if _HORIZ:
            # Compact: the temps inline, left-aligned just past the label.
            label(d, PAD, y, "thermals", STEEL)
            gx = PAD + measure(F(UI_SEMI, T_LABEL), "THERMALS", 1.6) / SS + 12
            for labu, lw, val, vw, col in groups:
                text(d, gx, y + 1, labu, f_tl, TEXT, tracking=1.4)
                text(d, gx + lw + 6, y - 1, val, f_tv, col)
                gx += lw + 6 + vw + 16
            return y + gap(12)
        label(d, PAD, y, "thermals", STEEL)
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
        if _HORIZ:
            col = state_color(mfrac * 100, TEAL)
            label(d, PAD, y, "memory", STEEL)
            vx = PAD + measure(F(UI_SEMI, T_LABEL), "MEMORY", 1.6) / SS + 12
            text(d, vx, y - 2, f"{fmt_bytes(mem_used)}/{fmt_bytes(mem_total)}", f_val, col)
            y += 15
            bar(img, PAD, y, 118, 5, mfrac, col)
            y += 11
            if swap_total:
                sfrac = swap_used / swap_total
                scol = state_color(sfrac * 100, TEAL)
                label(d, PAD, y, "swap", STEEL, size=T_MICRO)
                vx2 = PAD + measure(F(UI_SEMI, T_MICRO), "SWAP", 1.6) / SS + 10
                text(d, vx2, y - 2, f"{fmt_bytes(swap_used)}/{fmt_bytes(swap_total)}", f_val_sm, scol)
                y += 13
                bar(img, PAD, y, 118, 4, sfrac, scol)
                y += 8
            return y + gap(6)
        label(d, PAD, y, "memory", STEEL)
        text(d, R, y - 2, f"{fmt_bytes(mem_used)} / {fmt_bytes(mem_total)}", f_val, TEXT, anchor="r")
        y += 16
        bar(img, PAD, y, CW, 6, mfrac, state_color(mfrac * 100, TEAL))
        y += 16
        if swap_total:
            sfrac = swap_used / swap_total
            label(d, PAD, y, "swap", STEEL, size=T_MICRO)
            text(d, R, y - 2, f"{fmt_bytes(swap_used)} / {fmt_bytes(swap_total)}", f_val_sm, TEXT, anchor="r")
            y += 14
            bar(img, PAD, y, CW, 4, sfrac, state_color(sfrac * 100, TEAL))
            y += 8
        return y + gap(22)

    def sec_disk(y):
        label(d, PAD, y, "disk", STEEL)
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
        if _HORIZ:
            # Compact: each mount as value + short bar, then r/w inline.
            vx = PAD + measure(F(UI_SEMI, T_LABEL), "DISK", 1.6) / SS + 12
            single = len(infos) == 1
            if single:
                mp, usd, tot = infos[0]
                dcol = state_color(usd / tot * 100, PINK)
                text(d, vx, y - 2, f"{fmt_bytes(usd)}/{fmt_bytes(tot)}", f_val, dcol)
                y += 15
                bar(img, PAD, y, 118, 5, usd / tot, dcol)
                y += 11
            else:
                y += 15
                for mp, usd, tot in infos:
                    dcol = state_color(usd / tot * 100, PINK)
                    label(d, PAD, y, mp, STEEL, size=T_MICRO)
                    text(d, PAD + measure(F(UI_SEMI, T_MICRO), mp.upper(), 1.6) / SS + 10,
                         y - 2, f"{fmt_bytes(usd)}/{fmt_bytes(tot)}", f_val_sm, dcol)
                    y += 13
                    bar(img, PAD, y, 118, 4, usd / tot, dcol)
                    y += 9
            rw = f"↓ {fmt_bytes(rd, True)}"
            text(d, PAD, y, rw, f_val_sm, PINK)
            text(d, PAD + measure(f_val_sm, rw) / SS + 14, y, f"↑ {fmt_bytes(wr, True)}", f_val_sm, CORAL)
            return y + 14 + gap(6)
        if len(infos) == 1:
            mp, usd, tot = infos[0]
            text(d, R, y - 2, f"{fmt_bytes(usd)} / {fmt_bytes(tot)}", f_val, TEXT, anchor="r")
            y += 16
            bar(img, PAD, y, CW, 6, usd / tot, state_color(usd / tot * 100, PINK))
            y += 15
        else:
            y += 18
            for mp, usd, tot in infos:
                label(d, PAD, y, mp, STEEL, size=T_MICRO)
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
        if _HORIZ:
            # Compact: ↓/↑ rates inline, then the link detail on one tight line.
            label(d, PAD, y, "network", STEEL)
            y += 15
            dn = f"↓ {fmt_bytes(down, True)}"
            text(d, PAD, y, dn, f_val, ACCENT)
            text(d, PAD + measure(f_val, dn) / SS + 16, y, f"↑ {fmt_bytes(up, True)}", f_val, CORAL)
            y += 16
            ni = net_link_info()
            f_mi = F(UI_MED, T_MICRO)
            f_mo = F(MONO_REG, T_MICRO)
            lx = PAD
            left = ni.get("ssid") or ni.get("kind") or ""
            if left:
                text(d, lx, y, left, f_mi, TEXT)
                lx += measure(f_mi, left) / SS + 8
            sig = ni.get("signal")
            if sig is not None:
                scol = GREEN if sig >= 60 else AMBER if sig >= 35 else CORAL
                text(d, lx, y, f"{sig}%", f_mo, scol)
                lx += measure(f_mo, f"{sig}%") / SS + 8
            if ni.get("vpn"):
                label(d, lx, y, "vpn", GREEN, size=T_MICRO)
                lx += measure(F(UI_SEMI, T_MICRO), "VPN", 1.6) / SS + 8
            ip = ni.get("ip")
            if ip:
                text(d, lx, y, ip, f_mo, MUTE)
            return y + 12 + gap(8)
        label(d, PAD, y, "network", STEEL)
        y += 16
        peak = net_chart(img, PAD, y, CW, 60, ACCENT, CORAL, floor=64 * 1024)
        y += 60 + 6
        text(d, PAD, y, f"↓ {fmt_bytes(down, True)}", f_val_sm, ACCENT)
        text(d, W / 2, y, f"peak {fmt_bytes(peak, True)}", F(MONO_REG, T_LABEL), TEXT, anchor="c")
        text(d, R, y, f"↑ {fmt_bytes(up, True)}", f_val_sm, CORAL, anchor="r")
        # connection detail: SSID + signal (wifi) or link kind on the left, the
        # local IP on the right, with a VPN tag when a tunnel is up.
        ni = net_link_info()
        y += 16
        f_mi = F(UI_MED, T_MICRO)
        f_mo = F(MONO_REG, T_MICRO)
        lx = PAD
        left = ni.get("ssid") or ni.get("kind") or ""
        if left:
            text(d, lx, y, left, f_mi, TEXT)
            lx += measure(f_mi, left) / SS + 8
        sig = ni.get("signal")
        if sig is not None:
            scol = GREEN if sig >= 60 else AMBER if sig >= 35 else CORAL
            text(d, lx, y, f"{sig}%", f_mo, scol)
        rx = R
        ip = ni.get("ip")
        if ip:
            text(d, rx, y, ip, f_mo, MUTE, anchor="r")
            rx -= measure(f_mo, ip) / SS + 10
        if ni.get("vpn"):
            label_r(d, rx, y, "vpn", GREEN, size=T_MICRO)
        return y + gap(20)

    def sec_power(y):
        if not have_power:
            return y
        if _HORIZ:
            # Compact: device draw (and AC in) inline, no chart or legend.
            label(d, PAD, y, "power", STEEL)
            y += 15
            lx = PAD
            label(d, lx, y, "dev", TEXT, size=T_MICRO)
            lx += measure(F(UI_SEMI, T_MICRO), "DEV", 1.6) / SS + 8
            dcol = (consumption_color(1.0 if bstatus != "Discharging" else 0.0)
                    if watts > 0.05 else MUTE)
            dtxt = f"{watts:.1f} W"
            text(d, lx, y - 2, dtxt, f_val, dcol)
            lx += measure(f_val, dtxt) / SS + 14
            if ac_w > 0.05 and have_battery:
                label(d, lx, y, "ac", TEXT, size=T_MICRO)
                lx += measure(F(UI_SEMI, T_MICRO), "AC", 1.6) / SS + 8
                text(d, lx, y - 2, f"{ac_w:.1f} W", f_val, TEXT)
            return y + 14 + gap(8)
        label(d, PAD, y, "power", STEEL)
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
        if _HORIZ:
            # Compact: percent + status on one line, a short bar, then a brief
            # voltage/draw/eta line only when those figures exist.
            label(d, PAD, y, "battery", STEEL)
            vx = PAD + measure(F(UI_SEMI, T_LABEL), "BATTERY", 1.6) / SS + 11
            text(d, vx, y - 2, f"{cap}%", f_val, bcol)
            sx = vx + measure(f_val, f"{cap}%") / SS + 10
            text(d, sx, y - 1, bstatus.lower(), F(UI_MED, T_MICRO), scol)
            y += 15
            bar(img, PAD, y, 118, 5, cap / 100, bcol)
            y += 11
            extra = []
            if batt_v > 0.05:
                extra.append(f"{batt_v:.1f}V")
            if batt_w > 0.05 and bstatus in ("Charging", "Discharging"):
                extra.append(f"{batt_w:.1f}W")
            if eta:
                extra.append(fmt_dur(eta))
            if extra:
                text(d, PAD, y, "   ".join(extra), f_val_sm, MUTE)
                y += 13
            return y + gap(6)
        label(d, PAD, y, "battery", STEEL)
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
        present = {p["id"]: p for p in peripheral_batteries()}
        rows = [present.get(pid) or {"id": pid, "name": _periph_name_from_id(pid),
                                     "capacity": None, "status": "disconnected"}
                for pid in periph_sel]
        label(d, PAD, y, "devices", STEEL)
        if _HORIZ:
            y += 16
            for p in rows:
                nm = PERIPH_NAMES.get(p["id"]) or _periph_short(p["name"])
                dcap = p["capacity"]
                if dcap is None:
                    text(d, PAD, y - 2, nm[:18], F(UI_MED, T_MICRO), MUTE)
                    text(d, PAD + measure(F(UI_MED, T_MICRO), nm[:18]) / SS + 8, y - 2,
                         "off", F(UI_MED, T_MICRO), MUTE)
                    y += 13
                    bar(img, PAD, y, 100, 4, 0.0, MUTE)
                    y += 9
                else:
                    col = GREEN if p["status"] == "Charging" else ramp_rgb(1 - dcap / 100)
                    text(d, PAD, y - 2, nm[:18], F(UI_MED, T_BODY), TEXT)
                    text(d, PAD + measure(F(UI_MED, T_BODY), nm[:18]) / SS + 10, y - 2,
                         f"{dcap}%", f_val_sm, col)
                    y += 13
                    bar(img, PAD, y, 100, 4, dcap / 100, col)
                    y += 9
            return y + gap(6)
        y += 18
        for p in rows:
            nm = PERIPH_NAMES.get(p["id"]) or _periph_short(p["name"])
            dcap = p["capacity"]
            if dcap is None:
                text(d, PAD, y - 2, nm[:22], F(UI_MED, T_BODY), MUTE)
                text(d, R, y - 2, "disconnected", F(UI_MED, T_MICRO), MUTE, anchor="r")
                y += 15
                bar(img, PAD, y, CW, 5, 0.0, MUTE)
                y += 13
            else:
                col = GREEN if p["status"] == "Charging" else ramp_rgb(1 - dcap / 100)
                text(d, PAD, y - 2, nm[:26], F(UI_MED, T_BODY), TEXT)
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
        label(d, PAD, y, title, STEEL, tracking=1.7)
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
        if _HORIZ:
            # Compact: top-3 cpu then top-3 memory as tight name+value rows,
            # no row bars — keeps the module short for a thin bar.
            def mini(yy, title, hue, rows, fmt_of, colour_of):
                label(d, PAD, yy, title, STEEL, tracking=1.7)
                yy += 14
                for r in rows[:3]:
                    col = colour_of(r)
                    text(d, PAD, yy - 1, r[0][:16], F(UI_MED, T_MICRO), TEXT)
                    text(d, PAD + 108, yy - 1, fmt_of(r), F(MONO_REG, T_MICRO), col, anchor="r")
                    yy += 13
                return yy
            y = mini(y, "top cpu", ACCENT, top_cpu,
                     lambda r: f"{r[1]:.1f}%",
                     lambda r: ACCENT if r[1] > 1 else MUTE)
            y += 6
            y = mini(y, "top memory", TEAL, top_mem,
                     lambda r: fmt_bytes(r[2]),
                     lambda r: TEAL)
            return y + gap(6)
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

    def sec_spotify(y):
        sp = spotify
        if not sp:
            # Nothing is playing (Spotify / the media player is closed). Say so
            # rather than dropping the section, so an enabled Spotify module keeps
            # its place instead of vanishing when the app is shut.
            if _HORIZ:
                label(d, PAD, y, "spotify", STEEL)
                text(d, PAD + measure(F(UI_SEMI, T_LABEL), "SPOTIFY", 1.6) / SS + 10,
                     y, "closed", F(UI_MED, T_MICRO), MUTE)
                y += 16
                text(d, PAD, y, "not running", F(UI_MED, T_BODY), MUTE)
                return y + 14 + gap(8)
            label(d, PAD, y, "spotify", STEEL)
            text(d, R, y, "closed", F(UI_MED, T_MICRO), MUTE, anchor="r")
            y += 20
            text(d, PAD, y, "Spotify isn’t running", F(UI_MED, T_BODY), MUTE)
            return y + 20 + gap(20)
        if _HORIZ:
            # Compact: title + artist ellipsized, a thin progress bar, and the
            # five transport controls. The album cover fills the whole module as
            # a darkened background (text sits on a dark scrim over it); a state
            # tag ("playing"/"paused") stands in until the cover has loaded.
            _sbw = _HBAR_WIDTH.get("spotify", 200)
            _has_bar = (sp.get("length") or 0) > 0
            _box_top = max(0.0, y - 5)
            _box_bot = y + (78 if _has_bar else 68)
            cover_bg = album_art_bg(sp.get("art"), int(_sbw * SS),
                                    max(1, int((_box_bot - _box_top) * SS)), int(7 * SS))
            if cover_bg is not None:
                img.alpha_composite(cover_bg, (0, int(_box_top * SS)))
                _scrim = Image.new("RGBA", cover_bg.size, (0, 0, 0, 0))
                ImageDraw.Draw(_scrim).rounded_rectangle(
                    [0, 0, cover_bg.size[0] - 1, cover_bg.size[1] - 1],
                    radius=int(7 * SS), fill=(6, 8, 12, 170))
                img.alpha_composite(_scrim, (0, int(_box_top * SS)))
            label(d, PAD, y, sp.get("player", "media"), STEEL)
            if cover_bg is not None:
                maxw = _sbw - 2 * PAD
            else:
                statetxt = "playing" if sp.get("status") == "Playing" else "paused"
                text(d, PAD + measure(F(UI_SEMI, T_LABEL), sp.get("player", "media").upper(), 1.6) / SS + 10,
                     y, statetxt, F(UI_MED, T_MICRO), MUTE)
                maxw = 150
            y += 15
            title = sp.get("title") or ""
            while title and measure(F(UI_MED, T_BODY), title) / SS > maxw:
                title = title[:-1]
            text(d, PAD, y, title, F(UI_MED, T_BODY), TEXT)
            y += 15
            artist = sp.get("artist") or ""
            while artist and measure(F(UI_MED, T_MICRO), artist) / SS > maxw:
                artist = artist[:-1]
            text(d, PAD, y, artist, F(UI_MED, T_MICRO), MUTE)
            y += 14
            length = sp.get("length") or 0
            if length > 0:
                frac = max(0.0, min(1.0, (sp.get("position") or 0) / length))
                bar(img, PAD, y, maxw if cover_bg is not None else 150, 4, frac, GREEN)
                y += 10
            cy = y + 7
            cxs = (PAD + 10, PAD + 38, PAD + 66, PAD + 94, PAD + 122)
            sh_on = sp.get("shuffle")
            loop = sp.get("loop")
            sh_col = ACCENT if sh_on else MUTE
            rp_col = MUTE if loop in (None, "None") else ACCENT
            rp_kind = "repeat_one" if loop == "Track" else "repeat"
            media_glyph(d, cxs[0], cy, "shuffle", 6, sh_col)
            media_glyph(d, cxs[1], cy, "previous", 7, TEXT)
            media_glyph(d, cxs[2], cy, "pause" if sp.get("status") == "Playing" else "play", 9, GREEN)
            media_glyph(d, cxs[3], cy, "next", 7, TEXT)
            media_glyph(d, cxs[4], cy, rp_kind, 6, rp_col)
            _SPOTIFY_BOX["box"] = (int(cxs[0] - 14), int(y - 2), int(cxs[4] + 14), int(cy + 14),
                                   [int(c) for c in cxs])
            # ensure the column keeps the full cover-background height
            return (max(y + 16, _box_bot) if cover_bg is not None else y + 16) + gap(8)
        # The album cover fills the section as a darkened background card (like
        # the horizontal layout). The "playing/paused" tag and the left
        # thumbnail are only used when there is no cover to show.
        _has_bar = (sp.get("length") or 0) > 0
        _cx0, _cx1 = 12, W - 12
        _box_top = max(0.0, y - 4)
        _box_bot = y + (105 if _has_bar else 80)
        cover_bg = album_art_bg(sp.get("art"), max(1, int((_cx1 - _cx0) * SS)),
                                max(1, int((_box_bot - _box_top) * SS)), int(12 * SS))
        if cover_bg is not None:
            img.alpha_composite(cover_bg, (int(_cx0 * SS), int(_box_top * SS)))
            _scrim = Image.new("RGBA", cover_bg.size, (0, 0, 0, 0))
            ImageDraw.Draw(_scrim).rounded_rectangle(
                [0, 0, cover_bg.size[0] - 1, cover_bg.size[1] - 1],
                radius=int(12 * SS), fill=(6, 8, 12, 170))
            img.alpha_composite(_scrim, (int(_cx0 * SS), int(_box_top * SS)))
        label(d, PAD, y, sp.get("player", "media"), STEEL)
        if cover_bg is None:
            statetxt = "playing" if sp.get("status") == "Playing" else "paused"
            text(d, R, y, statetxt, F(UI_MED, T_MICRO), MUTE, anchor="r")
        y += 18
        if cover_bg is not None:
            tx, avail, ty = PAD, R - PAD, y
        else:
            ART = 46
            art = album_art(sp.get("art"), ART)
            tx = PAD
            if art is not None:
                img.alpha_composite(art, (PAD * SS, int(y * SS)))
                tx = PAD + ART + 11
            avail = R - tx
            ty = y + (6 if art is not None else 0)
        title = sp.get("title") or ""
        while title and measure(F(UI_MED, T_BODY), title) / SS > avail:
            title = title[:-1]
        text(d, tx, ty, title, F(UI_MED, T_BODY), TEXT)
        artist = sp.get("artist") or ""
        while artist and measure(F(UI_MED, T_MICRO), artist) / SS > avail:
            artist = artist[:-1]
        text(d, tx, ty + 17, artist, F(UI_MED, T_MICRO), MUTE)
        y += 34 if cover_bg is not None else (ART if art is not None else 30)
        length = sp.get("length") or 0
        if length > 0:
            frac = max(0.0, min(1.0, (sp.get("position") or 0) / length))
            text(d, PAD, y - 1, fmt_mmss(sp.get("position") or 0), F(MONO_REG, T_MICRO), MUTE)
            text(d, R, y - 1, fmt_mmss(length), F(MONO_REG, T_MICRO), MUTE, anchor="r")
            y += 13
            bar(img, PAD, y, CW, 4, frac, GREEN)
            y += 12
        # transport controls, centred — a clickable band (see run_window).
        # Five buttons: shuffle · previous · play/pause · next · repeat. Shuffle
        # and repeat are tinted by their on/off state.
        cy = y + 8
        cxs = (W / 2 - 84, W / 2 - 42, W / 2, W / 2 + 42, W / 2 + 84)
        sh_on = sp.get("shuffle")
        loop = sp.get("loop")
        sh_col = ACCENT if sh_on else MUTE
        rp_col = MUTE if loop in (None, "None") else ACCENT
        rp_kind = "repeat_one" if loop == "Track" else "repeat"
        media_glyph(d, cxs[0], cy, "shuffle", 6, sh_col)
        media_glyph(d, cxs[1], cy, "previous", 7, TEXT)
        media_glyph(d, cxs[2], cy, "pause" if sp.get("status") == "Playing" else "play", 9, GREEN)
        media_glyph(d, cxs[3], cy, "next", 7, TEXT)
        media_glyph(d, cxs[4], cy, rp_kind, 6, rp_col)
        _SPOTIFY_BOX["box"] = (int(cxs[0] - 16), int(y - 2), int(cxs[4] + 16), int(cy + 16),
                               [int(c) for c in cxs])
        _end = y + gap(28)
        # keep panel background below the cover card equal to its left/right inset
        # (12px), so when Spotify is the last section the margin looks even and the
        # album art can't run into the panel's edge
        return max(_end, _box_bot + 12) if cover_bg is not None else _end

    def sec_clock(y):
        now = datetime.datetime.now()
        show_sec = bool(cfg.get("clock_seconds", False))
        h24 = bool(cfg.get("clock_24h", True))
        fmt = ("%H:%M:%S" if show_sec else "%H:%M") if h24 else \
              ("%I:%M:%S" if show_sec else "%I:%M")
        tstr = now.strftime(fmt)
        if not h24:
            tstr = tstr.lstrip("0")
        f_time = F(MONO_LIGHT, T_HERO)
        suffix = "" if h24 else now.strftime(" %p").rstrip("0")
        tw = measure(f_time, tstr) / SS
        text(d, PAD, y + 4, tstr, f_time, TEXT)
        if suffix:
            text(d, PAD + tw + 6, y + 18, suffix.strip(), F(UI_MED, T_BODY), MUTE)
        if _HORIZ:
            # Compact: date left-aligned under the time so the column stays narrow.
            if cfg.get("clock_date", True):
                dfmt = cfg.get("clock_date_format") or "%a, %d %b %Y"
                try:
                    dstr = now.strftime(dfmt)
                except Exception:
                    dstr = now.strftime("%a, %d %b %Y")
                text(d, PAD, y + 36, dstr, F(UI_MED, T_BODY), MUTE)
            return y + 50 + gap(8)
        if cfg.get("clock_date", True):
            dfmt = cfg.get("clock_date_format") or "%a, %d %b %Y"
            try:
                dstr = now.strftime(dfmt)
            except Exception:
                dstr = now.strftime("%a, %d %b %Y")
            text(d, R, y + 22, dstr, F(UI_MED, T_BODY), MUTE, anchor="r")
        return y + 40 + gap(22)

    def sec_modules(y):
        mods = [m for m in (_s.get("modules") or []) if m.get("command")]
        # Per-panel selection: cfg["module_ids"] lists which modules (by id) this
        # panel shows, in this panel's own order. Absent => show all (keeps older
        # configs, which never had a selection, working unchanged). Empty list =>
        # show none. Unknown ids (a deleted module) are simply skipped.
        sel = cfg.get("module_ids")
        if isinstance(sel, list):
            pos = {str(mid): i for i, mid in enumerate(sel)}
            mods = sorted((m for m in mods if str(m.get("id")) in pos),
                          key=lambda m: pos[str(m.get("id"))])
        if not mods:
            return y
        label(d, PAD, y, "custom", STEEL)
        y += 16
        f_name = F(UI_MED, T_BODY)
        f_val = F(MONO_REG, T_VALUE)
        # Custom-module text carries the theme's colour (like Matrix's green),
        # so every theme's modules read as themed rather than plain white.
        # Mostly accent (strong, saturated) with a touch of foreground mixed in
        # so even the darkest accents clear a readable contrast on every skin.
        mod_col = _mix(ACCENT, TEXT, 0.22)
        # Horizontal: align all values to one column just past the widest label,
        # so the compact bar still reads as a tidy table.
        _valx = PAD + 12 + (max((measure(f_name, (m.get("label") or "").strip()) / SS
                                 for m in mods if (m.get("label") or "").strip()), default=0)
                            if _HORIZ else 0)
        for m in mods:
            mid = str(m.get("id") or "")
            interval = max(1, int(m.get("interval") or 10))
            typ = m.get("type") or "text"
            raw = cached_cmd("mod_" + mid, ["sh", "-c", m["command"]], interval).strip()
            name = (m.get("label") or "").strip()
            val, pct = "", None
            if typ == "json":
                try:
                    j = json.loads(raw) if raw else {}
                except Exception:
                    j = {}
                if not isinstance(j, dict):   # a module may print [], 42, "ok", null
                    j = {}
                val = str(j.get("text", (raw.splitlines() or [""])[0]))
                try:
                    pct = float(j.get("percentage")) if j.get("percentage") is not None else None
                except (TypeError, ValueError):
                    pct = None
            else:
                val = (raw.splitlines() or [""])[0]
                if typ in ("bar", "number"):
                    nums = re.findall(r"-?\d+\.?\d*", raw)
                    if nums:
                        pct = float(nums[0])
                        if typ == "number" and not name:
                            val = nums[0]
            show_bar = (typ == "bar") or (typ == "json" and pct is not None)
            # truncate the value to the room it has
            maxw = (R - PAD) if not name else (R - PAD - measure(f_name, name) / SS - 12)
            while val and measure(f_val, val) / SS > maxw:
                val = val[:-1]
            if name:
                text(d, PAD, y, name, f_name, mod_col)
                if _HORIZ:
                    # Compact: values in one column just past the widest label (not
                    # right-aligned to the full column) so the column trims to its
                    # real content — no big empty gap in a left-to-right bar.
                    text(d, _valx, y, val, f_val, mod_col)
                else:
                    text(d, R, y, val, f_val, mod_col, anchor="r")
            else:
                text(d, PAD, y, val, f_val, mod_col)
            y += 18
            if show_bar:
                bar(img, PAD, y, (min(CW, 140) if _HORIZ else CW), 6,
                    max(0.0, min(1.0, (pct or 0) / 100)), ACCENT, ramp=True)
                y += 12
            y += 4
        return y + gap(16)

    have_battery = bstatus != "no battery"
    have_power = power_src != "battery" or have_battery
    charging = bstatus == "Charging"
    section_fns = {
        "top": sec_top, "claude": sec_claude, "weather": sec_weather,
        "clock": sec_clock,
        "load": sec_load, "gauges": sec_gauges, "cores": sec_cores,
        "history": sec_history, "thermals": sec_thermals, "memory": sec_memory,
        "disk": sec_disk, "network": sec_network, "power": sec_power,
        "battery": sec_battery, "devices": sec_devices, "spotify": sec_spotify,
        "processes": sec_processes, "modules": sec_modules,
    }
    horizontal = bool(cfg.get("horizontal"))
    if horizontal:
        # Lay the sections out LEFT-TO-RIGHT: render each into its own native-width
        # column (redirect the shared img/d the section closures draw into), then
        # paste the columns side by side. No vertical stretch in this mode.
        FLEX = 0.0
        # Native per-section column width — NOT the config span (which the margins
        # set and a move would change, ballooning the bar). Point W/R/CW at it so
        # the section closures draw into a native-width column.
        col_w = globals()["W"]
        W, R, CW = col_w, col_w - PAD, col_w - 2 * PAD
        gap_ss = 12 * SS            # breathing room between columns
        _HPAD = 9                   # thin top/bottom padding for the bar (vs PAD)
        cols = []
        for key in order_cfg:
            fn = section_fns.get(key)
            if fn is None or not SECTIONS.get(key, True):
                continue
            col_img = Image.new("RGBA", (col_w * SS, surf_h * SS), (0, 0, 0, 0))
            img = col_img
            d = ImageDraw.Draw(col_img)
            cy = _HPAD
            try:
                cy = fn(_HPAD)       # content starts near the top, not at PAD
            except Exception:
                _log_section_error(key)
            ch = min(surf_h, max(1, int(round(cy)) + _HPAD))
            col = col_img.crop((0, 0, col_w * SS, ch * SS))
            bb = col.getbbox()
            if not bb:
                continue                 # empty section — don't reserve a blank gap
            # Fixed width per section (stable, no reflow as values change); fall
            # back to content-width trimming for any section not in the table.
            _fw = _HBAR_WIDTH.get(key)
            cwid = (min(col_w * SS, _fw * SS) if _fw is not None
                    else min(col_w * SS, bb[2] + PAD * SS))
            # Height = real content bottom + a small pad — cy includes the
            # section's trailing gap, which would make the bar needlessly tall.
            chh = min(ch * SS, bb[3] + _HPAD * SS)
            cols.append((key, col.crop((0, 0, max(1, cwid), max(1, chh)))))
        if not cols:
            cols = [("", Image.new("RGBA", (40 * SS, 40 * SS), (0, 0, 0, 0)))]
        maxh = max(c.height for _k, c in cols)
        total_ss = sum(c.width for _k, c in cols) + gap_ss * (len(cols) - 1)
        W = max(1, total_ss // SS)
        img = Image.new("RGBA", (W * SS, maxh, ), (0, 0, 0, 0))
        _xo = 0
        for _k, c in cols:
            img.paste(c, (_xo, 0))
            # The Spotify click band was recorded relative to its own column;
            # shift it by the column's paste offset so the hit test lands on
            # the buttons in the composed bar.
            if _k == "spotify" and _SPOTIFY_BOX["box"] is not None:
                x0, y0, x1, y1, cxs = _SPOTIFY_BOX["box"]
                dx = _xo / SS
                _SPOTIFY_BOX["box"] = (x0 + dx, y0, x1 + dx, y1, [c0 + dx for c0 in cxs])
            _xo += c.width + gap_ss
        H = maxh // SS
        natural = H
        next_flex = 0.0
    else:
        for key in order_cfg:
            fn = section_fns.get(key)
            if fn is not None and SECTIONS.get(key, True):
                try:
                    y = fn(y)
                except Exception:
                    # one bad section must not freeze the whole panel
                    _log_section_error(key)

        H = int(round(y))

        natural = y - FLEX_POINTS * FLEX
        # The panel is as tall as its content plus the user's extra stretch. The
        # gaps share that stretch, so the height is natural + stretch: toggling a
        # section changes `natural` (the panel grows/shrinks by that section)
        # while the stretch the user dragged in stays constant. stretch 0 = exact
        # content size.
        next_flex = 0.0
        if FLEX_POINTS:
            next_flex = max(0.0, min(FLEX_MAX, float(stretch) / FLEX_POINTS))

    if _BG.get("wall"):
        _BG["pos"] = pos
        _BG["screen"] = screen
    panel = panel_bg(H, W)

    out = Image.alpha_composite(panel, img.crop((0, 0, W * SS, H * SS)))
    # UI scale: the content is drawn at SS supersampling, so downscaling the
    # full-res composite to W*sc (sc up to SS) stays crisp — a true zoom of the
    # whole panel (fonts, gauges, layout) at the user's chosen size.
    sc = max(0.5, min(float(ui_scale or 1.0), 2.0))
    out = out.resize((max(1, round(W * sc)), max(1, round(H * sc))), Image.BOX)
    if _SPOTIFY_BOX["box"] is not None and sc != 1.0:
        x0, y0, x1, y1, cxs = _SPOTIFY_BOX["box"]
        _SPOTIFY_BOX["box"] = (round(x0 * sc), round(y0 * sc), round(x1 * sc),
                               round(y1 * sc), [round(c * sc) for c in cxs])

    if write_png:
        tmp = f"{PNG_PATH}.{os.getpid()}.tmp"
        out.save(tmp, "PNG", compress_level=1)
        os.replace(tmp, PNG_PATH)

    return out, next_flex, int(round(natural))

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

_SECTION_ERRS = {}

def _log_section_error(key):
    """Log a section's render failure once per distinct traceback (re-logging
    after 5 min), so a broken section is skipped — not fatal — and the panel
    keeps drawing every other section instead of freezing on the last frame."""
    tb = traceback.format_exc()
    h = hash(tb)
    prev = _SECTION_ERRS.get(key)
    now = time.time()
    if prev is None or prev[0] != h or now - prev[1] > 300:
        log(f"section '{key}' failed and was skipped:\n{tb.rstrip()}")
        _SECTION_ERRS[key] = (h, now)

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

# --- Threshold alerts ---------------------------------------------------------
# A small rule engine: the user arms metrics with thresholds in the settings
# window, and when one is breached for a sustained window the panel fires a
# native desktop notification. Hold + cooldown keep a flapping value from
# spamming. (key, label, unit, default threshold, comparison).
ALERT_DEFS = [
    ("cpu_temp", "CPU temperature", "°C", 85, "above"),
    ("cpu_load", "CPU load", "%", 90, "above"),
    ("ram", "Memory usage", "%", 90, "above"),
    ("disk", "Disk usage", "%", 90, "above"),
    ("battery_low", "Battery", "%", 15, "below"),
]
_ALERT_STATE = {}
ALERT_HOLD = 15.0       # condition must persist this long before firing
ALERT_COOLDOWN = 600.0  # minimum seconds between repeats while still breached
_NOTIFY = {"init": False}
_LIVE_NOTIFS = []   # keep notifications with action buttons alive (see notify_version_update)

def desktop_notify(summary, body):
    """Fire a desktop notification by the best available means, or do nothing."""
    try:
        import gi
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify
        if not _NOTIFY["init"]:
            Notify.init("linux-mint-hud")
            _NOTIFY["init"] = True
        Notify.Notification.new(summary, body, "mint-hud").show()
        return True
    except Exception:
        pass
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(["notify-send", "-a", "linux-mint-hud",
                              "-i", "mint-hud", summary, body])
            return True
        except Exception:
            pass
    return False

def _open_whats_new(*_):
    try:
        subprocess.Popen(["xdg-open", RELEASES_URL],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def notify_version_update():
    """Once, on the first launch after a version change (an update, not a fresh
    install), show a "what's new" notification with a button to the release
    notes. The last-seen version is recorded in settings so it fires only once.

    A pre-2.0 install never wrote `last_seen_version`; distinguish that genuine
    update from a brand-new install by whether a settings file already existed."""
    try:
        had_settings = os.path.exists(SETTINGS_FILE)
        s = load_settings()
        seen = s.get("last_seen_version")
        if seen == __version__:
            return False
        ns = dict(load_settings())
        ns["last_seen_version"] = __version__
        save_settings(ns)
        if seen is None and not had_settings:
            return False                      # fresh install — nothing to announce
        summary = f"Linux Mint HUD updated to {__version__}"
        body = "See what's new in this release."
        try:
            import gi
            gi.require_version("Notify", "0.7")
            from gi.repository import Notify
            if not _NOTIFY["init"]:
                Notify.init("linux-mint-hud")
                _NOTIFY["init"] = True
            n = Notify.Notification.new(summary, body, "mint-hud")
            n.set_timeout(Notify.EXPIRES_NEVER)
            n.add_action("whatsnew", "What's new", lambda *a: _open_whats_new(), None)
            # Keep the notification (and thus its action callback) referenced —
            # a local would be garbage-collected right after this function
            # returns, so clicking the button would do nothing. Drop it only once
            # it's closed/dismissed.
            _LIVE_NOTIFS.append(n)
            n.connect("closed", lambda _n: _LIVE_NOTIFS.remove(_n)
                      if _n in _LIVE_NOTIFS else None)
            n.show()
        except Exception:
            desktop_notify(summary, "Open the tray menu → What's new to see the changes.")
    except Exception:
        pass
    return False                              # one-shot when used as a GLib timeout

def _alert_value(key, M):
    if key == "cpu_temp":
        return M.get("cpu_t")
    if key == "cpu_load":
        return M.get("cpu_pct")
    if key == "ram":
        t = M.get("mem_total") or 0
        return (M.get("mem_used", 0) / t * 100) if t else None
    if key == "disk":
        t = M.get("disk_total") or 0
        return (M.get("disk_used", 0) / t * 100) if t else None
    if key == "battery_low":
        # only meaningful on a real battery that is actually draining
        if M.get("bstatus") != "Discharging":
            return None
        return M.get("cap")
    return None

def check_alerts(M):
    s = load_settings()
    if s.get("alerts_muted"):      # master off-switch from the tray
        _ALERT_STATE.clear()
        return
    rules = s.get("alerts") or {}
    if not rules:
        return
    now = time.time()
    for key, label, unit, dflt, op in ALERT_DEFS:
        r = rules.get(key) or {}
        if not r.get("on"):
            _ALERT_STATE.pop(key, None)
            continue
        val = _alert_value(key, M)
        if val is None:
            _ALERT_STATE.pop(key, None)
            continue
        th = float(r.get("th", dflt))
        breached = (val < th) if op == "below" else (val > th)
        st = _ALERT_STATE.setdefault(key, {"since": None, "last": 0.0})
        if not breached:
            st["since"] = None
            continue
        if st["since"] is None:
            st["since"] = now
        if now - st["since"] >= ALERT_HOLD and now - st["last"] >= ALERT_COOLDOWN:
            st["last"] = now
            desktop_notify("linux-mint-hud",
                           f"{label} {op} {th:g}{unit} — now {val:.0f}{unit}")

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

    # Native Wayland placement via gtk-layer-shell (wlroots/KWin/Mutter's
    # zwlr_layer_shell_v1). Only used on a real Wayland session where the library
    # is present; otherwise the X11 path runs (directly, or over XWayland).
    LS = None
    if IS_WAYLAND:
        try:
            gi.require_version("GtkLayerShell", "0.1")
            from gi.repository import GtkLayerShell as LS
        except (ValueError, ImportError):
            LS = None
    USE_LAYER = LS is not None

    _apply_window_icon(Gtk)

    # Style the tray icon's right-click menu to match the panel skin (first
    # panel's theme). Only menus/popovers are styled — never the panel windows,
    # which are click-through and drawn by cairo. Kept in sync with the theme.
    _menu_prov = {"prov": None, "name": None}

    def _theme_name():
        _si = load_settings()
        return ((_si.get("panels") or [{}])[0].get("theme")
                or _si.get("theme") or "midnight")

    def _refresh_menu_css():
        try:
            name = _theme_name()
            if name == _menu_prov["name"]:
                return
            if _menu_prov["prov"] is None:
                _menu_prov["prov"] = Gtk.CssProvider()
                Gtk.StyleContext.add_provider_for_screen(
                    Gdk.Screen.get_default(), _menu_prov["prov"],
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            _menu_prov["prov"].load_from_data(menu_css(name))
            _menu_prov["name"] = name
        except Exception:
            pass
    _refresh_menu_css()

    disp = Gdk.Display.get_default()
    last_frame = [None]
    # Serialises the two code paths that call render() and touch the module-global
    # render state (palette, FLEX, _SPOTIFY_BOX, caches): the worker's render_all
    # and the main-thread grip-resize _resize_render.
    _render_lock = threading.Lock()

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
        if USE_LAYER:
            # Desktop-widget layer: above the wallpaper, below normal windows,
            # reserving no space and never taking keyboard focus. Placement is
            # by anchor+margin in place() since Wayland clients can't self-move.
            LS.init_for_window(win)
            LS.set_layer(win, LS.Layer.BOTTOM)
            LS.set_namespace(win, "mint-hud")
            LS.set_exclusive_zone(win, 0)
            try:
                LS.set_keyboard_mode(win, LS.KeyboardMode.NONE)
            except Exception:
                pass
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
        # X11-only: get_xid()/get_window_stack() raise or return nothing on a
        # native Wayland surface, where the BOTTOM layer handles stacking anyway.
        if IS_WAYLAND:
            return
        gw = win.get_window()
        if gw is None:
            return
        try:
            stack = win.get_screen().get_window_stack() or []
            if len(stack) < 2:
                return
            xids = [w.get_xid() for w in stack]
            if xids and xids[0] == gw.get_xid():
                win.hide()
                win.show()
        except Exception:
            pass

    GRIP = 30
    live_box = [None]

    def eff_margins(pw, cfg, wa):
        """The panel's box (x, y, w, h) and resolved four margins, honouring an
        in-flight grip resize of this panel."""
        lb = live_box[0]
        c = {**cfg, **lb["margins"]} if (lb and lb["idx"] == pw["idx"]) else cfg
        return panel_box(c, wa)

    def place(pw, cfg, w, h):
        win = pw["win"]
        mon = monitor_of(cfg)
        wa = mon.get_workarea()
        x, y, bw, bh, m = eff_margins(pw, cfg, wa)
        # panel_box works in logical width (W/bw); the window is drawn at that
        # width times ui_scale, so for a scaled panel its x would leave a gap on
        # the anchored side (a flush-right panel bounced ~(bw-w) inward). Anchor
        # the ACTUAL window (width w, height h) to whichever margin is smaller —
        # the edge the panel was placed against — so the gap it was dropped with
        # is the gap it keeps. For an unscaled panel (w == bw) this is identical.
        # Anchor to the nearer edge; on a tie the panel's home is top-right, so
        # the vertical tie goes to the top (anchor top) and the horizontal tie to
        # the right. Without this, top=bottom=0 anchored to the bottom and left a
        # ~2px gap at the very top (window a hair shorter than the work area).
        x = (wa.x + wa.width - w - m["right"]) if m["right"] <= m["left"] else (wa.x + m["left"])
        y = (wa.y + wa.height - h - m["bottom"]) if m["bottom"] < m["top"] else (wa.y + m["top"])
        x = max(wa.x, min(x, wa.x + wa.width - w))
        y = max(wa.y, min(y, wa.y + wa.height - h))
        win.set_size_request(w, h)
        if USE_LAYER:
            # A Wayland client can't position itself; express the same spot as
            # a top-left anchor on this output plus margins (the compositor's
            # own panels are accounted for by the workarea offset).
            LS.set_monitor(win, mon)
            LS.set_anchor(win, LS.Edge.TOP, True)
            LS.set_anchor(win, LS.Edge.LEFT, True)
            LS.set_anchor(win, LS.Edge.RIGHT, False)
            LS.set_anchor(win, LS.Edge.BOTTOM, False)
            LS.set_margin(win, LS.Edge.TOP, int(y - wa.y))
            LS.set_margin(win, LS.Edge.LEFT, int(x - wa.x))
        else:
            win.move(x, y)

    panels = []

    def snapped(pw, x, y, w, h):
        """Nudge a dragged window onto tidy targets: flush against a work-area
        edge, a small even gap from it, and the edges of any other panel.

        Flush (gap 0) and the tidy gap (G) are both offered, and we snap to the
        *nearest* target rather than the first in range — otherwise, with the
        snap radius wider than G, the flush position sits inside the tidy gap's
        pull and the panel can never rest against the screen edge."""
        mon = disp.get_monitor_at_point(x + w // 2, y + h // 2) or disp.get_primary_monitor()
        wa = mon.get_workarea()
        SNAP, G = 26, MARGIN
        sx = [wa.x, wa.x + G, wa.x + wa.width - w - G, wa.x + wa.width - w]
        sy = [wa.y, wa.y + G, wa.y + wa.height - h - G, wa.y + wa.height - h]
        # Other panels' edges come from their CONFIG box (+ true rendered size),
        # never from their live GTK window — get_position()/get_allocated_* are
        # unreliable for these desktop windows.
        _all = load_settings().get("panels") or []
        for other in panels:
            if other is pw:
                continue
            oi = other.get("idx")
            if oi is None or oi >= len(_all):
                continue
            try:
                owa = monitor_of(_all[oi]).get_workarea()
                obx, oby, obw, obh, _ = panel_box(_all[oi], owa)
            except Exception:
                continue
            oaw = int(other["state"].get("w") or obw)
            oah = int(other["state"].get("h") or obh)
            sx += [obx, obx + oaw - w]
            sy += [oby, oby + oah - h]

        def _nearest(v, targets):
            best = None
            for t in targets:
                d = abs(v - t)
                if d <= SNAP and (best is None or d < best[0]):
                    best = (d, t)
            return best[1] if best else v
        return _nearest(x, sx), _nearest(y, sy)

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
        maxh = wa.height - 2
        if img.height > maxh > 0:
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
        st["natw"], st["nath"] = dimg.width, dimg.height
        st["w"], st["h"] = dimg.width, dimg.height
        base = st.get("img")
        st["render_scale"] = (dimg.height / base.height) if (base and base.height) else 1.0
        return dimg

    def hud_hit(pw):
        """The Spotify control band in window pixels (scaled), or None."""
        st = pw["state"]
        box = st.get("spotify_box")
        if not box or st.get("moving"):
            return None
        s = st.get("render_scale", 1.0)
        x0, y0, x1, y1, cxs = box
        return (int(x0 * s), int(y0 * s), int(x1 * s), int(y1 * s),
                [int(c * s) for c in cxs])

    def set_hud_shape(pw):
        """When not moving, make only the Spotify control band catch clicks (the
        rest of the panel stays click-through); empty when there's no band."""
        gw = pw["win"].get_window()
        if gw is None or pw["state"].get("moving"):
            return
        hit = hud_hit(pw)
        if hit:
            x0, y0, x1, y1, _ = hit
            gw.input_shape_combine_region(
                cairo.Region(cairo.RectangleInt(x0, y0, max(1, x1 - x0), max(1, y1 - y0))), 0, 0)
        else:
            gw.input_shape_combine_region(cairo.Region(), 0, 0)

    def setup(pw):
        win, st = pw["win"], pw["state"]

        def on_draw(_w, cr):
            if st["surface"] is not None:
                aw, ah = win.get_allocated_width(), win.get_allocated_height()
                nw, nh = st.get("natw") or aw, st.get("nath") or ah
                cr.set_operator(cairo.OPERATOR_SOURCE)
                if nw and nh and (aw != nw or ah != nh):
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
                w = st.get("w") or win.get_allocated_width()
                h = st.get("h") or win.get_allocated_height()
                # A horizontal panel has no drag-resize (its size is content-driven)
                # — just a move handle, so draw no grip.
                if bool(_cfg_of().get("horizontal")):
                    return False
                # The grip sits on the panel's INNER bottom corner — away from the
                # anchored screen edge — so dragging it outward widens the panel
                # into the open space rather than against the edge it hugs.
                try:
                    _wa = monitor_of(_cfg_of()).get_workarea()
                    _mm = panel_box(_cfg_of(), _wa)[4]
                    grip_left = _mm["right"] <= _mm["left"]
                except Exception:
                    grip_left = False
                cx = (GRIP / 2 + 4) if grip_left else (w - GRIP / 2 - 4)
                cy = h - GRIP / 2 - 4
                sgn = -1 if grip_left else 1
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
                    cr.move_to(cx - sgn * (5 - d), cy + 5)
                    cr.line_to(cx + sgn * 5, cy - 5 + d)
                cr.stroke()
            return False

        def _cfg_of():
            cfgs = load_settings().get("panels") or []
            if pw["idx"] < len(cfgs):
                return dict(cfgs[pw["idx"]])
            return {"monitor": 0}

        def on_realize(_w):
            win.get_window().input_shape_combine_region(cairo.Region(), 0, 0)
            set_hud_shape(pw)

        def enter_move():
            # A DESKTOP-type window is forced to the very bottom of the stack by
            # the WM (EWMH), so even keep_above can't raise it and it never
            # receives the pointer — the grip isn't clickable. Switch it to a
            # NORMAL, raised window for the duration of the move so the WM lets it
            # come to the top and take clicks. (The type hint only takes effect on
            # map, so cycle hide/show.)
            if USE_LAYER:
                win.set_keep_below(False)
                win.set_keep_above(True)
            else:
                win.hide()
                win.set_type_hint(Gdk.WindowTypeHint.NORMAL)
                win.set_accept_focus(True)
                win.set_keep_below(False)
                win.set_keep_above(True)
                win.show()
                # reposition at the panel's real spot (the WM may re-place on map)
                cfg = _cfg_of()
                wa = monitor_of(cfg).get_workarea()
                _x, _y, _bw, _bh, m = eff_margins(pw, cfg, wa)
                usc = float(cfg.get("ui_scale") or 1.0)
                w = int(st.get("w") or round(_bw * usc))
                h = int(st.get("h") or _bh)
                px = (wa.x + wa.width - w - m["right"]) if m["right"] <= m["left"] else (wa.x + m["left"])
                py = (wa.y + wa.height - h - m["bottom"]) if m["bottom"] < m["top"] else (wa.y + m["top"])
                win.set_size_request(w, h)   # re-map lost the sized allocation
                win.move(max(wa.x, min(px, wa.x + wa.width - w)),
                         max(wa.y, min(py, wa.y + wa.height - h)))
            gw = win.get_window()
            if gw is not None:
                # whole window catches the pointer while moving (set once, not per
                # motion — re-shaping mid-drag could drop the grab)
                gw.input_shape_combine_region(
                    cairo.Region(cairo.RectangleInt(0, 0, 16384, 16384)), 0, 0)
            win.present()
            st["moving"] = True
            win.queue_draw()

        def exit_move():
            st["moving"] = False
            gw = win.get_window()
            if gw is not None:
                gw.set_cursor(None)
            if not USE_LAYER:
                win.hide()
                win.set_type_hint(Gdk.WindowTypeHint.DESKTOP)
                win.set_accept_focus(False)
                win.set_keep_above(False)
                win.set_keep_below(True)
                win.show()
            else:
                win.set_keep_above(False)
                win.set_keep_below(True)
            set_hud_shape(pw)
            st["placekey"] = None   # force a re-place to the saved spot
            win.queue_draw()

        def _live_place():
            # Size + position the (NORMAL, move-mode) window to the panel's TRUE
            # rendered size at its saved anchor, so the instant a drag ends the
            # panel already sits exactly where it will after 'Save position' —
            # no flash of content left-aligned inside an over-wide window.
            cfg = _cfg_of()
            wa = monitor_of(cfg).get_workarea()
            m = eff_margins(pw, cfg, wa)[4]
            w = int(st.get("w") or 1)
            h = int(st.get("h") or 1)
            px = (wa.x + wa.width - w - m["right"]) if m["right"] <= m["left"] else (wa.x + m["left"])
            py = (wa.y + wa.height - h - m["bottom"]) if m["bottom"] < m["top"] else (wa.y + m["top"])
            win.set_size_request(w, h)
            win.move(max(wa.x, min(px, wa.x + wa.width - w)),
                     max(wa.y, min(py, wa.y + wa.height - h)))
        st["live_place"] = _live_place

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
            usc = float(cfg.get("ui_scale") or 1.0)
            # window px -> logical: height = (natural+stretch)*scale, so the live
            # stretch is derived in logical units.
            live_stretch = max(0.0, float(st.get("h", 0)) / usc - float(st.get("natural_h", 0) or 0))
            pos = (x - wa.x, y - wa.y)
            screen = (wa.width, wa.height)
            # same lock as the worker's render_all: render() mutates module-global
            # render state, so the two must not run at once.
            with _render_lock:
                M = last_frame[0] if last_frame[0] is not None else gather_frame()
                img, fl, nat = st.get("img"), st.get("flex", 0.0), st.get("natural_h", 0)
                for _ in range(2):
                    img, fl, nat = render(frame=M, cfg=cfg, width=int(bw),
                                          stretch=live_stretch, flex_in=fl,
                                          pos=pos, screen=screen, ui_scale=usc)
                st["img"], st["flex"], st["natural_h"] = img, fl, nat
                st["ui_scale"] = usc
                paint(pw, cfg)
            # paint() sets st["w"]/st["h"] to the true on-screen (scaled) size;
            # size AND re-anchor the window to it so the fixed edge stays put.
            _live_place()
            win.queue_draw()
            return False

        def on_press(_w, ev):
            if not st.get("moving"):
                hit = hud_hit(pw)
                if hit:
                    x0, y0, x1, y1, cxs = hit
                    if x0 <= ev.x <= x1 and y0 <= ev.y <= y1:
                        # nearest of the transport buttons
                        actions = ("shuffle", "previous", "play_pause", "next", "repeat")
                        i = min(range(len(cxs)), key=lambda k: abs(ev.x - cxs[k]))
                        spotify_control(actions[i])
                        GLib.timeout_add(120, lambda: (tick(passes=1), False)[1])
                        return True
                return False
            drag["active"] = True
            drag["mode"] = None
            drag["lastx"] = drag["lasty"] = None   # fresh per drag
            drag["sx"], drag["sy"] = ev.x_root, ev.y_root
            wa = monitor_of(_cfg_of()).get_workarea()
            usc0 = float(_cfg_of().get("ui_scale") or 1.0)
            pb = panel_box(_cfg_of(), wa)
            m0, bw0 = pb[4], pb[2]
            # Use the panel's TRUE rendered size (config width x scale, rendered
            # height), not the toplevel allocation — the window can be allocated
            # wider than the panel content, which made it jump to that width the
            # instant a resize began and hid the grip off the panel edge.
            # Use the TRUE on-screen size (matches the drawn grip exactly). A
            # horizontal panel's size is content-driven, so there is nothing to
            # drag-resize — the whole panel is a move handle (size it with the
            # Size slider / section choice).
            cw = max(1, int(st.get("w") or round(bw0 * usc0)))
            ch = max(1, int(st.get("h") or win.get_allocated_height()))
            horizontal = bool(_cfg_of().get("horizontal"))
            # The resize grip is on the panel's inner bottom corner — the side with
            # room — so a right-hugging panel widens LEFTWARD and a left-hugging one
            # rightward, instead of jamming against the edge it sits on.
            grip_left = m0["right"] <= m0["left"]
            on_grip = (not horizontal) and (ev.y >= ch - GRIP - 8) and (
                (ev.x <= GRIP + 8) if grip_left else (ev.x >= cw - GRIP - 8))
            if on_grip:
                drag["mode"] = "resize"
                drag["anchor"] = "right" if grip_left else "left"
                drag["m0"] = m0
                drag["wa"] = wa
                drag["sw0"], drag["sh0"] = cw, ch
            else:
                drag["mode"] = "move"
                # Seed the move from the CONFIG box origin, not get_position()
                # (unreliable for this window) — so the drag tracks from the
                # panel's real on-screen spot.
                drag["wx"], drag["wy"] = pb[0], pb[1]
            # Take an EXPLICIT pointer grab for the drag. The implicit button grab
            # on this desktop window can be stolen/dropped (then the release is
            # lost and the drag wedges); an explicit device grab holds until we
            # ungrab it in on_release/grab-broken.
            dev = ev.get_device()
            gw = win.get_window()
            drag["device"] = None
            if dev is not None and gw is not None:
                try:
                    dev.grab(gw, Gdk.GrabOwnership.NONE, False,
                             Gdk.EventMask.BUTTON_RELEASE_MASK
                             | Gdk.EventMask.POINTER_MOTION_MASK
                             | Gdk.EventMask.BUTTON1_MOTION_MASK,
                             None, ev.time)
                    drag["device"] = dev
                except Exception:
                    pass
            return True

        def on_motion(_w, ev):
            if st.get("moving") and not drag["active"]:
                # Hover feedback: show a resize cursor over the grip corner so the
                # user knows the corner resizes (and the rest moves).
                gw = win.get_window()
                if gw is not None:
                    cwh = int(st.get("w") or win.get_allocated_width())
                    chh = int(st.get("h") or win.get_allocated_height())
                    try:
                        _mm = panel_box(_cfg_of(), monitor_of(_cfg_of()).get_workarea())[4]
                        _gl = _mm["right"] <= _mm["left"]
                    except Exception:
                        _gl = False
                    over = (not _cfg_of().get("horizontal")) and (ev.y >= chh - GRIP - 8) and (
                        (ev.x <= GRIP + 8) if _gl else (ev.x >= cwh - GRIP - 8))
                    try:
                        gw.set_cursor(Gdk.Cursor.new_from_name(
                            disp, ("nesw-resize" if _gl else "nwse-resize") if over else "default"))
                    except Exception:
                        pass
                return False
            if not (st.get("moving") and drag["active"]):
                return False
            if drag.get("mode") == "resize":
                m0, wa = drag["m0"], drag["wa"]
                sw0, sh0 = drag["sw0"], drag["sh0"]
                usc = float(_cfg_of().get("ui_scale") or 1.0)
                # A panel taller than the work area is scaled down to fit, so the
                # on-screen size is config-size x (ui-scale x height-fit scale).
                # The cursor moves in on-screen px, so map it through BOTH scales,
                # and size/position the window in on-screen px too — otherwise the
                # preview tracks at the wrong rate and sits left of the cursor.
                scale = st.get("render_scale", 1.0) or 1.0
                eff = usc * scale
                nat = float(st.get("natural_h") or 120)
                dx = (ev.x_root - drag["sx"]) / eff
                dy = (ev.y_root - drag["sy"]) / eff
                new_h = min(max(nat, sh0 / usc + dy), (wa.height - m0["top"]) / usc)
                new_stretch = int(round(max(0.0, new_h - nat)))
                if drag.get("anchor") == "right":
                    # Right edge fixed; widen leftward (drag the inner grip out).
                    new_bw = min(max(W, sw0 / usc - dx), (wa.width - m0["right"]) / usc)
                    new_left = int(round(wa.width - m0["right"] - new_bw))
                    margins = {**m0, "left": new_left}
                else:
                    # Left edge fixed; widen rightward.
                    new_bw = min(max(W, sw0 / usc + dx), (wa.width - m0["left"]) / usc)
                    new_right = int(round(wa.width - m0["left"] - new_bw))
                    margins = {**m0, "right": new_right}
                live_box[0] = {"idx": pw["idx"], "margins": margins, "stretch": new_stretch}
                st["resizing"] = True
                # Window sized/positioned in ON-SCREEN px with the anchored edge
                # pinned from the CONFIG margins (reliable — get_position() lies
                # for this window), so the moving edge follows the cursor 1:1.
                # Height stays at the current on-screen height (a height-limited
                # panel is auto-fit); the stretch change re-renders and _live_place
                # resizes it — so the panel never shrinks vertically mid-drag.
                new_sw = int(round(new_bw * eff))
                new_sh = int(st.get("h") or new_sw)
                st["w"], st["h"] = new_sw, new_sh
                new_x = (wa.x + wa.width - m0["right"] - new_sw) if drag.get("anchor") == "right" \
                    else (wa.x + m0["left"])
                win.move(int(new_x), int(wa.y + m0["top"]))
                win.set_size_request(new_sw, new_sh)
                # (Input shape is left alone — enter_move cleared it for the whole
                # window, so there is no per-motion re-shape that could drop the grab.)
                win.queue_draw()
                if drag.get("rr"):
                    GLib.source_remove(drag["rr"])
                drag["rr"] = GLib.timeout_add(80, _resize_render)
            else:
                nx = int(drag["wx"] + (ev.x_root - drag["sx"]))
                ny = int(drag["wy"] + (ev.y_root - drag["sy"]))
                w = int(st.get("w") or win.get_allocated_width())
                h = int(st.get("h") or win.get_allocated_height())
                nx, ny = snapped(pw, nx, ny, w, h)
                win.move(nx, ny)
                drag["lastx"], drag["lasty"] = nx, ny   # the commanded position,
            return False                                 # saved verbatim on release

        def on_release(_w, ev):
            if not st.get("moving") or not drag.get("active"):
                return False
            drag["active"] = False
            dev = drag.pop("device", None)
            if dev is not None:
                try:
                    dev.ungrab(ev.time if ev is not None else Gdk.CURRENT_TIME)
                except Exception:
                    pass
            # Did anything actually move? (A grab-broken or a plain click should
            # clean up without rewriting the saved geometry.)
            moved = (live_box[0] is not None) if drag.get("mode") == "resize" \
                else (drag.get("lastx") is not None)
            if not moved:
                if drag.get("rr"):
                    GLib.source_remove(drag["rr"]); drag["rr"] = None
                st["resizing"] = False
                drag["mode"] = None
                return False
            s = dict(load_settings())
            cfgs = [dict(c) for c in (s.get("panels") or [])]
            while len(cfgs) <= pw["idx"]:
                cfgs.append({"monitor": 0})
            pcfg = dict(cfgs[pw["idx"]])
            if drag.get("mode") == "resize":
                if drag.get("rr"):
                    GLib.source_remove(drag["rr"])
                    drag["rr"] = None
                if live_box[0] is not None:
                    pcfg.update(live_box[0]["margins"])
                    if "stretch" in live_box[0]:
                        pcfg["stretch"] = int(live_box[0]["stretch"])
                    live_box[0] = None
                st["resizing"] = False
            else:
                # Use the position we commanded during the drag, not a fresh
                # get_position(): while moving, the window is keep-above and the
                # WM can report a stale/offset position, which saved the panel in
                # the wrong place (or not where it was dropped).
                # Width: for a vertical panel from the CONFIG span (× scale) so a
                # move is a pure translation that preserves width; for a horizontal
                # panel the width is content-driven, so use the true on-screen size.
                _umove = float(_cfg_of().get("ui_scale") or 1.0)
                if _cfg_of().get("horizontal"):
                    ww = int(st.get("w") or win.get_allocated_width() or W)
                else:
                    try:
                        ww = int(round(panel_box(_cfg_of(),
                                       monitor_of(_cfg_of()).get_workarea())[2] * _umove))
                    except Exception:
                        ww = int(st.get("w") or win.get_allocated_width() or W)
                wh = int(st.get("h") or win.get_allocated_height() or 0)
                if drag.get("lastx") is not None:
                    wx, wy = drag["lastx"], drag["lasty"]
                else:
                    wx, wy = win.get_position()
                mon = (disp.get_monitor_at_point(wx + ww // 2, wy + wh // 2)
                       or disp.get_primary_monitor())
                wa = mon.get_workarea()
                # Clamp the drop fully on-screen so a panel pushed to (or past) an
                # edge saves as a 0 gap, never a negative margin — a negative margin
                # would bounce it back inward on reload instead of staying flush.
                wx = max(wa.x, min(wx, wa.x + wa.width - ww))
                wy = max(wa.y, min(wy, wa.y + wa.height - wh))
                lft, top = int(wx - wa.x), int(wy - wa.y)
                pcfg.update({"monitor": mon_index(disp, mon),
                             "left": lft, "top": top,
                             "right": max(0, int(wa.width - lft - ww)),
                             "bottom": max(0, int(wa.height - top - wh))})
                drag["lastx"] = drag["lasty"] = None
            cfgs[pw["idx"]] = pcfg
            s["panels"] = cfgs
            save_settings(s)
            st["placekey"] = None
            if st.get("moving"):   # snap the live window to the saved geometry now
                _live_place()
            return True

        def on_destroy(_w):
            if not pw.get("closing"):
                Gtk.main_quit()

        def on_grab_broken(_w, _ev):
            # If the implicit pointer grab is lost mid-drag the button-release may
            # never arrive; finalise the drag anyway so the size/position is saved
            # and drag['active'] can't stay stuck (which would freeze rendering).
            if drag.get("active"):
                on_release(_w, None)
            return False

        win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                       | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON1_MOTION_MASK)
        win.connect("draw", on_draw)
        win.connect("realize", on_realize)
        win.connect("button-press-event", on_press)
        win.connect("motion-notify-event", on_motion)
        win.connect("button-release-event", on_release)
        win.connect("grab-broken-event", on_grab_broken)
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
        two passes, so a settings change asks for passes=2. Touches no windows,
        and no GDK — tick() snapshots each panel's work area on the main thread
        (GDK is main-thread-only) into st["wa"], read here."""
        with _render_lock:
            if any(pw["state"].get("drag", {}).get("active") for pw in panels):
                return
            M = gather_frame()
            last_frame[0] = M
            cfgs = load_settings().get("panels") or [DEFAULT_CFG]
            for i, pw in enumerate(panels):
                st = pw["state"]
                cfg = cfgs[i] if i < len(cfgs) else {}
                wat = st.get("wa")
                if wat is None:
                    continue   # no work-area snapshot yet; next tick fills it
                wa = types.SimpleNamespace(x=wat[0], y=wat[1], width=wat[2], height=wat[3])
                x, y, bw, bh, m = eff_margins(pw, cfg, wa)
                stretch = float(cfg.get("stretch") or 0)
                usc = float(cfg.get("ui_scale") or 1.0)
                pos = (x - wa.x, y - wa.y)
                screen = (wa.width, wa.height)
                img, fl, nat = st.get("img"), st.get("flex", 0.0), st.get("natural_h", 0)
                for _ in range(max(1, passes)):
                    img, fl, nat = render(frame=M, cfg=cfg, width=bw, stretch=stretch,
                                          flex_in=fl, pos=pos, screen=screen, ui_scale=usc)
                st["img"], st["flex"], st["natural_h"] = img, fl, nat
                st["ui_scale"] = usc
                st["spotify_box"] = _SPOTIFY_BOX["box"]

    # Rendering (the metric sample + the ~70ms Pillow render) runs on a worker
    # thread so it never blocks the GTK main loop / stutters the desktop. The
    # worker fills each panel's st["img"]; _apply_frame, back on the main thread,
    # turns those into cairo surfaces and places/paints the windows. The
    # _render_applied handshake stops the worker overwriting a frame that
    # _apply_frame is still painting. Only the worker calls render_all(), so the
    # module-global render state it touches has a single writer. The synchronous
    # grip-resize path (_resize_render) is left alone — render_all() bails while a
    # drag is active, so it never races the resize render.
    _render_want = threading.Event()
    _render_applied = threading.Event()
    _render_applied.set()
    _render_passes = [1]

    def request_render(passes=1):
        _render_passes[0] = max(_render_passes[0], passes)
        _render_want.set()

    def _apply_frame():
        try:
            s = load_settings()
            cfgs = s.get("panels") or [DEFAULT_CFG]
            if last_frame[0] is not None:
                check_alerts(last_frame[0])
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
                if not st.get("resizing"):
                    paint(pw, cfg)
                if st.get("w") is None:
                    # no frame rendered for this panel yet (e.g. just added) —
                    # don't place() with a missing size; next frame heals it
                    pw["win"].queue_draw()
                    continue
                if not st.get("moving"):
                    key = (st.get("w"), st.get("h"), cfg.get("monitor"), cfg.get("top"),
                           cfg.get("bottom"), cfg.get("left"), cfg.get("right"))
                    if key != st.get("placekey"):
                        st["placekey"] = key
                        place(pw, cfg, st["w"], st["h"])
                    keep_above_desktop(pw["win"])
                    set_hud_shape(pw)
                else:
                    # In move mode, keep the live window sized/positioned to the
                    # real content (unless a drag is actively in progress) so it
                    # always sits where it will on save — no post-release shift.
                    drg = st.get("drag") or {}
                    if not drg.get("active") and not st.get("resizing"):
                        st["live_place"]()
                pw["win"].queue_draw()
        except Exception:
            log(f"apply-frame failed:\n{traceback.format_exc().rstrip()}")
        finally:
            _render_applied.set()
        return False

    def _render_worker():
        while True:
            _render_want.wait()
            _render_applied.wait()   # don't overwrite a frame still being painted
            _render_want.clear()
            passes = _render_passes[0]
            _render_passes[0] = 1
            _render_applied.clear()
            try:
                render_all(passes)
            except Exception:
                log(f"render failed:\n{traceback.format_exc().rstrip()}")
            GLib.idle_add(_apply_frame)

    threading.Thread(target=_render_worker, daemon=True).start()

    def tick(passes=1):
        try:
            s = load_settings()
            cfgs = s.get("panels") or [DEFAULT_CFG]
            if s.get("panel_hidden"):
                # Toggled off from the tray: tear the panel windows down entirely
                # (hiding this desktop/override window doesn't reliably unmap it).
                sync_count(0)
                return True
            sync_count(max(1, len(cfgs)))   # creating/removing windows must be main-thread
            for i, pw in enumerate(panels):
                pw["idx"] = i
                # snapshot the work area here (GDK is main-thread-only); the
                # worker reads st["wa"] instead of calling get_workarea itself
                cfg = cfgs[i] if i < len(cfgs) else {}
                wa = monitor_of(cfg).get_workarea()
                pw["state"]["wa"] = (wa.x, wa.y, wa.width, wa.height)
            request_render(passes)
        except Exception:
            log(f"tick failed:\n{traceback.format_exc().rstrip()}")
        return True

    tick()
    GLib.timeout_add(int(interval * 1000), tick)

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
            _refresh_menu_css()   # keep the tray menu's colours on the current theme
            tick(passes=2)
        return True

    watch_settings.stamp = _settings_stamp()
    GLib.timeout_add(120, watch_settings)

    def _open_settings(*_):
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--settings"],
                         start_new_session=True)

    def _make_tray():
        # A system-tray icon is the only way back to settings or to quit from the
        # GUI — the panel itself is click-through. Best-effort: silently skip if
        # the desktop has no tray.
        try:
            icon = Gtk.StatusIcon()
            _png = os.path.join(CODE_DIR, "icons", "mint-hud-256.png")
            if os.path.exists(_png):
                icon.set_from_file(_png)
            else:
                icon.set_from_icon_name("mint-hud")
            icon.set_tooltip_text("Linux Mint HUD")

            def _set_flag(key, val):
                s = dict(load_settings())
                s[key] = val
                save_settings(s)

            menu = Gtk.Menu()
            mi_show = Gtk.CheckMenuItem(label="Show panel")
            mi_show.set_active(not load_settings().get("panel_hidden"))
            _sh = mi_show.connect("toggled",
                                  lambda it: (_set_flag("panel_hidden", not it.get_active()), tick()))
            mi_alert = Gtk.CheckMenuItem(label="Alerts")
            mi_alert.set_active(not load_settings().get("alerts_muted"))
            _ah = mi_alert.connect("toggled",
                                   lambda it: _set_flag("alerts_muted", not it.get_active()))
            mi_set = Gtk.MenuItem(label="Settings…")
            mi_set.connect("activate", _open_settings)
            mi_new = Gtk.MenuItem(label="What's new…")
            mi_new.connect("activate", _open_whats_new)
            mi_about = Gtk.MenuItem(label="About")
            mi_about.connect("activate", lambda *_: subprocess.Popen(
                ["xdg-open", "https://github.com/Versifft/linux-mint-hud#readme"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            mi_bug = Gtk.MenuItem(label="Report a bug…")
            mi_bug.connect("activate", lambda *_: subprocess.Popen(
                ["xdg-open", "https://github.com/Versifft/linux-mint-hud/issues"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            mi_quit = Gtk.MenuItem(label="Quit panel")
            mi_quit.connect("activate", lambda *_: Gtk.main_quit())
            for mi in (mi_show, mi_alert, Gtk.SeparatorMenuItem(), mi_set, mi_new,
                       mi_about, mi_bug, Gtk.SeparatorMenuItem(), mi_quit):
                menu.append(mi)
            menu.show_all()

            def _popup(ic, b, t):
                # reflect the current saved state each time the menu opens
                s = load_settings()
                for it, h, active in ((mi_show, _sh, not s.get("panel_hidden")),
                                      (mi_alert, _ah, not s.get("alerts_muted"))):
                    it.handler_block(h)
                    it.set_active(active)
                    it.handler_unblock(h)
                menu.popup(None, None, Gtk.StatusIcon.position_menu, ic, b, t)

            icon.connect("popup-menu", _popup)
            icon.connect("activate", _open_settings)
            return icon
        except Exception:
            return None

    _tray = _make_tray()   # kept referenced so it isn't garbage-collected

    # One-time "what's new" announcement on the first run after an update.
    GLib.timeout_add_seconds(3, notify_version_update)

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

    try:
        _probe = open(LOCK_FILE, "a+")
        try:
            fcntl.flock(_probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(_probe, fcntl.LOCK_UN)
            subprocess.Popen([sys.executable, os.path.abspath(__file__)],
                             start_new_session=True)
        except OSError:
            pass
        finally:
            _probe.close()
    except Exception:
        pass

    _sinit = load_settings()
    _init_theme = ((_sinit.get("panels") or [{}])[0].get("theme")
                   or _sinit.get("theme") or "midnight")
    prov = Gtk.CssProvider()

    def apply_settings_css(name=None):
        """Style the settings window in the edited panel's theme, so it matches the
        skin. Now that the window is fully app-drawn (custom titlebar + no
        border_width frame), the theme colours the whole surface — titlebar,
        groups, inputs and all. Transparent/image/light themes are handled by
        make_settings_css (a solid dark chrome for the see-through ones, a light
        palette for the light ones)."""
        dark, data = make_settings_css(name or _init_theme)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", dark)
        try:
            prov.load_from_data(data)
        except Exception:
            pass
    apply_settings_css(_init_theme)
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
    # Standard window-manager titlebar, so the settings window looks like any
    # other window on the desktop; only the body is themed (below). An opaque
    # (24-bit) visual keeps the window free of an ARGB margin.
    _sysvis = win.get_screen().get_system_visual()
    if _sysvis is not None:
        win.set_visual(_sysvis)
    outer = _cls(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0), "content")
    win.add(outer)

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    outer.pack_start(scroller, True, True, 0)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    _cls(content, "content", "pagebody")
    # Inset the content with CSS padding, NOT set_border_width: a container
    # border width is drawn OUTSIDE the widget's background, so on this theme it
    # showed as a 20px black frame around the content (visible over a light
    # backdrop); CSS padding is inside the background and paints the panel colour.
    scroller.add(content)

    header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    header.set_margin_bottom(8)
    header.pack_start(_cls(Gtk.Label(label="Linux Mint HUD", xalign=0), "app-title"), False, False, 0)
    content.pack_start(header, False, False, 0)

    _grp = [0]
    _ui_exp = load_settings().get("ui_expanded") or {}
    _group_exp = {}   # title -> Expander, so groups can be reordered after creation
    _lbl_sg = Gtk.SizeGroup(Gtk.SizeGroupMode.HORIZONTAL)   # one label column for every group

    def _noscroll(w):
        """Stop the mouse wheel from changing a combo/spin value while the user
        is just scrolling the window past it."""
        w.connect("scroll-event", lambda *a: True)
        return w

    def _on_expand(exp, _p, title):
        if _loading[0]:
            return
        new = dict(load_settings())
        ex = dict(new.get("ui_expanded") or {})
        ex[title] = exp.get_expanded()
        new["ui_expanded"] = ex
        save_settings(new)
        _own_stamp[0] = _file_stamp()

    def make_group(title, advanced=False):
        """One collapsible section (a Gtk.Expander). Everything is on a single
        page, but each group can be folded away to stay compact; basic groups
        start open and advanced ones folded, and the open/closed choice is
        remembered in settings."""
        exp = Gtk.Expander()
        _group_exp[title] = exp
        _ghead = _cls(Gtk.Label(label=title.upper(), xalign=0), "group-heading")
        _ghead.set_hexpand(True)   # fill the row so the title's divider line spans full width
        exp.set_label_widget(_ghead)
        exp.set_label_fill(True)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner.set_margin_top(10)
        inner.set_margin_bottom(10)
        inner.set_margin_start(20)   # align field labels under the heading text, not the arrow
        g = Gtk.Grid(row_spacing=10, column_spacing=14)
        inner.pack_start(g, False, False, 0)
        exp.add(inner)
        exp.set_expanded(bool(_ui_exp.get(title, not advanced)))
        exp.connect("notify::expanded", _on_expand, title)   # connect after, so the
        content.pack_start(exp, False, False, 0)              # initial state doesn't save
        _grp[0] += 1
        return inner, g, [0]

    def field(grid, counter, label_text, widget):
        """Attach a label/control row inside a group grid. All labels share one
        column width (so controls line up down the page); a control keeps an
        explicit halign=START (the numeric spins) and only FILL controls stretch."""
        if label_text:
            lbl = _cls(Gtk.Label(label=label_text, xalign=0, yalign=0.5), "field-label")
            _lbl_sg.add_widget(lbl)
            grid.attach(lbl, 0, counter[0], 1, 1)
            if widget.get_halign() in (Gtk.Align.FILL, Gtk.Align.BASELINE):
                widget.set_hexpand(True)
            grid.attach(widget, 1, counter[0], 1, 1)
        else:
            widget.set_hexpand(True)
            grid.attach(widget, 0, counter[0], 2, 1)
        counter[0] += 1

    def hint(grid, counter, text):
        """A full-width grey explanatory line inside a group."""
        field(grid, counter, "",
              _cls(Gtk.Label(label=text, xalign=0, wrap=True), "hint"))

    # A per-section group can show an "off on this panel" banner at its top so it
    # is clear its settings don't affect anything until the section is enabled in
    # Content. The predicate is evaluated at refresh time (checks exist by then).
    _off_banners = []

    def off_banner(grid, counter, pred):
        lbl = _cls(Gtk.Label(
            label="This section is off on this panel — turn it on in Content.",
            xalign=0, wrap=True), "off-note")
        lbl.set_no_show_all(True)
        field(grid, counter, "", lbl)
        _off_banners.append((lbl, pred))

    def _refresh_off_banners():
        for lbl, pred in _off_banners:
            try:
                lbl.set_visible(not pred())
            except Exception:
                lbl.set_visible(False)

    def apply_group_order(order):
        """Arrange the setting groups on the page in `order` (a list of titles);
        groups omitted from the list keep their original relative order at the
        end. Groups are created in code order (so widget dependencies are safe)
        and only repositioned here. The top group gets the 'first' class so it
        has no divider above its title."""
        if not _group_exp:
            return
        kids = content.get_children()
        base = min(kids.index(e) for e in _group_exp.values())   # first group slot
        seq = [t for t in order if t in _group_exp]
        seq += [t for t in _group_exp if t not in seq]           # dict = insertion order
        for i, t in enumerate(seq):
            content.reorder_child(_group_exp[t], base + i)
            ctx = _group_exp[t].get_style_context()
            (ctx.add_class if i == 0 else ctx.remove_class)("first")

    pbox, pg, pc = make_group("Panels")
    disp = Gdk.Display.get_default()
    editing = [0]
    _loading = [False]

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

    # Placing and sizing a panel is its own group, kept apart from identity.
    posbox, posg, posc = make_group("Position & size")
    move1 = _cls(Gtk.Button(label="Move panel…"), "ghost")
    move1.set_halign(Gtk.Align.START)
    reset_btn = _cls(Gtk.Button(label="Reset all panels to top-right…"), "subtle")
    reset_btn.set_halign(Gtk.Align.START)
    place_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    place_box.pack_start(move1, False, False, 0)
    place_box.pack_start(reset_btn, False, False, 0)
    field(posg, posc, "", place_box)

    def refresh_move_labels():
        mv = load_settings().get("move")
        move1.set_label("Save position" if mv == editing[0] else "Move panel…")

    def toggle_move(*_):
        idx = editing[0]
        st = dict(load_settings())
        if st.get("move") == idx:
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
        if dup:
            new = json.loads(json.dumps(src))   # deep copy: don't share lists
            if (src.get("name") or "").strip():  # (module_ids, order, …)
                base = (src["name"] or "").strip()
                head, _, tail = base.rpartition(" ")
                root = head.strip() if (head and tail.isdigit()) else base
                existing = {(p.get("name") or "").strip() for p in cfgs}
                n = 2
                while f"{root} {n}" in existing:
                    n += 1
                new["name"] = f"{root} {n}"
        else:
            new = {"monitor": src.get("monitor", 0),
                   "sections": {k: True for k in SECTION_ORDER},
                   "order": list(SECTION_ORDER),
                   "module_ids": [],   # a fresh panel picks its own modules
                   "top": MARGIN, "bottom": MARGIN, "left": None, "right": MARGIN}
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
        name = _panel_label(editing[0], cfgs[editing[0]] if editing[0] < len(cfgs) else {})
        dlg = Gtk.MessageDialog(transient_for=win, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL,
                                text=f"Remove {name}?")
        dlg.format_secondary_text("Its sections, position, sensors and devices are "
                                  "discarded. This can't be undone.")
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
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
        dlg = Gtk.MessageDialog(transient_for=win, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL,
                                text="Reset all panels to the top-right corner?")
        dlg.format_secondary_text("Every panel's position and size is put back to the "
                                  "top-right default. This can't be undone.")
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        st = dict(load_settings())
        cfgs = [dict(c) for c in (st.get("panels") or [{}])]
        for c in cfgs:
            # reset the actual geometry keys (the old position/offset keys are
            # stripped on load, so writing them did nothing)
            for k in ("position", "offset", "vmargin", "hmargin"):
                c.pop(k, None)
            # flush into the actual top-right corner (0 gaps), now that the
            # placement puts top=0 truly at the top edge
            c["top"], c["bottom"], c["right"], c["left"] = 0, 0, 0, None
        st["panels"] = cfgs
        st["move"] = None
        save_settings(st)
        _refill_combo()
        load_panel()
        refresh_move_labels()
        status.set_text("All panels reset to the top-right corner.")

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

    def _panel_wa(pcfg):
        mon = (disp.get_monitor(pcfg.get("monitor", 0))
               or disp.get_primary_monitor() or disp.get_monitor(0))
        return mon.get_workarea()

    def _resolved_margins(pcfg):
        return panel_box(pcfg, _panel_wa(pcfg))[4]

    _m0 = _resolved_margins(_P0)
    margin_spins = {}
    for _key, _lbl in (("top", "Top"), ("left", "Left"), ("right", "Right")):
        sp = _noscroll(Gtk.SpinButton.new_with_range(0, 4000, 1))
        sp.set_value(_m0.get(_key, 22))
        sp.set_hexpand(False)
        sp.set_halign(Gtk.Align.START)
        sp.set_size_request(110, -1)
        field(posg, posc, _lbl, sp)
        margin_spins[_key] = sp
    # Height is content-driven; this is the extra the gaps spread beyond content
    # (0 = exactly content height, so toggling a section resizes the panel).
    stretch_spin = _noscroll(Gtk.SpinButton.new_with_range(0, 3000, 1))
    stretch_spin.set_value(int(_P0.get("stretch") or 0))
    stretch_spin.set_hexpand(False)
    stretch_spin.set_halign(Gtk.Align.START)
    stretch_spin.set_size_request(110, -1)
    field(posg, posc, "Extra height", stretch_spin)
    horiz_switch = Gtk.Switch()
    horiz_switch.set_halign(Gtk.Align.START)
    field(posg, posc, "Horizontal layout", horiz_switch)
    hint(posg, posc, "Gaps are in pixels from the screen edge. Set both Left and "
                     "Right to control the width. “Extra height” adds space below "
                     "the content so the sections spread further apart (0 = fit "
                     "the content exactly). “Horizontal layout” lines the sections "
                     "up left-to-right instead of stacking them.")

    # ---- Appearance (theme + background + fine knobs), per panel ----------
    THEME_ORDER = ("midnight", "obsidian", "onyx", "graphite", "slatemono",
                   "nord", "dracula", "rosedusk", "neonnoir", "gruvbox", "umber",
                   "catppuccin", "solarized", "matrix", "daylight", "sandstone",
                   "solarized-light", "transparent", "wallpaper")
    THEME_LABELS = {"midnight": "Midnight (default)", "obsidian": "Obsidian",
                    "onyx": "Onyx (AMOLED)", "graphite": "Graphite",
                    "slatemono": "Slate Mono", "nord": "Nord", "dracula": "Dracula",
                    "rosedusk": "Rosé Dusk", "neonnoir": "Neon Noir",
                    "gruvbox": "Gruvbox", "umber": "Umber", "catppuccin": "Catppuccin",
                    "solarized": "Solarized (dark)", "matrix": "Matrix",
                    "daylight": "Daylight (light)", "sandstone": "Sandstone (light)",
                    "solarized-light": "Solarized (light)",
                    "transparent": "Transparent", "wallpaper": "Wallpaper image"}
    _, apg, apc = make_group("Appearance")

    theme_combo = _noscroll(Gtk.ComboBoxText())
    for _tid in THEME_ORDER:
        theme_combo.append(_tid, THEME_LABELS[_tid])
    theme_combo.set_halign(Gtk.Align.FILL)
    theme_combo.set_size_request(190, -1)
    field(apg, apc, "Theme", theme_combo)
    hint(apg, apc, "Changing the theme keeps your tweaks below. Use "
                   "“Reset to theme defaults” to clear them.")

    bgmode_combo = _noscroll(Gtk.ComboBoxText())
    bgmode_combo.append("", "Theme default")
    for _mid, _ml in (("glass", "Glass"), ("solid", "Solid"),
                      ("transparent", "Transparent"), ("image", "Image")):
        bgmode_combo.append(_mid, _ml)
    bgmode_combo.set_halign(Gtk.Align.FILL)
    bgmode_combo.set_size_request(190, -1)
    field(apg, apc, "Background style", bgmode_combo)

    # One accent control: pick a preset, "Theme default", or "Custom…" (which
    # enables the colour button beside it). Replaces three overlapping widgets.
    ACCENT_PRESETS = (("Sky blue", (96, 176, 255)), ("Violet", (180, 120, 255)),
                      ("Pink", (255, 96, 180)), ("Cyan", (48, 228, 236)),
                      ("Teal", (40, 200, 180)), ("Green", (72, 199, 116)),
                      ("Lime", (170, 220, 90)), ("Amber", (240, 176, 80)),
                      ("Orange", (240, 140, 70)), ("Coral", (224, 128, 93)),
                      ("Red", (233, 84, 82)), ("Rose", (255, 140, 170)),
                      ("Magenta", (220, 90, 230)), ("Gold", (240, 200, 90)))
    accent_btn = Gtk.ColorButton()
    accent_preset = _noscroll(Gtk.ComboBoxText())
    accent_preset.append("", "Theme default")
    for _nm, _c in ACCENT_PRESETS:
        accent_preset.append("%d,%d,%d" % _c, _nm)
    accent_preset.append("custom", "Custom…")
    accbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    accbox.pack_start(accent_preset, True, True, 0)
    accbox.pack_start(accent_btn, False, False, 0)
    field(apg, apc, "Accent colour", accbox)

    def _mkscale(lo, hi, step):
        sc = _noscroll(Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step))
        sc.set_draw_value(True); sc.set_value_pos(Gtk.PositionType.RIGHT)
        sc.set_halign(Gtk.Align.FILL)   # full-width slider bar (field() hexpands FILL)
        # every slider here is a percentage — show the unit on the value
        sc.connect("format-value", lambda _s, v: f"{v:.0f}%")
        return sc
    opacity_scale = _mkscale(0, 100, 1)
    field(apg, apc, "Opacity", opacity_scale)
    radius_spin = _noscroll(Gtk.SpinButton.new_with_range(0, 48, 1))
    radius_spin.set_hexpand(False); radius_spin.set_halign(Gtk.Align.START)
    radius_spin.set_size_request(110, -1)
    field(apg, apc, "Corner rounding", radius_spin)
    border_scale = _mkscale(0, 200, 5)
    field(apg, apc, "Outline strength", border_scale)
    glow_scale = _mkscale(0, 200, 5)
    field(apg, apc, "Top-edge glow", glow_scale)

    # A plain button that opens an explicit file chooser on click. The old
    # Gtk.FileChooserButton opened its own internal dialog, which never appeared
    # on this desktop (clicking did nothing); the explicit dialog — exactly how
    # Import/Export work — opens reliably.
    bg_image_btn = _cls(Gtk.Button(), "actionbtn")
    bg_image_btn.set_halign(Gtk.Align.FILL)
    bg_image_btn.set_size_request(220, -1)
    _bg_image_lbl = Gtk.Label(label="(None)")
    _bg_image_lbl.set_xalign(0.0)
    bg_image_btn.add(_bg_image_lbl)
    _bg_image = {"path": None}
    def _bg_image_set(path):
        _bg_image["path"] = path or None
        name = os.path.basename(path) if path else "(None)"
        if len(name) > 30:
            name = name[:14] + "…" + name[-13:]
        _bg_image_lbl.set_text(name)
    def _pick_bg_image(_btn):
        dlg = Gtk.FileChooserDialog(title="Background image", transient_for=win,
                                    action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Choose", Gtk.ResponseType.ACCEPT)
        flt = Gtk.FileFilter(); flt.set_name("Images")
        for _p in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp"):
            flt.add_pattern(_p)
        dlg.add_filter(flt)
        if _bg_image["path"]:
            try:
                dlg.set_filename(_bg_image["path"])
            except Exception:
                pass
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            _bg_image_set(dlg.get_filename())
            commit_appearance()
        dlg.destroy()
    bg_image_btn.connect("clicked", _pick_bg_image)
    field(apg, apc, "Background image", bg_image_btn)
    blur_spin = _noscroll(Gtk.SpinButton.new_with_range(0, 40, 1))
    blur_spin.set_hexpand(False); blur_spin.set_halign(Gtk.Align.START)
    blur_spin.set_size_request(110, -1)
    field(apg, apc, "Image blur", blur_spin)
    imgsize_scale = _mkscale(5, 300, 5)
    field(apg, apc, "Image size", imgsize_scale)
    tile_switch = Gtk.Switch()
    tile_switch.set_halign(Gtk.Align.START)
    field(apg, apc, "Repeat image", tile_switch)

    contrast_scale = _mkscale(0, 100, 5)
    field(apg, apc, "Text contrast", contrast_scale)
    uiscale_scale = _mkscale(50, 200, 5)
    field(apg, apc, "Size", uiscale_scale)

    appearance_reset = _cls(Gtk.Button(label="Reset to theme defaults"), "subtle")
    appearance_reset.set_halign(Gtk.Align.START)
    field(apg, apc, "", appearance_reset)

    # ---- Text & data colours (per role, per panel) -----------------------
    # One picker per text/data colour role. Each follows the theme until set
    # here; "Theme default" clears the override. Same widget shape as the
    # accent picker (preset dropdown + colour button). Commit/reload below
    # iterate color_pickers, so adding a role to COLOR_ROLES is enough.
    _, tcg, tcc = make_group("Text")
    textsize_scale = _mkscale(75, 140, 5)
    field(tcg, tcc, "Text size", textsize_scale)
    color_pickers = {}   # role -> (preset_combo, colour_button)
    for _role, _rlabel in COLOR_ROLES:
        _cbtn = Gtk.ColorButton()
        _cpre = _noscroll(Gtk.ComboBoxText())
        _cpre.append("", "Theme default")
        for _nm, _c in ACCENT_PRESETS:
            _cpre.append("%d,%d,%d" % _c, _nm)
        _cpre.append("custom", "Custom…")
        _cbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        _cbox.pack_start(_cpre, True, True, 0)
        _cbox.pack_start(_cbtn, False, False, 0)
        field(tcg, tcc, _rlabel, _cbox)
        color_pickers[_role] = (_cpre, _cbtn)
    hint(tcg, tcc, "“Text size” scales just the lettering (the panel “Size” "
                   "under Appearance scales everything). Each text colour "
                   "follows the theme until you set it — “Titles” are the "
                   "section headers, “Values / data” the readouts, “Subtitles / "
                   "muted” the secondary text. Pick a preset or Custom… to "
                   "override just that colour for this panel; “Theme default” "
                   "hands it back. The accent colour is set above, and “Reset "
                   "to theme defaults” clears all of these.")

    # Plain-language tooltips on the fine-tune controls (none existed before).
    for _w, _tip in (
        (theme_combo, "The overall colour scheme for this panel."),
        (bgmode_combo, "How the panel background looks: frosted glass, solid, "
                       "see-through, or your own image."),
        (accent_preset, "The highlight colour — gauges, graphs and highlights."),
        (opacity_scale, "How solid the panel background is (lower = more see-through)."),
        (radius_spin, "How rounded the panel's corners are, in pixels."),
        (border_scale, "Strength of the thin outline around the panel."),
        (glow_scale, "Strength of the soft glow along the top edge."),
        (contrast_scale, "Nudges the text brighter or dimmer against the background."),
        (uiscale_scale, "Scales the whole panel — fonts and all — up or down."),
        (blur_spin, "Blur applied to a background image, in pixels."),
        (imgsize_scale, "How large the background image is drawn: 100% fills the "
                        "panel, below shows more of the image, above zooms in."),
        (tile_switch, "Repeat the image to fill the panel (tile it) instead of "
                      "showing it once on the background colour."),
        (stretch_spin, "Extra height beyond the content; spreads the sections apart."),
        (appearance_reset, "Put this panel's appearance back to its theme defaults."),
    ):
        _w.set_tooltip_text(_tip)

    _, wg, wc = make_group("Weather", advanced=True)
    off_banner(wg, wc, lambda: checks["top"].get_active() or checks["weather"].get_active())
    def _panel_locs(c):
        ls = c.get("locations")
        if isinstance(ls, list) and ls:
            return [dict(L) for L in ls if isinstance(L, dict) and L.get("lat") is not None]
        one = c.get("location") or s.get("location")
        return [dict(one)] if isinstance(one, dict) and one.get("lat") is not None else []

    loc_state = {"list": _panel_locs(_P0)}
    locs_list = Gtk.ListBox()
    locs_list.set_selection_mode(Gtk.SelectionMode.NONE)
    _cls(locs_list, "order-list")

    def _del_loc(ix):
        if 0 <= ix < len(loc_state["list"]):
            loc_state["list"].pop(ix)
            _refresh_locs()
            commit()

    def _refresh_locs():
        for ch in locs_list.get_children():
            locs_list.remove(ch)
        for i, L in enumerate(loc_state["list"]):
            row = Gtk.ListBoxRow()
            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hb.set_margin_start(10); hb.set_margin_end(6)
            hb.set_margin_top(3); hb.set_margin_bottom(3)
            hb.pack_start(Gtk.Label(label=L.get("name", "?"), xalign=0), True, True, 0)
            rm = _cls(Gtk.Button(label="✕"), "subtle")
            rm.connect("clicked", lambda _b, ix=i: _del_loc(ix))
            hb.pack_end(rm, False, False, 0)
            row.add(hb)
            locs_list.add(row)
        locs_list.show_all()
        _locs_empty.set_visible(not loc_state["list"])

    field(wg, wc, "Locations", locs_list)
    _locs_empty = _cls(Gtk.Label(label="No location yet — add one below.", xalign=0), "hint")
    field(wg, wc, "", _locs_empty)
    town = Gtk.Entry()
    town.set_placeholder_text("Add a town or city")
    addloc = _cls(Gtk.Button(label="Add"), "subtle")
    addbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    addbox.pack_start(town, True, True, 0)
    addbox.pack_start(addloc, False, False, 0)
    field(wg, wc, "", addbox)
    loc_label = _cls(Gtk.Label(label="", xalign=0), "result")
    loc_label.set_no_show_all(True)
    field(wg, wc, "", loc_label)
    rotate_spin = _noscroll(Gtk.SpinButton.new_with_range(2, 600, 1))
    rotate_spin.set_value(int(_P0.get("weather_rotate") or s.get("weather_rotate") or 8))
    rotate_spin.set_hexpand(False); rotate_spin.set_halign(Gtk.Align.START)
    rotate_spin.set_size_request(110, -1)
    field(wg, wc, "Rotate every (s)", rotate_spin)

    showloc_chk = Gtk.CheckButton(label="Show location name")
    showloc_chk.set_active(bool(_P0.get("weather_show_location", True)))
    field(wg, wc, "", showloc_chk)
    hint(wg, wc, "Locations are per panel. Add several cities and the weather "
                 "rotates through them; US cities show °F automatically. "
                 "“Rotate every” sets how long each city shows (only matters "
                 "with more than one).")

    def do_add_loc(_b):
        q = town.get_text().strip()
        if not q:
            return
        try:
            url = "https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + urllib.parse.quote(q)
            res = json.load(urllib.request.urlopen(url, timeout=8))["results"][0]
            _nl = {"lat": res["latitude"], "lon": res["longitude"], "name": res["name"]}
            if (res.get("country_code") or "").upper() == "US":
                _nl["units"] = "f"       # US cities default to Fahrenheit
            loc_state["list"].append(_nl)
            town.set_text("")
            loc_label.get_style_context().remove_class("result")
            loc_label.get_style_context().add_class("result-ok")
            loc_label.set_text(f"✓ added {res['name']}, {res.get('admin1', '')} {res['country_code']}")
            _refresh_locs()
            commit()
        except Exception:
            loc_label.get_style_context().remove_class("result-ok")
            loc_label.get_style_context().add_class("result")
            loc_label.set_text("couldn't find that place")
        loc_label.set_visible(True)
    addloc.connect("clicked", do_add_loc)
    town.connect("activate", do_add_loc)
    rotate_spin.connect("value-changed", lambda *_: commit())
    _refresh_locs()

    # One list that is both "what to show" (the checkbox) and "in what order"
    # (drag the ≡ handle) — the old separate Content grid + Order list merged, so
    # there's one list of the 18 sections instead of two.
    _, seg, sec_ = make_group("Content")
    full_order = list(_P0.get("order") or SECTION_ORDER)
    checks = {}
    content_list = Gtk.ListBox()
    content_list.set_selection_mode(Gtk.SelectionMode.NONE)
    _cls(content_list, "order-list")
    _ROW_TARGET = [Gtk.TargetEntry.new("HUD_ROW", Gtk.TargetFlags.SAME_APP, 0)]
    _drag_src = {"key": None}
    _content_rows = {}

    def _c_drag_begin(w, ctx):
        _drag_src["key"] = w.key
        w.row.get_style_context().add_class("dragging")

    def _c_drag_end(w, ctx):
        w.row.get_style_context().remove_class("dragging")
        _drag_src["key"] = None

    def _c_drag_get(w, ctx, sel, info, t):
        sel.set(sel.get_target(), 8, w.key.encode())

    def _c_drag_received(dest, ctx, x, y, sel, info, t):
        sk, dk = _drag_src["key"], dest.key
        if not sk or sk == dk or sk not in full_order or dk not in full_order:
            return
        full_order.remove(sk)
        di = full_order.index(dk)
        if y > dest.get_allocated_height() / 2:
            di += 1
        full_order.insert(di, sk)
        _reorder_content_rows()
        commit()

    def _make_content_row(key):
        row = Gtk.ListBoxRow(); row.key = key
        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hb.set_margin_start(4); hb.set_margin_end(4)
        hb.set_margin_top(3); hb.set_margin_bottom(3)
        cb = Gtk.CheckButton()
        cb.set_active((_P0.get("sections") or {}).get(key, True))
        checks[key] = cb
        hb.pack_start(cb, False, False, 0)
        hb.pack_start(Gtk.Label(label=SECTION_LABELS.get(key, key), xalign=0), True, True, 0)
        handle = Gtk.EventBox(); handle.key = key; handle.row = row
        handle.add(_cls(Gtk.Label(label="≡"), "hint"))
        handle.set_tooltip_text("Drag to reorder")
        handle.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, _ROW_TARGET, Gdk.DragAction.MOVE)
        handle.connect("drag-begin", _c_drag_begin)
        handle.connect("drag-end", _c_drag_end)
        handle.connect("drag-data-get", _c_drag_get)
        hb.pack_start(handle, False, False, 0)
        ev = Gtk.EventBox(); ev.key = key; ev.add(hb)
        ev.drag_dest_set(Gtk.DestDefaults.ALL, _ROW_TARGET, Gdk.DragAction.MOVE)
        ev.connect("drag-data-received", _c_drag_received)
        row.add(ev)
        return row

    def _reorder_content_rows():
        for r in list(content_list.get_children()):
            content_list.remove(r)          # unparent; refs kept in _content_rows
        for k in full_order:
            r = _content_rows.get(k)
            if r is not None:
                content_list.add(r)
        content_list.show_all()

    for _k in full_order:
        _content_rows[_k] = _make_content_row(_k)
        content_list.add(_content_rows[_k])
    field(seg, sec_, "", content_list)
    hint(seg, sec_, "Tick what to show on this panel; drag the ≡ handle to reorder.")

    _, clkg, clkc = make_group("Clock", advanced=True)
    off_banner(clkg, clkc, lambda: checks["clock"].get_active())
    clock_24h_chk = Gtk.CheckButton(label="24-hour time")
    clock_24h_chk.set_active(bool(_P0.get("clock_24h", True)))
    clock_sec_chk = Gtk.CheckButton(label="Show seconds")
    clock_sec_chk.set_active(bool(_P0.get("clock_seconds", False)))
    clock_date_chk = Gtk.CheckButton(label="Show date")
    clock_date_chk.set_active(bool(_P0.get("clock_date", True)))
    clkrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
    for _c in (clock_24h_chk, clock_sec_chk, clock_date_chk):
        clkrow.pack_start(_c, False, False, 0)
    field(clkg, clkc, "", clkrow)

    import datetime as _dt
    _now = _dt.datetime.now()
    DATE_FORMATS = ["%a, %d %b %Y", "%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d",
                    "%d/%m/%Y", "%b %d, %Y", "%A, %d. %B %Y", "%x"]
    date_fmt_combo = _noscroll(Gtk.ComboBoxText())
    for _f in DATE_FORMATS:
        label = ("System format" if _f == "%x" else _now.strftime(_f))
        date_fmt_combo.append(_f, label)
    date_fmt_combo.set_active_id(_P0.get("clock_date_format") or "%a, %d %b %Y")
    date_fmt_combo.set_halign(Gtk.Align.FILL)
    date_fmt_combo.set_tooltip_text("How the date is written. “System format” follows "
                                    "your language/region settings; the time format is "
                                    "set by the 24-hour and seconds options above.")
    field(clkg, clkc, "Date format", date_fmt_combo)

    _, dg, dc = make_group("Disks", advanced=True)
    off_banner(dg, dc, lambda: checks["disk"].get_active())
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

    _, tg, tc = make_group("Temperatures", advanced=True)
    off_banner(tg, tc, lambda: checks["thermals"].get_active())
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
    field(tg, tc, "", _cls(Gtk.Label(label="The first 4 ticked sensors are shown.", xalign=0), "hint"))

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
                commit_sensor_names()
            ent.connect("changed", _on_name)
            row.pack_start(ent, True, True, 0)
            names_box.pack_start(row, False, False, 0)
        names_box.show_all()

    tg.attach(_cls(Gtk.Label(label="Names", xalign=0, yalign=0), "field-label"), 0, tc[0], 1, 1)
    names_box.set_hexpand(True)
    tg.attach(names_box, 1, tc[0], 1, 1)
    tc[0] += 1

    _, glg, glc = make_group("Gauge names", advanced=True)
    gauge_names_state = dict(s.get("gauge_names") or {})

    def commit_gauge_names(*_):
        if _loading[0]:
            return
        new = dict(load_settings())
        new["gauge_names"] = {k: v.strip() for k, v in gauge_names_state.items() if v.strip()}
        save_settings(new)
        _own_stamp[0] = _file_stamp()

    gauge_name_entries = {}
    for _gk, _gd in (("cpu", "CPU"), ("gpu", "GPU"), ("ram", "RAM")):
        _ge = Gtk.Entry()
        _ge.set_placeholder_text(_gd)
        _ge.set_text(gauge_names_state.get(_gk, ""))

        def _on_gname(e, gk=_gk):
            if _loading[0]:
                return
            gauge_names_state[gk] = e.get_text()
            commit_gauge_names()
        _ge.connect("changed", _on_gname)
        gauge_name_entries[_gk] = _ge
        field(glg, glc, _gd, _ge)
    hint(glg, glc, "Rename the CPU / GPU / RAM gauges.")

    _, deg, dec = make_group("Device batteries", advanced=True)
    off_banner(deg, dec, lambda: checks["devices"].get_active())
    cur_periph = _P0.get("peripherals") or []
    periph_names_state = dict(s.get("peripheral_names") or {})
    periph_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
    periph_checks = {}

    def rebuild_periph_rows(sel):
        for c in periph_box.get_children():
            periph_box.remove(c)
        periph_checks.clear()
        devs = peripheral_batteries()
        present = {p["id"] for p in devs}
        for pid in sel:
            if pid not in present:
                devs.append({"id": pid, "name": _periph_name_from_id(pid),
                             "capacity": None, "status": "disconnected"})
        if not devs:
            periph_box.pack_start(
                _cls(Gtk.Label(label="No wireless mouse/keyboard/headset batteries detected.",
                               xalign=0), "hint"), False, False, 0)
            periph_box.show_all()
            return
        for p in devs:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            cap = p["capacity"]
            suffix = f"   {cap}%" if cap is not None else "   (off)"
            cb = Gtk.CheckButton(label=_periph_short(p["name"]) + suffix)
            cb.set_active(p["id"] in sel)
            cb.connect("toggled", commit)
            periph_checks[p["id"]] = cb
            ent = Gtk.Entry()
            ent.set_placeholder_text(_periph_short(p["name"]))
            ent.set_text(periph_names_state.get(p["id"], ""))
            ent.set_width_chars(14)

            def _on_pname(e, pid=p["id"]):
                if _loading[0]:
                    return
                periph_names_state[pid] = e.get_text()
                commit_periph_names()
            ent.connect("changed", _on_pname)
            row.pack_start(cb, True, True, 0)
            row.pack_start(ent, False, False, 0)
            periph_box.pack_start(row, False, False, 0)
        periph_box.show_all()
    field(deg, dec, "", periph_box)
    hint(deg, dec, "Tick the devices to show on this panel; the names you give "
                   "them are shared across all panels.")

    _, alg, alc = make_group("Alerts", advanced=True)
    _alerts0 = load_settings().get("alerts") or {}
    alert_rows = {}
    for _ak, _albl, _aunit, _adef, _aop in ALERT_DEFS:
        _r = _alerts0.get(_ak) or {}
        _cb = Gtk.CheckButton(label=f"{_albl} {_aop}")
        _cb.set_active(bool(_r.get("on")))
        _sp = _noscroll(Gtk.SpinButton.new_with_range(0, 250, 1))
        _sp.set_value(float(_r.get("th", _adef)))
        _sp.set_size_request(90, -1)
        _sp.set_halign(Gtk.Align.START)
        _rb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        _rb.pack_start(_cb, False, False, 0)
        _rb.pack_start(_sp, False, False, 0)
        _rb.pack_start(_cls(Gtk.Label(label=_aunit, xalign=0), "hint"), False, False, 0)
        field(alg, alc, "", _rb)
        alert_rows[_ak] = (_cb, _sp)
    hint(alg, alc, "You get a desktop notification when a value stays past its "
                   "limit for 15 s (repeats at most every 10 min).")

    def commit_alerts(*_):
        if _loading[0]:
            return
        new = dict(load_settings())
        new["alerts"] = {k: {"on": cb.get_active(), "th": sp.get_value()}
                         for k, (cb, sp) in alert_rows.items()}
        save_settings(new)
        _own_stamp[0] = _file_stamp()
    for _cb, _sp in alert_rows.values():
        _cb.connect("toggled", commit_alerts)
        _sp.connect("value-changed", commit_alerts)

    _, modg, modc = make_group("Custom modules", advanced=True)
    modules_state = [dict(m) for m in (load_settings().get("modules") or [])]
    modules_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    pick_mids = list(_P0.get("module_ids") or [])   # edited panel's module ids (ordered)
    _picker_refresh = [lambda: None]    # set by the per-panel picker group below

    def _panels_showing(mid):
        """Names of the panels whose module_ids include `mid` (for the 'Shown on'
        line under each definition). Reflects saved state."""
        out = []
        for i, p in enumerate(load_settings().get("panels") or []):
            if mid in (p.get("module_ids") or []):
                out.append(_panel_label(i, p))
        return out

    def commit_module_ids():
        """Write the edited panel's module selection (pick_mids)."""
        if _loading[0]:
            return
        new = dict(load_settings())
        cfgs = [dict(c) for c in (new.get("panels") or [{"monitor": 0}])]
        idx = max(0, min(editing[0], len(cfgs) - 1))
        pcfg = dict(cfgs[idx])
        pcfg["module_ids"] = list(pick_mids)
        cfgs[idx] = pcfg
        new["panels"] = cfgs
        save_settings(new)
        _own_stamp[0] = _file_stamp()

    def _add_to_current_panel(mid):
        """Show a (new) module on the panel being edited, and turn the section on."""
        if mid not in pick_mids:
            pick_mids.append(mid)
        commit_module_ids()
        _enable_modules_section()   # defined below; called at runtime
        _picker_refresh[0]()

    def commit_modules(*_):
        if _loading[0]:
            return
        new = dict(load_settings())
        clean = []
        for m in modules_state:
            if (m.get("command") or "").strip() or (m.get("label") or "").strip():
                clean.append({"id": m["id"], "label": m.get("label", ""),
                              "command": m.get("command", ""),
                              "interval": int(m.get("interval") or 10),
                              "type": m.get("type") or "text"})
        new["modules"] = clean
        save_settings(new)
        _own_stamp[0] = _file_stamp()
        _picker_refresh[0]()   # labels/commands changed -> refresh the picker

    def rebuild_modules_rows():
        for _c in modules_box.get_children():
            modules_box.remove(_c)
        if not modules_state:
            modules_box.pack_start(_cls(Gtk.Label(
                label="No custom modules yet. Add one to run a command and show its output.",
                xalign=0, wrap=True), "hint"), False, False, 0)
        else:
            # column headers so it's clear what each field is
            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            h_name = _cls(Gtk.Label(label="Name on panel", xalign=0), "hint")
            h_shows = _cls(Gtk.Label(label="Shows", xalign=0), "hint")
            h_shows.set_size_request(104, -1)
            h_every = _cls(Gtk.Label(label="Every (s)", xalign=0), "hint")
            h_every.set_size_request(68, -1)
            h_rm = _cls(Gtk.Label(label="", xalign=0), "hint")
            h_rm.set_size_request(30, -1)
            hdr.pack_start(h_name, True, True, 0)
            hdr.pack_start(h_shows, False, False, 0)
            hdr.pack_start(h_every, False, False, 0)
            hdr.pack_start(h_rm, False, False, 0)
            modules_box.pack_start(hdr, False, False, 0)
        for _i, m in enumerate(modules_state):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            le = Gtk.Entry(); le.set_placeholder_text("e.g. Bitcoin"); le.set_text(m.get("label", "") or "")
            le.set_width_chars(9)
            le.set_tooltip_text("The name shown on the panel, to the left of the value.")
            tc = _noscroll(Gtk.ComboBoxText())
            for _tid, _tl in (("text", "Text"), ("number", "Number"),
                              ("bar", "Bar"), ("json", "JSON")):
                tc.append(_tid, _tl)
            tc.set_active_id(m.get("type") or "text")
            tc.set_size_request(104, -1)
            tc.set_tooltip_text("How to show the output: plain text, a number, a 0–100 "
                                "bar, or JSON {\"text\",\"percentage\"}.")
            iv = _noscroll(Gtk.SpinButton.new_with_range(1, 3600, 1))
            iv.set_value(int(m.get("interval") or 10)); iv.set_size_request(68, -1)
            iv.set_tooltip_text("How often to run the command, in seconds.")
            rm = _cls(Gtk.Button(label="✕"), "subtle")
            rm.set_tooltip_text("Delete this module (removes it from all panels)")
            row1.pack_start(le, True, True, 0)
            row1.pack_start(tc, False, False, 0)
            row1.pack_start(iv, False, False, 0)
            row1.pack_start(rm, False, False, 0)
            ce = Gtk.Entry()
            ce.set_placeholder_text("Command to run — e.g.  curl -s ifconfig.me")
            ce.set_text(m.get("command", "") or "")
            ce.set_tooltip_text("The shell command whose output is shown. Runs as you, "
                                "on the interval.")
            card.pack_start(row1, False, False, 0)
            card.pack_start(ce, False, False, 0)
            # which panels show this module (only meaningful with >1 panel)
            if len(load_settings().get("panels") or []) > 1:
                _on = _panels_showing(m.get("id"))
                _txt = ("Shown on: " + ", ".join(_on)) if _on else "Not shown on any panel"
                card.pack_start(_cls(Gtk.Label(label=_txt, xalign=0), "hint"), False, False, 0)
            modules_box.pack_start(card, False, False, 0)

            def _upd(_w, mm=m, _le=le, _ce=ce, _tc=tc, _iv=iv):
                if _loading[0]:
                    return
                mm["label"] = _le.get_text()
                mm["command"] = _ce.get_text()
                mm["type"] = _tc.get_active_id() or "text"
                mm["interval"] = int(_iv.get_value())
                commit_modules()
            le.connect("changed", _upd); ce.connect("changed", _upd)
            tc.connect("changed", _upd); iv.connect("value-changed", _upd)

            def _rm(_b, mm=m):
                mid = mm.get("id")
                modules_state[:] = [x for x in modules_state if x is not mm]
                # drop this module's id from every panel's selection
                new = dict(load_settings())
                cfgs = [dict(c) for c in (new.get("panels") or [])]
                for c in cfgs:
                    if isinstance(c.get("module_ids"), list) and mid in c["module_ids"]:
                        c["module_ids"] = [x for x in c["module_ids"] if x != mid]
                new["panels"] = cfgs
                save_settings(new)
                _own_stamp[0] = _file_stamp()
                rebuild_modules_rows()
                commit_modules()
                _picker_refresh[0]()
            rm.connect("clicked", _rm)
        modules_box.show_all()
        _picker_refresh[0]()   # definitions changed -> refresh the per-panel picker

    def _add_module(_b):
        nid = "m" + os.urandom(4).hex()
        modules_state.append({"id": nid, "label": "",
                              "command": "", "interval": 10, "type": "text"})
        _add_to_current_panel(nid)   # show it on the panel being edited
        rebuild_modules_rows()

    def _enable_modules_section():
        # When the user adds modules from the UI, switch the section on for this
        # panel so the modules actually appear (its toggle commits the sections).
        cb = checks.get("modules")
        if cb is not None and not cb.get_active():
            cb.set_active(True)

    def _open_catalog(_b):
        dlg = Gtk.Dialog(title="Module catalog", transient_for=win, modal=True)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Add selected", Gtk.ResponseType.ACCEPT)
        dlg.set_default_size(470, 580)
        area = dlg.get_content_area()
        area.set_border_width(14)
        area.pack_start(_cls(Gtk.Label(
            label="Pick ready-made modules — no shell command needed. Tick any, then "
                  "Add selected. Some note a tool they need (curl, jq, …).",
            xalign=0, wrap=True), "hint"), False, False, 0)
        # Currency + thousands separator applied to price modules when added.
        CRYPTO_CURRENCIES = ["eur", "usd", "chf", "gbp", "jpy", "cad", "aud"]
        cur_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cur_row.pack_start(_cls(Gtk.Label(label="Currency for prices:", xalign=0),
                                "field-label"), False, False, 0)
        cur_combo = _noscroll(Gtk.ComboBoxText())
        for _code in CRYPTO_CURRENCIES:
            cur_combo.append(_code, _code.upper())
        cur_combo.set_active_id("eur")
        cur_row.pack_start(cur_combo, False, False, 0)
        cur_row.pack_start(_cls(Gtk.Label(label="Thousands:", xalign=0),
                                "field-label"), False, False, 0)
        sep_combo = _noscroll(Gtk.ComboBoxText())
        for _sid, _slabel in (("'", "1'000"), (",", "1,000"), (".", "1.000"), ("", "1000")):
            sep_combo.append(_sid, _slabel)
        sep_combo.set_active_id("'")
        cur_row.pack_start(sep_combo, False, False, 0)
        area.pack_start(cur_row, False, False, 4)
        scr = Gtk.ScrolledWindow()
        scr.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scr.set_vexpand(True)
        colbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scr.add(colbox)
        area.pack_start(scr, True, True, 8)
        shows = {"text": "shows text", "number": "shows a number",
                 "bar": "shows a bar", "json": "shows text + bar"}
        picks = []
        for cat, mods in MODULE_CATALOG:
            head = _cls(Gtk.Label(label=cat.upper(), xalign=0), "group-heading")
            head.set_margin_top(12)
            colbox.pack_start(head, False, False, 0)
            for lbl, cmd, typ, iv, note in mods:
                cb = Gtk.CheckButton()
                # are the tools it needs installed? (handles "needs X" and "X and Y")
                m = re.search(r"needs ([\w-]+)(?:\s+and\s+([\w-]+))?", note)
                need = [t for t in (m.group(1), m.group(2)) if t] if m else []
                missing = any(shutil.which(t) is None for t in need)
                meta = shows.get(typ, "shows text")
                if "curl" in cmd or "http" in cmd:
                    meta += "  ·  uses the internet"
                inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
                title = lbl + ("   —   not installed" if missing else "")
                inner.pack_start(_cls(Gtk.Label(label=title, xalign=0),
                                      "field-label"), False, False, 0)
                inner.pack_start(_cls(Gtk.Label(label=f"{note}   ·   {meta}", xalign=0,
                                                wrap=True), "hint"), False, False, 0)
                cb.add(inner)
                cb.set_sensitive(not missing)
                colbox.pack_start(cb, False, False, 0)
                picks.append((cb, (lbl, cmd, typ, iv)))
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            code = cur_combo.get_active_id() or "eur"
            sep = sep_combo.get_active_id()
            if sep is None:
                sep = "'"
            added = 0
            for cb, (lbl, cmd, typ, iv) in picks:
                if cb.get_active():
                    coin_m = re.search(r"ids=([\w-]+)", cmd) if "coingecko" in cmd else None
                    if coin_m:   # a price module: rebuild for the chosen currency/separator
                        cmd = price_command(coin_m.group(1), code, sep)
                    nid = "m" + os.urandom(4).hex()
                    modules_state.append({"id": nid, "label": lbl,
                                          "command": cmd, "interval": iv, "type": typ})
                    if nid not in pick_mids:   # show it on the panel being edited
                        pick_mids.append(nid)
                    added += 1
            if added:
                commit_modules()
                commit_module_ids()
                _enable_modules_section()
                rebuild_modules_rows()
                _picker_refresh[0]()
                _pname = _panel_label(editing[0], (load_settings().get("panels") or [{}])[editing[0]])
                status.set_text(f"Added {added} module(s) to {_pname}. To show them on "
                                "other panels, use “Custom modules on this panel” there.")
        dlg.destroy()

    add_mod = _cls(Gtk.Button(label="Add module"), "subtle")
    add_mod.connect("clicked", _add_module)
    cat_mod = _cls(Gtk.Button(label="Browse catalog…"), "ghost")
    cat_mod.connect("clicked", _open_catalog)
    # two full-width buttons so neither label is clipped
    mod_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    mod_btns.pack_start(cat_mod, True, True, 0)
    mod_btns.pack_start(add_mod, True, True, 0)
    field(modg, modc, "", modules_box)
    field(modg, modc, "", mod_btns)
    hint(modg, modc, "This is the shared module library — define modules once here. "
                     "Choose which ones each panel shows under “Custom modules on this "
                     "panel” (above the divider).")
    field(modg, modc, "", _cls(Gtk.Label(
        label="Modules are small commands run as you, every few seconds — only add ones "
              "you understand or that come from the catalog. Bar/Number read the first "
              "number in the output; JSON reads {\"text\", \"percentage\"}.",
        xalign=0, wrap=True), "hint"))
    rebuild_modules_rows()

    # ---- Per-panel module picker: which defined modules THIS panel shows -------
    _, pkg, pkc = make_group("Custom modules on this panel", advanced=True)
    pick_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)

    def _scroll_to_defs(_b=None):
        exp = _group_exp.get("Custom modules")
        if exp is not None:
            exp.set_expanded(True)
            GLib.idle_add(lambda: (exp.get_label_widget().grab_focus(), False)[1])

    def _mod_row_label(m):
        name = (m.get("label") or "").strip() or "(unnamed)"
        tail = "" if (m.get("command") or "").strip() else "   (no command)"
        return f"{name}   ·   {m.get('type', 'text')}{tail}"

    def rebuild_module_picker():
        for c in pick_box.get_children():
            pick_box.remove(c)
        by_id = {m["id"]: m for m in modules_state if m.get("id")}
        pick_mids[:] = [i for i in pick_mids if i in by_id]   # prune to existing
        if not modules_state:
            r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            r.pack_start(_cls(Gtk.Label(label="No modules defined yet.", xalign=0),
                              "hint"), True, True, 0)
            b = _cls(Gtk.Button(label="Define modules…"), "subtle")
            b.connect("clicked", _scroll_to_defs)
            r.pack_start(b, False, False, 0)
            pick_box.pack_start(r, False, False, 0)
            pick_box.show_all()
            return
        cb_sec = checks.get("modules")
        if cb_sec is not None and not cb_sec.get_active():
            r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            r.pack_start(_cls(Gtk.Label(
                label="The Custom modules section is off on this panel.",
                xalign=0, wrap=True), "hint"), True, True, 0)
            b = _cls(Gtk.Button(label="Turn on"), "subtle")
            b.connect("clicked", lambda _b: cb_sec.set_active(True))
            r.pack_start(b, False, False, 0)
            pick_box.pack_start(r, False, False, 0)
        pick_box.pack_start(_cls(Gtk.Label(label="SHOWN ON THIS PANEL", xalign=0),
                                 "group-heading"), False, False, 0)
        if not pick_mids:
            pick_box.pack_start(_cls(Gtk.Label(
                label="Nothing selected — tick modules below.", xalign=0), "hint"),
                False, False, 0)
        for pos, mid in enumerate(list(pick_mids)):
            m = by_id[mid]
            r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            cbw = Gtk.CheckButton(label=_mod_row_label(m))
            cbw.set_active(True)

            def _off(_w, mid=mid):
                if _loading[0]:
                    return
                if mid in pick_mids:
                    pick_mids.remove(mid)
                commit_module_ids()
                rebuild_module_picker()
            cbw.connect("toggled", _off)
            up = _cls(Gtk.Button(label="▲"), "subtle"); up.set_sensitive(pos > 0)
            dn = _cls(Gtk.Button(label="▼"), "subtle"); dn.set_sensitive(pos < len(pick_mids) - 1)

            def _mv(mid, delta):
                i = pick_mids.index(mid); j = i + delta
                if 0 <= j < len(pick_mids):
                    pick_mids[i], pick_mids[j] = pick_mids[j], pick_mids[i]
                    commit_module_ids()
                    rebuild_module_picker()
            up.connect("clicked", lambda _b, mid=mid: _mv(mid, -1))
            dn.connect("clicked", lambda _b, mid=mid: _mv(mid, +1))
            r.pack_start(cbw, True, True, 0)
            r.pack_start(up, False, False, 0)
            r.pack_start(dn, False, False, 0)
            pick_box.pack_start(r, False, False, 0)
        not_shown = [m["id"] for m in modules_state
                     if m.get("id") and m["id"] not in pick_mids]
        if not_shown:
            pick_box.pack_start(_cls(Gtk.Label(label="NOT SHOWN", xalign=0),
                                     "group-heading"), False, False, 0)
            for mid in not_shown:
                cbw = Gtk.CheckButton(label=_mod_row_label(by_id[mid]))
                cbw.set_active(False)

                def _on(_w, mid=mid):
                    if _loading[0]:
                        return
                    if mid not in pick_mids:
                        pick_mids.append(mid)
                    commit_module_ids()
                    _enable_modules_section()
                    rebuild_module_picker()
                cbw.connect("toggled", _on)
                pick_box.pack_start(cbw, False, False, 0)
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        b_all = _cls(Gtk.Button(label="All"), "subtle")
        b_none = _cls(Gtk.Button(label="None"), "subtle")
        b_def = _cls(Gtk.Button(label="Define modules…"), "subtle")

        def _all(_b):
            for m in modules_state:
                if m.get("id") and m["id"] not in pick_mids:
                    pick_mids.append(m["id"])
            commit_module_ids(); _enable_modules_section(); rebuild_module_picker()

        def _none(_b):
            pick_mids[:] = []
            commit_module_ids(); rebuild_module_picker()
        b_all.connect("clicked", _all)
        b_none.connect("clicked", _none)
        b_def.connect("clicked", _scroll_to_defs)
        for b in (b_all, b_none, b_def):
            foot.pack_start(b, False, False, 0)
        pick_box.pack_start(foot, False, False, 0)
        pick_box.show_all()

    _picker_refresh[0] = rebuild_module_picker
    hint(pkg, pkc, "Pick which of the defined modules this panel shows, and their "
                   "order. The definitions themselves are shared by all panels.")
    field(pkg, pkc, "", pick_box)
    rebuild_module_picker()

    actionbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    _cls(actionbar, "actionbar")
    actionbar.set_border_width(0)
    actionbar.set_margin_start(20); actionbar.set_margin_end(20)
    actionbar.set_margin_top(14); actionbar.set_margin_bottom(10)
    status = _cls(Gtk.Label(label="Changes apply as you make them.", xalign=0), "status")
    status.set_line_wrap(True)
    actionbar.pack_start(status, True, True, 0)
    close = _cls(Gtk.Button(label="Close"), "actionbtn")
    actionbar.pack_end(close, False, False, 0)

    export_btn = _cls(Gtk.Button(label="Export"), "actionbtn")
    import_btn = _cls(Gtk.Button(label="Import"), "actionbtn")

    def do_export(_b):
        dlg = Gtk.FileChooserDialog(title="Export configuration", transient_for=win,
                                    action=Gtk.FileChooserAction.SAVE)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Export", Gtk.ResponseType.ACCEPT)
        dlg.set_current_name("mint-hud-config.json")
        dlg.set_do_overwrite_confirmation(True)
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            path = dlg.get_filename()
            try:
                data = dict(load_settings())
                data.pop("move", None)  # transient edit state, not config
                with open(path, "w") as f:
                    json.dump(data, f, indent=2)
                status.set_text(f"Exported to {os.path.basename(path)}")
            except Exception as e:
                status.set_text(f"Export failed: {e}")
        dlg.destroy()

    def do_import(_b):
        dlg = Gtk.FileChooserDialog(title="Import configuration", transient_for=win,
                                    action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Import", Gtk.ResponseType.ACCEPT)
        flt = Gtk.FileFilter(); flt.set_name("Configuration (*.json)"); flt.add_pattern("*.json")
        dlg.add_filter(flt)
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            path = dlg.get_filename()
            try:
                with open(path) as f:
                    data = json.load(f)
                if not isinstance(data, dict) or not isinstance(data.get("panels"), list):
                    raise ValueError("not a mint-hud configuration")
                data.pop("move", None)
                dlg.hide()   # get the file chooser out of the way first
                n_mod = len(data.get("modules") or [])
                conf = Gtk.MessageDialog(transient_for=win, modal=True,
                                         message_type=Gtk.MessageType.WARNING,
                                         buttons=Gtk.ButtonsType.OK_CANCEL,
                                         text=f"Replace your current setup with "
                                              f"{len(data['panels'])} imported panel(s)?")
                sec = ("This overwrites all your panels and settings. This can't be undone."
                       + (f"\n\nIt includes {n_mod} custom module(s) — commands that run "
                          "as you. Only import a file you trust, and review the commands "
                          "under Custom modules before turning that section on."
                          if n_mod else ""))
                conf.format_secondary_text(sec)
                ok = conf.run()
                conf.destroy()
                if ok != Gtk.ResponseType.OK:
                    status.set_text("Import cancelled.")
                    dlg.destroy()
                    return
                save_settings(data)
                editing[0] = 0
                _loading[0] = True
                _refill_combo()
                _loading[0] = False
                load_panel()
                refresh_move_labels()
                status.set_text(
                    f"Imported {len(data['panels'])} panel(s) from {os.path.basename(path)}")
            except Exception as e:
                status.set_text(f"Import failed: {e}")
        dlg.destroy()

    export_btn.connect("clicked", do_export)
    import_btn.connect("clicked", do_import)
    actionbar.pack_end(import_btn, False, False, 0)
    actionbar.pack_end(export_btn, False, False, 0)
    outer.pack_start(actionbar, False, False, 0)

    def commit(*_):
        if _loading[0]:
            return
        new = dict(load_settings())
        cfgs = [dict(c) for c in (new.get("panels") or [{"monitor": 0}])]
        idx = max(0, min(editing[0], len(cfgs) - 1))
        pcfg = dict(cfgs[idx])
        pcfg["weather_show_location"] = showloc_chk.get_active()
        pcfg["locations"] = list(loc_state["list"])
        # keep the single "location" in sync (first city) for back-compat with
        # the global weather and older readers
        pcfg["location"] = loc_state["list"][0] if loc_state["list"] else None
        pcfg["weather_rotate"] = int(rotate_spin.get_value())
        pcfg["units"] = units_combo.get_active_id() or "c"
        pcfg["sections"] = {k: cb.get_active() for k, cb in checks.items()}
        pcfg["clock_24h"] = clock_24h_chk.get_active()
        pcfg["clock_seconds"] = clock_sec_chk.get_active()
        pcfg["clock_date"] = clock_date_chk.get_active()
        pcfg["clock_date_format"] = date_fmt_combo.get_active_id() or "%a, %d %b %Y"
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
        _own_stamp[0] = _file_stamp()

    def commit_sensor_names(*_):
        if _loading[0]:
            return
        new = dict(load_settings())
        new["sensor_names"] = {k: v.strip() for k, v in sensor_names_state.items() if v.strip()}
        save_settings(new)
        _own_stamp[0] = _file_stamp()

    def commit_periph_names(*_):
        if _loading[0]:
            return
        new = dict(load_settings())
        new["peripheral_names"] = {k: v.strip() for k, v in periph_names_state.items() if v.strip()}
        save_settings(new)
        _own_stamp[0] = _file_stamp()

    def commit_margins(*_):
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

    def commit_stretch(*_):
        if _loading[0]:
            return
        new = dict(load_settings())
        cfgs = [dict(c) for c in (new.get("panels") or [{"monitor": 0}])]
        idx = max(0, min(editing[0], len(cfgs) - 1))
        pcfg = dict(cfgs[idx])
        pcfg["stretch"] = int(stretch_spin.get_value())
        cfgs[idx] = pcfg
        new["panels"] = cfgs
        save_settings(new)
        _own_stamp[0] = _file_stamp()

    def _theme_base(name):
        b = dict(_BASE_PALETTE)
        b.update(THEMES.get(name, {}))
        return b

    def _reload_appearance(pcfg):
        name = pcfg.get("theme") or s.get("theme") or "midnight"
        base = _theme_base(name)
        theme_combo.set_active_id(name)
        bgmode_combo.remove_all()
        bgmode_combo.append("", "Theme default (%s)" % base["MODE"].capitalize())
        for _mid, _ml in (("glass", "Glass"), ("solid", "Solid"),
                          ("transparent", "Transparent"), ("image", "Image")):
            bgmode_combo.append(_mid, _ml)
        bgmode_combo.set_active_id(pcfg.get("bg_mode") or "")
        acc = pcfg.get("accent")
        rgba = Gdk.RGBA()
        ar, ag_, ab = tuple(acc) if acc else base["ACCENT"]
        rgba.red, rgba.green, rgba.blue, rgba.alpha = ar / 255, ag_ / 255, ab / 255, 1.0
        accent_btn.set_rgba(rgba)
        if acc is None:
            accent_preset.set_active_id("")
            accent_btn.set_sensitive(False)
        else:
            pid = "%d,%d,%d" % tuple(acc)
            if any(pid == "%d,%d,%d" % _c for _n, _c in ACCENT_PRESETS):
                accent_preset.set_active_id(pid)
                accent_btn.set_sensitive(False)
            else:
                accent_preset.set_active_id("custom")
                accent_btn.set_sensitive(True)
        op = pcfg.get("opacity")
        opacity_scale.set_value(round((base["OPACITY"] if op is None else op) * 100))
        radius_spin.set_value(pcfg.get("radius", base["RADIUS"]))
        bd = pcfg.get("border")
        border_scale.set_value(round((base["BORDER"] if bd is None else bd) * 100))
        gl = pcfg.get("glow")
        glow_scale.set_value(round((base["GLOW"] if gl is None else gl) * 100))
        _bg_image_set(pcfg.get("bg_image"))
        blur_spin.set_value(pcfg.get("bg_blur") if pcfg.get("bg_blur") is not None else 10)
        imgsize_scale.set_value(round((pcfg.get("bg_scale") or 1.0) * 100))
        tile_switch.set_active(bool(pcfg.get("bg_tile")))
        horiz_switch.set_active(bool(pcfg.get("horizontal")))
        contrast_scale.set_value(round((pcfg.get("contrast") or 0) * 100))
        uiscale_scale.set_value(round((pcfg.get("ui_scale") or 1.0) * 100))
        is_img = (pcfg.get("bg_mode") or base["MODE"]) == "image"
        bg_image_btn.set_sensitive(is_img)
        blur_spin.set_sensitive(is_img)
        imgsize_scale.set_sensitive(is_img)
        tile_switch.set_sensitive(is_img)
        textsize_scale.set_value(round((pcfg.get("text_scale") or 1.0) * 100))
        # per-role text/data colours: show the override, or the theme's own
        # colour as a disabled preview under "Theme default".
        pcols = pcfg.get("colors") or {}
        for _role, (_combo, _btn) in color_pickers.items():
            _ov = pcols.get(_role)
            _r, _g, _b = tuple(_ov) if _ov else tuple(base.get(_role, base["TEXT"]))[:3]
            _rgba = Gdk.RGBA()
            _rgba.red, _rgba.green, _rgba.blue, _rgba.alpha = _r / 255, _g / 255, _b / 255, 1.0
            _btn.set_rgba(_rgba)
            if not _ov:
                _combo.set_active_id("")
                _btn.set_sensitive(False)
            else:
                _pid = "%d,%d,%d" % tuple(_ov)
                if any(_pid == "%d,%d,%d" % _c for _n, _c in ACCENT_PRESETS):
                    _combo.set_active_id(_pid)
                    _btn.set_sensitive(False)
                else:
                    _combo.set_active_id("custom")
                    _btn.set_sensitive(True)

    def commit_appearance(*_):
        if _loading[0]:
            return
        new = dict(load_settings())
        cfgs = [dict(c) for c in (new.get("panels") or [{"monitor": 0}])]
        idx = max(0, min(editing[0], len(cfgs) - 1))
        pcfg = dict(cfgs[idx])
        pcfg["theme"] = theme_combo.get_active_id() or "midnight"
        mode = bgmode_combo.get_active_id()
        pcfg["bg_mode"] = mode or None
        if accent_preset.get_active_id() == "":
            pcfg["accent"] = None
        else:
            c = accent_btn.get_rgba()
            pcfg["accent"] = [round(c.red * 255), round(c.green * 255), round(c.blue * 255)]
        _tbase = _theme_base(pcfg["theme"])
        def _od(key, val, default):
            # Store a theme-look value only when it differs from the theme's own
            # default; otherwise leave it unset so it follows the theme — and a
            # later theme switch picks up the new theme's default instead of
            # pinning this one.
            if abs(float(val) - float(default)) <= 1e-3:
                pcfg.pop(key, None)
            else:
                pcfg[key] = val
        _od("opacity", round(opacity_scale.get_value() / 100, 3), _tbase["OPACITY"])
        _od("radius", int(radius_spin.get_value()), _tbase["RADIUS"])
        _od("border", round(border_scale.get_value() / 100, 3), _tbase["BORDER"])
        _od("glow", round(glow_scale.get_value() / 100, 3), _tbase["GLOW"])
        pcfg["bg_image"] = _bg_image["path"]
        pcfg["bg_blur"] = int(blur_spin.get_value())
        pcfg["bg_scale"] = round(imgsize_scale.get_value() / 100, 3)
        pcfg["bg_tile"] = tile_switch.get_active()
        pcfg["horizontal"] = horiz_switch.get_active()
        pcfg["contrast"] = round(contrast_scale.get_value() / 100, 3)
        pcfg["ui_scale"] = round(uiscale_scale.get_value() / 100, 3)
        _od("text_scale", round(textsize_scale.get_value() / 100, 3), 1.0)
        # per-role text/data colours: keep only the roles the user overrode
        # ("Theme default" leaves the role out so it follows the theme).
        cols = {}
        for _role, (_combo, _btn) in color_pickers.items():
            if _combo.get_active_id() == "":
                continue
            _c = _btn.get_rgba()
            cols[_role] = [round(_c.red * 255), round(_c.green * 255), round(_c.blue * 255)]
        pcfg["colors"] = cols or None
        cfgs[idx] = pcfg
        new["panels"] = cfgs
        save_settings(new)
        _own_stamp[0] = _file_stamp()
        accent_btn.set_sensitive(accent_preset.get_active_id() == "custom")
        for _combo, _btn in color_pickers.values():
            _btn.set_sensitive(_combo.get_active_id() == "custom")
        is_img = (pcfg.get("bg_mode") or _theme_base(pcfg["theme"])["MODE"]) == "image"
        bg_image_btn.set_sensitive(is_img)
        blur_spin.set_sensitive(is_img)
        imgsize_scale.set_sensitive(is_img)
        tile_switch.set_sensitive(is_img)

    def _on_theme_change(*_):
        # Switching theme adopts the new theme's LOOK: the theme-derived visual
        # params (opacity, border, glow, radius) reset to the new theme's
        # defaults rather than keeping the old theme's values. Content that is
        # independent of the theme (accent, background image + its size/tile,
        # contrast, panel size) is kept. "Reset appearance" clears everything.
        if _loading[0]:
            return
        new = dict(load_settings())
        cfgs = [dict(c) for c in (new.get("panels") or [{"monitor": 0}])]
        idx = max(0, min(editing[0], len(cfgs) - 1))
        pcfg = dict(cfgs[idx])
        for _k in ("opacity", "border", "glow", "radius"):
            pcfg.pop(_k, None)
        pcfg["theme"] = theme_combo.get_active_id() or "midnight"
        cfgs[idx] = pcfg
        new["panels"] = cfgs
        save_settings(new)
        _own_stamp[0] = _file_stamp()
        apply_settings_css(pcfg["theme"])
        _loading[0] = True
        try:
            _reload_appearance(pcfg)
        finally:
            _loading[0] = False

    def reset_appearance(*_):
        # Clear every per-panel appearance override, back to the theme's defaults.
        if _loading[0]:
            return
        new = dict(load_settings())
        cfgs = [dict(c) for c in (new.get("panels") or [{"monitor": 0}])]
        idx = max(0, min(editing[0], len(cfgs) - 1))
        pcfg = dict(cfgs[idx])
        for k in ("accent", "opacity", "radius", "border", "glow", "bg_mode",
                  "bg_image", "bg_blur", "bg_scale", "bg_tile", "contrast", "ui_scale",
                  "colors", "text_scale"):
            pcfg.pop(k, None)
        cfgs[idx] = pcfg
        new["panels"] = cfgs
        save_settings(new)
        _own_stamp[0] = _file_stamp()
        _loading[0] = True
        try:
            _reload_appearance(pcfg)
        finally:
            _loading[0] = False

    def commit_name(*_):
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
            stretch_spin.set_value(int(pcfg.get("stretch") or 0))
            name_entry.set_text(pcfg.get("name", "") or "")
            units_combo.set_active_id(pcfg.get("units", "c"))
            loc_state["list"] = _panel_locs(pcfg)
            _refresh_locs()
            town.set_text("")
            loc_label.set_visible(False)
            rotate_spin.set_value(int(pcfg.get("weather_rotate") or s.get("weather_rotate") or 8))
            showloc_chk.set_active(bool(pcfg.get("weather_show_location", True)))
            sec = pcfg.get("sections") or {}
            for k, cb in checks.items():
                cb.set_active(bool(sec.get(k, True)))
            clock_24h_chk.set_active(bool(pcfg.get("clock_24h", True)))
            clock_sec_chk.set_active(bool(pcfg.get("clock_seconds", False)))
            clock_date_chk.set_active(bool(pcfg.get("clock_date", True)))
            date_fmt_combo.set_active_id(pcfg.get("clock_date_format") or "%a, %d %b %Y")
            dsel = pcfg.get("disks") or ["/"]
            for mp, cb in disk_checks.items():
                cb.set_active(mp in dsel)
            ssel = pcfg.get("sensors") or []
            for sid, cb in sens_checks.items():
                cb.set_active((sid in ssel) if ssel else (_sens_paths.get(sid) in auto_paths))
            rebuild_periph_rows(pcfg.get("peripherals") or [])
            full_order[:] = normalize_order(pcfg.get("order"))
            _reorder_content_rows()
            rebuild_sensor_names()
            pick_mids[:] = list(pcfg.get("module_ids") or [])
            rebuild_module_picker()
            _refresh_off_banners()
            _reload_appearance(pcfg)
            apply_settings_css(pcfg.get("theme") or s.get("theme") or "midnight")
        finally:
            _loading[0] = False

    name_entry.connect("changed", commit_name)
    units_combo.connect("changed", commit)
    showloc_chk.connect("toggled", commit)
    for _c in (clock_24h_chk, clock_sec_chk, clock_date_chk):
        _c.connect("toggled", commit)
    date_fmt_combo.connect("changed", commit)
    theme_combo.connect("changed", _on_theme_change)
    bgmode_combo.connect("changed", commit_appearance)
    accent_btn.connect("color-set", commit_appearance)

    def _on_accent_preset(*_):
        if _loading[0]:
            return
        pid = accent_preset.get_active_id()
        if pid in ("", "custom"):
            accent_btn.set_sensitive(pid == "custom")
            commit_appearance()
            return
        r, g, b = (int(x) for x in pid.split(","))
        rgba = Gdk.RGBA()
        rgba.red, rgba.green, rgba.blue, rgba.alpha = r / 255, g / 255, b / 255, 1.0
        _loading[0] = True
        try:
            accent_btn.set_rgba(rgba)
            accent_btn.set_sensitive(False)
        finally:
            _loading[0] = False
        commit_appearance()
    accent_preset.connect("changed", _on_accent_preset)

    # per-role colour pickers: a preset dropdown mirrors the accent one (choosing
    # a preset fills the button and commits; Custom… enables the button), and the
    # button commits on colour-set. One handler factory, bound per role.
    def _make_color_preset_handler(combo, btn):
        def _h(*_):
            if _loading[0]:
                return
            pid = combo.get_active_id()
            if pid in ("", "custom"):
                btn.set_sensitive(pid == "custom")
                commit_appearance()
                return
            r, g, b = (int(x) for x in pid.split(","))
            rgba = Gdk.RGBA()
            rgba.red, rgba.green, rgba.blue, rgba.alpha = r / 255, g / 255, b / 255, 1.0
            _loading[0] = True
            try:
                btn.set_rgba(rgba)
                btn.set_sensitive(False)
            finally:
                _loading[0] = False
            commit_appearance()
        return _h
    for _combo, _btn in color_pickers.values():
        _combo.connect("changed", _make_color_preset_handler(_combo, _btn))
        _btn.connect("color-set", commit_appearance)

    for _w in (opacity_scale, border_scale, glow_scale, contrast_scale, uiscale_scale,
               textsize_scale):
        _w.connect("value-changed", commit_appearance)
    radius_spin.connect("value-changed", commit_appearance)
    blur_spin.connect("value-changed", commit_appearance)
    imgsize_scale.connect("value-changed", commit_appearance)
    tile_switch.connect("notify::active", commit_appearance)
    horiz_switch.connect("notify::active", commit_appearance)
    appearance_reset.connect("clicked", reset_appearance)

    def _on_section_toggle(*_):
        if _loading[0]:
            return
        rebuild_module_picker()   # the modules section on/off drives the picker banner
        _refresh_off_banners()    # grey-note on the per-section groups
        commit()
    for _cb in checks.values():
        _cb.connect("toggled", _on_section_toggle)

    def _on_sensor_toggle(*_):
        if _loading[0]:
            return
        rebuild_sensor_names()
        commit()
    for _cb in sens_checks.values():
        _cb.connect("toggled", _on_sensor_toggle)
    for _cb in disk_checks.values():
        _cb.connect("toggled", commit)
    for _sp in margin_spins.values():
        _sp.connect("value-changed", commit_margins)
    stretch_spin.connect("value-changed", commit_stretch)
    close.connect("clicked", lambda *_: win.close())

    def _file_stamp():
        try:
            with open(SETTINGS_FILE, "rb") as f:
                return hash(f.read())
        except OSError:
            return None

    _own_stamp = [_file_stamp()]

    def _watch_external():
        cur = _file_stamp()
        if cur != _own_stamp[0]:
            _own_stamp[0] = cur
            _refill_combo()
            load_panel()
            refresh_move_labels()
        return True
    GLib.timeout_add(400, _watch_external)

    _refill_combo()
    load_panel()
    refresh_move_labels()

    # Reorder the groups into a flow that matches how people work: pick the panel,
    # choose its content and order, style it, place it, then the per-section
    # settings, then the groups that apply to every panel. (Groups are built in
    # code order above for safe widget dependencies; only repositioned here.)
    apply_group_order([
        "Panels", "Content", "Appearance", "Text", "Position & size",
        "Weather", "Clock", "Temperatures", "Disks", "Device batteries",
        "Custom modules on this panel",
        "Gauge names", "Alerts", "Custom modules",
    ])
    # A scope divider so it's clear the last groups are shared by every panel
    # (unlike everything above, which is for the panel picked in "Edit panel").
    _scope_div = _cls(Gtk.Label(label="APPLIES TO ALL PANELS", xalign=0), "scope-divider")
    content.pack_start(_scope_div, False, False, 0)
    content.reorder_child(_scope_div, content.get_children().index(_group_exp["Gauge names"]))
    _scope_div.show()

    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()

if __name__ == "__main__":
    if "--settings" in sys.argv:
        run_settings()
    elif "--png" in sys.argv:
        _M = gather_frame()
        _, _fl, _ = render(frame=_M, write_png=False)
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
        threading.Thread(target=autodetect_location, daemon=True).start()
        _welcome = os.path.join(CONF_DIR, ".welcomed")
        if not os.path.exists(_welcome):
            try:
                open(_welcome, "w").close()
                subprocess.Popen([sys.executable, os.path.abspath(__file__), "--settings"],
                                 start_new_session=True)
            except Exception:
                pass
        run_window()
