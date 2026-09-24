#!/usr/bin/env python3
"""
Harvest Moon - forecast MAP grids (for Harvest Watch -> Weather -> Maps)
=======================================================================
The forecast table needs one point; a map needs a field. Every few hours this
pulls a grid of points around the boat from Open-Meteo for each model, packs
it small, and writes it to Firestore so the app can draw colour maps, wind
particles and isobars - and compare two models side by side - without the
phone fetching or storing anything itself.

  weather/<vessel>-grid-<model>   one document per model (ecmwf, gfs, ...)
  weather/<vessel>-grid-marine    waves, currents, sea temperature

Packing: every value is quantised to one byte (value = off + byte * step,
255 = no data) and base64'd, one field per variable. A 15 x 15 grid, 3-hourly
for 5 days, is about 100 KB per model - well inside Firestore's 1 MB limit.

API budget: Open-Meteo's free tier is 10,000 calls a day and counts each grid
point as a call. 225 points x 8 grids every 6 hours is about 7,200 a day,
leaving room for the hourly point forecast in weather.py. Change GRID_N or
REFRESH_H with that in mind.

Environment (wxgrid.service):
  GRID_N      points per side          (default 15)
  GRID_HALF   half-height of box, deg  (default 2.0 -> about 240 nm tall)
  REFRESH_H   hours between pulls      (default 6)
  GRID_DAYS   days of forecast         (default 5)
Standard library only; borrows position/HTTP helpers from weather.py.
"""
import os, sys, json, time, math, base64, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import weather as W        # get_json, get_position, FS, VESSEL_ID, num, FC_MODELS

GRID_N    = int(os.environ.get("GRID_N", "15"))
GRID_HALF = float(os.environ.get("GRID_HALF", "2.0"))
REFRESH_H = float(os.environ.get("REFRESH_H", "6"))
GRID_DAYS = int(os.environ.get("GRID_DAYS", "5"))
STEP_H    = 3
CHUNK     = 75                                   # locations per request

def doc_url(name): return f"{W.FS}/weather/{W.VESSEL_ID}-grid-{name}"

# (open-meteo variable, short key, offset, step)  value = off + byte*step
ATMOS = [
    ("wind_speed_10m",     "ws",   0.0, 0.5),       # kn, 0..127
    ("wind_direction_10m", "wd",   0.0, 360/254),   # deg, from
    ("wind_gusts_10m",     "wg",   0.0, 0.5),       # kn
    ("precipitation",      "pr",   0.0, 0.1),       # mm in the hour
    ("cloud_cover",        "cc",   0.0, 0.4),       # %
    ("pressure_msl",       "ps", 940.0, 0.5),       # hPa, 940..1067
    ("temperature_2m",     "ta", -20.0, 0.5),       # F
    ("cape",               "ce",   0.0, 20.0),      # J/kg
]
MARINE = [
    ("wave_height",             "wh",   0.0, 0.1),      # ft
    ("wave_direction",          "wvd",  0.0, 360/254),  # deg, from
    ("wave_period",             "wp",   0.0, 0.1),      # s
    ("ocean_current_velocity",  "cv",   0.0, 0.02),     # kn (converted)
    ("ocean_current_direction", "cd",   0.0, 360/254),  # deg, TOWARDS
    ("sea_surface_temperature", "st",  20.0, 0.4),      # F (converted)
]

def grid_points(lat0, lon0):
    half_lon = GRID_HALF / max(0.2, math.cos(math.radians(lat0)))
    lats = [lat0 - GRID_HALF + 2 * GRID_HALF * j / (GRID_N - 1) for j in range(GRID_N)]
    lons = [lon0 - half_lon + 2 * half_lon * i / (GRID_N - 1) for i in range(GRID_N)]
    return lats, lons, (lat0 - GRID_HALF, lon0 - half_lon, lat0 + GRID_HALF, lon0 + half_lon)

def fetch_grid(base_url, extra, varlist, lats, lons):
    """Returns (epoch-hour list, {key: [per-point hourly lists]}) or raises."""
    pts = [(la, lo) for la in lats for lo in lons]      # row-major, south row first
    series = {k: [] for _, k, _, _ in varlist}
    times = None
    for c in range(0, len(pts), CHUNK):
        part = pts[c:c + CHUNK]
        url = (f"{base_url}?latitude={','.join(f'{p[0]:.3f}' for p in part)}"
               f"&longitude={','.join(f'{p[1]:.3f}' for p in part)}"
               f"&hourly={','.join(v for v, _, _, _ in varlist)}"
               f"&timezone=GMT&timeformat=unixtime&forecast_days={GRID_DAYS}{extra}")
        j = W.get_json(url, timeout=60, retries=2, backoff=5.0)
        if isinstance(j, dict):
            if j.get("error"): raise RuntimeError(j.get("reason", "error"))
            j = [j]
        for loc in j:
            h = loc.get("hourly") or {}
            if times is None: times = h.get("time") or []
            for v, k, _, _ in varlist:
                series[k].append(h.get(v) or [None] * len(times))
        time.sleep(1.0)       # be polite to the free API
    return times or [], series

