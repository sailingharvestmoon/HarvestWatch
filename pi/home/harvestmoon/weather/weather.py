#!/usr/bin/env python3
"""
Harvest Moon - Weather + Tide fetcher
-------------------------------------
Reads the boat's position from Firebase (vessels/), pulls current conditions,
a short forecast, and tide predictions from the US National Weather Service and
NOAA CO-OPS, and writes a compact blob to Firebase (weather/) for the e-ink
frame to display. Runs on the Pi as its own service, alongside sensors.py.

Free, no API keys. Standard library only.

Environment variables (set in weather.service):
  PROJECT_ID    Firebase project     (default harvest-moon-watch)
  VESSEL_ID     short name           (default harvest-moon)
  FETCH_MIN     minutes between pulls (default 30)
  FC_MIN        minutes between model-forecast pulls (default 60)
  FALLBACK_LAT  used if no position   (default 44.30 - midcoast Maine)
  FALLBACK_LON                         (default -68.31)
  NWS_UA        User-Agent for NWS (they require one identifying the app)
"""

import os, json, time, math, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

PROJECT_ID = os.environ.get("PROJECT_ID", "harvest-moon-watch").strip()
VESSEL_ID  = os.environ.get("VESSEL_ID", "harvest-moon").strip()
FETCH_MIN  = int(os.environ.get("FETCH_MIN", "30"))
FC_MIN     = int(os.environ.get("FC_MIN", "60"))
FALLBACK_LAT = float(os.environ.get("FALLBACK_LAT", "44.30"))
FALLBACK_LON = float(os.environ.get("FALLBACK_LON", "-68.31"))
NWS_UA = os.environ.get("NWS_UA", "harvest-moon-frame (sailingharvestmoon@gmail.com)")

FS = f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/(default)/documents"
STATION_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tide_stations.json")
PRESS_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pressure_log.json")

MPH_TO_KT = 0.868976
MPS_TO_KT = 1.943844
M_TO_FT   = 3.280839895

def get_json(url, headers=None, timeout=15, retries=2, backoff=2.0):
    """
    The NWS API is intermittently flaky - 500s and occasional 404s that succeed
    on a second attempt. A single failure used to cost the whole forecast for a
    full 30-minute cycle, so retry briefly before giving up.
    """
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise last

# ---- position from Firebase ----------------------------------------------
def num(f):
    if not f: return None
    if "doubleValue" in f: return f["doubleValue"]
    if "integerValue" in f: return int(f["integerValue"])
    return None

def get_position():
    try:
        d = get_json(f"{FS}/vessels/{VESSEL_ID}")
        f = d.get("fields", {})
        lat, lon = num(f.get("lat")), num(f.get("lon"))
        if lat is not None and lon is not None:
            return lat, lon
    except Exception as e:
        print("position read failed:", e, flush=True)
    return FALLBACK_LAT, FALLBACK_LON

