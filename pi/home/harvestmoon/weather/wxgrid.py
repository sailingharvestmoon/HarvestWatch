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

def default_bbox(lat0, lon0):
    half_lon = GRID_HALF / max(0.2, math.cos(math.radians(lat0)))
    return (lat0 - GRID_HALF, lon0 - half_lon, lat0 + GRID_HALF, lon0 + half_lon)

def grid_points(bbox):
    """About GRID_N*GRID_N points spread over bbox, keeping the cells square-ish."""
    la0, lo0, la1, lo1 = bbox
    w = (lo1 - lo0) * max(0.2, math.cos(math.radians((la0 + la1) / 2)))
    h = max(1e-6, la1 - la0)
    total = GRID_N * GRID_N
    ny = max(6, min(30, round(math.sqrt(total * h / max(w, 1e-6)))))
    nx = max(6, min(30, round(total / ny)))
    lats = [la0 + (la1 - la0) * j / (ny - 1) for j in range(ny)]
    lons = [lo0 + (lo1 - lo0) * i / (nx - 1) for i in range(nx)]
    return lats, lons, nx, ny

# The app says which area and which grids it is looking at
# (weather/<vessel>-gridview: bbox, models, at). The Pi pulls that area,
# and only those grids, so panning to a new area costs 2-3 grids, not 8.
VIEW_URL = f"{W.FS}/weather/{W.VESSEL_ID}-gridview"
VIEW_IDLE_S = 2 * 86400     # nobody has looked for 2 days: stop refreshing

def clamp_bbox(b):
    la0, lo0, la1, lo1 = b
    la0, la1 = max(-80.0, min(la0, la1)), min(80.0, max(la0, la1))
    if la1 - la0 < 1.0: c = (la0 + la1) / 2; la0, la1 = c - 0.5, c + 0.5
    if lo1 - lo0 < 1.0: c = (lo0 + lo1) / 2; lo0, lo1 = c - 0.5, c + 0.5
    if la1 - la0 > 40: c = (la0 + la1) / 2; la0, la1 = c - 20, c + 20
    if lo1 - lo0 > 60: c = (lo0 + lo1) / 2; lo0, lo1 = c - 30, c + 30
    return (round(la0, 3), round(lo0, 3), round(la1, 3), round(lo1, 3))

def target():
    """(bbox, set of grid names to keep fresh, place label, request time s)."""
    try:
        f = W.get_json(VIEW_URL, retries=0).get("fields", {})
        at = (W.num(f.get("at")) or 0) / 1000.0
        bbox = json.loads((f.get("bbox") or {}).get("stringValue") or "null")
        models = json.loads((f.get("models") or {}).get("stringValue") or "null")
        if bbox and models:
            return clamp_bbox(bbox), set(models), "view", at
    except Exception:
        pass
    lat0, lon0, place = W.forecast_position()
    return clamp_bbox(default_bbox(lat0, lon0)), None, place, 0.0

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

def pack(times, series, varlist, convert, npts):
    now = time.time()
    keep = [i for i, t in enumerate(times) if (t // 3600) % STEP_H == 0 and t >= now - 3 * 3600]
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
    """(seconds since epoch of the last pull, bbox it covered)."""
    try:
        f = W.get_json(doc_url(name) + "?mask.fieldPaths=meta", retries=0).get("fields", {})
        m = json.loads((f.get("meta") or {}).get("stringValue") or "{}")
        return (m.get("at") or 0) / 1000.0, tuple(m.get("bbox") or ())
    except Exception:
        return 0.0, ()

MIN_GAP_S = 60        # never re-pull one grid more often than this

def pull_all(force=False):
    bbox, wanted, place, req_at = target()
    if wanted is not None and time.time() - req_at > VIEW_IDLE_S and not force:
        return                                       # nobody is looking - save the API budget
    lats, lons, nx, ny = grid_points(bbox)
    jobs = [(mid, apis) for mid, apis in W.FC_MODELS] + [("marine", None)]
    for name, apis in jobs:
        if wanted is not None and name not in wanted:
            continue
        if not force:
            at, old_bbox = last_pull(name)
            age = time.time() - at
            elsewhere = tuple(round(x, 3) for x in old_bbox) != bbox
            if age < REFRESH_H * 3600 * 0.9 and not (elsewhere and age > MIN_GAP_S):
                continue
        varlist = MARINE if name == "marine" else ATMOS
        meta = {"at": int(time.time() * 1000), "bbox": list(bbox), "nx": nx, "ny": ny, "place": place,
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
            t, fields, anyval = pack(times, series, varlist, conv, nx * ny)
            meta.update(times=t, ok=anyval, err="" if anyval else "no coverage here")
            st = write(name, meta, fields)
            size = sum(len(f["stringValue"]) for f in fields.values()) // 1024
            print(f"grid {name}: {meta.get('api')} {len(t)} steps, {size} KB, {'ok' if anyval else 'no data'} [{st}]", flush=True)
        except Exception as e:
            print(f"grid {name}: FAILED {e}", flush=True)
            # Stamped as if pulled a while ago, so it is retried in ~15 min
            # rather than every 30 s (or not for 6 hours).
            meta.update(err=str(e)[:160], times=[], at=int((time.time() - REFRESH_H * 3600 * 0.9 + 900) * 1000))
            try: write(name, meta, {})
            except Exception: pass

def run():
    print(f"wxgrid: ~{GRID_N}x{GRID_N} points over the area the app is viewing, every {REFRESH_H} h, {GRID_DAYS} days", flush=True)
    while True:
        try:
            pull_all()
        except Exception as e:
            print("wxgrid cycle error:", e, flush=True)
        # Checked every 2 minutes so a new forecast location is picked up
        # quickly; each model is only re-pulled when it is REFRESH_H old or
        # the spot has moved more than MOVE_NM from its grid.
        time.sleep(30)

if __name__ == "__main__":
    if "--now" in sys.argv: pull_all(force=True)
    else: run()
