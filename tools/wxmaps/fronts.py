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

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 harvest-watch")

# WPC doesn't answer GitHub's build machines (connections time out), so
# files are fetched through the harvest-weather Cloudflare worker's /wpc relay.
RELAY = os.environ.get("WPC_RELAY", "https://harvest-weather.sailingharvestmoon.workers.dev/wpc?u=")

def _via(url, relay):
    return RELAY + urllib.parse.quote(url, safe="") if relay else url

def wpc_get(url, tries=2):
    import urllib.request, urllib.error
    why = ""
    for relay, timeout in ((True, 60), (False, 15)):
        for k in range(tries if relay else 1):
            try:
                req = urllib.request.Request(_via(url, relay), headers={"User-Agent": BROWSER_UA, "Accept": "*/*"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                why = f"HTTP {e.code} ({'relay' if relay else 'direct'})"
                if e.code in (403, 404): break
            except Exception as e:
                why = f"{e} ({'relay' if relay else 'direct'})"
            time.sleep(2)
    print(f"fronts: fetch failed {url.rsplit('/', 1)[-1]}: {why}", flush=True)
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

# WPC's KMLs carry no valid times - only names like "12Z Surface Analysis",
# "36-hour forecast" or "Day 5". The valid time is worked out from the name
# plus the image's Last-Modified time on WPC's server.
def _last_modified(url):
    import urllib.request, email.utils
    try:
        req = urllib.request.Request(_via(url, True), method="HEAD", headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            lm = r.headers.get("Last-Modified")
        return email.utils.parsedate_to_datetime(lm).timestamp() if lm else None
    except Exception:
        return None

def _floor(ts, hours):
    step = hours * 3600
    return int(ts // step * step)

def infer_time(it, now):
    name = it["name"] or ""
    url = urllib.parse.urljoin(it["base"], it["href"])
    lm = _last_modified(url) or now
    m = re.search(r"(\d{1,2})\s*Z\s+Surface Analysis", name, re.I)
    if m:
        hh = int(m.group(1))
        day = _floor(lm, 24)
        t = day + hh * 3600
        return t if t <= lm else t - 86400
    if re.search(r"latest", name, re.I):
        return _floor(lm - 1800, 3)
    m = re.search(r"(\d+)\s*-?\s*hour", name, re.I)
    if m:
        return _floor(lm - 3 * 3600, 6) + int(m.group(1)) * 3600
    m = re.search(r"Day\s*(\d)", name, re.I)
    if m:
        return _floor(lm - 12 * 3600, 24) + 12 * 3600 + int(m.group(1)) * 86400
    return None

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
    http = wpc_get                      # WPC needs its own fetcher (see wpc_get)
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
    for it in items:
        if it["t"] is None:
            it["t"] = infer_time(it, now)
    by_t = {}
    for it in items:
        if it["t"] is None:
            print(f"fronts: no time for {it['name']!r}", flush=True); continue
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
