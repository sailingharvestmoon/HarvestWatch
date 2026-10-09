#!/usr/bin/env python3
"""
Harvest Moon - Deck camera snapshot (Reolink Argus PT Ultra -> Firebase)
------------------------------------------------------------------------
At the SNAP_TIMES (default 30 min after sunrise and 30 min before sunset,
worked out each day from the boat's position) this asks the camera for a
still through Neolink and saves it to camera/harvest-moon. The Wix camera widget shows
whatever is there.

Full quality: the camera's own 4K JPEG is uploaded untouched whenever it fits
in one Firestore document (1 MiB limit). Busy scenes can come out bigger; only
then is it re-saved, still at full size, at the best JPEG quality that fits,
and scaled down only if even that is too big.

The camera is a battery/solar camera with no RTSP. Neolink talks Reolink's own
protocol to it over the boat Wi-Fi. Each Neolink command wakes the camera for a
few seconds, so this program only touches the camera at the scheduled times.

Two ways to run it:
  python3 camera.py           the service: sleeps, takes a photo at SNAP_TIMES
  python3 camera.py --once    take one photo right now and upload it (testing)

"Take a photo now" from the Harvest Watch app: the app writes requestAt into
camera/<vessel>; the service looks every REQ_POLL_SEC, takes the photo, and
writes requestDoneAt (and requestNote if it has to wait or refuse). To spare
the camera battery - anyone who can reach Firestore can write requestAt - it
takes at most one photo every REQ_GAP_MIN minutes and REQ_PER_DAY a day on
request, and ignores requests older than REQ_MAX_AGE_MIN.

"See live view" (Wix widget): the viewer writes liveRequestAt (and keeps
writing liveWatchAt every 30 s while watching) into camera/<vessel>. The
service starts Neolink in its MQTT mode, which asks the camera for a still
every LIVE_EVERY_MS over one open connection and hands each one to Mosquitto
on the Pi. This program shrinks each still to LIVE_W px and writes it into
camera/<vessel>-live, which the widget shows as it changes. A session ends
after LIVE_MIN minutes, or LIVE_IDLE_SEC after the last viewer stops watching;
LIVE_DAY_MIN caps the camera's awake time per day. Each live picture is a
full-size still from the camera, so when a session ends the last one is saved,
at full quality, as the latest photo in camera/<vessel>. While a live session runs,
scheduled and requested photos wait for it to finish.

The service never photographs at start-up, so a code update or a reboot does
not wake the camera. Each photo runs as a separate short-lived process, so the
picture handling (Pillow) only uses memory for the few seconds it runs.

Environment variables (set in camera.service; the defaults match):
  PROJECT_ID    Firebase project            (default harvest-moon-watch)
  VESSEL_ID     document id                 (default harvest-moon)
  SNAP_TIMES    when to shoot, comma-separated: sunrise+30, sunset-30, or a
                clock time like 15:00 (default sunrise+30,sunset-30)
  CAM_TZ        time zone for clock times and the log (default America/New_York)
  CAM_LAT, CAM_LON  position for sunrise/sunset only if the Pi has never seen
                one in vessels/<vessel> (default Coney Island, 40.574, -73.986)
  CAM_NAME      camera name in neolink.toml (default HarvestMoon)
  CAM_PRESET    PTZ preset id to move to first; blank = don't move (default blank)
  NEOLINK       path to the Neolink program
  NEOLINK_CONF  path to the Neolink config  (default /etc/harvest-moon/neolink.toml)
  MAX_W         force a smaller photo, longest side in px; 0 = full size (default 0)
  RETRY_MIN     if a scheduled photo fails, try once more this many minutes later (default 10)
  REQ_POLL_SEC  how often to look for a "take a photo now" request; 0 = never (default 30)
  REQ_GAP_MIN   at most one photo every this many minutes, on request (default 10)
  REQ_PER_DAY   at most this many photos a day on request (default 12)
  REQ_MAX_AGE_MIN  ignore requests older than this, e.g. made while the Pi was off (default 30)
  LIVE_MIN      longest live session, minutes (default 5)
  LIVE_DAY_MIN  most live minutes per day, to spare the camera battery (default 30)
  LIVE_EVERY_MS a new live picture this often (default 2000)
  LIVE_W        live pictures are shrunk to this many px wide (default 1280)
  LIVE_IDLE_SEC end the session this long after the last viewer stopped watching (default 75)

Neolink config with the camera password: /etc/harvest-moon/neolink.toml
(chmod 600, owned by harvestmoon). Never in this file or the unit file.

Standard library, plus Pillow (python3-pil) for the photos, and for live view
Mosquitto (mosquitto + mosquitto-clients, listening on 127.0.0.1 only).
"""

