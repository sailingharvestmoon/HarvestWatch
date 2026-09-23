#!/usr/bin/env python3
"""
Harvest Moon - Sensor Telemetry (NMEA -> Firebase)
--------------------------------------------------
Runs alongside the photo frame on the same Pi. Opens its OWN read-only TCP
socket to the Vesper (the frame doc confirms multiple readers are fine), parses
the same sentences frame.py uses, computes TRUE wind exactly like the dashboard,
and pushes an imperial-units summary to Firebase every minute.

Whenever the Vesper is on, the data is automatically online - there is no mode to
switch. When the Vesper is off there's no data, so it simply idles.

Environment variables (set in sensors.service):
  XB_HOST     Vesper address    (default 192.168.1.167)
  XB_PORT     NMEA TCP port     (default 39150)
  PROJECT_ID  Firebase project  (default harvest-moon-watch)
  VESSEL_ID   short name        (default harvest-moon)
  PUSH_SEC    upload interval   (default 60)
  FRESH_SEC   drop readings older than this many seconds (default 45)

Standard library only - nothing to pip install.
"""

import os, socket, json, time, math, urllib.request
from collections import deque

XB_HOST    = os.environ.get("XB_HOST", "192.168.1.167")
XB_PORT    = int(os.environ.get("XB_PORT", "39150"))
PROJECT_ID = os.environ.get("PROJECT_ID", "harvest-moon-watch").strip()
VESSEL_ID  = os.environ.get("VESSEL_ID", "harvest-moon").strip()
PUSH_SEC   = int(os.environ.get("PUSH_SEC", "60"))
FRESH_SEC  = int(os.environ.get("FRESH_SEC", "45"))
STALL_SEC  = int(os.environ.get("STALL_SEC", "90"))   # no NMEA for this long => force reconnect

# ---- latest readings: name -> (value, epoch_seen) -------------------------
latest = {}
def remember(name, value): latest[name] = (value, time.time())
def fresh(name):
    if name not in latest: return None
    value, seen = latest[name]
    return value if (time.time() - seen) <= FRESH_SEC else None

def fnum(s):
    try: return float(s)
    except (TypeError, ValueError): return None

# ---- true wind, ported verbatim from frame.py -----------------------------
def true_wind(awa_deg, aws, boat_speed):
    awa = math.radians(awa_deg)
    x = aws * math.cos(awa) - boat_speed
    y = aws * math.sin(awa)
    return math.degrees(math.atan2(y, x)) % 360, math.hypot(x, y)

# ---- Gust: peak TRUE wind over the trailing hour ---------------------------
# Same method as the cabin frame (frame.py): every wind sentence becomes a
# true-wind sample, a median of the last few samples knocks out single-sample
# anemometer glitches, and the highest filtered value in the last hour is the
# gust. Published as gustKn / gustDirDeg / gustAt for the Instruments tab.
GUST_WINDOW = int(os.environ.get("GUST_WINDOW", "3600"))
GUST_MAX_KT = 70                       # anything above is treated as garbage
gust_hist = deque()                    # (epoch, knots, true dir or None)
_recent_tws = deque(maxlen=5)

def nmea_ok(line):
    # Reject sentences whose checksum is wrong - corrupted wind sentences
    # are exactly what produce phantom gusts.
    line = line.strip()
    if not line.startswith('$'): return False
    if '*' not in line: return True
    body, _, ck = line[1:].partition('*')
    try: want = int(ck[:2], 16)
    except ValueError: return False
    got = 0
    for ch in body: got ^= ord(ch)
    return got == want

def _median(v):
    s = sorted(v); n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

def gust_sample(awa, aws):
    if not (0 <= aws <= GUST_MAX_KT): return
    twa, tws = true_wind(awa, aws, fresh('boatspd') or 0.0)
    _recent_tws.append(tws)
    if len(_recent_tws) < 3: return
    g = _median(_recent_tws)
    hdg = fresh('hdg_mag')
    twd = None
    if hdg is not None:
        var = fresh('var')
        if var is None: var = bow_offset_config()[2]
        twd = (hdg + var + twa) % 360
    now = time.time()
    gust_hist.append((now, g, twd))
    while gust_hist and now - gust_hist[0][0] > GUST_WINDOW:
        gust_hist.popleft()

