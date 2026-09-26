#!/usr/bin/env python3
"""
Harvest Watch - pre-built forecast maps
=======================================
Runs on GitHub Actions a few times a day (.github/workflows/wxmaps.yml).
Downloads the latest model runs straight from the agencies' open-data
buckets, cuts out a fixed region (Maine to the Caribbean and well offshore),
and writes small grayscale PNGs - one per variable per forecast step - that
the Harvest Watch app loads directly. Panning and zooming then never waits on
anything: the whole region is already there.

Sources (all free, no keys, no per-point limits):
  GFS, GFS-Wave  NOAA on AWS  noaa-gfs-bdp-pds
  HRRR           NOAA on AWS  noaa-hrrr-bdp-pds   (US waters, ~2 days)
  NAM            NOAA on AWS  noaa-nam-pds        (North America, 3.5 days)
  ECMWF, AIFS    ECMWF open data on AWS  ecmwf-forecasts
Only the fields needed are fetched, using each file's index and HTTP byte
ranges, so a full build downloads roughly 1 GB rather than tens of GB.

Output (the folder given on the command line, published to GitHub Pages):
  index.json                     what is available, and from which run
  <model>/meta.json              grid, times, variables, packing
  <model>/<var>_<step>.png       8-bit grayscale; value = off + byte*step,
                                 255 = no data; row 0 is the SOUTH edge

UKMO, ICON and ocean currents are not built here (no convenient regular-grid
open data); the app falls back to the Pi's on-demand grids for those.

  python build_maps.py site              build everything
  python build_maps.py site gfs hrrr     build only these models
  (isobars/highs/lows for the Fronts view are added every run - see isobars.py)
"""
import sys, os, json, math, time, datetime as dt, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
import numpy as np

REGION = (5.0, -100.0, 52.0, -40.0)          # south, west, north, east
HOURS = 120                                   # forecast length kept
UA = {"User-Agent": "harvest-watch-maps (github actions)"}

# value = off + byte * step (byte 255 = missing) - same packing as wxgrid.py
PACK = {
    "ws": (0.0, 0.5), "wd": (0.0, 360 / 254), "wg": (0.0, 0.5), "pr": (0.0, 0.1),
    "pa": (0.0, 1.0), "cc": (0.0, 0.4), "ps": (940.0, 0.5), "ta": (-20.0, 0.5),
    "ce": (0.0, 20.0), "wh": (0.0, 0.1), "wvd": (0.0, 360 / 254), "wp": (0.0, 0.1),
    "st": (20.0, 0.4),
}

# NOAA fields: key -> (GRIB short name, level text as in the .idx file)
NOAA_FIELDS = {
    "u": ("UGRD", "10 m above ground"), "v": ("VGRD", "10 m above ground"),
    "gust": ("GUST", "surface"), "prate": ("PRATE", "surface"),
    "tcc": ("TCDC", "entire atmosphere"), "msl": ("PRMSL", "mean sea level"),
    "t2": ("TMP", "2 m above ground"), "cape": ("CAPE", "surface"),
}
S3_GFS = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
S3_ECMWF = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"

