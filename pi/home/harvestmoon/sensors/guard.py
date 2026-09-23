#!/usr/bin/env python3
"""
Harvest Moon - Anchor Guard (fast, on-boat watcher)
====================================================
Third reader on the Vesper's NMEA stream, alongside frame.py and sensors.py.
This is the PRIMARY anchor watcher. It evaluates every 10 seconds instead of
every 60, and it pushes to ntfy directly from the boat rather than from a
shared Cloudflare egress IP.

Division of labour with the Cloudflare worker:

    guard.py (here, 10s)     drag, swing, wind, gust, apparent wind angle, AIS
    worker   (cloud, 60s)    boat-gone-dark heartbeat, scope, depth, daily ping,
                             track recording, and drag as a slower backup

Both read and write state/<vessel>, sharing the dragAlerted latch, so whichever
one notices a drag first suppresses a duplicate from the other. Neither can be
silenced by the other failing - that is the whole point of running two.

AIS: the Vesper is a transponder, so !AIVDM sentences are already on the wire
we are reading. This decodes position reports (types 1/2/3 Class A, 18/19
Class B) and static reports (5, 24) for vessel names, then watches for anything
creeping down on us while we are anchored.

Environment (set in guard.service):
  XB_HOST       Vesper address        (default 192.168.1.167)
  XB_PORT       NMEA TCP port         (default 39150)
  PROJECT_ID    Firebase project      (default harvest-moon-watch)
  VESSEL_ID     short name            (default harvest-moon)
  NTFY_TOPIC    ntfy topic            (REQUIRED - same one the worker uses)
  NTFY_TOKEN    ntfy account token    (recommended)
  NTFY_SERVER   default https://ntfy.sh
  TICK_SEC      evaluation interval   (default 10)
  CFG_SEC       config refresh        (default 30)
  BEAT_SEC      heartbeat + AIS write (default 60)

Standard library only.
"""

import os, sys, signal, socket, json, time, math, urllib.request
from collections import deque

XB_HOST     = os.environ.get("XB_HOST", "192.168.1.167")
XB_PORT     = int(os.environ.get("XB_PORT", "39150"))
PROJECT_ID  = os.environ.get("PROJECT_ID", "harvest-moon-watch").strip()
VESSEL_ID   = os.environ.get("VESSEL_ID", "harvest-moon").strip()
NTFY_TOPIC  = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_TOKEN  = os.environ.get("NTFY_TOKEN", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TICK_SEC    = int(os.environ.get("TICK_SEC", "10"))
CFG_SEC     = int(os.environ.get("CFG_SEC", "30"))
BEAT_SEC    = int(os.environ.get("BEAT_SEC", "60"))
FRESH_SEC   = int(os.environ.get("FRESH_SEC", "45"))
STALL_SEC   = int(os.environ.get("STALL_SEC", "90"))

FS = f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/(default)/documents"

M_TO_FT = 3.280839895
FT_TO_M = 0.3048

# Defaults for every tunable. The watch page writes these into alarms/<vessel>;
# anything it has not written yet falls back to the value here, so a fresh
# install guards the boat sensibly before you have touched a single setting.
DEFAULTS = {
    "radius": 150.0,          # ft
    "confirmSec": 60,         # seconds continuously outside before alerting
    "swingEnabled": False,
    "swingRateDeg": 90.0,     # bearing change from anchor...
    "swingRateMin": 20,       # ...within this many minutes
    "windEnabled": False,
    "windSustainedKn": 25.0,  # 5-minute average true wind
    "gustKn": 35.0,           # peak true wind in the last 10 minutes
    "awaEnabled": False,
    "awaMaxDeg": 70.0,        # lying this far off the bow = something is odd
    "awaSustainMin": 5,
    "aisEnabled": False,
    "aisWarnM": 50.0,         # warning zone radius, metres
    "aisCollisionM": 5.0,     # collision zone radius, metres
    "aisSpeedKn": 3.0,        # targets faster than this are assumed under way
    "aisTcpaMin": 20.0,       # only care about closing inside this many minutes
    "ownMMSI": 0,
    "renotifyMin": 10,
    "bowOffsetEnabled": False,
    "bowOffsetFt": 0.0,
    "magVarDeg": 0.0,          # used when the GPS sends no variation
    # Dead man's switch. See deadman_refresh() for why this exists.
    "deadmanEnabled": True,
    "deadmanDelayMin": 30,     # alert fires this long after the last refresh
}

# The refresh interval is DERIVED from the delay, never set independently.
# A refresh slower than the delay fires the alert on a perfectly healthy boat
# every single time - which is exactly what happened when the two were separate
# settings and the delay was shorter than the refresh. Refreshing at a third of
# the delay also means two consecutive failed refreshes still do not cry wolf.
# Floor of 5 min keeps the message cost sane: at a 30 min delay that is 10 min
# between refreshes, or 144 a day against a 250-per-12-hours budget.
DEADMAN_SEQ = "hm-watchdog"
DEADMAN_MIN_DELAY = 15          # minutes; anything shorter is not worth alerting on

def deadman_delay_min():
    d = float(cfg.get("deadmanDelayMin") or DEFAULTS["deadmanDelayMin"])
    return max(DEADMAN_MIN_DELAY, d)

def deadman_refresh_sec():
    return max(300.0, deadman_delay_min() * 60.0 / 3.0)

# ---------------------------------------------------------------- readings
latest = {}
def remember(name, value): latest[name] = (value, time.time())
def fresh(name, max_age=None):
    if name not in latest: return None
    value, seen = latest[name]
    return value if (time.time() - seen) <= (max_age or FRESH_SEC) else None

def fnum(s):
    try: return float(s)
    except (TypeError, ValueError): return None

def true_wind(awa_deg, aws, boat_speed):
    awa = math.radians(awa_deg)
    x = aws * math.cos(awa) - boat_speed
    y = aws * math.sin(awa)
    return math.degrees(math.atan2(y, x)) % 360, math.hypot(x, y)

# ---------------------------------------------------------------- geometry
def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0; rad = math.pi / 180
    p1, p2 = lat1 * rad, lat2 * rad
    dp, dl = (lat2 - lat1) * rad, (lon2 - lon1) * rad
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def bearing_deg(lat1, lon1, lat2, lon2):
    rad = math.pi / 180
    p1, p2 = lat1 * rad, lat2 * rad
    dl = (lon2 - lon1) * rad
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1)*math.sin(p2) - math.sin(p1)*math.cos(p2)*math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360