def gust_peak():
    now = time.time()
    while gust_hist and now - gust_hist[0][0] > GUST_WINDOW:
        gust_hist.popleft()
    return max(gust_hist, key=lambda g: g[1]) if gust_hist else None

# ---- NMEA parse (mirrors frame.py's proven field extraction) --------------
def parse(sentence):
    try:
        p = sentence.strip().split('*')[0].split(',')
        t = p[0]
        if t.endswith('MWV') and len(p) > 3 and p[2] == 'R':   # apparent wind
            awa, aws = fnum(p[1]), fnum(p[3])
            if awa is not None: remember('awa', awa)
            if aws is not None: remember('aws', aws)
            if awa is not None and aws is not None and nmea_ok(sentence):
                gust_sample(awa, aws)
        elif t.endswith('DPT') and len(p) > 1 and p[1]:        # depth (m below transducer)
            m = fnum(p[1])
            if m is not None: remember('depth_m', m)
        elif t.endswith('MTW') and len(p) > 1 and p[1]:        # water temp (C)
            c = fnum(p[1])
            if c is not None: remember('temp_c', c)
        elif t.endswith('VHW') and len(p) > 5 and p[5]:        # speed through water (kt)
            spd = fnum(p[5])
            if spd is not None: remember('boatspd', spd)
        elif t.endswith('HDG') and len(p) > 1 and p[1]:        # magnetic heading
            h = fnum(p[1])
            if h is not None: remember('hdg_mag', h)
        elif t.endswith('RMC') and len(p) > 8 and p[2] == 'A': # GPS fix
            sog, cog = fnum(p[7]), fnum(p[8])
            if sog is not None: remember('sog', sog)
            if cog is not None: remember('cog', cog)
            if len(p) > 6 and p[3] and p[5]:
                try:
                    lat = int(p[3][:2]) + float(p[3][2:]) / 60.0
                    if p[4] == 'S': lat = -lat
                    lon = int(p[5][:3]) + float(p[5][3:]) / 60.0
                    if p[6] == 'W': lon = -lon
                    remember('lat', lat); remember('lon', lon)
                except Exception: pass
            if len(p) > 11 and p[10]:
                v = fnum(p[10])
                if v is not None: remember('var', v * (-1 if p[11] == 'W' else 1))
    except Exception:
        pass

# ---- Firestore write ------------------------------------------------------
def fs_value(v):
    if isinstance(v, bool):  return {"booleanValue": v}
    if isinstance(v, int):   return {"integerValue": str(v)}
    if isinstance(v, float): return {"doubleValue": v}
    return {"stringValue": str(v)}

def build_fields():
    f = {}
    aws, awa = fresh('aws'), fresh('awa')
    boatspd = fresh('boatspd') or 0.0
    if aws is not None: f['windSpeedApparentKn'] = round(aws, 1)
    if awa is not None: f['windAngleApparentDeg'] = round(awa)
    # True wind, computed like the dashboard
    if aws is not None and awa is not None:
        twa, tws = true_wind(awa, aws, boatspd)
        f['windSpeedTrueKn'] = round(tws, 1)
        hdg = fresh('hdg_mag')
        if hdg is not None:
            hdg_true = hdg + (fresh('var') or 0.0)
            f['windDirTrueDeg'] = round((hdg_true + twa) % 360)
    depth_m = fresh('depth_m')
    if depth_m is not None: f['depthFt'] = round(depth_m * 3.280839895, 1)
    temp_c = fresh('temp_c')
    if temp_c is not None: f['waterTempF'] = round(temp_c * 9.0 / 5.0 + 32.0)
    if fresh('boatspd') is not None: f['boatSpeedKn'] = round(fresh('boatspd'), 1)
    if fresh('sog') is not None: f['sogKn'] = round(fresh('sog'), 1)
    if fresh('cog') is not None: f['cogDeg'] = round(fresh('cog'))
    if fresh('hdg_mag') is not None: f['headingMagDeg'] = round(fresh('hdg_mag'))
    # True heading, published so consumers can do geographic offsets (the watch
    # page projects the GPS-to-bow distance along it when marking the anchor).
    # Magnetic would be wrong by the local variation - about 13 degrees in Long
    # Island Sound, which throws a 30 ft offset sideways by roughly 7 ft.
    if fresh('hdg_mag') is not None:
        _v = fresh('var')
        if _v is None:
            _v = bow_offset_config()[2]      # configured fallback
        f['headingTrueDeg'] = round((fresh('hdg_mag') + _v) % 360)
        f['variationDeg'] = round(_v, 1)
        f['variationFromGps'] = (fresh('var') is not None)
    pk = gust_peak()
    if pk is not None:
        f['gustKn'] = round(pk[1], 1)
        if pk[2] is not None: f['gustDirDeg'] = round(pk[2])
        f['gustAt'] = int(pk[0] * 1000)
    if fresh('lat') is not None: f['lat'] = round(fresh('lat'), 6)
    if fresh('lon') is not None: f['lon'] = round(fresh('lon'), 6)
    return f