# ---- NWS weather ----------------------------------------------------------
def nws(lat, lon, shore=None):
    """
    shore: optional (lat, lon, name) of a nearby ON-SHORE point, used only if
    the boat's own position has no forecast grid. NWS draws gridpoint forecasts
    over land, so a position out in the Sound returns a valid /points/ response
    (which is why city and observations still work) but a 404 on the forecast
    URL it hands back. NOAA tide stations sit on the shore, so the nearest one
    doubles as a perfectly good land point to ask about instead.
    """
    out = {}
    hdr = {"User-Agent": NWS_UA, "Accept": "application/geo+json"}

    def points(la, lo):
        return get_json(f"https://api.weather.gov/points/{la:.4f},{lo:.4f}", hdr)["properties"]

    try:
        p = points(lat, lon)
        rel = p.get("relativeLocation", {}).get("properties", {})
        out["city"] = rel.get("city", "")
        out["state"] = rel.get("state", "")
        fc_url = p.get("forecast")
        obs_url = p.get("observationStations")
    except Exception as e:
        print("nws points failed:", e, flush=True)
        return out

    # forecast periods
    try:
        try:
            fc = get_json(fc_url, hdr)
            fc_src = "boat position"
        except Exception as e_boat:
            # No grid here - almost always because we are on the water.
            print(f"nws forecast at boat position failed ({e_boat}); trying shore point", flush=True)
            if not shore:
                raise
            s_lat, s_lon, s_name = shore
            sp = points(s_lat, s_lon)
            fc = get_json(sp.get("forecast"), hdr)
            fc_src = s_name or "nearby shore"
            print(f"nws forecast obtained from shore point: {fc_src}", flush=True)
        out["forecastFrom"] = fc_src
        periods = fc["properties"]["periods"]
        if periods:
            cur = periods[0]
            out["nowTemp"] = cur.get("temperature")
            out["nowSky"] = cur.get("shortForecast", "")
            out["nowWind"] = fmt_wind(cur.get("windDirection"), cur.get("windSpeed"))
        # next 3 daytime periods for the forecast strip
        days = []
        for pd in periods:
            if pd.get("isDaytime") and len(days) < 3:
                lbl = pd.get("name", "")[:3]
                st = pd.get("startTime")
                if st:
                    try: lbl = datetime.fromisoformat(st).strftime("%a")
                    except Exception: pass
                days.append("{}: {}\u00b0 {} {}".format(
                    lbl,
                    pd.get("temperature", "--"),
                    fmt_wind(pd.get("windDirection"), pd.get("windSpeed")),
                    short_sky(pd.get("shortForecast", ""))))
        out["forecast"] = " | ".join(days)
        # Stamped only on success. A PATCH never deletes fields, so without
        # this a failed forecast leaves the previous one sitting in Firestore
        # looking current - which is how you end up reading Tuesday's weather
        # on Friday without knowing it.
        out["forecastAt"] = int(time.time() * 1000)
    except Exception as e:
        print("nws forecast failed:", e, flush=True)

    # latest observation -> ACTUAL current temp/conditions/pressure. The forecast
    # period's temperature is the day's high, so the observation must override it.
    try:
        stns = get_json(obs_url, hdr)
        sid = stns["features"][0]["properties"]["stationIdentifier"]
        obp = get_json(f"https://api.weather.gov/stations/{sid}/observations/latest", hdr)["properties"]
        tC = obp.get("temperature", {}).get("value")
        if tC is not None:
            out["nowTemp"] = round(tC * 9/5 + 32)
        desc = obp.get("textDescription")
        if desc:
            out["nowSky"] = desc
        pr = obp.get("barometricPressure", {}).get("value")
        if pr is None:
            pr = obp.get("seaLevelPressure", {}).get("value")
        if pr is not None:
            out["pressureMb"] = round(pr / 100.0, 1)  # Pa -> hPa/mb
    except Exception as e:
        print("nws obs failed:", e, flush=True)
    return out

def fmt_wind(direction, speed):
    # speed like "10 mph" or "5 to 10 mph" -> take the top number, convert to kt
    if not speed:
        return (direction or "")
    nums = [int(s) for s in ''.join(c if c.isdigit() else ' ' for c in speed).split()]
    kt = round(max(nums) * MPH_TO_KT) if nums else 0
    return f"{direction or ''} {kt}kt".strip()

def short_sky(s):
    s = s.split(" then ")[0]                      # drop "... then ..." tails
    s = s.replace("Slight Chance ", "").replace("Chance ", "")
    s = s.replace("Showers And Thunderstorms", "T-storms").replace("Thunderstorms", "T-storms")
    return s.strip()[:18]

# ---- NOAA tides -----------------------------------------------------------
def haversine(lat1, lon1, lat2, lon2):
    R=6371.0; rad=math.pi/180
    dp=(lat2-lat1)*rad; dl=(lon2-lon1)*rad
    a=math.sin(dp/2)**2+math.cos(lat1*rad)*math.cos(lat2*rad)*math.sin(dl/2)**2
    return R*2*math.atan2(math.sqrt(a),math.sqrt(1-a))

def load_stations():
    # cache the tide-station list; refresh weekly
    try:
        if os.path.exists(STATION_CACHE) and (time.time()-os.path.getmtime(STATION_CACHE) < 7*86400):
            with open(STATION_CACHE) as f: return json.load(f)
    except Exception:
        pass
    url = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type=tidepredictions"
    data = get_json(url)
    stns = [{"id":s["id"],"name":s["name"],"lat":s["lat"],"lng":s["lng"]} for s in data.get("stations",[])]
    try:
        with open(STATION_CACHE,"w") as f: json.dump(stns,f)
    except Exception:
        pass
    return stns

