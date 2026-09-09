# Roadmap

Things to work on, mostly from early user feedback. This lives on the `dev`
branch — nothing here has landed on `main` yet.

## Packaging & install
- [ ] Ship a `.deb` so it installs and uninstalls like a normal package
- [ ] A graphical installer, not just the command-line one
- [ ] A clean uninstaller (autostart entry, files, the udev rule, cache)

## Configuration
- [ ] A real config file with an obvious, documented location
      (e.g. `~/.config/mint-hud/config.toml`)
- [ ] Move the in-code constants — placement, sizes, section toggles — into it
- [ ] Make it clear in the README/installer where settings live
- [ ] Maybe a small settings GUI

## Multi-monitor
- [ ] Choose which monitor the panel shows on
- [ ] Move / position it (corner + offset), instead of only auto top-right
- [ ] Handle per-monitor work areas and mixed DPI

## Hardware coverage
- [ ] Keep collecting reports of missing/wrong readouts on AMD / NVIDIA /
      desktops, and widen the detection where needed