def push():
    if not PROJECT_ID:
        print("PROJECT_ID not set - cannot push", flush=True); return
    vals = build_fields()
    if not vals:
        print("no fresh readings to push (Vesper off?)", flush=True); return
    fields = {k: fs_value(v) for k, v in vals.items()}
    fields["updatedAt"] = fs_value(int(time.time() * 1000))
    url = (f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}"
           f"/databases/(default)/documents/telemetry/{VESSEL_ID}")
    body = json.dumps({"fields": fields}).encode()
    req = urllib.request.Request(url, data=body, method="PATCH",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            summary = ", ".join(f"{k}={v}" for k, v in vals.items())
            print(f"pushed [{r.status}]: {summary}", flush=True)
    except Exception as e:
        print(f"push failed: {e}", flush=True)

_cfg_cache = {"at": 0.0, "offset_ft": 0.0, "enabled": False, "magvar": 0.0}

def bow_offset_config():
    """
    Read the GPS-antenna-to-bow-roller offset from the alarms document, cached
    for five minutes. It changes about once in the life of the boat.
    """
    now = time.time()
    if now - _cfg_cache["at"] < 300:
        return _cfg_cache["offset_ft"], _cfg_cache["enabled"], _cfg_cache["magvar"]
    _cfg_cache["at"] = now
    try:
        url = (f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}"
               f"/databases/(default)/documents/alarms/{VESSEL_ID}")
        with urllib.request.urlopen(url, timeout=10) as r:
            f = json.load(r).get("fields", {})
        v = f.get("bowOffsetFt", {})
        off = float(v.get("doubleValue", v.get("integerValue", 0)) or 0)
        en = bool(f.get("bowOffsetEnabled", {}).get("booleanValue"))
        mv = f.get("magVarDeg", {})
        var = float(mv.get("doubleValue", mv.get("integerValue", 0)) or 0)
        _cfg_cache["offset_ft"], _cfg_cache["enabled"], _cfg_cache["magvar"] = off, en, var
    except Exception as e:
        print(f"bow offset config read failed: {e}", flush=True)
    return _cfg_cache["offset_ft"], _cfg_cache["enabled"], _cfg_cache["magvar"]

def dest_point(lat, lon, bearing_deg, dist_m):
    R = 6371000.0
    br = math.radians(bearing_deg); d = dist_m / R
    la1 = math.radians(lat); lo1 = math.radians(lon)
    la2 = math.asin(math.sin(la1)*math.cos(d) + math.cos(la1)*math.sin(d)*math.cos(br))
    lo2 = lo1 + math.atan2(math.sin(br)*math.sin(d)*math.cos(la1),
                           math.cos(d) - math.sin(la1)*math.sin(la2))
    return math.degrees(la2), (math.degrees(lo2) + 540) % 360 - 180