def angle_diff(a, b):
    """Smallest signed difference between two compass bearings, -180..180."""
    return (a - b + 180) % 360 - 180

def compass(deg):
    pts = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return pts[int(round(deg / 22.5)) % 16]

# ---------------------------------------------------------------- AIS decode
# AIVDM payloads are 6-bit ASCII packed into a bitstring. Multi-fragment
# messages arrive as separate sentences and have to be reassembled before
# decoding, which is what _ais_frags holds.
_ais_frags = {}

def sixbit(payload):
    bits = []
    for ch in payload:
        v = ord(ch) - 48
        if v > 40: v -= 8
        if v < 0 or v > 63: return None
        bits.append(format(v, "06b"))
    return "".join(bits)

def ubits(bits, start, length):
    if start + length > len(bits): return None
    return int(bits[start:start+length], 2)

def sbits(bits, start, length):
    v = ubits(bits, start, length)
    if v is None: return None
    if v & (1 << (length - 1)):
        v -= (1 << length)
    return v

def ais_text(bits, start, length):
    """Decode a 6-bit ASCII string field (vessel names, call signs)."""
    chars = []
    for i in range(start, start + length, 6):
        v = ubits(bits, i, 6)
        if v is None: break
        if v == 0: break
        chars.append(chr(v + 64) if v < 32 else chr(v))
    return "".join(chars).replace("@", "").strip()

ais_targets = {}   # mmsi -> dict
own_mmsi_seen = [0]

