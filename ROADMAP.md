# Roadmap

Things to work on, mostly from early user feedback. This lives on the `dev`
branch — nothing here has landed on `main` yet.

## Packaging & install
- [ ] Ship a `.deb` so it installs and uninstalls like a normal package
- [ ] A graphical installer, not just the command-line one
- [ ] A clean uninstaller (autostart entry, files, the udev rule, cache)

## Configuration
- [x] A machine-managed settings file (`settings.json`), never hand-edited
- [x] A settings GUI (`hud.py --settings`) that reads and writes it
- [x] Panel picks up saved settings live (no restart)
- [x] Fold the old `weather.json` location into it (with migration)
- [ ] Move the remaining in-code constants (sizes, colours) in too
- [ ] Document it in the README + a menu launcher (.desktop) for the GUI

## Multi-monitor
- [x] Choose which monitor the panel shows on
- [x] Position it in any corner (+ configurable edge margin)
- [ ] Free offset, not just corners
- [ ] Handle mixed DPI

## Readouts (from tester feedback)
- [x] °C / °F switch, applied to every temperature (Celsius internally)
- [x] Skip peripheral batteries (a Solaar mouse/keyboard) so desktops show none
- [x] Show multiple disks/mounts, chosen in the settings window
- [x] Pick which temperature sensors appear in the thermals row
- [ ] Optional per-disk I/O and per-disk temperature

## Hardware coverage
- [ ] Keep collecting reports of missing/wrong readouts on AMD / NVIDIA /
      desktops, and widen the detection where needed