MODELS = {
    "gfs": dict(src="noaa", cycle=6, lag=3.5, res=0.25, steps=list(range(0, HOURS + 1, 3)),
                url=lambda d, h, f: f"{S3_GFS}/gfs.{d}/{h:02d}/atmos/gfs.t{h:02d}z.pgrb2.0p25.f{f:03d}",
                fields=dict(NOAA_FIELDS, sst=("TMP", "surface"), land=("LAND", "surface"))),
    "hrrr": dict(src="noaa", cycle=6, lag=1.5, res=0.1, steps=list(range(0, 49, 3)),
                 url=lambda d, h, f: f"https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{d}/conus/hrrr.t{h:02d}z.wrfsfcf{f:02d}.grib2",
                 fields=dict(NOAA_FIELDS, msl=("MSLMA", "mean sea level"))),
    "nam": dict(src="noaa", cycle=6, lag=2.0, res=0.1, steps=list(range(0, 85, 3)),
                url=lambda d, h, f: f"https://noaa-nam-pds.s3.amazonaws.com/nam.{d}/nam.t{h:02d}z.awphys{f:02d}.tm00.grib2",
                fields=NOAA_FIELDS),
    "ecmwf": dict(src="ecmwf", cycle=12, lag=7.0, res=0.25, steps=list(range(0, HOURS + 1, 3)),
                  url=lambda d, h, f: f"{S3_ECMWF}/{d}/{h:02d}z/ifs/0p25/oper/{d}{h:02d}0000-{f}h-oper-fc.grib2",
                  params={"u": "10u", "v": "10v", "gust": "10fg", "tp": "tp", "tcc": "tcc",
                          "msl": "msl", "t2": "2t", "cape": "mucape"}),
    "aifs": dict(src="ecmwf", cycle=6, lag=5.0, res=0.25, steps=list(range(0, HOURS + 1, 6)),
                 url=lambda d, h, f: f"{S3_ECMWF}/{d}/{h:02d}z/aifs-single/0p25/oper/{d}{h:02d}0000-{f}h-oper-fc.grib2",
                 params={"u": "10u", "v": "10v", "tp": "tp", "tcc": "tcc", "msl": "msl", "t2": "2t"}),
    "marine": dict(src="noaa", cycle=6, lag=4.0, res=0.25, steps=list(range(0, HOURS + 1, 3)),
                   url=lambda d, h, f: f"{S3_GFS}/gfs.{d}/{h:02d}/wave/gridded/gfswave.t{h:02d}z.global.0p25.f{f:03d}.grib2",
                   fields={"hs": ("HTSGW", "surface"), "dir": ("DIRPW", "surface"), "per": ("PERPW", "surface")}),
}

# ---------------------------------------------------------------- HTTP
def http(url, rng=None, tries=4):
    for k in range(tries):
        try:
            hdr = dict(UA)
            if rng: hdr["Range"] = f"bytes={rng[0]}-{rng[1] if rng[1] is not None else ''}"
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404): return None
            last = e
        except Exception as e:
            last = e
        time.sleep(2 * (k + 1))
    raise RuntimeError(f"{url}: {last}")

def exists(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA, method="HEAD"), timeout=30) as r:
            return r.status < 300
    except Exception:
        return False

# ---------------------------------------------------------------- indexes
def noaa_ranges(idx_text, wanted):
    """wanted: key -> (var, level). Returns key -> (start, end or None)."""
    lines = [l.split(":") for l in idx_text.strip().splitlines() if l.strip()]
    out = {}
    for n, p in enumerate(lines):
        if len(p) < 5: continue
        start = int(p[1]); var, lev = p[3], p[4]
        end = int(lines[n + 1][1]) - 1 if n + 1 < len(lines) else None
        for key, (wv, wl) in wanted.items():
            if key not in out and var == wv and (lev == wl or lev.startswith(wl)):
                out[key] = (start, end)
    return out

def ecmwf_ranges(index_text, params):
    want = {p: k for k, p in params.items()}
    out = {}
    for line in index_text.strip().splitlines():
        try: j = json.loads(line)
        except Exception: continue
        if j.get("levtype") != "sfc": continue
        k = want.get(j.get("param"))
        if k and k not in out:
            out[k] = (int(j["_offset"]), int(j["_offset"]) + int(j["_length"]) - 1)
    return out

# ---------------------------------------------------------------- regridding
def target_grid(res, domain=None):
    s, w, n, e = REGION
    if domain:
        s, w, n, e = max(s, domain[0]), max(w, domain[1]), min(n, domain[2]), min(e, domain[3])
    lats = np.arange(math.ceil(s / res) * res, n + 1e-9, res)
    lons = np.arange(math.ceil(w / res) * res, e + 1e-9, res)
    return lats, lons