def handle_ais(sentence):
    """Feed one !AIVDM / !AIVDO sentence in. Reassembles and decodes."""
    try:
        body = sentence.strip().split("*")[0]
        p = body.split(",")
        if len(p) < 6: return
        total, seq, mid, chan, payload = p[1], p[2], p[3], p[4], p[5]
        is_own = p[0].endswith("VDO")   # own-ship report from the Vesper itself
        total_i = int(total); seq_i = int(seq)

        if total_i == 1:
            bits = sixbit(payload)
        else:
            key = mid or chan
            if seq_i == 1: _ais_frags[key] = {}
            if key not in _ais_frags: return
            _ais_frags[key][seq_i] = payload
            if len(_ais_frags[key]) < total_i: return
            joined = "".join(_ais_frags[key][i] for i in sorted(_ais_frags[key]))
            del _ais_frags[key]
            bits = sixbit(joined)
        if not bits: return

        msgtype = ubits(bits, 0, 6)
        mmsi = ubits(bits, 8, 30)
        if mmsi is None: return

        # Learn our own MMSI from the transponder's own-ship sentences so we
        # never alarm on ourselves even if ownMMSI was never configured.
        if is_own:
            own_mmsi_seen[0] = mmsi
            return

        if msgtype in (1, 2, 3):
            sog = ubits(bits, 50, 10); lon = sbits(bits, 61, 28)
            lat = sbits(bits, 89, 27); cog = ubits(bits, 116, 12)
        elif msgtype in (18, 19):
            sog = ubits(bits, 46, 10); lon = sbits(bits, 57, 28)
            lat = sbits(bits, 85, 27); cog = ubits(bits, 112, 12)
        elif msgtype == 5:
            nm = ais_text(bits, 112, 120)
            if nm: ais_targets.setdefault(mmsi, {})["name"] = nm
            return
        elif msgtype == 24:
            if ubits(bits, 38, 2) == 0:
                nm = ais_text(bits, 40, 120)
                if nm: ais_targets.setdefault(mmsi, {})["name"] = nm
            return
        else:
            return

        if lat is None or lon is None: return
        lat = lat / 600000.0; lon = lon / 600000.0
        if abs(lat) > 90 or abs(lon) > 180: return      # 91/181 = not available

        t = ais_targets.setdefault(mmsi, {})
        t.update({
            "mmsi": mmsi, "lat": lat, "lon": lon,
            "sog": (sog / 10.0) if (sog is not None and sog < 1023) else None,
            "cog": (cog / 10.0) if (cog is not None and cog < 3600) else None,
            "seen": time.time(),
        })
    except Exception:
        pass

def cpa_tcpa(range_m, brg_deg, tgt_sog_kn, tgt_cog_deg):
    """
    Closest point of approach for a target relative to US, treating our own
    boat as stationary - which is a fair assumption at anchor and errs toward
    alerting. Returns (cpa_metres, tcpa_minutes) or (None, None).
    """
    if tgt_sog_kn is None or tgt_cog_deg is None or tgt_sog_kn <= 0:
        return None, None
    # Target position in a local frame with us at the origin, x=east, y=north.
    br = math.radians(brg_deg)
    px, py = range_m * math.sin(br), range_m * math.cos(br)
    spd_ms = tgt_sog_kn * 0.514444
    cg = math.radians(tgt_cog_deg)
    vx, vy = spd_ms * math.sin(cg), spd_ms * math.cos(cg)
    vsq = vx*vx + vy*vy
    if vsq <= 0: return None, None
    t = -(px*vx + py*vy) / vsq          # seconds until closest approach
    if t < 0: return range_m, -1.0      # already opening
    cx, cy = px + vx*t, py + vy*t
    return math.hypot(cx, cy), t / 60.0

# ---------------------------------------------------------------- NMEA parse
def parse(sentence):
    s = sentence.strip()
    if not s: return
    if s.startswith("!"):
        handle_ais(s)
        return
    try:
        p = s.split("*")[0].split(",")
        t = p[0]
        if t.endswith("MWV") and len(p) > 3 and p[2] == "R":
            awa, aws = fnum(p[1]), fnum(p[3])
            if awa is not None: remember("awa", awa)
            if aws is not None: remember("aws", aws)
        elif t.endswith("DPT") and len(p) > 1 and p[1]:
            m = fnum(p[1])
            if m is not None: remember("depth_m", m)
        elif t.endswith("VHW") and len(p) > 5 and p[5]:
            spd = fnum(p[5])
            if spd is not None: remember("boatspd", spd)
        elif t.endswith("HDG") and len(p) > 1 and p[1]:
            h = fnum(p[1])
            if h is not None: remember("hdg_mag", h)
        elif t.endswith("RMC") and len(p) > 8 and p[2] == "A":
            sog, cog = fnum(p[7]), fnum(p[8])
            if sog is not None: remember("sog", sog)
            if cog is not None: remember("cog", cog)
            if len(p) > 6 and p[3] and p[5]:
                try:
                    lat = int(p[3][:2]) + float(p[3][2:]) / 60.0
                    if p[4] == "S": lat = -lat
                    lon = int(p[5][:3]) + float(p[5][3:]) / 60.0
                    if p[6] == "W": lon = -lon
                    remember("lat", lat); remember("lon", lon)
                except Exception: pass
            if len(p) > 11 and p[10]:
                v = fnum(p[10])
                if v is not None: remember("var", v * (-1 if p[11] == "W" else 1))
    except Exception:
        pass