def nearest_station(lat, lon):
    best=None; bestd=1e9
    for s in load_stations():
        try:
            d=haversine(lat,lon,float(s["lat"]),float(s["lng"]))
        except Exception:
            continue
        if d<bestd: bestd=d; best=s
    return best

def tide_height_at(rows, when):
    """
    Interpolate the tide height between two consecutive high/low points.

    Between an extreme and the next, tide height follows very nearly a cosine
    (this is what the traditional "rule of twelfths" approximates in integer
    steps). Anchoring the curve on the two surrounding extremes gives the
    height at any moment to well within the accuracy we need for scope.

    rows: sorted list of (datetime, height_ft, 'H'|'L')
    """
    prev = nxt = None
    for t, v, _k in rows:
        if t <= when:
            prev = (t, v)
        elif nxt is None:
            nxt = (t, v)
            break
    if prev and nxt:
        span = (nxt[0] - prev[0]).total_seconds()
        if span <= 0:
            return prev[1]
        frac = (when - prev[0]).total_seconds() / span
        mid = (prev[1] + nxt[1]) / 2.0
        amp = (prev[1] - nxt[1]) / 2.0
        return mid + amp * math.cos(math.pi * frac)
    if prev: return prev[1]
    if nxt:  return nxt[1]
    return None

def tide_numbers(rows):
    """
    The hi/lo string is for the e-ink frame to read. The scope alarm needs
    NUMBERS, and specifically the rise still to come: scope is rode / (depth +
    bow roller), so a tide that comes up two feet quietly cuts your scope while
    you are ashore and nothing aboard announces it.

    Derived from the high/low points rather than NOAA's 6-minute series,
    because SUBORDINATE stations (like Niantic) publish hi/lo only - asking
    them for the interval series returns an empty list. Working from the
    extremes means this behaves identically at every station.

    Heights are MLLW predictions - a tidal datum offset, NOT water depth. Only
    the DIFFERENCE between two of them means anything here, which is why every
    output below is a rise or a fall.
    """
    out = {}
    try:
        if len(rows) < 2:
            print("tide numbers: need at least two hi/lo points", flush=True)
            return out
        now = datetime.now()
        cur = tide_height_at(rows, now)
        if cur is None:
            print("tide numbers: now falls outside the prediction window", flush=True)
            return out
        out["tideNowFt"] = round(cur, 2)

        horizon = now + timedelta(hours=12)
        ahead = [(t, v) for t, v, _k in rows if now <= t <= horizon]
        # Include the present height so a tide already past its peak reports a
        # rise of zero rather than inventing one from a later extreme.
        cand = ahead + [(now, cur)]
        hi_t, hi_v = max(cand, key=lambda r: r[1])
        lo_t, lo_v = min(cand, key=lambda r: r[1])
        out["tideMaxNext12Ft"] = round(hi_v, 2)
        out["tideMinNext12Ft"] = round(lo_v, 2)
        out["tideRiseToMaxFt"] = round(max(0.0, hi_v - cur), 2)
        out["tideFallToMinFt"] = round(max(0.0, cur - lo_v), 2)
        if hi_t > now: out["tideMaxAt"] = hi_t.strftime("%-I:%M%p").lower()
        if lo_t > now: out["tideMinAt"] = lo_t.strftime("%-I:%M%p").lower()
        print(f"tide numbers: now {cur:.2f} ft, rise to max {out['tideRiseToMaxFt']} ft", flush=True)
    except Exception as e:
        print("tide numbers failed:", e, flush=True)
    return out

