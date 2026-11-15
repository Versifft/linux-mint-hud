# Changelog

All notable user-facing changes to linux-mint-hud. See the
[app guide](docs/guide.md) for how everything described here works, and the
[README](README.md) for the project overview.

## 2.0 — 2026

### New

- **Horizontal panels** — a per-panel toggle lays the panel out as a thin
  bar across the top of the screen, with sections arranged left-to-right in
  compact, fixed-width form.
- **Per-role text colours** — pick colours for titles, values, subtitles,
  and the data accent colours (GPU, RAM, Pink, Upload, Amber, Good, Alerts,
  Warning, Critical, Dark red) independently of the theme.
- **Text size scale** — resize just the lettering (75–140%) per panel,
  independent of the panel's overall Size.
- **Spotify album cover as the module background** — now-playing shows the
  album art as a darkened backdrop behind the track info, in both vertical
  and horizontal layouts; a clear "Spotify closed" state when nothing is
  playing.
- **Multiple rotating weather locations per panel** — add several cities
  and rotate through them on a timer; US cities default to °F
  automatically.
- **Custom module catalog overhaul** — new Crypto, Stocks, Indices, and
  Forex & metals categories (all free, no-key sources), plus more
  System/Audio/Hardware/Homelab modules; a currency picker for crypto
  prices; modules the panel already shows natively were dropped from the
  catalog.
- **Tray menu additions** — Show panel on/off and Alerts on/off, plus
  About (opens the project's GitHub page) and Report a bug (opens the
  GitHub issues page) entries, alongside Settings and Quit.
- **Many more themes** — now 19, with a full palette overhaul — plus
  background-image size/zoom and repeat/tile controls, and a Transparent
  theme at 30% opacity by default.

### Improved

- **Move & resize overhaul** — the panel stays interactive while in move
  mode, saves reliably, resizes gradually into the open side, never shrinks
  vertically mid-drag, and placement is now true WYSIWYG (it stays exactly
  where you drop it).
- **Theme switching** adopts the new theme's look (opacity, glow, border)
  while keeping your accent colour, background image and size.
- **Horizontal weather** reworked — icon moved to the right, with a compact
  min/max/feels line; horizontal column widths are tighter and no longer
  reflow; the weather cloud icon stays in place whether or not it's
  raining.
- **Settings window polish** — full-width group dividers and sliders, a
  clearer divider between per-panel and global settings, a tidier action
  bar.

### Fixed

- Add/Duplicate panel buttons doing nothing (a name-shadowing bug).
- Spotify transport buttons not being clickable in horizontal layout.
- The weather top slot always showing the same one city instead of
  rotating (a rotation aliasing bug), and the weather panel's size jumping.
- The background-image picker not opening.
- A set of resize bugs: lost saves, dropped input grabs, wrong geometry,
  the panel jumping to the wrong size, a left-shift after releasing a drag,
  and vertical shrink during a resize.

## 1.1

Prior public release. Single-panel desktop stats with CPU/GPU/RAM, thermals,
network, power/battery, device batteries, top processes, weather, and
Claude plan quota; multiple independent panels; a handful of themes; custom
modules via shell commands.
