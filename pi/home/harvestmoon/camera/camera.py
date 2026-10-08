#!/usr/bin/env python3
"""
Harvest Moon - Deck camera snapshot (Reolink Argus PT Ultra -> Firebase)
------------------------------------------------------------------------
Once a day (SNAP_TIMES, boat time) this asks the camera for a still through
Neolink and saves it to camera/harvest-moon. The Wix camera widget shows
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

The service never photographs at start-up, so a code update or a reboot does
not wake the camera. Each photo runs as a separate short-lived process, so the
picture handling (Pillow) only uses memory for the few seconds it runs.

Environment variables (set in camera.service; the defaults match):
  PROJECT_ID    Firebase project            (default harvest-moon-watch)
  VESSEL_ID     document id                 (default harvest-moon)
  SNAP_TIMES    when to shoot, 24 h, comma-separated  (default 15:00)
  CAM_TZ        time zone for SNAP_TIMES    (default America/New_York)
  CAM_NAME      camera name in neolink.toml (default HarvestMoon)
  CAM_PRESET    PTZ preset id to move to first; blank = don't move (default blank)
  NEOLINK       path to the Neolink program
  NEOLINK_CONF  path to the Neolink config  (default /etc/harvest-moon/neolink.toml)
  MAX_W         force a smaller photo, longest side in px; 0 = full size (default 0)
  RETRY_MIN     if a scheduled photo fails, try once more this many minutes later (default 10)

Neolink config with the camera password: /etc/harvest-moon/neolink.toml
(chmod 600, owned by harvestmoon). Never in this file or the unit file.

Standard library, plus Pillow (python3-pil) for reading and, if needed, re-saving the photo.
"""

import os, sys, re, io, json, time, base64, subprocess
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PROJECT_ID   = os.environ.get("PROJECT_ID", "harvest-moon-watch").strip()
VESSEL_ID    = os.environ.get("VESSEL_ID", "harvest-moon").strip()
SNAP_TIMES   = os.environ.get("SNAP_TIMES", "15:00").strip()
CAM_TZ       = os.environ.get("CAM_TZ", "America/New_York").strip()
CAM_NAME     = os.environ.get("CAM_NAME", "HarvestMoon").strip()
CAM_PRESET   = os.environ.get("CAM_PRESET", "").strip()
NEOLINK      = os.environ.get("NEOLINK", "/home/harvestmoon/neolink/neolink_linux_armhf/neolink").strip()
NEOLINK_CONF = os.environ.get("NEOLINK_CONF", "/etc/harvest-moon/neolink.toml").strip()
MAX_W        = int(os.environ.get("MAX_W", "0"))
RETRY_MIN    = int(os.environ.get("RETRY_MIN", "10"))

WORK_DIR     = f"/tmp/hm-camera-{os.getuid()}"   # per user, so a sudo test run can't block the service
MAX_BYTES    = 1_040_000        # Firestore's limit is 1,048,576 bytes per document; room for the other fields
NEOLINK_SEC  = 120              # give up on any one Neolink command after this
ONCE_SEC     = 420              # give up on a whole photo run after this

DOC_URL = (f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}"
           f"/databases/(default)/documents/camera/{VESSEL_ID}")


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


def fs_patch(values, only_these=False):
    """
    Write fields to camera/<vessel>. With only_these=True the other fields
    (the last good photo) are left alone; otherwise the document is replaced.
    Returns True on success. Never raises.
    """
    url = DOC_URL
    if only_these:
        url += "?" + "&".join("updateMask.fieldPaths=" + urllib.parse.quote(k) for k in values)
    body = json.dumps({"fields": {k: fs_value(v) for k, v in values.items()}}).encode()
    req = urllib.request.Request(url, data=body, method="PATCH",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            log(f"firestore [{r.status}] camera/{VESSEL_ID}: {', '.join(values)}")
            return True
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        log(f"firestore write failed: HTTP {e.code} {detail}")
    except Exception as e:
        log(f"firestore write failed: {e}")
    return False


# ---- Neolink ---------------------------------------------------------------
def neolink(*args):
    """Run one Neolink command. Returns (ok, stdout). Never raises."""
    cmd = [NEOLINK] + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=NEOLINK_SEC)
    except subprocess.TimeoutExpired:
        log(f"neolink {args[0]} timed out after {NEOLINK_SEC} s")
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
    ok, out = neolink("battery", f"--config={NEOLINK_CONF}", CAM_NAME)
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
    ok = fs_patch(fields)
    log(f"photo run {'done' if ok else 'took the photo but the upload FAILED'} "
        f"in {time.time() - t0:.0f} s")
    return ok


# ---- the service: wait for SNAP_TIMES ----------------------------------------
def parse_times(spec):
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        h, _, m = part.partition(":")
        h, m = int(h), int(m or 0)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"bad time {part!r}")
        out.append((h, m))
    if not out:
        raise ValueError("SNAP_TIMES is empty")
    return sorted(set(out))


def next_slot(times, tz, after):
    for days in range(0, 3):
        d = (after + timedelta(days=days)).date()
        for h, m in times:
            t = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
            if t > after:
                return t
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


def service():
    tz = ZoneInfo(CAM_TZ)
    times = parse_times(SNAP_TIMES)
    if not os.path.exists(NEOLINK):
        log(f"WARNING: Neolink not found at {NEOLINK} - photos will fail")
    if not os.access(NEOLINK_CONF, os.R_OK):
        log(f"WARNING: cannot read {NEOLINK_CONF} - photos will fail")
    log(f"camera service up: photos at {', '.join(f'{h:02d}:{m:02d}' for h, m in times)} "
        f"({CAM_TZ}), camera {CAM_NAME}, preset {CAM_PRESET or 'none'}")

    retry_at = None
    target = next_slot(times, tz, datetime.now(tz))
    log(f"next photo {target:%a %b %d %H:%M %Z}")
    while True:
        now = datetime.now(tz)
        due = target if retry_at is None else min(target, retry_at)
        if now < due:
            # Short sleeps so a clock jump (NTP after a reboot, DST) can't make us miss it.
            time.sleep(min(60, max(1, (due - now).total_seconds())))
            continue

        is_retry = retry_at is not None and due == retry_at
        log("taking the retry photo" if is_retry else f"taking the {due:%H:%M} photo")
        ok = run_once_subprocess()

        if is_retry:
            retry_at = None
        else:
            target = next_slot(times, tz, datetime.now(tz))
            retry_at = None if ok else datetime.now(tz) + timedelta(minutes=RETRY_MIN)
            if retry_at and retry_at >= target:
                retry_at = None
        if retry_at:
            log(f"will try again at {retry_at:%H:%M}")
        log(f"next photo {target:%a %b %d %H:%M %Z}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--once", "--now"):
        sys.exit(0 if take_one() else 1)
    service()