import os, sys, re, io, json, math, time, base64, subprocess, threading
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Firebase sign-in for writes (shared helper: ~/lib/fsauth.py, login file
# /etc/harvest-moon/firebase.env). If either is missing, writes go out
# unsigned - exactly as before the database rules were locked.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
try:
    import fsauth
    def fs_auth():
        return fsauth.headers()
except Exception:
    def fs_auth():
        return {}

PROJECT_ID   = os.environ.get("PROJECT_ID", "harvest-moon-watch").strip()
VESSEL_ID    = os.environ.get("VESSEL_ID", "harvest-moon").strip()
SNAP_TIMES   = os.environ.get("SNAP_TIMES", "sunrise+30,sunset-30").strip()
CAM_TZ       = os.environ.get("CAM_TZ", "America/New_York").strip()
CAM_NAME     = os.environ.get("CAM_NAME", "HarvestMoon").strip()
CAM_PRESET   = os.environ.get("CAM_PRESET", "").strip()
NEOLINK      = os.environ.get("NEOLINK", "/home/harvestmoon/neolink/neolink_linux_armhf/neolink").strip()
NEOLINK_CONF = os.environ.get("NEOLINK_CONF", "/etc/harvest-moon/neolink.toml").strip()
MAX_W        = int(os.environ.get("MAX_W", "0"))
RETRY_MIN    = int(os.environ.get("RETRY_MIN", "10"))
REQ_POLL_SEC = int(os.environ.get("REQ_POLL_SEC", "30"))
CAM_LAT      = float(os.environ.get("CAM_LAT", "40.574"))
CAM_LON      = float(os.environ.get("CAM_LON", "-73.986"))
POS_CACHE    = os.path.expanduser("~/.cache/hm-camera-position.json")
REQ_GAP_MIN  = int(os.environ.get("REQ_GAP_MIN", "10"))
REQ_PER_DAY  = int(os.environ.get("REQ_PER_DAY", "12"))
REQ_MAX_AGE_MIN = int(os.environ.get("REQ_MAX_AGE_MIN", "30"))
LIVE_MIN     = int(os.environ.get("LIVE_MIN", "5"))
LIVE_DAY_MIN = int(os.environ.get("LIVE_DAY_MIN", "30"))
LIVE_EVERY_MS = int(os.environ.get("LIVE_EVERY_MS", "2000"))
LIVE_W       = int(os.environ.get("LIVE_W", "1280"))
LIVE_IDLE_SEC = int(os.environ.get("LIVE_IDLE_SEC", "75"))
MOSQ_SUB     = os.environ.get("MOSQ_SUB", "/usr/bin/mosquitto_sub").strip()
MOSQ_PUB     = os.environ.get("MOSQ_PUB", "/usr/bin/mosquitto_pub").strip()

WORK_DIR     = f"/tmp/hm-camera-{os.getuid()}"   # per user, so a sudo test run can't block the service
MAX_BYTES    = 1_040_000        # Firestore's limit is 1,048,576 bytes per document; room for the other fields
NEOLINK_SEC  = 120              # give up on any one Neolink command after this
BATTERY_SEC  = int(os.environ.get("BATTERY_SEC", "20"))   # the battery reading is a nice-to-have:
                                # never let it hold up a photo or a waiting live-view request
ONCE_SEC     = 420              # give up on a whole photo run after this

DOC_URL = (f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}"
           f"/databases/(default)/documents/camera/{VESSEL_ID}")
LIVE_DOC_URL = DOC_URL + "-live"       # camera/<vessel>-live: the current live picture
BATTERY_KEYS = ("batteryPct", "chargeStatus", "adapterStatus")
LIVE_START_SEC = 75     # give up if the camera hasn't sent a live picture by then
LIVE_STALL_SEC = 25     # ... or if it stops sending for this long
LIVE_REQ_MAX_AGE = 180  # a live request older than this (s) means the viewer has gone


def log(msg):
    print(msg, flush=True)


def now_ms():
    return int(time.time() * 1000)


# ---- Firestore -------------------------------------------------------------
def fs_value(v):
    if isinstance(v, bool):  return {"booleanValue": v}
    if isinstance(v, int):   return {"integerValue": str(v)}
    if isinstance(v, float): return {"doubleValue": v}
    if isinstance(v, bytes): return {"bytesValue": base64.b64encode(v).decode("ascii")}
    if v is None:            return {"nullValue": None}
    return {"stringValue": str(v)}


