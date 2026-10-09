#!/usr/bin/env python3
"""
Harvest Moon - test the locked database rules before they're switched on
-----------------------------------------------------------------------
The "check" rules (firebase/firestore-rules.txt while the lock is being
prepared) add a test collection, rulecheck/, guarded by exactly the same rules
the locked database will use. This script tries every kind of write against it,
signed in and not, and reports whether Firebase allowed or refused each one.

    python3 /home/harvestmoon/lib/rulecheck.py            test the rules
    python3 /home/harvestmoon/lib/rulecheck.py --status   who has signed in, and when

Every line should end in "ok". It only writes to rulecheck/ - nothing the boat
uses. Needs the boat systems login in /etc/harvest-moon/firebase.env.

Since the lock went live (Oct 9, 2026) the live rules no longer include the
check-stage test areas, so the two "public may ask" tests now report FAIL - that
is expected. --status still works any time.
"""

import os, sys, json, time, urllib.request, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fsauth

PROJECT_ID = os.environ.get("PROJECT_ID", "harvest-moon-watch").strip()
BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/(default)/documents/rulecheck"


def write(doc, fields, signed):
    mask = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
    body = json.dumps({"fields": fields}).encode()
    hdr = {"Content-Type": "application/json"}
    if signed:
        hdr.update(fsauth.headers())
    req = urllib.request.Request(f"{BASE}/{doc}?{mask}", data=body, method="PATCH", headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return f"no answer ({e})"


def read(doc):
    try:
        with urllib.request.urlopen(f"{BASE}/{doc}", timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return f"no answer ({e})"


EXPECTED = ["pi-sensors", "pi-guard", "pi-camera", "cloud-anchor", "cloud-weather", "app"]


def status():
    """Each program's last sign-in check-in (authcheck/<name>), newest first."""
    url = BASE.rsplit("/", 1)[0] + "/authcheck?pageSize=50"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            docs = json.load(r).get("documents", [])
    except Exception as e:
        print(f"Couldn't read authcheck/: {e}")
        return 1
    seen = {}
    for d in docs:
        f = d.get("fields", {})
        seen[d["name"].rsplit("/", 1)[1]] = int(f.get("at", {}).get("integerValue", "0"))
    now = time.time() * 1000
    for name in EXPECTED + sorted(set(seen) - set(EXPECTED)):
        if name in seen:
            mins = (now - seen[name]) / 60000
            age = f"{mins:.0f} min ago" if mins < 120 else f"{mins / 60:.1f} h ago"
            print(f"ok    {name:14s} signed in {age}")
        else:
            print(f"--    {name:14s} not yet")
    missing = [n for n in EXPECTED if n not in seen]
    print("\nALL SIGNED IN - safe to lock." if not missing else f"\nWaiting for: {', '.join(missing)}")
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        return status()
    if not fsauth.headers():
        print("Can't sign in with the boat systems login - see the message above. Stopping.")
        return 1
    now = {"integerValue": str(int(time.time() * 1000))}
    tests = [
        # (what, how, expect)
        ("boat systems may write anything", lambda: write("camera", {"image": {"stringValue": "test"}, "requestAt": now}, True), 200),
        ("anyone may read",                 lambda: read("camera"), 200),
        ("public may ask for a photo",      lambda: write("camera", {"requestAt": now}, False), 200),
        ("public may ask for live view",    lambda: write("camera", {"liveRequestAt": now, "liveWatchAt": now}, False), 200),
        ("public may NOT change the photo", lambda: write("camera", {"image": {"stringValue": "x"}}, False), 403),
        ("public may NOT sneak in a photo with a request",
                                            lambda: write("camera", {"requestAt": now, "image": {"stringValue": "x"}}, False), 403),
        ("public request must be a number", lambda: write("camera", {"requestAt": {"stringValue": "x"}}, False), 403),
        ("public may NOT write elsewhere",  lambda: write("other", {"anchorLat": {"doubleValue": 1.0}}, False), 403),
        ("boat systems may write elsewhere", lambda: write("other", {"anchorLat": {"doubleValue": 1.0}}, True), 200),
    ]
    bad = 0
    for what, fn, expect in tests:
        got = fn()
        ok = got == expect
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'}  {what:52s} (Firebase said {got}, expected {expect})")
    print("\nALL GOOD - the locked rules behave as intended." if not bad
          else f"\n{bad} test(s) failed - don't lock the rules yet. Paste this output to Claude.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
