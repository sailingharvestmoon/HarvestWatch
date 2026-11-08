#!/usr/bin/env python3
"""
Harvest Moon polar logger.

Listens to an NMEA 0183 stream, computes true wind from apparent wind and
speed through water, and writes one row per second to a daily CSV.

Three things to know before running it:

  * It needs SPEED THROUGH WATER, not SOG. GPS speed includes current, and a
    polar built from current-contaminated speed is a record of the tide.
  * True wind is computed here, not taken from the instrument, so the maths is
    visible and the masthead offset can be corrected after the fact.
  * Motoring rows are marked, not discarded. polar_build.py drops them.
  * The boat state (Sailing / Motoring / Anchored) comes from the e-ink frame's
    side buttons (frame.py saves it). Anchored time isn't logged at all.
  * It also keeps the TRIP distance shown on the e-ink dashboard (see Trip
    below), because this is the one program that hears the Vesper all the
    time and already follows the boat state.

Usage:
    python3 polar_logger.py --probe          # what's on the wire?
    python3 polar_logger.py                  # log
    python3 polar_logger.py --simulate       # fake data, for dock testing
"""

import argparse
import csv
import glob
import json
import math
import os
import random
import socket
import sys
import time
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Configuration you may want to change
# ---------------------------------------------------------------------------

# Masthead rotation error, degrees. Positive = unit is rotated to starboard,
# i.e. reported AWA reads higher than truth on starboard tack. polar_build.py
# estimates this for you from port/starboard asymmetry once you have data.
AWA_OFFSET_DEG = 0.0

# Speed-through-water calibration multiplier. 1.0 until you have reason.
STW_SCALE = 1.0

# A field older than this many seconds is treated as missing.
MAX_AGE_S = 3.0

# Engine RPM above this counts as motoring.
MOTORING_RPM = 400.0

# Touch this file when the engine is on if your tach isn't on the NMEA bus.
# Still honoured, but the frame's buttons (below) are the normal way now.
MOTORING_FLAG = "/run/harvestmoon.motoring"

# Boat state set with the e-ink frame's side buttons (frame.py writes these).
# The newest valid file wins; nothing set yet counts as sailing.
BOAT_STATE_FILES = [
    "/var/lib/harvest-moon/boat_state.json",
    "/home/harvestmoon/polar/boat_state.json",
]
BOAT_STATES = ("sailing", "motoring", "anchored")
USE_BOAT_STATE = True      # turned off for --simulate

# Places to look for a stream when --kind auto. Vesper XB-8000/8010 defaults
# first, then common Signal K / multiplexer ports.
AUTO_TARGETS = [
    ("tcp", "192.168.1.167", 39150),
]

CSV_FIELDS = [
    "utc", "lat", "lon", "sog", "cog", "hdg_true",
    "stw", "awa", "aws", "twa", "tws", "twd",
    "heel", "depth", "water_temp", "rpm", "motoring", "state",
]


# ---------------------------------------------------------------------------
# NMEA plumbing
# ---------------------------------------------------------------------------

def checksum_ok(line):
    """Validate the *NN checksum. Sentences without one are accepted."""
    if "*" not in line:
        return True
    body, _, cks = line[1:].partition("*")
    if len(cks) < 2:
        return False
    try:
        want = int(cks[:2], 16)
    except ValueError:
        return False
    got = 0
    for ch in body:
        got ^= ord(ch)
    return got == want


def with_checksum(body):
    """Build a full sentence from a body like 'GPVHW,,T,,M,5.4,N,,K'."""
    cks = 0
    for ch in body:
        cks ^= ord(ch)
    return "${}*{:02X}".format(body, cks)


def to_knots(value, unit):
    unit = (unit or "N").upper()
    if unit == "N":
        return value
    if unit == "K":            # km/h
        return value * 0.539957
    if unit == "M":            # m/s
        return value * 1.94384
    if unit == "S":            # mph
        return value * 0.868976
    return value