def fs_patch(values, only_these=False, mask=None, url=None):
    """
    Write fields to camera/<vessel>. With only_these=True (or a mask) every
    other field is left alone - the last good photo, the app's requestAt. A
    field named in the mask but missing from values is deleted. Without
    either, the whole document is replaced. Returns True on success. Never raises.
    """
    url = url or DOC_URL
    if only_these and mask is None:
        mask = list(values)
    if mask is not None:
        url += "?" + "&".join("updateMask.fieldPaths=" + urllib.parse.quote(k) for k in mask)
    body = json.dumps({"fields": {k: fs_value(v) for k, v in values.items()}}).encode()
    req = urllib.request.Request(url, data=body, method="PATCH",
                                 headers={"Content-Type": "application/json", **fs_auth()})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if "seq" not in values:                         # don't log every live picture
                where = url.split("/documents/", 1)[1].split("?", 1)[0]
                log(f"firestore [{r.status}] {where}: {', '.join(values)}")
            return True
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        log(f"firestore write failed: HTTP {e.code} {detail}")
    except Exception as e:
        log(f"firestore write failed: {e}")
    return False


def fs_get(paths):
    """
    Read a few small fields of camera/<vessel> (never the photo). Returns a
    dict of plain values, {} if the document doesn't exist yet, None on error.
    """
    url = DOC_URL + "?" + "&".join("mask.fieldPaths=" + urllib.parse.quote(p) for p in paths)
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            f = json.load(r).get("fields", {})
    except urllib.error.HTTPError as e:
        return {} if e.code == 404 else None
    except Exception:
        return None
    out = {}
    for k, v in f.items():
        if "integerValue" in v:  out[k] = int(v["integerValue"])
        elif "doubleValue" in v: out[k] = float(v["doubleValue"])
        elif "stringValue" in v: out[k] = v["stringValue"]
    return out


# ---- Neolink ---------------------------------------------------------------
def neolink(*args, timeout=None):
    """Run one Neolink command. Returns (ok, stdout). Never raises."""
    cmd = [NEOLINK] + list(args)
    limit = timeout or NEOLINK_SEC
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
    except subprocess.TimeoutExpired:
        log(f"neolink {args[0]} timed out after {limit} s")
        return False, ""
    except Exception as e:
        log(f"neolink {args[0]} could not run: {e}")
        return False, ""
    if p.returncode != 0:
        tail = " | ".join(p.stderr.strip().splitlines()[-3:])
        log(f"neolink {args[0]} exit {p.returncode}: {tail}")
        return False, p.stdout
    return True, p.stdout


def move_to_preset():
    if not CAM_PRESET:
        return None
    ok, _ = neolink("ptz", f"--config={NEOLINK_CONF}", CAM_NAME, "preset", CAM_PRESET)
    if ok:
        log(f"moved to preset {CAM_PRESET}; waiting for the camera to settle")
        time.sleep(8)
        return int(CAM_PRESET) if CAM_PRESET.isdigit() else CAM_PRESET
    log(f"could not move to preset {CAM_PRESET}; taking the photo where it points")
    return None


def grab_still():
    """Returns the camera's JPEG bytes, or raises RuntimeError with a short reason."""
    os.makedirs(WORK_DIR, exist_ok=True)
    path = os.path.join(WORK_DIR, "snap.jpeg")   # Neolink always writes .jpeg
    if os.path.exists(path):
        os.remove(path)
    ok, _ = neolink("image", f"--config={NEOLINK_CONF}", f"--file-path={path}", CAM_NAME)
    if not os.path.exists(path) or os.path.getsize(path) < 1000:
        raise RuntimeError("camera did not return a photo" + ("" if ok else " (Neolink error)"))
    with open(path, "rb") as f:
        data = f.read()
    os.remove(path)
    return data


def read_battery():
    """Battery percent and charge state from the camera's XML, or {}. Never raises."""
    ok, out = neolink("battery", f"--config={NEOLINK_CONF}", CAM_NAME, timeout=BATTERY_SEC)
    info = {}
    m = re.search(r"<batteryPercent>\s*(\d+)\s*<", out or "")
    if m:
        info["batteryPct"] = int(m.group(1))
    m = re.search(r"<chargeStatus>\s*([^<]+?)\s*<", out or "")
    if m:
        info["chargeStatus"] = m.group(1)
    m = re.search(r"<adapterStatus>\s*([^<]+?)\s*<", out or "")
    if m:
        info["adapterStatus"] = m.group(1)
    if not info:
        log("battery: no reading" + ("" if ok else " (Neolink error)"))
    return info