class Sampler:
    """Maps a GRIB message's values onto the target lat/lon grid."""
    def __init__(self, h, lats, lons):
        import eccodes as ec
        self.lats, self.lons = lats, lons
        self.kind = ec.codes_get(h, "gridType")
        self.rot = None
        if self.kind == "regular_ll":
            self.ni, self.nj = ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")
            la0 = ec.codes_get(h, "latitudeOfFirstGridPointInDegrees")
            lo0 = ec.codes_get(h, "longitudeOfFirstGridPointInDegrees")
            dla = ec.codes_get(h, "jDirectionIncrementInDegrees")
            dlo = ec.codes_get(h, "iDirectionIncrementInDegrees")
            if not ec.codes_get(h, "jScansPositively"): dla = -dla
            fj = (lats[:, None] - la0) / dla
            fi = ((lons[None, :] - lo0) % 360) / dlo
            self.j0 = np.floor(fj).astype(int); self.i0 = np.floor(fi).astype(int)
            self.tj = fj - self.j0; self.ti = fi - self.i0
            self.ok = (self.j0 >= 0) & (self.j0 + 1 < self.nj)
            self.j0 = np.clip(self.j0, 0, self.nj - 2)
            self.j0 = np.broadcast_to(self.j0, (len(lats), len(lons)))
            self.i0 = np.broadcast_to(self.i0, (len(lats), len(lons)))
            self.tj = np.broadcast_to(self.tj, (len(lats), len(lons)))
            self.ti = np.broadcast_to(self.ti, (len(lats), len(lons)))
            self.ok = np.broadcast_to(self.ok, (len(lats), len(lons)))
        else:
            # Projected grid (HRRR/NAM Lambert): nearest neighbour via a KD-tree.
            from scipy.spatial import cKDTree
            sla = np.asarray(ec.codes_get_array(h, "latitudes"))
            slo = np.asarray(ec.codes_get_array(h, "longitudes"))
            def xyz(la, lo):
                la, lo = np.radians(la), np.radians(lo)
                return np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], -1)
            tree = cKDTree(xyz(sla, slo))
            TL, TO = np.meshgrid(lats, lons, indexing="ij")
            dist, idx = tree.query(xyz(TL.ravel(), TO.ravel()))
            dx_km = (ec.codes_get(h, "DxInMetres") if ec.codes_get(h, "DxInMetres") else 12000) / 1000
            self.idx = idx.reshape(TL.shape)
            self.ok = (dist * 6371.0).reshape(TL.shape) < dx_km * 1.5
            # Grid-relative winds -> earth-relative (NCEP Lambert convention).
            if ec.codes_get(h, "gridType") == "lambert":
                lov = ec.codes_get(h, "LoVInDegrees"); lat1 = ec.codes_get(h, "Latin1InDegrees")
                d = ((TO - lov + 180) % 360) - 180
                self.rot = np.radians(d) * math.sin(math.radians(lat1))

    def sample(self, h):
        import eccodes as ec
        vals = np.asarray(ec.codes_get_values(h), dtype=np.float64)
        if ec.codes_get(h, "bitmapPresent"):
            vals[vals == ec.codes_get(h, "missingValue")] = np.nan
        if self.kind == "regular_ll":
            v = vals.reshape(self.nj, self.ni)
            j0, i0, i1 = self.j0, self.i0 % self.ni, (self.i0 + 1) % self.ni
            a, b = v[j0, i0], v[j0, i1]; c, d = v[j0 + 1, i0], v[j0 + 1, i1]
            out = (a * (1 - self.ti) + b * self.ti) * (1 - self.tj) + (c * (1 - self.ti) + d * self.ti) * self.tj
        else:
            out = vals[self.idx]
        out = np.where(self.ok, out, np.nan)
        return out

def uv_relative_to_grid(h):
    import eccodes as ec
    try: return bool(ec.codes_get(h, "uvRelativeToGrid"))
    except Exception: return False

# ---------------------------------------------------------------- one step
def fetch_step(cfg, d, hh, f):
    """Returns key -> GRIB message bytes for one forecast step."""
    url = cfg["url"](d, hh, f)
    if cfg["src"] == "noaa":
        idx = http(url + ".idx")
        if not idx: return {}
        ranges = noaa_ranges(idx.decode("ascii", "ignore"), cfg["fields"])
    else:
        idx = http(url[:-len(".grib2")] + ".index")
        if not idx: return {}
        ranges = ecmwf_ranges(idx.decode("ascii", "ignore"), cfg["params"])
    return {k: http(url, r) for k, r in ranges.items()}

