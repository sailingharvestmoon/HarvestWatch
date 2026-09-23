# DataHub — Harvest Moon boat systems

Everything that gets boat data (Vesper NMEA, weather, tides, AIS) off the boat,
watches the anchor, and shows it on the phone and the e-ink frame.

All parts talk through one Firebase project: `harvest-moon-watch`, vessel `harvest-moon`.
Design rationale and the "why" behind the alarms: `docs/design-notes.md`.

---

## Folder layout

```
Michael DataHub Files/
├── README.md               ← this file
├── pi/                     ← everything that runs ON THE RASPBERRY PI
│   ├── home/harvestmoon/   ←   mirrors the Pi's home folder exactly
│   └── etc/                ←   mirrors the Pi's system folders (/etc/...)
├── netlify/                ← the phone/web pages (drag this whole folder to Netlify)
├── cloudflare-worker/      ← the cloud backup watcher (paste into Cloudflare)
├── firebase/               ← Firestore security rules (paste into Firebase console)
├── wix/                    ← "conditions aboard" widget for the Wix site
├── docs/                   ← design notes, screenshots
├── data/                   ← data pulled off the Pi (polar logs) — not code
└── archive/                ← old versions, backups, full Pi snapshots. Nothing here is live.
```

**Rule of thumb:** a file's path under `pi/` IS its path on the Pi.
`pi/home/harvestmoon/sensors/guard.py` goes to `/home/harvestmoon/sensors/guard.py`.

---

## The Pi (hostname `inkyframe`, user `harvestmoon`)

| File here | Goes on the Pi at | Run by (systemd unit) | After changing it |
|---|---|---|---|
| `pi/home/harvestmoon/sensors/sensors.py` | `/home/harvestmoon/sensors/sensors.py` | `sensors` | `sudo systemctl restart sensors` |
| `pi/home/harvestmoon/sensors/guard.py` | `/home/harvestmoon/sensors/guard.py` | `guard` | `sudo systemctl restart guard` |
| `pi/home/harvestmoon/weather/weather.py` | `/home/harvestmoon/weather/weather.py` | weather unit ⚠ | `sudo systemctl restart <weather unit>` |
| `pi/home/harvestmoon/inky/frame.py` | `/home/harvestmoon/inky/frame.py` ⚠ | frame unit ⚠ (runs as root) | `sudo systemctl restart <frame unit>` |
| `pi/home/harvestmoon/polar/polar_logger.py` | `/home/harvestmoon/polar/polar_logger.py` | `harvest-moon-polar` | `sudo systemctl restart harvest-moon-polar` |
| `pi/home/harvestmoon/polar/polar_build.py` | `/home/harvestmoon/polar/polar_build.py` | run by hand | — |
| `pi/etc/systemd/system/*.service` | `/etc/systemd/system/` | — | `sudo systemctl daemon-reload` then restart that unit |
| `pi/etc/harvest-moon/ntfy.env.example` | template for `/etc/harvest-moon/ntfy.env` (secret, never stored here) | read by `guard` | `sudo systemctl restart guard` |

What each service does:

- **sensors** — reads the Vesper (192.168.1.167:39150) and posts wind/depth/temp/speed/heading to `telemetry/`, and GPS + bow position to `vessels/` every 60 s.
- **guard** — the main anchor watcher (every 10 s): drag, swing, wind/gust, lying off the wind, AIS; sends ntfy alerts from the boat; runs the dead-man's-switch offline alert.
- **weather** — NWS forecast/obs, NOAA tides, sun/moon → `weather/` every 30 min.
- **frame** — drives the e-ink display (photos / dashboard / info screen), mode set from `netlify/frame.html`.
- **harvest-moon-polar** — logs 1 Hz sailing data to `~/polar_logs/` for building polars.

Files the programs create on the Pi (don't copy these over): `~/sensors/tide_stations.json`,
`~/weather/tide_stations.json`, `~/weather/pressure_log.json`, `~/polar_logs/*.csv`.

Third-party, not ours: `~/inky/` and `~/Pimoroni/` are the Pimoroni Inky display library and installer.
The slideshow photos live on the Pi at `~/inky/examples/7color/slideshow_photos/` (a copy is in the 9/23 snapshot).

### Deploying a change to the Pi (from the Mac's Terminal, not from inside SSH)

```bash
cd ~/Documents/"Michael DataHub Files"/pi/home/harvestmoon
scp sensors/guard.py harvestmoon@inkyframe.local:/home/harvestmoon/sensors/
ssh harvestmoon@inkyframe.local 'sudo systemctl restart guard'
```

### ⚠ Gaps — not yet captured from the Pi

The 9/23 copy was of the home folder only, so a few things that live in `/etc` are missing:

1. **The frame and weather unit files** (names unknown). On the Pi, run
   `grep -l harvestmoon /etc/systemd/system/*.service` then `systemctl cat <each name>` and save them into `pi/etc/systemd/system/`.
2. **Which `frame.py` is live.** The Pi has two identical copies, `~/inky/frame.py` and `~/sensors/frame.py`. The frame unit file's `ExecStart` line settles it; retire the other.

---

## Netlify (web pages)

Drag the **whole `netlify/` folder** onto the Netlify site — each deploy replaces the site, so every page must be in it.

| Page | Purpose |
|---|---|
| `index.html` | landing page |
| `watch.html` | anchor watch: map, drag circle, set anchor, all alert settings, health, scope calculator |
| `instruments.html` | live wind rose + instruments |
| `frame.html` | switch the e-ink frame mode |

## Cloudflare worker

`cloudflare-worker/anchor-watcher-worker.js` → Cloudflare dashboard → the worker → Edit code → paste → Deploy.
Runs every minute: boat-gone-dark, scope, depth, daily all-clear, track recording, backup drag alarm.
Variables (set in Cloudflare, not in the file): `PROJECT_ID`, `VESSEL_ID`, `NTFY_TOPIC`, `NTFY_TOKEN`.

⚠ This is the 9/7 evening version (records the swing track at the bow). An earlier same-day version
(records at the GPS) is in `archive/cloudflare-worker-older/`. Confirm which one is actually deployed.

## Firebase

`firebase/firestore-rules.txt` → Firebase console → Firestore → Rules → paste → Publish.

| Collection | Written by | Holds |
|---|---|---|
| `vessels/` | sensors | GPS + bow position |
| `telemetry/` | sensors | wind, depth, temp, speed, heading |
| `weather/` | weather | forecast, tides, sun/moon |
| `alarms/` | watch.html | anchor, radius, every alert setting, snooze |
| `state/` | worker + guard | shared alert latches |
| `tracks/` | worker | swing track |
| `health/` | worker + guard | heartbeats, push results, ntfy quota |
| `ais/` | guard | nearby AIS targets |
| `modes/` | frame.html | e-ink display mode |

## Wix

`wix/wix-conditions-widget.html` → Wix editor → Add → Embed Code → Embed HTML → paste the whole file.

---

## Archive

| Folder | What it is |
|---|---|
| `archive/pi-snapshot-2026-09-23/` | Full copy of the Pi's home folder on 9/23 (includes photos). Untouched. |
| `archive/pi-backup-2026-09-07-v4.0/` (+ `.zip`) | The 9/7 V4.0 backup. |
| `archive/cloudflare-worker-older/` | Superseded worker version. |
| `archive/docs-older/` | July setup notes (sensors only). |
| `archive/retired/` | `transmit.html` (the Pi replaced it), `dashboard.py` and `slideshow.py` (early frame programs `frame.py` replaced). |