def prepare(jpeg):
    """
    The camera's JPEG byte for byte when it fits under MAX_BYTES. Otherwise
    re-save at full size at the highest quality that fits, then at 2880 and
    1920 px as a last resort. MAX_W > 0 forces that size instead.
    Returns (bytes, (w, h), (camera w, camera h), quality or "original").
    """
    from PIL import Image                      # only loaded in the --once process
    im = Image.open(io.BytesIO(jpeg))          # reads the header only
    src = im.size
    if MAX_W <= 0 and len(jpeg) <= MAX_BYTES:
        return jpeg, src, src, "original"
    im.load()
    if im.mode != "RGB":
        im = im.convert("RGB")
    sizes = [MAX_W] if MAX_W > 0 else [max(src), 2880, 1920]
    out = b""
    for side in sizes:
        if side < max(im.size):
            im.thumbnail((side, side), Image.LANCZOS)   # sizes only ever shrink
        for q in (92, 86, 80, 72, 64):
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
            out = buf.getvalue()
            if len(out) <= MAX_BYTES:
                return out, im.size, src, q
    raise RuntimeError(f"photo still {len(out)//1024} KB after re-saving - set MAX_W")


# ---- one photo ---------------------------------------------------------------
def take_one():
    """Take and upload one photo. Returns True on success."""
    t0 = time.time()
    tried = now_ms()
    log(f"photo run: camera {CAM_NAME}, preset {CAM_PRESET or 'none'}")
    try:
        preset = move_to_preset()
        raw = grab_still()
        taken = now_ms()
        batt = read_battery()
        jpeg, size, src, q = prepare(raw)
    except Exception as e:
        reason = str(e) or e.__class__.__name__
        log(f"photo run FAILED: {reason}")
        fs_patch({"lastTryAt": tried, "lastError": reason[:300]}, only_these=True)
        return False

    if q == "original":
        log(f"photo: {src[0]}x{src[1]} {len(raw)//1024} KB, uploading the camera's original")
    else:
        why = f"MAX_W={MAX_W}" if MAX_W > 0 else "too big for one record"
        log(f"photo: camera {src[0]}x{src[1]} {len(raw)//1024} KB {why} -> "
            f"{size[0]}x{size[1]} {len(jpeg)//1024} KB (JPEG quality {q})")
    fields = {
        "image": jpeg,
        "mime": "image/jpeg",
        "takenAt": taken,
        "lastTryAt": tried,
        "lastError": "",
        "width": size[0],
        "height": size[1],
        "sizeKB": round(len(jpeg) / 1024),
        "cameraWidth": src[0],
        "cameraHeight": src[1],
        "quality": q,
        "preset": preset,
        "camera": CAM_NAME,
        "source": "pi camera.py via neolink",
    }
    fields.update(batt)
    # Masked write: keeps the app's requestAt/requestDoneAt; a battery field
    # we couldn't read this time is cleared rather than left stale.
    ok = fs_patch(fields, mask=list(fields) + [k for k in BATTERY_KEYS if k not in fields])
    log(f"photo run {'done' if ok else 'took the photo but the upload FAILED'} "
        f"in {time.time() - t0:.0f} s")
    return ok


