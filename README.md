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
| `pi/home/harvestmoon/weather/wxgrid.py` | `/home/harvestmoon/weather/wxgrid.py` | `wxgrid` | `sudo systemctl restart wxgrid` |
| `pi/home/harvestmoon/polar/polar_logger.py` | `/home/harvestmoon/polar/polar_logger.py` | `harvest-moon-polar` | `sudo systemctl restart harvest-moon-polar` |
| `pi/home/harvestmoon/polar/polar_build.py` | `/home/harvestmoon/polar/polar_build.py` | run by hand | — |
| `pi/etc/systemd/system/*.service` | `/etc/systemd/system/` | — | `sudo systemctl daemon-reload` then restart that unit |
| `pi/etc/harvest-moon/ntfy.env.example` | template for `/etc/harvest-moon/ntfy.env` (secret, never stored here) | read by `guard` | `sudo systemctl restart guard` |

What each service does:

- **sensors** — reads the Vesper (192.168.1.167:39150) and posts wind/depth/temp/speed/heading to `telemetry/`, and GPS + bow position to `vessels/` every 60 s.
- **guard** — the main anchor watcher (every 10 s): drag, swing, wind/gust, lying off the wind, AIS; sends ntfy alerts from the boat; runs the dead-man's-switch offline alert.
- **weather** — NWS forecast/obs, NOAA tides, sun/moon → `weather/` every 30 min; the seven model forecasts (ECMWF, GFS, UKMO, ICON, NAM, HRRR, AIFS, plus waves) from Open-Meteo → `weather/harvest-moon-forecast` every hour, or within a minute when the app's ↻ Refresh asks.
- **wxgrid** — pulls forecast map grids (~225 points) for the area the app's Maps view is showing, for just the models on screen, and keeps them fresh every 6 h while someone has looked in the last 2 days → `weather/harvest-moon-grid-<model>`. The app asks for a new area via `weather/harvest-moon-gridview` when you pan or zoom off the data. Each new area costs ~225 of Open-Meteo's 10,000 free daily calls per model shown.
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

## Netlify — the Harvest Watch app

Drag the **whole `netlify/` folder** onto the Netlify site — each deploy replaces the site, so every file must be in it.

`index.html` **is the app** — one page with five tabs:

| Tab | What it does |
|---|---|
| Anchor | map, drag circle, swing track, AIS targets; set / adjust / end the anchor watch |
| Instruments | wind rose, wind, depth, temp, speed, heading, scope, tide |
| Alerts | every alert (Off / Ready / Active / Snoozed / Triggered), snooze, watcher health |
| Weather | conditions, 7-model forecast table, tides, sun & moon; **Maps**: wind / gust / CAPE / rain / cloud / isobars / temp / waves / currents / sea temp per model, split-screen compare |
| Boat | cabin display mode, boat setup (bow offset, roller, transducer, variation), phone settings, backup GPS |

`watch.html`, `instruments.html`, `frame.html` are one-line redirects so old bookmarks still work.
The previous separate pages are in `archive/netlify-v1-2026-09-23/`.

The app stores nothing on the phone — settings, track and forecasts all live in Firestore.

On the iPhone: open the site in Safari → Share → **Add to Home Screen** for a full-screen app icon.

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
| `weather/` | weather, wxgrid, app | forecast, tides, sun/moon; `harvest-moon-forecast` = model table; `harvest-moon-grid-*` = map grids; `harvest-moon-places` = saved forecast locations and the one in use; `harvest-moon-gridview` = the map area the app wants |
| `alarms/` | Harvest Watch | anchor, radius, every alert setting, snooze, app preferences (`ui*`, `fc*`) |
| `state/` | worker + guard | shared alert latches |
| `tracks/` | worker | swing track |
| `health/` | worker + guard | heartbeats, push results, ntfy quota |
| `ais/` | guard | nearby AIS targets |
| `modes/` | Harvest Watch (Boat tab) | e-ink display mode |

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
| `archive/netlify-v1-2026-09-23/` | The separate watch / instruments / frame / index pages before Harvest Watch. |
| `archive/retired/` | `transmit.html` (the Pi replaced it), `dashboard.py` and `slideshow.py` (early frame programs `frame.py` replaced). |
