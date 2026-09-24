"""
WPC surface fronts & pressure, day 0-7 (for Harvest Watch -> Weather -> Fronts)
===============================================================================
The same charts as https://www.wpc.ncep.noaa.gov/basicwx/day0-7loop.html -
analyses of the last 24 h, then WPC's forecasts to day 7 - but taken from
WPC's KML feeds, which carry each frame's exact valid time and map corners.
That lets the app lay each chart over the real map (zoomable) and label it
in local time.

WPC's KML images are plain lat/lon ("equirectangular"); web maps are Web
Mercator, so every image is re-sampled row by row here to line up exactly.

Writes into the Pages site:
  fronts/index.json       [{t, kind, name, src, bounds:[s,w,n,e]}] oldest first
  fronts/<n>.png          transparent overlays
"""
import io, os, re, json, math, time, zipfile, datetime as dt, urllib.parse
import xml.etree.ElementTree as ET
import numpy as np

KMLS = [
    ("analysis", "https://www.wpc.ncep.noaa.gov/kml/conus_png/conus_analysis_transparent.kml"),
    ("analysis", "https://www.wpc.ncep.noaa.gov/kml/conus_png/conus_analysis_latest_transparent.kml"),
    ("forecast", "https://www.wpc.ncep.noaa.gov/kml/conus_png/conus_pmsl_fcst_transparent_shortrange.kml"),
    ("forecast", "https://www.wpc.ncep.noaa.gov/kml/conus_png/conus_pmsl_fcst_transparent.kml"),
]

def _tag(e): return e.tag.rsplit("}", 1)[-1]
def _find(e, name):
    for c in e.iter():
        if _tag(c) == name: return c
    return None
def _text(e, name):
    c = _find(e, name)
    return (c.text or "").strip() if c is not None and c.text else ""

MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
def _parse_time(s):
    if not s: return None
    s = s.strip()
    try:
        return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        pass
    # "Valid 12Z Thu Sep 25 2026" style text in a name/description
    m = re.search(r"(\d{1,2})\s*Z\s+\w{3}\.?\s+(\w{3})\.?\s+(\d{1,2}),?\s+(\d{4})", s, re.I)
    if m and m.group(2).upper() in MONTHS:
        return int(dt.datetime(int(m.group(4)), MONTHS[m.group(2).upper()], int(m.group(3)), int(m.group(1)),
                               tzinfo=dt.timezone.utc).timestamp())
    m = re.search(r"(\d{4})(\d{2})(\d{2})[_ ]?(\d{2})\s*Z?", s)
    if m:
        try: return int(dt.datetime(*map(int, m.groups()), tzinfo=dt.timezone.utc).timestamp())
        except Exception: pass
    return None

def _load(http, url):
    """(kml bytes, zipfile or None) - KMZ unpacked."""
    data = http(url)
    if not data: return None, None
    if data[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(data))
        kml = next((n for n in z.namelist() if n.lower().endswith(".kml")), None)
        return (z.read(kml) if kml else None), z
    return data, None

def collect(http, url, kind, depth=0, out=None):
    out = [] if out is None else out
    kml, z = _load(http, url)
    if not kml: return out
    root = ET.fromstring(kml)
    for el in root.iter():
        t = _tag(el)
        if t == "NetworkLink" and depth < 3:
            href = _text(el, "href")
            if href: collect(http, urllib.parse.urljoin(url, href), kind, depth + 1, out)
        elif t == "GroundOverlay":
            href = _text(el, "href")
            box = _find(el, "LatLonBox")
            if not href or box is None: continue            # (LatLonQuad would need warping; WPC uses boxes)
            try:
                n, s_, e, w = (float(_text(box, k)) for k in ("north", "south", "east", "west"))
            except ValueError:
                continue
            name = _text(el, "name")
            when = _text(el, "when") or _text(el, "begin")
            ts = _parse_time(when) or _parse_time(name) or _parse_time(_text(el, "description")) or _parse_time(href)
            out.append({"kind": kind, "name": name, "t": ts, "href": href, "base": url, "zip": z,
                        "bounds": [s_, w, n, e], "rot": float(_text(box, "rotation") or 0)})
    return out

def _merc(lat): return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))

def to_mercator(png_bytes, s, n):
    """Re-sample an equirectangular image (rows evenly spaced in latitude) so
    its rows are evenly spaced in Web Mercator instead."""
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    a = np.asarray(im)
    H = a.shape[0]
    y = np.linspace(_merc(n), _merc(s), H)                  # top row = north
    lat = np.degrees(2 * np.arctan(np.exp(y)) - math.pi / 2)
    src = np.clip(np.round((n - lat) / (n - s) * (H - 1)).astype(int), 0, H - 1)
    out = Image.fromarray(a[src], "RGBA")
    buf = io.BytesIO()
    out.quantize(colors=128, method=Image.Quantize.FASTOCTREE).save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def build_fronts(http, out_dir):
    fdir = os.path.join(out_dir, "fronts"); os.makedirs(fdir, exist_ok=True)
    items = []
    for kind, url in KMLS:
        try:
            got = collect(http, url, kind)
            print(f"fronts: {url.rsplit('/', 1)[-1]} -> {len(got)} frames", flush=True)
            items += got
        except Exception as e:
            print(f"fronts: {url} FAILED {e}", flush=True)
    # One frame per valid time: analyses win for the past, forecasts for the future.
    now = time.time()
    by_t = {}
    for it in items:
        if it["t"] is None: continue
        k = it["t"]
        keep = by_t.get(k)
        if keep is None or (it["kind"] == "analysis") == (k <= now):
            by_t[k] = it
    frames = []
    for n, (t, it) in enumerate(sorted(by_t.items())):
        try:
            if it["zip"] is not None and it["href"] in it["zip"].namelist():
                img = it["zip"].read(it["href"])
            else:
                img = http(urllib.parse.urljoin(it["base"], it["href"]))
            if not img: continue
            s, w, nn, e = it["bounds"]
            png = to_mercator(img, s, nn) if abs(it["rot"]) < 0.01 else img
            fname = f"{n}.png"
            with open(os.path.join(fdir, fname), "wb") as fh: fh.write(png)
            frames.append({"t": t, "kind": it["kind"], "name": it["name"], "src": f"fronts/{fname}", "bounds": it["bounds"]})
        except Exception as e:
            print(f"fronts: frame {it['name']!r} FAILED {e}", flush=True)
    with open(os.path.join(fdir, "index.json"), "w") as fh:
        json.dump({"generated": int(now * 1000), "frames": frames}, fh, separators=(",", ":"))
    print(f"fronts: {len(frames)} frames written", flush=True)
    return len(frames)