# ---- live view ---------------------------------------------------------------
def live_config():
    """
    A private copy of neolink.toml with the MQTT preview switched on, written
    to WORK_DIR (mode 600) for this session and deleted after. Keeps the
    camera password in one place.
    """
    with open(NEOLINK_CONF) as f:
        text = f.read()
    headers = re.findall(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*$", text, re.M)
    if not headers or headers[-1] != "cameras":
        raise RuntimeError(f"{NEOLINK_CONF} must end with its [[cameras]] section for live view")
    if re.search(r"^\s*\[mqtt\]", text, re.M):
        raise RuntimeError(f"{NEOLINK_CONF} already has an [mqtt] section - remove it for live view")
    extra = "\n"
    if not re.search(r"^\s*push_notifications\s*=", text, re.M):
        extra += "push_notifications = false\n"   # retries every 5 s otherwise; the service is gone
    extra += ("[cameras.mqtt]\nenable_motion = false\nenable_light = false\n"
              "enable_battery = false\nenable_floodlight = false\nenable_preview = true\n"
              f"preview_update = {LIVE_EVERY_MS}\n\n"
              '[mqtt]\nbroker_addr = "127.0.0.1"\nport = 1883\n')
    os.makedirs(WORK_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(WORK_DIR, "live.toml")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text.rstrip("\n") + "\n" + extra)
    return path


def live_shrink(jpeg):
    """A camera still -> a small JPEG for live view. Decodes at reduced size (fast on the Pi Zero)."""
    from PIL import Image
    im = Image.open(io.BytesIO(jpeg))
    im.draft("RGB", (LIVE_W, LIVE_W))
    im = im.convert("RGB")
    im.thumbnail((LIVE_W, LIVE_W), Image.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=70)
    return buf.getvalue(), im.size


def clear_retained(topic):
    """Drop a picture Mosquitto kept from an earlier session, so nobody sees it as live."""
    try:
        subprocess.run([MOSQ_PUB, "-h", "127.0.0.1", "-t", topic, "-r", "-n"],
                       timeout=10, capture_output=True)
    except Exception:
        pass


def live_session(req, max_sec):
    """
    One live session (runs as 'camera.py --live <requestAt> <max seconds>').
    Returns True if any picture went out.
    """
    t0 = time.time()
    started = now_ms()
    ends = started + int(max_sec * 1000)
    topic = f"neolink/{CAM_NAME}/status/preview"
    latest = {"jpeg": None, "at": 0.0}
    lock = threading.Lock()
    procs, conf, error, reason, sent = [], None, "", "ended", 0
    bad_frames = 0
    last_b64, last_at = None, 0.0     # the newest picture that went out, full size

    def live_doc(fields, mask=None):
        return fs_patch(fields, mask=mask, url=LIVE_DOC_URL)

    log(f"live view: starting, up to {max_sec / 60:.1f} min")
    try:
        conf = live_config()
        clear_retained(topic)
        live_doc({"state": "starting", "startedAt": started, "endsAt": ends, "error": ""},
                 mask=["state", "startedAt", "endsAt", "error"])
        os.makedirs(WORK_DIR, mode=0o700, exist_ok=True)
        neo_log = open(os.path.join(WORK_DIR, "live-neolink.log"), "w")
        neo = subprocess.Popen([NEOLINK, "mqtt", f"--config={conf}"],
                               stdout=subprocess.DEVNULL, stderr=neo_log)
        procs.append(neo)
        # -R: ignore anything Mosquitto kept from before; only pictures sent from now on
        sub = subprocess.Popen([MOSQ_SUB, "-h", "127.0.0.1", "-t", topic, "-R"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        procs.append(sub)

        def reader():
            for line in sub.stdout:
                line = line.strip()
                if line:
                    with lock:
                        latest["jpeg"], latest["at"] = line, time.time()
        threading.Thread(target=reader, daemon=True).start()

        last_sent = 0.0
        next_watch = time.time() + 20
        while True:
            now = time.time()
            if now_ms() >= ends:
                reason = "time limit"
                break
            if neo.poll() is not None:
                raise RuntimeError("Neolink stopped unexpectedly")
            if not sent and now - t0 > LIVE_START_SEC:
                raise RuntimeError("the camera didn't send a picture")
            if sent and now - last_sent > LIVE_STALL_SEC:
                raise RuntimeError("the camera stopped sending pictures")
            if now >= next_watch:                      # is anyone still watching?
                next_watch = now + 10
                st = fs_get(["liveWatchAt"])
                if st is not None and now_ms() - max(st.get("liveWatchAt", 0), req) > LIVE_IDLE_SEC * 1000:
                    reason = "nobody watching"
                    break
            with lock:
                b64, at = latest["jpeg"], latest["at"]
            if b64 and at > last_sent:
                last_sent = at
                try:
                    jpeg, size = live_shrink(base64.b64decode(b64))
                except Exception as e:
                    bad_frames += 1
                    if bad_frames == 1:
                        log(f"live: skipped a picture that wouldn't decode ({e})")
                    continue
                if live_doc({"frame": jpeg, "frameAt": int(at * 1000), "seq": sent + 1,
                             "state": "live", "startedAt": started, "endsAt": ends,
                             "width": size[0], "height": size[1], "error": ""}):
                    sent += 1
                    last_b64, last_at = b64, at
                    if sent == 1:
                        log(f"live: first picture after {at - t0:.0f} s, "
                            f"{size[0]}x{size[1]} {len(jpeg) // 1024} KB")
                continue
            time.sleep(0.2)
    except Exception as e:
        reason, error = "error", (str(e) or e.__class__.__name__)
        log(f"live view FAILED: {error}")
    finally:
        for proc in reversed(procs):
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if conf:
            try:
                os.remove(conf)
            except OSError:
                pass
        clear_retained(topic)
        live_doc({"state": "error" if error else "ended", "endedAt": now_ms(), "error": error[:300]},
                 mask=["state", "endedAt", "error"])
        log(f"live view {reason}: {sent} pictures in {time.time() - t0:.0f} s")
        if last_b64:
            save_live_photo(last_b64, last_at)
    return sent > 0


def save_live_photo(b64, at):
    """
    Save the session's last live picture - a full-size camera still - as the
    latest photo, so the widget and the app don't fall back to an older one.
    Battery fields are left as they were (live view doesn't read them). Never raises.
    """
    try:
        raw = base64.b64decode(b64)
        jpeg, size, src, q = prepare(raw)
    except Exception as e:
        log(f"live: couldn't save the last picture as the photo ({e})")
        return False
    fields = {
        "image": jpeg, "mime": "image/jpeg",
        "takenAt": int(at * 1000), "lastTryAt": int(at * 1000), "lastError": "",
        "width": size[0], "height": size[1], "sizeKB": round(len(jpeg) / 1024),
        "cameraWidth": src[0], "cameraHeight": src[1], "quality": q,
        "preset": None, "camera": CAM_NAME, "source": "live view (last picture)",
    }
    ok = fs_patch(fields, mask=list(fields))
    if ok:
        log(f"live: saved the last picture as the latest photo, {src[0]}x{src[1]} {len(jpeg) // 1024} KB")
    return ok


def run_live_subprocess(req, max_sec):
    """Run 'camera.py --live' as its own process, like the photos."""
    try:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--live", str(req), str(int(max_sec))],
                           timeout=max_sec + 150)
        return p.returncode == 0
    except subprocess.TimeoutExpired:
        log("live session ran too long - stopped")
        fs_patch({"state": "ended", "endedAt": now_ms(), "error": "stopped"},
                 mask=["state", "endedAt", "error"], url=LIVE_DOC_URL)
        return False
    except Exception as e:
        log(f"could not start live session: {e}")
        return False


# ---- when to shoot ------------------------------------------------------------
def sun_times(day, lat, lon):
    """
    Sunrise and sunset (UTC datetimes) for a calendar date at lat/lon (degrees,
    east positive), NOAA solar calculator equations; within about a minute.
    Returns (None, None) when the sun doesn't rise or set that day.
    """
    out = []
    for rising in (True, False):
        t = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc) - timedelta(hours=lon / 15)
        for _ in range(2):                     # second pass uses the first answer's time
            jd = t.timestamp() / 86400 + 2440587.5
            jc = (jd - 2451545) / 36525
            l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
            m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
            e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
            mr = math.radians(m)
            c = (math.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
                 + math.sin(2 * mr) * (0.019993 - 0.000101 * jc) + math.sin(3 * mr) * 0.000289)
            app = l0 + c - 0.00569 - 0.00478 * math.sin(math.radians(125.04 - 1934.136 * jc))
            obl = (23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
                   + 0.00256 * math.cos(math.radians(125.04 - 1934.136 * jc)))
            dec = math.asin(math.sin(math.radians(obl)) * math.sin(math.radians(app)))
            y = math.tan(math.radians(obl / 2)) ** 2
            l0r = math.radians(l0)
            eqt = 4 * math.degrees(y * math.sin(2 * l0r) - 2 * e * math.sin(mr)
                                   + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
                                   - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))
            la = math.radians(lat)
            cos_ha = (math.cos(math.radians(90.833)) / (math.cos(la) * math.cos(dec))
                      - math.tan(la) * math.tan(dec))
            if not -1 <= cos_ha <= 1:
                return None, None
            ha = math.degrees(math.acos(cos_ha))
            minutes = 720 - 4 * (lon + (ha if rising else -ha)) - eqt
            t = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(minutes=minutes)
        out.append(t)
    return out[0], out[1]


