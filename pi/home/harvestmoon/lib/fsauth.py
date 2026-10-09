"""
Harvest Moon - Firebase sign-in for the Pi's programs
------------------------------------------------------
The Firestore rules let anyone READ, but only signed-in boat systems (and the
Harvest Watch app, once signed in) WRITE. This module signs the Pi in with the
"boat systems" login and hands out the header that proves it:

    import fsauth
    headers = {"Content-Type": "application/json", **fsauth.headers()}

Login details live in /etc/harvest-moon/firebase.env (root:harvestmoon, 640),
never in code or unit files:

    FB_API_KEY=...        # Firebase project settings -> Web API key (not secret)
    FB_EMAIL=...          # the boat systems login
    FB_PASSWORD=...

Safe by design:
  * headers() NEVER raises. With no login file it returns {} and every program
    behaves exactly as before the rules were locked.
  * If sign-in fails (no internet, wrong password) it returns {} and tries
    again later (2 min, backing off to 15), logging the reason once per streak.
  * The ID token (good for 1 h) is renewed with the refresh token 5 min early;
    if that fails it signs in with the password again.
  * After each successful sign-in it writes authcheck/pi-<program> (time, uid),
    which only a signed-in writer can do - a live check that this program will
    pass the locked rules.

Standard library only.
"""

import os, sys, json, time, threading
import urllib.request, urllib.parse, urllib.error

CRED_FILE  = os.environ.get("FB_CRED_FILE", "/etc/harvest-moon/firebase.env")
PROJECT_ID = os.environ.get("PROJECT_ID", "harvest-moon-watch").strip()
NAME       = "pi-" + os.path.splitext(os.path.basename(sys.argv[0] or "python"))[0]

_lock = threading.Lock()
_st = {"id": None, "exp": 0.0, "refresh": None, "uid": None,
       "next_try": 0.0, "backoff": 120.0, "failing": False}


def _log(msg):
    print(f"firebase sign-in: {msg}", flush=True)


def _creds():
    """FB_API_KEY / FB_EMAIL / FB_PASSWORD from the environment, else the login file."""
    keys = ("FB_API_KEY", "FB_EMAIL", "FB_PASSWORD")
    vals = {k: os.environ.get(k, "").strip() for k in keys}
    if not all(vals.values()):
        try:
            with open(CRED_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k, v = k.strip(), v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                        v = v[1:-1]                       # quotes around the whole value only
                    if k in vals and not vals[k]:
                        vals[k] = v
        except OSError:
            pass
    return vals if all(vals.values()) else None


def _post(url, data, form=False):
    if form:
        body, ctype = urllib.parse.urlencode(data).encode(), "application/x-www-form-urlencoded"
    else:
        body, ctype = json.dumps(data).encode(), "application/json"
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _why(e):
    """Short reason from a Google error reply, e.g. INVALID_LOGIN_CREDENTIALS."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            return json.loads(e.read().decode("utf-8", "replace"))["error"]["message"]
        except Exception:
            return f"HTTP {e.code}"
    return str(e) or e.__class__.__name__


def _sign_in(c):
    """Refresh if we can, else sign in with the password. Raises on failure."""
    if _st["refresh"]:
        try:
            d = _post(f"https://securetoken.googleapis.com/v1/token?key={c['FB_API_KEY']}",
                      {"grant_type": "refresh_token", "refresh_token": _st["refresh"]}, form=True)
            _st.update(id=d["id_token"], refresh=d.get("refresh_token") or _st["refresh"],
                       exp=time.time() + int(d.get("expires_in", 3600)), uid=d.get("user_id") or _st["uid"])
            return False
        except urllib.error.HTTPError:
            _st["refresh"] = None                         # refused: fall back to the password
    d = _post(f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={c['FB_API_KEY']}",
              {"email": c["FB_EMAIL"], "password": c["FB_PASSWORD"], "returnSecureToken": True})
    _st.update(id=d["idToken"], refresh=d.get("refreshToken"),
               exp=time.time() + int(d.get("expiresIn", 3600)), uid=d.get("localId"))
    return True


def _checkin(token):
    """authcheck/<program>: proves this program can write under the locked rules."""
    now = int(time.time() * 1000)
    url = (f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/(default)"
           f"/documents/authcheck/{NAME}?updateMask.fieldPaths=at&updateMask.fieldPaths=uid")
    body = json.dumps({"fields": {"at": {"integerValue": str(now)},
                                  "uid": {"stringValue": _st["uid"] or ""}}}).encode()
    req = urllib.request.Request(url, data=body, method="PATCH",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
        _log(f"check-in written (authcheck/{NAME})")
    except Exception as e:
        _log(f"check-in FAILED: {_why(e)} - this program's writes may be refused")


def headers():
    """{'Authorization': 'Bearer <token>'} when signed in, {} otherwise. Never raises."""
    try:
        c = _creds()
        if not c:
            return {}
        with _lock:
            now = time.time()
            if _st["id"] and now < _st["exp"] - 300:
                return {"Authorization": "Bearer " + _st["id"]}
            if now >= _st["next_try"]:
                try:
                    used_password = _sign_in(c)
                    if _st["failing"] or used_password:
                        _log(f"signed in as {c['FB_EMAIL']} (uid {(_st['uid'] or '?')[-6:]})")
                    _st.update(failing=False, next_try=0.0, backoff=120.0)
                    if used_password:
                        _checkin(_st["id"])
                except Exception as e:
                    if not _st["failing"]:
                        _log(f"FAILED ({_why(e)}) - will keep retrying")
                    _st["failing"] = True
                    _st["next_try"] = now + _st["backoff"]
                    _st["backoff"] = min(_st["backoff"] * 2, 900.0)
            if _st["id"] and now < _st["exp"]:
                return {"Authorization": "Bearer " + _st["id"]}
            return {}
    except Exception:
        return {}


if __name__ == "__main__":
    # By hand on the Pi:  python3 /home/harvestmoon/lib/fsauth.py
    NAME = "pi-test"
    h = headers()
    print("OK - signed in, token ready" if h else "NOT signed in (see the message above, or no login file)")