def pick_run(cfg):
    """Newest cycle whose LAST needed step is already published."""
    now = dt.datetime.utcnow() - dt.timedelta(hours=cfg["lag"])
    h = (now.hour // cfg["cycle"]) * cfg["cycle"]
    t = now.replace(hour=h, minute=0, second=0, microsecond=0)
    last = cfg["steps"][-1]
    for _ in range(6):
        d, hh = t.strftime("%Y%m%d"), t.hour
        u = cfg["url"](d, hh, last)
        probe = u + ".idx" if cfg["src"] == "noaa" else u[:-len(".grib2")] + ".index"
        if exists(probe): return t
        t -= dt.timedelta(hours=cfg["cycle"])
    return None

# ---------------------------------------------------------------- build
def quantize(a, key):
    off, step = PACK[key]
    q = np.round((a - off) / step)
    q = np.clip(q, 0, 254)
    q[~np.isfinite(a)] = 255
    return q.astype(np.uint8)

def write_png(path, arr):
    from PIL import Image
    Image.fromarray(arr, mode="L").save(path, optimize=True)

def build_model(name, out_dir):
    import eccodes as ec
    cfg = MODELS[name]
    run = pick_run(cfg)
    if run is None:
        print(f"{name}: no complete run found", flush=True); return None
    d, hh = run.strftime("%Y%m%d"), run.hour
    print(f"{name}: run {d} {hh:02d}z, {len(cfg['steps'])} steps", flush=True)
    domain = {"hrrr": (21.0, -100.0, 50.0, -60.0), "nam": (12.0, -100.0, 52.0, -50.0)}.get(name)
    lats, lons = target_grid(cfg["res"], domain)
    mdir = os.path.join(out_dir, name); os.makedirs(mdir, exist_ok=True)

    with ThreadPoolExecutor(8) as pool:
        raw = list(pool.map(lambda f: (f, fetch_step(cfg, d, hh, f)), cfg["steps"]))

    sampler, times, written = None, [], set()
    acc, prev_tp, first_tp, prev_f = None, None, None, None
    gfs_sst = {}
    if name == "marine":
        # Sea temperature comes from GFS surface temperature over water.
        gcfg = MODELS["gfs"]
        grun = pick_run(gcfg) or run
        gd, gh = grun.strftime("%Y%m%d"), grun.hour
        sub = dict(gcfg, fields={"sst": ("TMP", "surface"), "land": ("LAND", "surface")})
        with ThreadPoolExecutor(8) as pool:
            for f, msgs in pool.map(lambda f: (f, fetch_step(sub, gd, gh, f)), cfg["steps"]):
                vt = grun + dt.timedelta(hours=f)
                gfs_sst[vt] = msgs

    for ti_out, (f, msgs) in enumerate(raw):
        vt = run + dt.timedelta(hours=f)
        if vt.timestamp() < time.time() - 3 * 3600: continue      # already in the past
        if not msgs: continue
        H = {}
        for k, b in msgs.items():
            if b: H[k] = ec.codes_new_from_message(b)
        if not H: continue
        if sampler is None:
            sampler = Sampler(next(iter(H.values())), lats, lons)
        S = {k: sampler.sample(h) for k, h in H.items()}
        ti = len(times); times.append(int(vt.replace(tzinfo=dt.timezone.utc).timestamp()))
        o = {}
        if name == "marine":
            if "hs" in S: o["wh"] = S["hs"] * 3.28084
            if "dir" in S: o["wvd"] = S["dir"]
            if "per" in S: o["wp"] = S["per"]
            g = gfs_sst.get(vt) or {}
            if g.get("sst") and g.get("land"):
                hs_, hl_ = ec.codes_new_from_message(g["sst"]), ec.codes_new_from_message(g["land"])
                gs = Sampler(hs_, lats, lons)
                sst, land = gs.sample(hs_), gs.sample(hl_)
                o["st"] = np.where(land < 0.5, (sst - 273.15) * 9 / 5 + 32, np.nan)
                ec.codes_release(hs_); ec.codes_release(hl_)
        else:
            if "u" in S and "v" in S:
                u, v = S["u"], S["v"]
                if sampler.rot is not None and uv_relative_to_grid(H["u"]):
                    c, s_ = np.cos(sampler.rot), np.sin(sampler.rot)
                    u, v = c * u + s_ * v, -s_ * u + c * v
                o["ws"] = np.hypot(u, v) * 1.943844
                o["wd"] = (np.degrees(np.arctan2(-u, -v)) + 360) % 360
            if "gust" in S: o["wg"] = S["gust"] * 1.943844
            if "prate" in S:
                o["pr"] = S["prate"] * 3600.0
                hrs = 0 if prev_f is None else f - prev_f
                acc = (np.zeros_like(o["pr"]) if acc is None else acc + np.nan_to_num(o["pr"]) * hrs)
                o["pa"] = acc
            if "tp" in S:                                     # ECMWF: metres, accumulated from t0
                tp = S["tp"] * 1000.0
                if prev_tp is not None and f > prev_f:
                    o["pr"] = np.maximum(0, tp - prev_tp) / (f - prev_f)
                if first_tp is None: first_tp = tp
                o["pa"] = np.maximum(0, tp - first_tp)
                prev_tp = tp
            if "tcc" in S:
                c = S["tcc"]; o["cc"] = c * 100 if np.nanmax(c) <= 1.01 else c
            if "msl" in S: o["ps"] = S["msl"] / 100.0
            if "t2" in S: o["ta"] = (S["t2"] - 273.15) * 9 / 5 + 32
            if "cape" in S: o["ce"] = S["cape"]
        prev_f = f
        for k, a in o.items():
            write_png(os.path.join(mdir, f"{k}_{ti}.png"), quantize(a, k))
            written.add(k)
        for h in H.values(): ec.codes_release(h)

    if not times:
        print(f"{name}: nothing usable", flush=True); return None
    meta = {
        "model": name, "run": f"{d}{hh:02d}", "at": int(time.time() * 1000),
        "bbox": [float(lats[0]), float(lons[0]), float(lats[-1]), float(lons[-1])],
        "nx": int(len(lons)), "ny": int(len(lats)), "times": times,
        "vars": {k: list(PACK[k]) for k in sorted(written)}, "ok": True, "pre": True,
    }
    with open(os.path.join(mdir, "meta.json"), "w") as fh: json.dump(meta, fh, separators=(",", ":"))
    print(f"{name}: {len(times)} steps, vars {sorted(written)}, grid {meta['nx']}x{meta['ny']}", flush=True)
    return {"run": meta["run"], "steps": len(times), "vars": sorted(written), "at": meta["at"], "bbox": meta["bbox"]}

def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "site"
    only = sys.argv[2:] or list(MODELS)
    os.makedirs(out, exist_ok=True)
    index = {"generated": int(time.time() * 1000), "region": REGION, "models": {}}
    for name in only:
        t0 = time.time()
        try:
            r = build_model(name, out)
            if r: index["models"][name] = r
        except Exception as e:
            print(f"{name}: FAILED {e}", flush=True)
        print(f"{name}: {time.time() - t0:.0f} s", flush=True)
    # Isobars + highs/lows for the Fronts view (see isobars.py). WPC's own
    # chart files can't be fetched from here - the app loads those itself.
    t0 = time.time()
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import isobars
        r = isobars.build(sys.modules[__name__], out)
        if r: index["iso"] = r
    except Exception as e:
        print(f"isobars: FAILED {e}", flush=True)
    print(f"isobars: {time.time() - t0:.0f} s", flush=True)
    with open(os.path.join(out, "index.json"), "w") as fh: json.dump(index, fh, separators=(",", ":"))
    # Tell GitHub Pages to publish the files as-is (no Jekyll processing).
    with open(os.path.join(out, ".nojekyll"), "w") as fh: fh.write("")
    print("built:", ", ".join(index["models"]) or "nothing", flush=True)
    if not index["models"]: sys.exit(1)

if __name__ == "__main__":
    main()