def boat_position():
    """
    The boat's last known position from vessels/<vessel> (sensors.py keeps it
    there; it stays put while the Vesper is off). Remembered in POS_CACHE so a
    reboot without internet still knows roughly where we are. Never raises.
    """
    url = (f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}"
           f"/databases/(default)/documents/vessels/{VESSEL_ID}?mask.fieldPaths=lat&mask.fieldPaths=lon")
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            f = json.load(r).get("fields", {})
        lat = float(f["lat"].get("doubleValue", f["lat"].get("integerValue")))
        lon = float(f["lon"].get("doubleValue", f["lon"].get("integerValue")))
        if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0.0, 0.0):
            try:
                os.makedirs(os.path.dirname(POS_CACHE), exist_ok=True)
                with open(POS_CACHE, "w") as fh:
                    json.dump({"lat": lat, "lon": lon, "at": now_ms()}, fh)
            except OSError:
                pass
            return lat, lon, "boat"
    except Exception:
        pass
    try:
        with open(POS_CACHE) as fh:
            d = json.load(fh)
        return float(d["lat"]), float(d["lon"]), "last known"
    except Exception:
        return CAM_LAT, CAM_LON, "default"


def parse_times(spec):
    """'sunrise+30, sunset-30, 15:00' -> [('sunrise', 30), ('sunset', -30), ('clock', 900)]"""
    out = []
    for part in spec.lower().replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(sunrise|sunset)([+-]\d+)?", part)
        if m:
            out.append((m.group(1), int(m.group(2) or 0)))
            continue
        h, _, mi = part.partition(":")
        h, mi = int(h), int(mi or 0)
        if not (0 <= h < 24 and 0 <= mi < 60):
            raise ValueError(f"bad time {part!r}")
        out.append(("clock", h * 60 + mi))
    if not out:
        raise ValueError("SNAP_TIMES is empty")
    return out


