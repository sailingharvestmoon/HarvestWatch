"""
Isobars + highs/lows for Harvest Watch -> Weather -> Fronts
============================================================
GFS mean-sea-level pressure on a 0.5 deg grid covering WPC's chart area,
from 30 h ago (each past cycle's 0 h and 3 h - effectively analyses) out
to 7 days (latest run, 3-hourly to 120 h then 6-hourly). The app draws the
isobars itself, so they stay sharp at any zoom.

Writes into the Pages site:
  iso/meta.json      {run, bbox, nx, ny, times[], hl:[[["H"|"L", mb, lat, lon], ...] per time]}
  iso/ps_<i>.png     8-bit pressure, value = 940 + byte * 0.5 (255 = missing), row 0 = south
"""
import os, json, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import numpy as np

BOX = (12.0, -135.0, 62.0, -50.0)         # s, w, n, e - WPC CONUS chart plus margin
RES = 0.5
FUTURE = list(range(0, 121, 3)) + list(range(126, 169, 6))
OFF, STEP = 940.0, 0.5

def _grid():
    s, w, n, e = BOX
    return np.arange(s, n + 1e-9, RES), np.arange(w, e + 1e-9, RES)

def _highs_lows(p, lats, lons):
    """Pressure centres: local extremes over ~8 deg that stand out >= 2 mb."""
    from scipy.ndimage import minimum_filter, maximum_filter, uniform_filter
    f = np.where(np.isfinite(p), p, np.nanmean(p))
    size = int(8 / RES) | 1
    mean = uniform_filter(f, size)
    out = []
    for kind, ext, sign in (("H", maximum_filter(f, size), 1), ("L", minimum_filter(f, size), -1)):
        ys, xs = np.where((f == ext) & (sign * (f - mean) >= 2.0))
        for y, x in zip(ys, xs):
            if 2 <= y < len(lats) - 2 and 2 <= x < len(lons) - 2:      # not on the edge
                out.append([kind, int(round(f[y, x])), round(float(lats[y]), 1), round(float(lons[x]), 1)])
    return out

def build(bm, out_dir):
    """bm: the build_maps module (http helpers, Sampler, MODELS)."""
    import eccodes as ec
    lats, lons = _grid()
    cfg = dict(bm.MODELS["gfs"], lag=5.0, steps=FUTURE, fields={"msl": ("PRMSL", "mean sea level")})
    run = bm.pick_run(cfg)
    if run is None:
        print("isobars: no complete GFS run", flush=True); return None
    jobs = [(run, f) for f in FUTURE]
    # past: 0 h and 3 h of each earlier cycle back ~30 h
    c = run - dt.timedelta(hours=6)
    while c > run - dt.timedelta(hours=36):
        jobs += [(c, 0), (c, 3)]; c -= dt.timedelta(hours=6)
    def get(job):
        r, f = job
        try: return job, bm.fetch_step(cfg, r.strftime("%Y%m%d"), r.hour, f).get("msl")
        except Exception: return job, None
    with ThreadPoolExecutor(8) as pool:
        got = list(pool.map(get, jobs))
    by_t = {}
    for (r, f), msg in got:
        if not msg: continue
        vt = r + dt.timedelta(hours=f)
        # prefer the newest run for any valid time
        if vt not in by_t or r > by_t[vt][0]: by_t[vt] = (r, msg)
    idir = os.path.join(out_dir, "iso"); os.makedirs(idir, exist_ok=True)
    times, hls, sampler = [], [], None
    for i, vt in enumerate(sorted(by_t)):
        h = ec.codes_new_from_message(by_t[vt][1])
        if sampler is None: sampler = bm.Sampler(h, lats, lons)
        p = sampler.sample(h) / 100.0
        ec.codes_release(h)
        q = np.clip(np.round((p - OFF) / STEP), 0, 254); q[~np.isfinite(p)] = 255
        bm.write_png(os.path.join(idir, f"ps_{len(times)}.png"), q.astype(np.uint8))
        times.append(int(vt.replace(tzinfo=dt.timezone.utc).timestamp()))
        hls.append(_highs_lows(p, lats, lons))
    meta = {"run": run.strftime("%Y%m%d%H"), "at": int(time.time() * 1000),
            "bbox": [float(lats[0]), float(lons[0]), float(lats[-1]), float(lons[-1])],
            "nx": int(len(lons)), "ny": int(len(lats)), "off": OFF, "step": STEP, "times": times, "hl": hls}
    with open(os.path.join(idir, "meta.json"), "w") as fh: json.dump(meta, fh, separators=(",", ":"))
    print(f"isobars: run {meta['run']}, {len(times)} times, grid {meta['nx']}x{meta['ny']}", flush=True)
    return {"run": meta["run"], "steps": len(times)}
