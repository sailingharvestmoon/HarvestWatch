#!/usr/bin/env python3
"""
RETIRED (2026-09-24) - weather no longer runs on the Pi.

The "Now" summary, tides and the 7-model forecast table are now produced by
the Cloudflare weather worker (cloudflare-weather/weather-worker.js), and the
forecast maps by GitHub Actions (tools/wxmaps/build_maps.py). Both keep
working when the boat is shut down or offline.

This stub only idles so the existing systemd unit stays healthy (the Pi's
auto-updater restarts it and checks it stays up). To remove it for good:
    sudo systemctl disable --now <unit>      # the unit that runs weather.py
The cabin display (frame.py) still reads weather/harvest-moon from Firebase,
so it is unaffected.
"""
import time
print("weather.py retired - weather now runs in the cloud (Cloudflare worker + GitHub Actions). Idling.", flush=True)
while True:
    time.sleep(86400)