def label(tok):
    kind, n = tok
    return f"{n // 60:02d}:{n % 60:02d}" if kind == "clock" else f"{kind}{n:+d} min"


def next_slot(times, tz, after, pos):
    """The first shooting time after 'after': (datetime in tz, label)."""
    lat, lon = pos[0], pos[1]
    found = []
    for days in range(0, 3):
        d = (after + timedelta(days=days)).date()
        rise, set_ = sun_times(d, lat, lon)
        for tok in times:
            kind, n = tok
            if kind == "clock":
                t = datetime(d.year, d.month, d.day, n // 60, n % 60, tzinfo=tz)
            else:
                base = rise if kind == "sunrise" else set_
                if base is None:              # no sunrise/sunset that day (polar) - skip
                    continue
                t = (base + timedelta(minutes=n)).astimezone(tz)
            if t > after:
                found.append((t, label(tok)))
        if found:
            return min(found)
    raise RuntimeError("no next time found")


def run_once_subprocess():
    """Run 'camera.py --once' as its own process so Pillow's memory is freed after."""
    try:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--once"],
                           timeout=ONCE_SEC)
        return p.returncode == 0
    except subprocess.TimeoutExpired:
        log(f"photo run took longer than {ONCE_SEC} s - stopped")
        fs_patch({"lastTryAt": now_ms(), "lastError": "photo run timed out"}, only_these=True)
        return False
    except Exception as e:
        log(f"could not start photo run: {e}")
        return False


class Requests:
    """
    "Take a photo now" from the app. poll() reads requestAt / requestDoneAt /
    lastTryAt (a small read, every REQ_POLL_SEC). ready_at() says when the
    pending request may be served; done() records it as handled.
    """
    def __init__(self, tz):
        self.tz = tz
        self.pending = None          # requestAt (ms) not yet served
        self.last_try = 0            # ms, last photo attempt of any kind
        self.waiting_note = None     # request we already told "waiting until ..."
        self.day, self.count = None, 0
        self.failing = False
        self.live_pending = None     # liveRequestAt (ms) not yet answered
        self.live_day, self.live_used = None, 0.0   # seconds of live view today

    def poll(self):
        st = fs_get(["requestAt", "requestDoneAt", "lastTryAt", "liveRequestAt", "liveDoneAt"])
        if st is None:
            if not self.failing:
                log("request check: can't reach Firestore - will keep trying")
            self.failing = True
            return
        if self.failing:
            log("request check: Firestore reachable again")
        self.failing = False
        # min(): a lastTryAt in the future (a clock that was wrong) mustn't block requests
        self.last_try = min(max(self.last_try, st.get("lastTryAt", 0)), now_ms())
        lreq, ldone = st.get("liveRequestAt", 0), st.get("liveDoneAt", 0)
        if lreq and lreq > ldone and lreq != self.live_pending:
            if now_ms() - lreq > LIVE_REQ_MAX_AGE * 1000:
                self.live_done(lreq, "")             # the viewer has long gone; nothing to say
            else:
                log("live view requested")
                self.live_pending = lreq
        req, done = st.get("requestAt", 0), st.get("requestDoneAt", 0)
        if not req or req <= done or req == self.pending:
            return
        age_min = (now_ms() - req) / 60000
        if age_min > REQ_MAX_AGE_MIN:
            log(f"request from {age_min:.0f} min ago is too old - ignored")
            self.done(req, f"Ignored: the request was more than {REQ_MAX_AGE_MIN} min old "
                           "when the Pi saw it.")
            return
        log("photo requested from the app")
        self.pending = req

    def ready_at(self):
        """Epoch seconds when the pending request may run, or None if none/refused."""
        if not self.pending:
            return None
        today = datetime.now(self.tz).date()
        if self.day != today:
            self.day, self.count = today, 0
        if self.count >= REQ_PER_DAY:
            log(f"request refused: already {self.count} on-request photos today")
            self.done(self.pending, f"Not taken: the limit is {REQ_PER_DAY} photos a day on "
                                    "request, to save the camera battery.")
            return None
        at = self.last_try / 1000 + REQ_GAP_MIN * 60
        if at > time.time() and self.waiting_note != self.pending:
            self.waiting_note = self.pending
            when = datetime.fromtimestamp(at, self.tz)
            log(f"request waiting until {when:%H:%M} (one photo per {REQ_GAP_MIN} min)")
            fs_patch({"requestNote": f"Waiting until {when:%-I:%M %p} - the camera takes at "
                                     f"most one photo every {REQ_GAP_MIN} minutes."},
                     only_these=True)
        return at

    def live_left(self):
        """Seconds of live view still allowed today."""
        today = datetime.now(self.tz).date()
        if self.live_day != today:
            self.live_day, self.live_used = today, 0.0
        return max(0.0, LIVE_DAY_MIN * 60 - self.live_used)

    def live_done(self, req, note):
        fs_patch({"liveDoneAt": req, "liveNote": note}, only_these=True)
        if self.live_pending == req:
            self.live_pending = None

    def done(self, req, note=""):
        fs_patch({"requestDoneAt": req, "requestNote": note}, only_these=True)
        if self.pending == req:
            self.pending = None