def push_position():
    # Publishes the boat's GPS position to vessels/ so the Pi acts as the anchor
    # transmitter (replacing the phone). The anchor alarm reads this doc. Only
    # publishes a valid, fresh fix - a Vesper dropout stops updates rather than
    # freezing the last position, which the anchor watcher reads as "lost contact".
    lat, lon = fresh('lat'), fresh('lon')
    if lat is None or lon is None:
        return
    fields = {
        "lat":       {"doubleValue": lat},
        "lon":       {"doubleValue": lon},
        "accuracy":  {"doubleValue": 5.0},   # nominal; Vesper GPS has no accuracy field
        "timestamp": {"integerValue": str(int(time.time() * 1000))},
    }
    # BOW POSITION. The anchor is marked at the bow, so measuring drag from the
    # GPS - which sits well aft - reports the boat as further out than it is by
    # the length of the boat. With 100 ft of chain down that showed as 130 ft
    # from the anchor on a calm night with nothing dragging at all.
    # Projected along the heading rather than subtracted as a constant, because
    # subtracting is only correct while the bow happens to point at the anchor.
    off_ft, off_en, cfg_var = bow_offset_config()
    hdg = fresh('hdg_mag')
    # Magnetic variation from the GPS if it sends one - many units, including
    # this Vesper, leave the RMC variation field empty - otherwise the value
    # configured on the watch page. Without it the bow would be projected along
    # a magnetic bearing, throwing a 30 ft offset sideways by about 7 ft here.
    var = fresh('var')
    if var is None:
        var = cfg_var
    if off_en and off_ft > 0 and hdg is not None:
        b_lat, b_lon = dest_point(lat, lon, (hdg + var) % 360, off_ft * 0.3048)
        fields["bowLat"] = {"doubleValue": b_lat}
        fields["bowLon"] = {"doubleValue": b_lon}
        fields["bowOffsetFt"] = {"doubleValue": off_ft}
    url = (f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}"
           f"/databases/(default)/documents/vessels/{VESSEL_ID}")
    body = json.dumps({"fields": fields}).encode()
    req = urllib.request.Request(url, data=body, method="PATCH",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            bow = ""
            if "bowLat" in fields:
                bow = (f"  bow {fields['bowLat']['doubleValue']:.6f}, "
                       f"{fields['bowLon']['doubleValue']:.6f} (+{off_ft:.0f} ft)")
            else:
                bow = "  bow OFF (no offset configured, or no heading)"
            print(f"position [{r.status}]: {lat:.6f}, {lon:.6f}{bow}", flush=True)
    except Exception as e:
        print(f"position push failed: {e}", flush=True)

# ---- main loop ------------------------------------------------------------
def run():
    print(f"sensors: {XB_HOST}:{XB_PORT} -> {PROJECT_ID}/{VESSEL_ID} every {PUSH_SEC}s", flush=True)
    # Start the clock now rather than at zero: at zero the first push fires
    # instantly on connect, before any NMEA has arrived, publishing a partial
    # telemetry document with no position in it.
    last_push = time.time()
    while True:
        try:
            with socket.create_connection((XB_HOST, XB_PORT), timeout=10) as sock:
                sock.settimeout(5)
                print("connected to Vesper NMEA stream", flush=True)
                buf = b""
                last_data = time.time()
                while True:
                    try:
                        chunk = sock.recv(4096)
                        if not chunk: raise ConnectionError("stream closed")
                        last_data = time.time()
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            parse(line.decode("ascii", "ignore"))
                    except socket.timeout:
                        pass
                    now = time.time()
                    # Watchdog: a WiFi/Vesper drop can leave the socket half-open,
                    # where recv() just times out forever and we never see an error.
                    # If no NMEA has arrived for STALL_SEC, drop the socket and
                    # reconnect - this is what lets it self-heal after an overnight blip.
                    if now - last_data > STALL_SEC:
                        print(f"no NMEA for {int(now - last_data)}s - reconnecting", flush=True)
                        break
                    if now - last_push >= PUSH_SEC:
                        push(); push_position(); last_push = now
        except Exception as e:
            print(f"connection error: {e} - retry in 10s", flush=True)
            time.sleep(10)

if __name__ == "__main__":
    run()