def fnum(fields, idx):
    """Float from a field list, or None if absent/blank/garbage."""
    if idx >= len(fields):
        return None
    raw = fields[idx].strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def norm180(deg):
    """Wrap to (-180, 180]. Negative is port."""
    d = (deg + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d


def norm360(deg):
    return deg % 360.0


def dm_to_deg(raw, hemi):
    """NMEA ddmm.mmmm -> signed decimal degrees."""
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    deg = int(val / 100)
    minutes = val - deg * 100
    out = deg + minutes / 60.0
    if hemi in ("S", "W"):
        out = -out
    return out


class State:
    """Latest value of every field we care about, with an age on each."""

    def __init__(self):
        self.v = {}
        self.sentences = {}     # talker+type -> count
        self.now = time.monotonic

    def set(self, name, value):
        if value is None:
            return
        self.v[name] = (value, self.now())

    def get(self, name, max_age=MAX_AGE_S):
        item = self.v.get(name)
        if item is None:
            return None
        value, stamp = item
        if self.now() - stamp > max_age:
            return None
        return value

    def seen(self, name):
        return name in self.v


def parse(line, st):
    """Feed one NMEA line into the state. Returns the sentence key, or None."""
    line = line.strip()
    if not line or line[0] not in "$!":
        return None
    if not checksum_ok(line):
        return None
    body = line[1:].split("*")[0]
    f = body.split(",")
    if not f or len(f[0]) < 5:
        return None
    typ = f[0][-3:].upper()
    st.sentences[typ] = st.sentences.get(typ, 0) + 1

    if typ == "MWV":
        angle, ref, speed, unit = fnum(f, 1), (f[2] if len(f) > 2 else ""), fnum(f, 3), (f[4] if len(f) > 4 else "N")
        status = f[5] if len(f) > 5 else "A"
        if angle is None or speed is None or status.upper().startswith("V"):
            return typ
        kn = to_knots(speed, unit)
        if ref.upper() == "R":
            st.set("awa_raw", norm180(angle))
            st.set("aws", kn)
        elif ref.upper() == "T":
            st.set("twa_inst", norm180(angle))
            st.set("tws_inst", kn)

    elif typ == "VWR":                       # older relative wind
        angle, side, kn = fnum(f, 1), (f[2] if len(f) > 2 else "R"), fnum(f, 3)
        if angle is not None and kn is not None:
            signed = -angle if side.upper() == "L" else angle
            st.set("awa_raw", norm180(signed))
            st.set("aws", kn)

    elif typ == "VWT":                       # older true wind, boat-relative
        angle, side, kn = fnum(f, 1), (f[2] if len(f) > 2 else "R"), fnum(f, 3)
        if angle is not None and kn is not None:
            st.set("twa_inst", norm180(-angle if side.upper() == "L" else angle))
            st.set("tws_inst", kn)

    elif typ == "MWD":                       # true wind direction, earth frame
        twd = fnum(f, 1)
        kn = fnum(f, 5)
        if twd is not None:
            st.set("twd_inst", norm360(twd))
        if kn is not None:
            st.set("tws_inst", kn)

    elif typ == "VHW":                       # heading + speed through water
        st.set("hdg_true", fnum(f, 1))
        st.set("hdg_mag", fnum(f, 3))
        kn = fnum(f, 5)
        if kn is None:
            kmh = fnum(f, 7)
            kn = kmh * 0.539957 if kmh is not None else None
        if kn is not None:
            st.set("stw", kn * STW_SCALE)

    elif typ == "VBW":                       # dual ground/water speed
        kn = fnum(f, 1)
        if kn is not None:
            st.set("stw", kn * STW_SCALE)

    elif typ == "VTG":
        st.set("cog", fnum(f, 1))
        kn = fnum(f, 5)
        if kn is not None:
            st.set("sog", kn)

    elif typ == "RMC":
        if len(f) > 6 and (f[2] or "").upper().startswith("A"):
            st.set("lat", dm_to_deg(f[3], f[4]))
            st.set("lon", dm_to_deg(f[5], f[6]))
        st.set("sog", fnum(f, 7))
        st.set("cog", fnum(f, 8))

    elif typ == "GGA":
        if len(f) > 5:
            st.set("lat", dm_to_deg(f[2], f[3]))
            st.set("lon", dm_to_deg(f[4], f[5]))

    elif typ == "HDT":
        st.set("hdg_true", fnum(f, 1))

    elif typ in ("HDM", "HDG"):
        st.set("hdg_mag", fnum(f, 1))

    elif typ == "DPT":
        depth, offset = fnum(f, 1), fnum(f, 2)
        if depth is not None:
            st.set("depth", depth + (offset or 0.0))

    elif typ == "DBT":
        m = fnum(f, 3)
        if m is not None:
            st.set("depth", m)

    elif typ == "MTW":
        st.set("water_temp", fnum(f, 1))

    elif typ == "RPM":
        if len(f) > 3 and (f[1] or "").upper().startswith("E"):
            st.set("rpm", fnum(f, 3))

    elif typ == "XDR":                       # heel, if anyone is sending it
        i = 1
        while i + 3 < len(f) + 1 and i + 3 <= len(f):
            kind, val, name = f[i], fnum(f, i + 1), (f[i + 3] if i + 3 < len(f) else "")
            if kind.upper() == "A" and val is not None:
                if any(k in name.upper() for k in ("ROLL", "HEEL")):
                    st.set("heel", val)
            i += 4

    return typ


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def tcp_lines(host, port, timeout=10):
    buf = b""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                yield raw.decode("ascii", "ignore")
    finally:
        sock.close()


def udp_lines(host, port, timeout=10):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except OSError:
        pass
    sock.bind((host or "", port))
    sock.settimeout(timeout)
    try:
        while True:
            data, _ = sock.recvfrom(4096)
            for raw in data.decode("ascii", "ignore").replace("\r", "\n").split("\n"):
                if raw:
                    yield raw
    finally:
        sock.close()


def serial_lines(dev, baud, timeout=10):
    try:
        import serial                        # pyserial
    except ImportError:
        raise SystemExit(
            "Serial input needs pyserial:  sudo apt install python3-serial")
    port = serial.Serial(dev, baud, timeout=timeout)
    try:
        while True:
            raw = port.readline()
            if raw:
                yield raw.decode("ascii", "ignore")
    finally:
        port.close()


# A crude speed model, used only by --simulate so the pipeline can be exercised
# at the dock. Real numbers come from your sailing, not from this.
SIM_POLAR = {
    6:  {45: 3.4, 60: 4.2, 90: 5.0, 120: 4.9, 150: 3.8, 180: 3.0},
    10: {45: 5.1, 60: 5.9, 90: 6.5, 120: 6.5, 150: 5.4, 180: 4.5},
    14: {45: 5.9, 60: 6.5, 90: 7.1, 120: 7.2, 150: 6.4, 180: 5.6},
    20: {45: 6.2, 60: 6.8, 90: 7.5, 120: 7.7, 150: 7.1, 180: 6.4},
    25: {45: 6.1, 60: 6.7, 90: 7.5, 120: 7.8, 150: 7.3, 180: 6.6},
}


def sim_speed(tws, twa):
    twa = abs(twa)
    if twa < 38:
        return 0.6 * tws * 0.3
    ws = sorted(SIM_POLAR)
    lo = max([w for w in ws if w <= tws], default=ws[0])
    hi = min([w for w in ws if w >= tws], default=ws[-1])

    def at(w):
        angles = sorted(SIM_POLAR[w])
        a_lo = max([a for a in angles if a <= twa], default=angles[0])
        a_hi = min([a for a in angles if a >= twa], default=angles[-1])
        if a_lo == a_hi:
            return SIM_POLAR[w][a_lo]
        frac = (twa - a_lo) / (a_hi - a_lo)
        return SIM_POLAR[w][a_lo] * (1 - frac) + SIM_POLAR[w][a_hi] * frac

    if lo == hi:
        return at(lo)
    frac = (tws - lo) / (hi - lo)
    return at(lo) * (1 - frac) + at(hi) * frac


def sim_lines(offset_deg=4.0, speed=1.0):
    """Synthetic NMEA from a boat sailing changing legs in changing wind.

    Injects a masthead offset so polar_build.py's calibration check has
    something real to find.
    """
    t = 0
    twd = 210.0
    tws = 11.0
    twa = 60.0
    tack = 1
    lat, lon = 44.10, -68.86
    while True:
        if t % 900 == 0:                      # new leg every 15 sim-minutes
            twa = random.choice([45, 52, 60, 75, 90, 110, 130, 150, 170])
            tack = random.choice([1, -1])
        twd += random.gauss(0, 0.25)
        tws = max(3.0, min(28.0, tws + random.gauss(0, 0.05)))
        gust = tws + random.gauss(0, 0.4)
        stw = max(0.0, sim_speed(gust, twa) + random.gauss(0, 0.12))
        signed_twa = twa * tack
        hdg = norm360(twd - signed_twa)

        # true -> apparent, in the boat frame
        tw_x = gust * math.cos(math.radians(signed_twa))
        tw_y = gust * math.sin(math.radians(signed_twa))
        aw_x, aw_y = tw_x + stw, tw_y
        aws = math.hypot(aw_x, aw_y)
        awa = math.degrees(math.atan2(aw_y, aw_x)) + offset_deg

        yield with_checksum("WIMWV,{:.1f},R,{:.1f},N,A".format(norm360(awa), aws))
        yield with_checksum("VWVHW,{:.1f},T,,M,{:.2f},N,,K".format(hdg, stw))
        yield with_checksum("GPVTG,{:.1f},T,,M,{:.2f},N,,K".format(hdg, stw + 0.4))
        yield with_checksum("SDDPT,{:.1f},0.0".format(random.uniform(12, 60)))
        yield with_checksum("GPRMC,{:%H%M%S},A,{:.4f},N,{:.4f},W,{:.2f},{:.1f},{:%d%m%y},,".format(
            datetime.now(timezone.utc), abs(lat) * 100 % 10000, abs(lon) * 100 % 10000,
            stw + 0.4, hdg, datetime.now(timezone.utc)))
        yield None                            # end of one simulated second
        t += 1
        if speed > 0:
            time.sleep(1.0 / speed)


def open_stream(args):
    """Yield lines from whichever source is configured, reconnecting forever."""
    if args.simulate:
        for line in sim_lines(offset_deg=args.sim_offset, speed=args.sim_speed):
            yield line
        return

    targets = []
    if args.kind == "auto":
        targets = list(AUTO_TARGETS)
        if args.host:
            targets.insert(0, ("tcp", args.host, args.port or 39150))
    elif args.kind == "serial":
        targets = [("serial", args.dev, args.baud)]
    else:
        targets = [(args.kind, args.host, args.port)]

    while True:
        for kind, host, port in targets:
            try:
                if kind == "tcp":
                    print("[src] tcp {}:{}".format(host, port), file=sys.stderr)
                    for line in tcp_lines(host, port):
                        yield line
                elif kind == "udp":
                    print("[src] udp :{}".format(port), file=sys.stderr)
                    for line in udp_lines(host, port):
                        yield line
                elif kind == "serial":
                    print("[src] serial {} @{}".format(host, port), file=sys.stderr)
                    for line in serial_lines(host, port):
                        yield line
            except (OSError, socket.timeout) as exc:
                print("[src] {} {}:{} -> {}".format(kind, host, port, exc), file=sys.stderr)
                continue
        if args.kind != "auto" and not args.retry:
            return
        time.sleep(5)


# ---------------------------------------------------------------------------
# Boat state from the frame's buttons
# ---------------------------------------------------------------------------

_state_cache = {"checked": -1e9, "state": "sailing"}


def boat_state():
    """'sailing', 'motoring' or 'anchored'. Re-read at most every 2 s."""
    if not USE_BOAT_STATE:
        return "sailing"
    now = time.monotonic()
    if now - _state_cache["checked"] < 2.0:
        return _state_cache["state"]
    _state_cache["checked"] = now
    best, best_since = None, -1.0
    for path in BOAT_STATE_FILES:
        try:
            with open(path) as fh:
                st = json.load(fh)
            since = float(st.get("since") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if st.get("state") in BOAT_STATES and since > best_since:
            best, best_since = st["state"], since
    best = best or "sailing"
    if best != _state_cache["state"]:
        print("[state] now {}".format(best), file=sys.stderr)
    _state_cache["state"] = best
    return best


# ---------------------------------------------------------------------------
# Trip distance, shown under SOG on the e-ink dashboard (frame.py reads it)
# ---------------------------------------------------------------------------
# A new trip starts when the boat leaves anchor: the state goes from Anchored
# to Sailing or Motoring. Time at anchor adds nothing.
#
# The distance is a breadcrumb trail. The next point is taken only once the
# boat is TRIP_STEP_M from the last one, and each step adds the straight line
# between them - so GPS wander while stopped adds nothing, and tacks are
# followed closely. A step made slower than TRIP_MIN_KN (swinging at anchor
# with the state left on Sailing) moves the trail without adding to it; one
# faster than TRIP_MAX_KN is a GPS glitch and is dropped the same way. After
# the Vesper has been off while the boat moved, the straight line from the
# last point is added, as long as it's at a believable speed.
TRIP_FILE = "/home/harvestmoon/polar/trip.json"
TRIP_STEP_M = 25.0
TRIP_MIN_KN = 0.5
TRIP_MAX_KN = 20.0
TRIP_SAVE_S = 60.0          # the frame redraws every 5 min; no need to write more often
M_PER_NM = 1852.0


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


class Trip:
    def __init__(self, path=None):
        self.path = path or TRIP_FILE
        self.nm, self.started = 0.0, time.time()
        self.last, self.last_t, self.prev_state = None, None, None
        self.saved, self.dirty = 0.0, True
        try:
            with open(self.path) as fh:
                d = json.load(fh)
            self.nm = float(d.get("nm") or 0.0)
            self.started = float(d.get("started") or self.started)
            if d.get("last") and d.get("last_t"):
                self.last, self.last_t = (float(d["last"][0]), float(d["last"][1])), float(d["last_t"])
            self.prev_state = d.get("state")
            self.dirty = False
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            pass

    def save(self, force=False):
        now = time.time()
        if not self.dirty or (not force and now - self.saved < TRIP_SAVE_S):
            return
        d = {"nm": round(self.nm, 3), "started": self.started, "updated": now,
             "state": self.prev_state,
             "last": list(self.last) if self.last else None, "last_t": self.last_t}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, self.path)          # never a half-written file for the frame
        self.saved, self.dirty = now, False

    def update(self, lat, lon, state):
        now = time.time()
        if state != self.prev_state:
            if self.prev_state == "anchored" and state in ("sailing", "motoring"):
                print("[trip] left anchor - new trip (last one {:.1f} nm)".format(self.nm), file=sys.stderr)
                self.nm, self.started, self.last = 0.0, now, None
                self.prev_state = state
                self.dirty = True
                self.save(force=True)
            self.prev_state, self.dirty = state, True
        if state == "anchored":
            if self.last is not None:
                self.last, self.dirty = None, True
            self.save()
            return
        if lat is None or lon is None:
            return
        if self.last is None:
            self.last, self.last_t, self.dirty = (lat, lon), now, True
            self.save()
            return
        d = haversine_m(self.last[0], self.last[1], lat, lon)
        if d < TRIP_STEP_M:
            self.save()
            return
        kn = d / max(1.0, now - (self.last_t or now)) * 3600.0 / M_PER_NM
        if TRIP_MIN_KN <= kn <= TRIP_MAX_KN:
            self.nm += d / M_PER_NM
        self.last, self.last_t, self.dirty = (lat, lon), now, True
        self.save()


def csv_path_for(log_dir, day):
    """Today's CSV, or a new part file (polar_<day>_2.csv ...) if today's file
    was started with a different column list (e.g. before the state column)."""
    n = 1
    while True:
        name = "polar_{}.csv".format(day) if n == 1 else "polar_{}_{}.csv".format(day, n)
        path = os.path.join(log_dir, name)
        if not os.path.exists(path):
            return path
        try:
            with open(path, newline="") as fh:
                header = next(csv.reader(fh), [])
        except OSError:
            header = []
        if header == CSV_FIELDS:
            return path
        n += 1


# ---------------------------------------------------------------------------
# Derived values
# ---------------------------------------------------------------------------

def true_wind(awa, aws, stw):
    """Apparent wind + speed through water -> true wind, boat frame.

    Ignores leeway and heel. Both matter; neither is available here, and the
    error is small compared with the ones we are trying to remove.
    """
    a = math.radians(awa)
    tw_x = aws * math.cos(a) - stw
    tw_y = aws * math.sin(a)
    tws = math.hypot(tw_x, tw_y)
    twa = math.degrees(math.atan2(tw_y, tw_x))
    return norm180(twa), tws


def sample(st, stamp=None):
    """One row of state, or None if the essentials are missing."""
    awa_raw = st.get("awa_raw")
    aws = st.get("aws")
    stw = st.get("stw")
    if awa_raw is None or aws is None or stw is None:
        return None
    awa = norm180(awa_raw - AWA_OFFSET_DEG)
    twa, tws = true_wind(awa, aws, stw)
    hdg = st.get("hdg_true")
    if hdg is None:
        mag = st.get("hdg_mag")
        hdg = mag                              # variation not applied; noted in README
    twd = norm360(hdg + twa) if hdg is not None else None
    rpm = st.get("rpm")
    state = boat_state()
    if state == "sailing" and (
            (rpm is not None and rpm > MOTORING_RPM) or os.path.exists(MOTORING_FLAG)):
        state = "motoring"

    def r(x, n=2):
        return None if x is None else round(x, n)

    return {
        "utc": (stamp or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "lat": r(st.get("lat", 10), 5), "lon": r(st.get("lon", 10), 5),
        "sog": r(st.get("sog")), "cog": r(st.get("cog"), 1),
        "hdg_true": r(hdg, 1),
        "stw": r(stw), "awa": r(awa, 1), "aws": r(aws),
        "twa": r(twa, 1), "tws": r(tws), "twd": r(twd, 1),
        "heel": r(st.get("heel"), 1), "depth": r(st.get("depth"), 1),
        "water_temp": r(st.get("water_temp"), 1), "rpm": r(rpm, 0),
        "motoring": int(state == "motoring"),
        "state": state,
    }


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

NEEDED = [
    ("apparent wind angle", "awa_raw", True),
    ("apparent wind speed", "aws", True),
    ("speed through water", "stw", True),
    ("heading", "hdg_true", False),
    ("position", "lat", False),
    ("SOG", "sog", False),
    ("depth", "depth", False),
    ("engine RPM", "rpm", False),
    ("heel", "heel", False),
]


def run_probe(stream, seconds):
    st = State()
    start = time.monotonic()
    total = 0
    for line in stream:
        if line is None:
            continue
        if parse(line, st):
            total += 1
        if time.monotonic() - start > seconds:
            break

    print("\nListened {:.0f}s, {} valid sentences.\n".format(
        time.monotonic() - start, total))
    if not total:
        print("Nothing on the wire. Try --kind tcp --host <ip> --port <port>,")
        print("or --kind udp --port 2000, or --kind serial --dev /dev/ttyUSB0.")
        return 1

    print("Sentences seen:")
    for typ, n in sorted(st.sentences.items(), key=lambda kv: -kv[1]):
        print("   {:<5} {:>6}".format(typ, n))

    print("\nFields:")
    missing_required = []
    for label, key, required in NEEDED:
        ok = st.seen(key)
        mark = "ok " if ok else ("MISSING" if required else "--")
        print("   {:<22} {}".format(label, mark))
        if required and not ok:
            missing_required.append(label)

    print()
    if missing_required:
        print("Not ready. Missing: {}.".format(", ".join(missing_required)))
        if "speed through water" in missing_required:
            print("STW comes from the DST810 as VHW or VBW. If wind is present")
            print("and STW isn't, the two aren't bridged into this stream yet.")
            print("Logging without it records current, not boat speed.")
        return 2

    print("Ready. All three required fields are present.")
    row = sample(st)
    if row:
        print("Sample: TWA {}  TWS {}  STW {}".format(
            row["twa"], row["tws"], row["stw"]))
    return 0


def run_logger(stream, args):
    log_dir = os.path.expanduser(args.log_dir)
    os.makedirs(log_dir, exist_ok=True)
    st = State()
    handle = None
    writer = None
    current_day = None
    last_tick = 0.0
    rows = 0
    tick = 1.0 / max(args.rate, 0.01)
    # In simulate mode the clock ticks in simulated seconds, so --sim-speed
    # changes wall-clock pace only, not the logging rate.

    sim_seconds = 0
    base = datetime.now(timezone.utc)
    trip = None if args.simulate else Trip()     # fake data mustn't touch the real trip
    trip_err = [False]

    print("Logging to {} (Ctrl-C to stop)".format(log_dir), file=sys.stderr)
    try:
        for line in stream:
            if line is None:                      # simulated second boundary
                sim_seconds += 1
                now = sim_seconds
            else:
                parse(line, st)
                if args.simulate:
                    continue
                now = time.monotonic()
            if now - last_tick < tick:
                continue
            last_tick = now
            stamp = base + timedelta(seconds=sim_seconds) if args.simulate else None
            if trip is not None:
                try:
                    trip.update(st.get("lat", 10), st.get("lon", 10), boat_state())
                except Exception as exc:         # the trip must never stop the polar log
                    if not trip_err[0]:
                        print("[trip] error: {}".format(exc), file=sys.stderr)
                        trip_err[0] = True
            row = sample(st, stamp)
            if row is None:
                continue
            if row["state"] == "anchored":      # anchored time stays out entirely
                continue
            day = row["utc"][:10]
            if day != current_day:
                if handle:
                    handle.close()
                path = csv_path_for(log_dir, day)
                fresh = not os.path.exists(path) or os.path.getsize(path) == 0
                handle = open(path, "a", newline="")
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                if fresh:
                    writer.writeheader()
                current_day = day
            writer.writerow(row)
            rows += 1
            if rows % 60 == 0:
                handle.flush()
                if args.verbose:
                    print("  {}  TWA {:>6}  TWS {:>5}  STW {:>5}{}".format(
                        row["utc"], row["twa"], row["tws"], row["stw"],
                        "  [motoring]" if row["motoring"] else ""), file=sys.stderr)
            if args.max_rows and rows >= args.max_rows:
                break
    except KeyboardInterrupt:
        pass
    finally:
        if handle:
            handle.close()
        if trip is not None:
            try:
                trip.save(force=True)
            except Exception:
                pass
    print("\n{} rows written.".format(rows), file=sys.stderr)
    return 0


def main():
    p = argparse.ArgumentParser(description="Harvest Moon polar logger")
    p.add_argument("--probe", action="store_true",
                   help="listen briefly and report what's available")
    p.add_argument("--probe-seconds", type=float, default=30)
    p.add_argument("--kind", choices=["auto", "tcp", "udp", "serial"], default="auto")
    p.add_argument("--host", default="")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--dev", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=4800)
    p.add_argument("--log-dir", default="~/polar_logs")
    p.add_argument("--rate", type=float, default=1.0, help="rows per second")
    p.add_argument("--retry", action="store_true", default=True)
    p.add_argument("--simulate", action="store_true", help="synthetic data")
    p.add_argument("--sim-speed", type=float, default=1.0,
                   help="simulation speed multiplier, 0 = as fast as possible")
    p.add_argument("--sim-offset", type=float, default=4.0,
                   help="masthead error to inject, degrees")
    p.add_argument("--max-rows", type=int, default=0)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    global USE_BOAT_STATE
    if args.simulate:
        USE_BOAT_STATE = False       # fake data shouldn't follow the real buttons
    stream = open_stream(args)
    if args.probe:
        return run_probe(stream, args.probe_seconds)
    return run_logger(stream, args)


if __name__ == "__main__":
    sys.exit(main())