def service():
    tz = ZoneInfo(CAM_TZ)
    times = parse_times(SNAP_TIMES)
    if not os.path.exists(NEOLINK):
        log(f"WARNING: Neolink not found at {NEOLINK} - photos will fail")
    if not os.access(NEOLINK_CONF, os.R_OK):
        log(f"WARNING: cannot read {NEOLINK_CONF} - photos will fail")
    log(f"camera service up: photos at {', '.join(label(t) for t in times)} "
        f"({CAM_TZ}), camera {CAM_NAME}, preset {CAM_PRESET or 'none'}; "
        + (f"app requests checked every {REQ_POLL_SEC} s" if REQ_POLL_SEC > 0 else "app requests off"))

    reqs = Requests(tz)
    next_poll = 0.0
    retry_at = None
    pos = boat_position()
    target, what = next_slot(times, tz, datetime.now(tz), pos)
    log(f"next photo {target:%a %b %d %H:%M %Z} ({what}, {pos[2]} position {pos[0]:.3f}, {pos[1]:.3f})")
    while True:
        if REQ_POLL_SEC > 0 and time.time() >= next_poll:
            next_poll = time.time() + REQ_POLL_SEC
            reqs.poll()

        now = datetime.now(tz)
        due = target if retry_at is None else min(target, retry_at)

        # ---- scheduled photo (and its one retry)
        if now >= due:
            is_retry = retry_at is not None and due == retry_at
            log("taking the retry photo" if is_retry else f"taking the {due:%H:%M} photo ({what})")
            started = now_ms()
            reqs.last_try = started
            ok = run_once_subprocess()
            if ok and reqs.pending and reqs.pending <= started:
                reqs.done(reqs.pending)              # this photo answers the app's request too
            if is_retry:
                retry_at = None
            else:
                pos = boat_position()             # the boat may have moved since yesterday
                target, what = next_slot(times, tz, datetime.now(tz), pos)
                retry_at = None if ok else datetime.now(tz) + timedelta(minutes=RETRY_MIN)
                if retry_at and retry_at >= target:
                    retry_at = None
            if retry_at:
                log(f"will try again at {retry_at:%H:%M}")
            log(f"next photo {target:%a %b %d %H:%M %Z} ({what}, {pos[2]} position {pos[0]:.3f}, {pos[1]:.3f})")
            continue

        # ---- "see live view" from the Wix widget
        if reqs.live_pending:
            req = reqs.live_pending
            left = reqs.live_left()
            if left < 30:
                log("live view refused: today's live minutes are used up")
                reqs.live_done(req, f"Live view is limited to {LIVE_DAY_MIN} minutes a day to "
                                    "save the camera's battery. Here's the latest photo.")
                continue
            reqs.live_done(req, "")                   # accepted - the widget watches the live doc
            t = time.time()
            run_live_subprocess(req, min(LIVE_MIN * 60, left))
            reqs.live_used += time.time() - t
            log(f"live view used {reqs.live_used / 60:.1f} of {LIVE_DAY_MIN} min today")
            next_poll = 0.0                           # look for new requests straight away
            continue

        # ---- "take a photo now" from the app
        ready = reqs.ready_at()
        if ready is not None and time.time() >= ready:
            req = reqs.pending
            log("taking a photo on request")
            reqs.last_try = now_ms()
            reqs.count += 1
            ok = run_once_subprocess()
            reqs.done(req)          # if it failed, lastError says why; the app shows it
            continue

        # Short sleeps so a clock jump (NTP after a reboot, DST) can't make us miss anything.
        wake = [(due - now).total_seconds(), 60]
        if REQ_POLL_SEC > 0:
            wake.append(next_poll - time.time())
        if ready is not None:
            wake.append(ready - time.time())
        time.sleep(max(1, min(wake)))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--once", "--now"):
        sys.exit(0 if take_one() else 1)
    if len(sys.argv) > 1 and sys.argv[1] == "--live":
        # --live [requestAt] [seconds]: both optional, for testing by hand
        req = int(sys.argv[2]) if len(sys.argv) > 2 else now_ms()
        secs = int(sys.argv[3]) if len(sys.argv) > 3 else LIVE_MIN * 60
        sys.exit(0 if live_session(req, secs) else 1)
    service()