def tides(lat, lon, st=None):
    out={}
    try:
        if st is None:
            st=nearest_station(lat,lon)
        if not st: return out
        out["tideStation"]=st["name"]
        # How far away the station is, so a sensible 2 nm pick can be told
        # apart from a nonsense 30 nm one. Stations are chosen by straight-line
        # distance, which can cross a headland the tide does not.
        try:
            d_nm = haversine(lat, lon, float(st["lat"]), float(st["lng"])) / 1.852
            out["tideStationNm"] = round(d_nm, 1)
            print(f"tide station: {st['name']} ({d_nm:.1f} nm away)", flush=True)
        except Exception:
            pass
        # Start YESTERDAY, not today: interpolating the height at this moment
        # needs an extreme on BOTH sides of now, and just after midnight the
        # preceding one is on the previous day.
        begin=(datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        url=("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?"
             "product=predictions&datum=MLLW&interval=hilo&units=english&"
             "time_zone=lst_ldt&format=json&begin_date=%s&range=72&station=%s" % (begin, st["id"]))
        data=get_json(url)
        now=datetime.now()
        rows=[]
        for p in data.get("predictions",[]):
            try:
                rows.append((datetime.strptime(p["t"], "%Y-%m-%d %H:%M"), float(p["v"]), p.get("type","")))
            except Exception:
                continue
        rows.sort(key=lambda r: r[0])
        print(f"tides: {len(rows)} hi/lo points", flush=True)
        items=[]
        for t,v,kind in rows:
            if t < now - timedelta(minutes=30):
                continue
            items.append("{} {} {}ft".format(
                "HIGH" if kind=="H" else "low",
                t.strftime("%-I:%M%p").lower().replace(":00",""),
                round(v,1)))
            if len(items)>=4: break
        out["tides"]=" / ".join(items)
        out.update(tide_numbers(rows))
        out["tideStationId"]=str(st["id"])
    except Exception as e:
        print("tides failed:", e, flush=True)
    return out

# ---- sun + moon (computed, no API) ---------------------------------------
def sun_times(lat, lon, when=None):
    when = when or datetime.now(timezone.utc)
    N = when.timetuple().tm_yday
    def hour_angle(rising):
        lngHour = lon / 15.0
        t = N + ((6 if rising else 18) - lngHour) / 24.0
        M = 0.9856 * t - 3.289
        L = (M + 1.916*math.sin(math.radians(M)) + 0.020*math.sin(math.radians(2*M)) + 282.634) % 360
        RA = math.degrees(math.atan(0.91764*math.tan(math.radians(L)))) % 360
        RA += (math.floor(L/90)*90 - math.floor(RA/90)*90)
        RA /= 15.0
        sinDec = 0.39782*math.sin(math.radians(L))
        cosDec = math.cos(math.asin(sinDec))
        zenith = 90.833
        cosH = (math.cos(math.radians(zenith)) - sinDec*math.sin(math.radians(lat))) / (cosDec*math.cos(math.radians(lat)))
        if cosH > 1 or cosH < -1:
            return None
        H = (360 - math.degrees(math.acos(cosH)) if rising else math.degrees(math.acos(cosH))) / 15.0
        T = H + RA - 0.06571*t - 6.622
        UT = (T - lngHour) % 24
        return UT
    def to_local(ut):
        if ut is None: return "--"
        base = when.replace(hour=0, minute=0, second=0, microsecond=0)
        dt = base + timedelta(hours=ut)
        return dt.astimezone().strftime("%-I:%M%p").lower()
    return to_local(hour_angle(True)), to_local(hour_angle(False))

def moon_phase(when=None):
    when = when or datetime.now(timezone.utc)
    # days since a known new moon (2000-01-06 18:14 UTC)
    known = datetime(2000,1,6,18,14,tzinfo=timezone.utc)
    days = (when - known).total_seconds()/86400.0
    syn = 29.530588853
    age = days % syn
    illum = round(50*(1-math.cos(2*math.pi*age/syn)))
    names = [(1.85,"New"),(5.5,"Waxing crescent"),(9.2,"First quarter"),
             (12.9,"Waxing gibbous"),(16.6,"Full"),(20.3,"Waning gibbous"),
             (23.9,"Last quarter"),(27.6,"Waning crescent"),(30,"New")]
    for lim,nm in names:
        if age < lim: return f"{nm} ({illum}%)"
    return f"New ({illum}%)"

# ---- Firestore write ------------------------------------------------------
def fs_val(v):
    if isinstance(v,bool): return {"booleanValue":v}
    if isinstance(v,int): return {"integerValue":str(v)}
    if isinstance(v,float): return {"doubleValue":v}
    return {"stringValue":str(v)}

def pressure_trend(now_mb):
    # Barometric tendency = change over ~3 hours (standard mariner practice),
    # not a 30-minute delta which is basically noise.
    hist = []
    try:
        with open(PRESS_LOG) as f: hist = json.load(f)
    except Exception:
        hist = []
    now = time.time()
    hist = [h for h in hist if now - h[0] <= 6 * 3600]     # keep 6 hours
    ref = None
    if hist:
        cand = min(hist, key=lambda h: abs(h[0] - (now - 3 * 3600)))
        if now - cand[0] >= 1.5 * 3600:                    # need real separation
            ref = cand[1]
    hist.append([now, now_mb])
    try:
        with open(PRESS_LOG, "w") as f: json.dump(hist, f)
    except Exception:
        pass
    if ref is None:
        return None                                        # not enough history yet
    diff = now_mb - ref
    if diff >= 1.0: return "rising"
    if diff <= -1.0: return "falling"
    return "steady"

def build_and_write():
    lat, lon = get_position()
    print(f"position {lat:.4f},{lon:.4f}", flush=True)
    data = {}
    # Resolve the tide station first: it doubles as the on-shore point the
    # forecast falls back to when we are out on the water.
    st = nearest_station(lat, lon)
    shore = None
    if st:
        try:
            shore = (float(st["lat"]), float(st["lng"]), st["name"])
        except Exception:
            shore = None
    data.update(nws(lat, lon, shore=shore))
    data.update(tides(lat, lon, st))
    sr, ss = sun_times(lat, lon)
    data["sunrise"], data["sunset"] = sr, ss
    data["moon"] = moon_phase()

    # pressure trend (3-hour tendency)
    if "pressureMb" in data:
        tr = pressure_trend(data["pressureMb"])
        if tr:
            data["pressureTrend"] = tr

    fields = {k: fs_val(v) for k, v in data.items() if v not in (None, "")}
    fields["updatedAt"] = fs_val(int(time.time()*1000))
    body = json.dumps({"fields": fields}).encode()
    req = urllib.request.Request(f"{FS}/weather/{VESSEL_ID}", data=body, method="PATCH",
                                 headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            keys = ", ".join(k for k in data if data.get(k))
            print(f"wrote weather [{r.status}]: {keys}", flush=True)
    except Exception as e:
        print("weather write failed:", e, flush=True)

# ---- Model forecasts (Open-Meteo) -> weather/<vessel>-forecast ------------
# Seven models, pulled on the boat and stored in Firestore so the Harvest
# Watch app only ever READS a finished forecast - nothing is fetched or kept
# on the phone. Free, no API key. One request per model, so a model that is
# down or has no coverage here (NAM/HRRR outside US waters) only blanks its
# own row. Always pulls 7 days; the app chooses how much to show.
FC_DOC = f"{FS}/weather/{VESSEL_ID}-forecast"
FC_MODELS = [
    ("ecmwf", ["ecmwf_ifs025", "ecmwf_ifs"]),
    # The pure global runs, not Open-Meteo's "seamless" blends. gfs_seamless
    # splices HRRR into the first two days near the US (which is why GFS and
    # HRRR came out identical), icon_seamless splices in ICON-EU/D2. The
    # plain runs match what PredictWind labels GFS / ICON / UKMO.
    ("gfs",   ["gfs_global", "gfs_seamless"]),
    ("ukmo",  ["ukmo_global_deterministic_10km", "ukmo_seamless"]),
    ("icon",  ["icon_global", "icon_seamless"]),
    ("nam",   ["ncep_nam_conus"]),
    ("hrrr",  ["ncep_hrrr_conus", "gfs_hrrr"]),
    ("aifs",  ["ecmwf_aifs025_single", "ecmwf_aifs025"]),
]
FC_VARS = ["weather_code", "is_day", "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m",
           "precipitation", "cape", "cloud_cover", "temperature_2m", "pressure_msl"]
FC_DAYS = 7
# Decimal places kept per variable - enough for display, and it keeps the
# stored document small (it is read by the phone over Starlink / cell).
FC_ROUND = {"precipitation": 1, "pressure_msl": 1, "wind_speed_10m": 1, "wind_gusts_10m": 1}

def _round_list(vals, nd):
    out = []
    for v in vals or []:
        if v is None: out.append(None)
        elif nd == 0: out.append(int(round(v)))
        else: out.append(round(v, nd))
    return out

def fc_model(apis, lat, lon):
    last = "failed"
    for name in apis:
        url = ("https://api.open-meteo.com/v1/forecast?"
               f"latitude={lat:.4f}&longitude={lon:.4f}&hourly={','.join(FC_VARS)}"
               f"&daily=sunrise,sunset&models={name}&wind_speed_unit=kn&temperature_unit=fahrenheit"
               f"&precipitation_unit=mm&timezone=auto&forecast_days={FC_DAYS}")
        try:
            j = get_json(url, retries=1)
        except Exception as e:
            last = str(e)[:120]
            continue
        if j.get("error"):
            last = str(j.get("reason", "error"))[:120]
            continue
        h = j.get("hourly") or {}
        ok = any(v is not None for v in h.get("wind_speed_10m") or [])
        return j, {"api": name, "ok": ok, "err": "" if ok else "no coverage here"}
    return None, {"ok": False, "err": last}

def build_forecast():
    lat, lon = get_position()
    models, base = {}, None
    for mid, apis in FC_MODELS:
        j, meta = fc_model(apis, lat, lon)
        if j is not None:
            h = j.get("hourly") or {}
            if base is None and h.get("time"):
                base = j
            # Align onto the base time axis in case a model returns a
            # different span; every model is requested identically, so
            # normally this is a straight copy.
            if base is not None and h.get("time") != base["hourly"]["time"]:
                idx = {t: k for k, t in enumerate(h.get("time") or [])}
                h = {v: [ (h.get(v) or [None]*len(idx))[idx[t]] if t in idx else None
                          for t in base["hourly"]["time"] ] for v in FC_VARS}
            meta["h"] = {v: _round_list(h.get(v), FC_ROUND.get(v, 0)) for v in FC_VARS}
        models[mid] = meta
        print(f"forecast {mid}: {meta.get('api', '-')} {'ok' if meta['ok'] else meta['err']}", flush=True)
    if base is None:
        print("forecast: no model answered - keeping the previous forecast", flush=True)
        return False
    marine = None
    try:
        m = get_json("https://marine-api.open-meteo.com/v1/marine?"
                     f"latitude={lat:.4f}&longitude={lon:.4f}&hourly=wave_height,wave_direction,wave_period"
                     f"&length_unit=imperial&timezone=auto&forecast_days={FC_DAYS}", retries=1)
        mh = m.get("hourly") or {}
        if mh.get("time"):
            marine = {"time": mh["time"], "wave_height": _round_list(mh.get("wave_height"), 1),
                      "wave_direction": _round_list(mh.get("wave_direction"), 0),
                      "wave_period": _round_list(mh.get("wave_period"), 0)}
    except Exception as e:
        print("forecast marine failed:", e, flush=True)
    data = {
        "at": int(time.time() * 1000), "lat": round(lat, 4), "lon": round(lon, 4),
        "off": base.get("utc_offset_seconds", 0), "tz": base.get("timezone_abbreviation", ""),
        "time": base["hourly"]["time"], "daily": base.get("daily"), "models": models, "marine": marine,
    }
    blob = json.dumps(data, separators=(",", ":"))
    # Whole-document write on purpose: it also clears any pending requestAt.
    body = json.dumps({"fields": {"data": {"stringValue": blob},
                                  "updatedAt": {"integerValue": str(data["at"])}}}).encode()
    req = urllib.request.Request(FC_DOC, data=body, method="PATCH",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ok_n = sum(1 for m in models.values() if m["ok"])
            print(f"wrote forecast [{r.status}]: {ok_n}/{len(models)} models, {len(blob)//1024} KB", flush=True)
            return True
    except Exception as e:
        print("forecast write failed:", e, flush=True)
        return False

def forecast_requested(since):
    """True if the app has asked for a fresh pull (requestAt) since `since`.
    Reads only that one field, not the whole forecast."""
    try:
        f = get_json(FC_DOC + "?mask.fieldPaths=requestAt", retries=0).get("fields", {})
        r = num(f.get("requestAt"))
        return bool(r and r / 1000.0 > since)
    except Exception:
        return False

def run():
    print(f"weather: {PROJECT_ID}/{VESSEL_ID}, weather every {FETCH_MIN} min, forecasts every {FC_MIN} min", flush=True)
    last_wx = last_fc = 0.0
    while True:
        now = time.time()
        if now - last_wx >= FETCH_MIN * 60:
            last_wx = now
            try:
                build_and_write()
            except Exception as e:
                print("cycle error:", e, flush=True)
        # Hourly, or sooner when the app's Refresh asks - but never more
        # than once every 5 minutes, whatever the app does.
        if now - last_fc >= FC_MIN * 60 or (now - last_fc >= 300 and forecast_requested(last_fc)):
            last_fc = now
            try:
                build_forecast()
            except Exception as e:
                print("forecast error:", e, flush=True)
        time.sleep(60)

if __name__ == "__main__":
    run()