# ---------------------------------------------------------------- Firestore
def fs_get(coll, doc):
    try:
        with urllib.request.urlopen(f"{FS}/{coll}/{doc}", timeout=10) as r:
            return json.load(r).get("fields", {})
    except Exception:
        return None

def fs_patch(coll, doc, fields):
    """Field-masked PATCH - the worker writes the same documents, so an
    unmasked write would clobber whatever it just put there."""
    mask = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
    url = f"{FS}/{coll}/{doc}?{mask}"
    body = json.dumps({"fields": fields}).encode()
    req = urllib.request.Request(url, data=body, method="PATCH",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status < 300
    except Exception as e:
        print(f"firestore patch {coll}/{doc} failed: {e}", flush=True)
        return False

def fnum_fs(f):
    if not f: return None
    if "doubleValue" in f: return float(f["doubleValue"])
    if "integerValue" in f: return int(f["integerValue"])
    return None

def fbool(f): return bool(f.get("booleanValue")) if f else False

# ---------------------------------------------------------------- ntfy
push_health = {"ok": None, "err": ""}

def push(title, message, priority=4, tags="anchor"):
    """Returns True only on a real 2xx. Alert latches are set on this and
    nothing else, so a failed push is retried on the next tick instead of
    being silently recorded as delivered."""
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set - cannot push", flush=True)
        push_health.update(ok=False, err="NTFY_TOPIC not set")
        return False
    payload = {
        "topic": NTFY_TOPIC, "title": title, "message": message,
        "priority": int(priority),
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
    }
    headers = {"Content-Type": "application/json"}
    if NTFY_TOKEN: headers["Authorization"] = "Bearer " + NTFY_TOKEN
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(payload).encode(),
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = r.status < 300
            push_health.update(ok=ok, err="" if ok else f"HTTP {r.status}")
            print(f"push [{r.status}] {title}", flush=True)
            return ok
    except Exception as e:
        push_health.update(ok=False, err=str(e)[:300])
        print(f"push FAILED (retry next tick): {e}", flush=True)
        return False

# ---------------------------------------------------------------- state
cfg = dict(DEFAULTS)
cfg_extra = {"armed": False, "anchorLat": None, "anchorLon": None, "snoozeUntil": 0}

alerts = {"drag": False, "swing": False, "wind": False, "gust": False,
          "awa": False, "ais": False}
last_drag_push = [0.0]
breach_since = [0.0]
awa_since = [0.0]
swing_hist = deque()     # (ts, bearing_from_anchor)
wind_hist = deque()      # (ts, true wind speed kn)
ais_alerted = {}         # mmsi -> ts of last alert

def load_config():
    f = fs_get("alarms", VESSEL_ID)
    if f is None: return
    for k, dflt in DEFAULTS.items():
        v = f.get(k)
        if v is None: continue
        if isinstance(dflt, bool): cfg[k] = fbool(v)
        else:
            n = fnum_fs(v)
            if n is not None: cfg[k] = n
    cfg_extra["armed"] = fbool(f.get("armed"))
    cfg_extra["anchorLat"] = fnum_fs(f.get("anchorLat"))
    cfg_extra["anchorLon"] = fnum_fs(f.get("anchorLon"))
    cfg_extra["snoozeUntil"] = fnum_fs(f.get("snoozeUntil")) or 0

def sync_drag_latch():
    """Read the shared drag flag so we do not double-alert with the worker."""
    f = fs_get("state", VESSEL_ID)
    if f is None: return
    alerts["drag"] = fbool(f.get("dragAlerted"))

def write_drag_latch(active):
    fs_patch("state", VESSEL_ID, {
        "dragAlerted": {"booleanValue": bool(active)},
        "lastDragTs": {"integerValue": str(int(time.time() * 1000))},
    })

deadman_armed = [False]

def deadman_refresh():
    """
    Schedule (or re-schedule) a 'the boat has gone dark' alert for
    deadmanDelayMin from now, replacing any previously scheduled one.

    ntfy holds the message on its server and delivers it unless we replace it
    again first. So the alert fires when this Pi STOPS calling - power loss,
    Starlink outage, SD card death, the Pi falling over. Nothing needs to be
    alive to send it, which is the whole point: a watcher that has to be
    running in order to tell you it stopped running is not a watcher.

    Publishing to <server>/<topic>/<sequence-id> with the In: header both
    schedules and replaces, per ntfy's scheduled-delivery API.
    """
    if not NTFY_TOPIC:
        return False
    delay = int(deadman_delay_min())
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}/{DEADMAN_SEQ}"
    body = (f"No heartbeat from Harvest Moon for {delay} min. The anchor watch is "
            f"NOT running - the Pi, the Vesper, or the internet is down. "
            f"Nothing is watching the boat right now.").encode()
    # ASCII only in headers: ntfy header values cannot carry characters above
    # Latin-1, and a stray en dash here would break every refresh silently.
    headers = {
        "Content-Type": "text/plain",
        "In": f"{delay}m",
        "Title": "Harvest Moon - watch is OFFLINE",
        "Priority": "5",
        "Tags": "rotating_light,electric_plug",
    }
    if NTFY_TOKEN:
        headers["Authorization"] = "Bearer " + NTFY_TOKEN
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = r.status < 300
            if ok:
                deadman_armed[0] = True
                print(f"deadman: offline alert rescheduled {delay} min out "
                      f"(next refresh in {deadman_refresh_sec()/60:.0f} min)", flush=True)
            return ok
    except Exception as e:
        print(f"deadman refresh failed: {e}", flush=True)
        return False