def pack(times, series, varlist, convert=None):
    now = time.time()
    keep = [i for i, t in enumerate(times) if (t // 3600) % STEP_H == 0 and t >= now - 3 * 3600]
    npts = GRID_N * GRID_N
    fields, anyval = {}, False
    for v, k, off, step in varlist:
        buf = bytearray(len(keep) * npts)
        col = series[k]
        for ti, i in enumerate(keep):
            base = ti * npts
            for p in range(npts):
                val = col[p][i] if i < len(col[p]) else None
                if val is None:
                    buf[base + p] = 255; continue
                if convert and k in convert: val = convert[k](val)
                b = int(round((val - off) / step))
                buf[base + p] = 0 if b < 0 else 254 if b > 254 else b
                if k in ("ws", "wh"): anyval = True
        fields[k] = {"stringValue": base64.b64encode(bytes(buf)).decode()}
    return [times[i] for i in keep], fields, anyval

def write(name, meta, fields):
    body = {"fields": dict(fields, meta={"stringValue": json.dumps(meta, separators=(",", ":"))},
                           updatedAt={"integerValue": str(meta["at"])})}
    req = urllib.request.Request(doc_url(name), data=json.dumps(body).encode(), method="PATCH",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status

def last_pull(name):
    """(seconds since epoch of the last pull, (lat, lon) it was centred on)."""
    try:
        f = W.get_json(doc_url(name) + "?mask.fieldPaths=meta", retries=0).get("fields", {})
        m = json.loads((f.get("meta") or {}).get("stringValue") or "{}")
        return (m.get("at") or 0) / 1000.0, tuple(m.get("center") or (None, None))
    except Exception:
        return 0.0, (None, None)

MOVE_NM   = 30        # re-pull when the forecast spot is this far from the grid centre
MIN_GAP_S = 20 * 60   # ...but never re-pull one model more often than this

def pull_all(force=False):
    lat0, lon0, place = W.forecast_position()
    lats, lons, bbox = grid_points(lat0, lon0)
    jobs = [(mid, apis) for mid, apis in W.FC_MODELS] + [("marine", None)]
    for name, apis in jobs:
        if not force:
            at, (clat, clon) = last_pull(name)
            age = time.time() - at
            moved = clat is None or W.haversine(clat, clon, lat0, lon0) / 1.852 > MOVE_NM
            if age < REFRESH_H * 3600 * 0.9 and not (moved and age > MIN_GAP_S):
                continue
        varlist = MARINE if name == "marine" else ATMOS
        meta = {"at": int(time.time() * 1000), "bbox": [round(b, 4) for b in bbox], "nx": GRID_N, "ny": GRID_N,
                "center": [round(lat0, 4), round(lon0, 4)], "place": place,
                "vars": {k: [off, step] for _, k, off, step in varlist}, "ok": False}
        try:
            if name == "marine":
                times, series = fetch_grid("https://marine-api.open-meteo.com/v1/marine", "&length_unit=imperial",
                                           varlist, lats, lons)
                conv = {"cv": lambda x: x / 1.852, "st": lambda c: c * 9 / 5 + 32}   # km/h -> kn, C -> F
                meta["api"] = "marine"
            else:
                times, series, used, err = None, None, None, "failed"
                for api in apis:
                    try:
                        times, series = fetch_grid("https://api.open-meteo.com/v1/forecast",
                            f"&models={api}&wind_speed_unit=kn&temperature_unit=fahrenheit&precipitation_unit=mm",
                            varlist, lats, lons)
                        used = api; break
                    except Exception as e:
                        err = str(e)[:120]
                if used is None: raise RuntimeError(err)
                conv, meta["api"] = None, used
            t, fields, anyval = pack(times, series, varlist, conv)
            meta.update(times=t, ok=anyval, err="" if anyval else "no coverage here")
            st = write(name, meta, fields)
            size = sum(len(f["stringValue"]) for f in fields.values()) // 1024
            print(f"grid {name}: {meta.get('api')} {len(t)} steps, {size} KB, {'ok' if anyval else 'no data'} [{st}]", flush=True)
        except Exception as e:
            print(f"grid {name}: FAILED {e}", flush=True)
            meta.update(err=str(e)[:160], times=[])
            try: write(name, meta, {})
            except Exception: pass

def run():
    print(f"wxgrid: {GRID_N}x{GRID_N}, +/-{GRID_HALF} deg, every {REFRESH_H} h, {GRID_DAYS} days", flush=True)
    while True:
        try:
            pull_all()
        except Exception as e:
            print("wxgrid cycle error:", e, flush=True)
        # Checked every 2 minutes so a new forecast location is picked up
        # quickly; each model is only re-pulled when it is REFRESH_H old or
        # the spot has moved more than MOVE_NM from its grid.
        time.sleep(120)

if __name__ == "__main__":
    if "--now" in sys.argv: pull_all(force=True)
    else: run()