def deadman_cancel():
    """Delete the pending alert. Called when you disarm, and on a clean
    shutdown, so a deliberate stop does not page you 30 minutes later."""
    if not NTFY_TOPIC or not deadman_armed[0]:
        return
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}/{DEADMAN_SEQ}"
    headers = {}
    if NTFY_TOKEN:
        headers["Authorization"] = "Bearer " + NTFY_TOKEN
    req = urllib.request.Request(url, method="DELETE", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"deadman cancelled [{r.status}]", flush=True)
    except Exception as e:
        print(f"deadman cancel failed: {e}", flush=True)
    deadman_armed[0] = False

def check_quota():
    """
    Read the ntfy account's remaining message budget and log it. This is a GET,
    so it does not itself consume messages. It exists because running out of
    quota is otherwise completely invisible: sends just stop arriving.
    """
    if not NTFY_TOKEN:
        return None
    req = urllib.request.Request(f"{NTFY_SERVER}/v1/account",
                                 headers={"Authorization": "Bearer " + NTFY_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
        stats = d.get("stats", {})
        limits = d.get("limits", {})
        remaining = stats.get("messages_remaining")
        print(f"ntfy quota: {remaining} of {limits.get('messages')} remaining "
              f"(basis {limits.get('basis')})", flush=True)
        fs_patch("health", VESSEL_ID, {
            "ntfyRemaining": {"integerValue": str(int(remaining or 0))},
            "ntfyCheckedAt": {"integerValue": str(int(time.time() * 1000))},
        })
        return remaining
    except Exception as e:
        print(f"quota check failed: {e}", flush=True)
        return None

def write_heartbeat():
    fields = {"guardLastRun": {"integerValue": str(int(time.time() * 1000))}}
    if push_health["ok"] is True:
        fields["guardLastPushOkTs"] = {"integerValue": str(int(time.time() * 1000))}
    if push_health["ok"] is False:
        fields["guardLastPushFailTs"] = {"integerValue": str(int(time.time() * 1000))}
        fields["guardLastPushError"] = {"stringValue": push_health["err"]}
    fs_patch("health", VESSEL_ID, fields)

def write_ais_doc(nearby):
    rows = []
    for t in nearby[:12]:
        rows.append("{}|{}|{:.0f}|{:.0f}|{}|{}".format(
            t["mmsi"], (t.get("name") or "").replace("|", " ")[:20],
            t["range_m"], t["brg"],
            "" if t["sog"] is None else round(t["sog"], 1),
            "" if t["cpa"] is None else round(t["cpa"])))
    fs_patch("ais", VESSEL_ID, {
        "targets": {"stringValue": ";".join(rows)},
        "count": {"integerValue": str(len(nearby))},
        "updatedAt": {"integerValue": str(int(time.time() * 1000))},
    })

def snoozed():
    return time.time() * 1000 < (cfg_extra["snoozeUntil"] or 0)

def clear_all_latches():
    for k in alerts: alerts[k] = False
    breach_since[0] = 0.0
    awa_since[0] = 0.0
    swing_hist.clear()
    ais_alerted.clear()

# ---------------------------------------------------------------- checks
def bow_position(lat, lon):
    """
    Where the BOW is, projected from the GPS along the true heading.

    The anchor is marked at the bow, so measuring from the GPS - which sits
    aft - overstates the distance by the length of the boat. Projecting rather
    than subtracting a fixed amount matters: subtracting is only correct while
    the bow points at the anchor, and is exactly backwards when it does not.
    """
    if not cfg["bowOffsetEnabled"] or cfg["bowOffsetFt"] <= 0:
        return lat, lon, False
    hdg = fresh("hdg_mag")
    if hdg is None:
        return lat, lon, False
    var = fresh("var")
    if var is None:
        var = cfg["magVarDeg"]        # GPS sends no variation; use the configured value
    d = cfg["bowOffsetFt"] * FT_TO_M
    brg = math.radians((hdg + var) % 360)
    R = 6371000.0
    la1, lo1 = math.radians(lat), math.radians(lon)
    dr = d / R
    la2 = math.asin(math.sin(la1)*math.cos(dr) + math.cos(la1)*math.sin(dr)*math.cos(brg))
    lo2 = lo1 + math.atan2(math.sin(brg)*math.sin(dr)*math.cos(la1),
                           math.cos(dr) - math.sin(la1)*math.sin(la2))
    return math.degrees(la2), (math.degrees(lo2) + 540) % 360 - 180, True

def check_drag(lat, lon):
    a_lat, a_lon = cfg_extra["anchorLat"], cfg_extra["anchorLon"]
    if a_lat is None or a_lon is None: return None
    b_lat, b_lon, _used = bow_position(lat, lon)
    dist_ft = haversine_m(a_lat, a_lon, b_lat, b_lon) * M_TO_FT
    radius = cfg["radius"]
    now = time.time()

    if dist_ft > radius:
        if breach_since[0] == 0.0: breach_since[0] = now
        held = now - breach_since[0]
        if held >= cfg["confirmSec"]:
            if not alerts["drag"]:
                if push("ANCHOR DRAGGING",
                        f"Harvest Moon is {dist_ft:.0f} ft from anchor (limit {radius:.0f} ft). "
                        f"Outside for {held/60:.0f} min. Check the boat now.",
                        5, "rotating_light,anchor"):
                    alerts["drag"] = True
                    last_drag_push[0] = now
                    write_drag_latch(True)
            elif now - last_drag_push[0] > cfg["renotifyMin"] * 60:
                if push("STILL DRAGGING",
                        f"Harvest Moon {dist_ft:.0f} ft from anchor (limit {radius:.0f} ft).",
                        5, "rotating_light,anchor"):
                    last_drag_push[0] = now
    else:
        breach_since[0] = 0.0
        if alerts["drag"] and dist_ft < radius * 0.9:
            if push("Harvest Moon \u2014 back inside",
                    f"Boat is {dist_ft:.0f} ft from anchor, within the {radius:.0f} ft limit.",
                    3, "white_check_mark"):
                alerts["drag"] = False
                write_drag_latch(False)
    return dist_ft

def check_swing(lat, lon):
    if not cfg["swingEnabled"]: return
    a_lat, a_lon = cfg_extra["anchorLat"], cfg_extra["anchorLon"]
    if a_lat is None or a_lon is None: return
    now = time.time()
    brg = bearing_deg(a_lat, a_lon, lat, lon)
    swing_hist.append((now, brg))
    window = cfg["swingRateMin"] * 60
    while swing_hist and now - swing_hist[0][0] > window:
        swing_hist.popleft()
    if len(swing_hist) < 3: return
    oldest = swing_hist[0][1]
    shift = abs(angle_diff(brg, oldest))
    if shift > cfg["swingRateDeg"]:
        if not alerts["swing"]:
            if push("Harvest Moon \u2014 big swing",
                    f"Bearing from the anchor has moved {shift:.0f}\u00b0 in the last "
                    f"{cfg['swingRateMin']:.0f} min (now lying {compass(brg)} of the anchor). "
                    f"Wind shift, tide change, or the anchor has reset.",
                    4, "warning,cyclone"):
                alerts["swing"] = True
    elif shift < cfg["swingRateDeg"] * 0.6:
        alerts["swing"] = False

def check_wind():
    if not cfg["windEnabled"]: return
    aws, awa = fresh("aws"), fresh("awa")
    if aws is None or awa is None: return
    boatspd = fresh("boatspd") or 0.0
    _, tws = true_wind(awa, aws, boatspd)
    now = time.time()
    wind_hist.append((now, tws))
    while wind_hist and now - wind_hist[0][0] > 600:   # keep 10 minutes
        wind_hist.popleft()

    recent5 = [w for t, w in wind_hist if now - t <= 300]
    if len(recent5) >= 5:
        avg5 = sum(recent5) / len(recent5)
        if avg5 > cfg["windSustainedKn"]:
            if not alerts["wind"]:
                if push("Harvest Moon \u2014 wind up",
                        f"True wind averaging {avg5:.0f} kn over the last 5 min "
                        f"(your limit is {cfg['windSustainedKn']:.0f} kn).",
                        4, "warning,wind_face"):
                    alerts["wind"] = True
        elif avg5 < cfg["windSustainedKn"] * 0.85:
            alerts["wind"] = False

    peak = max((w for _, w in wind_hist), default=0.0)
    if peak > cfg["gustKn"]:
        if not alerts["gust"]:
            if push("Harvest Moon \u2014 gusting",
                    f"Gust of {peak:.0f} kn in the last 10 min "
                    f"(your limit is {cfg['gustKn']:.0f} kn).",
                    4, "warning,wind_face"):
                alerts["gust"] = True
    elif peak < cfg["gustKn"] * 0.8:
        alerts["gust"] = False

def check_awa():
    """At anchor the boat should lie roughly head to wind. Sitting well off the
    bow for a sustained period means something is holding her sideways - current
    against wind, a fouled rode, or a bridle problem."""
    if not cfg["awaEnabled"]: return
    awa = fresh("awa")
    aws = fresh("aws")
    if awa is None or aws is None: return
    if aws < 5: return                      # too light to mean anything
    off_bow = abs(angle_diff(awa, 0))
    now = time.time()
    if off_bow > cfg["awaMaxDeg"]:
        if awa_since[0] == 0.0: awa_since[0] = now
        if now - awa_since[0] >= cfg["awaSustainMin"] * 60 and not alerts["awa"]:
            if push("Harvest Moon \u2014 lying off the wind",
                    f"Apparent wind {off_bow:.0f}\u00b0 off the bow for "
                    f"{cfg['awaSustainMin']:.0f} min in {aws:.0f} kn. "
                    f"Current against wind, or the rode may be fouled.",
                    4, "warning,compass"):
                alerts["awa"] = True
    else:
        awa_since[0] = 0.0
        alerts["awa"] = False

def check_ais(lat, lon):
    """Mirrors the zone logic PredictWind uses: anything inside the collision
    zone is alarming regardless, and inside the warning zone we only care about
    slow-moving targets, since a vessel making way is presumed to be steering."""
    now = time.time()
    own = int(cfg["ownMMSI"]) or own_mmsi_seen[0]
    nearby = []
    for mmsi, t in list(ais_targets.items()):
        if "lat" not in t: continue
        if now - t.get("seen", 0) > 300:
            del ais_targets[mmsi]; continue
        if own and mmsi == own: continue
        rng = haversine_m(lat, lon, t["lat"], t["lon"])
        if rng > 2000: continue
        brg = bearing_deg(lat, lon, t["lat"], t["lon"])
        cpa, tcpa = cpa_tcpa(rng, brg, t.get("sog"), t.get("cog"))
        nearby.append({"mmsi": mmsi, "name": t.get("name"), "range_m": rng,
                       "brg": brg, "sog": t.get("sog"), "cpa": cpa, "tcpa": tcpa})
    nearby.sort(key=lambda x: x["range_m"])

    if not cfg["aisEnabled"] or snoozed():
        return nearby

    for t in nearby:
        label = t["name"] or f"MMSI {t['mmsi']}"
        sog = t["sog"]
        hit = None
        if t["range_m"] <= cfg["aisCollisionM"]:
            hit = (f"{label} is {t['range_m']:.0f} m away, bearing {t['brg']:.0f}\u00b0 "
                   f"({compass(t['brg'])}). That is inside the collision zone.")
        elif t["range_m"] <= cfg["aisWarnM"]:
            if sog is not None and sog < cfg["aisSpeedKn"]:
                if (t["cpa"] is not None and t["cpa"] <= cfg["aisCollisionM"]
                        and t["tcpa"] is not None and 0 <= t["tcpa"] <= cfg["aisTcpaMin"]):
                    hit = (f"{label} is {t['range_m']:.0f} m away bearing {t['brg']:.0f}\u00b0 "
                           f"({compass(t['brg'])}), making {sog:.1f} kn, closing to "
                           f"{t['cpa']:.0f} m in {t['tcpa']:.0f} min. Possibly dragging down on us.")
        if hit:
            last = ais_alerted.get(t["mmsi"], 0)
            if now - last > cfg["renotifyMin"] * 60:
                if push("AIS \u2014 vessel closing", hit, 5, "rotating_light,ship"):
                    ais_alerted[t["mmsi"]] = now
    return nearby

# ---------------------------------------------------------------- main loop
def tick():
    load_needed = False
    lat, lon = fresh("lat"), fresh("lon")
    if lat is None or lon is None:
        # No fix. The cloud worker owns the "boat has gone dark" alert, so we
        # deliberately stay quiet here rather than double up.
        return None
    if not cfg_extra["armed"]:
        clear_all_latches()
        return None
    if snoozed():
        return None

    dist_ft = check_drag(lat, lon)
    check_swing(lat, lon)
    check_wind()
    check_awa()
    return dist_ft

def run():
    print(f"guard: {XB_HOST}:{XB_PORT} -> {PROJECT_ID}/{VESSEL_ID}, tick {TICK_SEC}s", flush=True)
    if not NTFY_TOPIC:
        print("WARNING: NTFY_TOPIC is empty - no alert can be sent", flush=True)
    last_tick = last_cfg = last_beat = 0.0
    last_deadman = 0.0
    last_quota = 0.0
    nearby = []
    while True:
        try:
            with socket.create_connection((XB_HOST, XB_PORT), timeout=10) as sock:
                sock.settimeout(2)
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
                    if now - last_data > STALL_SEC:
                        print(f"no NMEA for {int(now-last_data)}s - reconnecting", flush=True)
                        break
                    if now - last_cfg >= CFG_SEC:
                        load_config(); sync_drag_latch(); last_cfg = now
                    if now - last_tick >= TICK_SEC:
                        last_tick = now
                        try:
                            dist = tick()
                            la, lo = fresh("lat"), fresh("lon")
                            if la is not None and lo is not None:
                                nearby = check_ais(la, lo)
                            # Always report, armed or not. Silence would leave
                            # you unable to tell a correctly-idle guard apart
                            # from one that is parsing nothing at all.
                            aws, awa = fresh("aws"), fresh("awa")
                            dep = fresh("depth_m")
                            bits = []
                            bits.append(f"fix {la:.5f},{lo:.5f}" if la is not None and lo is not None
                                        else "fix NONE")
                            bits.append(f"{dist:.0f}ft from anchor" if dist is not None
                                        else ("armed, no anchor set" if cfg_extra["armed"] else "disarmed"))
                            if aws is not None and awa is not None:
                                bits.append(f"wind {aws:.1f}kn @ {awa:.0f}\u00b0")
                            if dep is not None:
                                bits.append(f"depth {dep * M_TO_FT:.1f}ft")
                            bits.append(f"AIS {len(nearby)} within 2km")
                            if nearby:
                                t0 = nearby[0]
                                bits.append("nearest {} {:.0f}m brg {:.0f}".format(
                                    t0.get("name") or t0["mmsi"], t0["range_m"], t0["brg"]))
                            if own_mmsi_seen[0]:
                                bits.append(f"own MMSI {own_mmsi_seen[0]}")
                            print("tick: " + " | ".join(bits), flush=True)
                        except Exception as e:
                            print(f"tick error: {e}", flush=True)
                    if now - last_beat >= BEAT_SEC:
                        last_beat = now
                        write_heartbeat()
                        if cfg["aisEnabled"]:
                            write_ais_doc(nearby)

                    # Dead man's switch. Only while armed - otherwise a boat
                    # sitting on the hard all winter would page you nightly.
                    if cfg_extra["armed"] and cfg["deadmanEnabled"]:
                        if not deadman_armed[0] or now - last_deadman >= deadman_refresh_sec():
                            if deadman_refresh():
                                last_deadman = now
                    elif deadman_armed[0]:
                        deadman_cancel()

                    if now - last_quota >= 3600:
                        last_quota = now
                        check_quota()
        except Exception as e:
            print(f"connection error: {e} - retry in 10s", flush=True)
            # Keep the heartbeat alive while disconnected so the watch page can
            # distinguish "guard is down" from "guard is up but blind".
            try: write_heartbeat()
            except Exception: pass
            time.sleep(10)

if __name__ == "__main__":
    # A deliberate stop (systemctl stop, Ctrl-C, reboot) must cancel the
    # pending offline alert, or every restart would page you 30 minutes later.
    # An UNCLEAN stop - power cut, kernel panic, drowned Pi - deliberately does
    # not, because that is precisely the case you need to hear about.
    def _bye(signum, frame):
        print("shutting down - cancelling pending offline alert", flush=True)
        try: deadman_cancel()
        except Exception: pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    run()
