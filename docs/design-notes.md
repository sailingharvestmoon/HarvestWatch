# Harvest Moon — boat systems

Four services on the Pi, one Cloudflare worker, and a set of static pages on
Netlify. Everything talks through the same Firebase project
(`harvest-moon-watch`, vessel `harvest-moon`).

---

## The anchor watch: two independent watchers

The single most important design decision here. There are **two** watchers, and
each covers the other's failure mode.

| | Runs | Interval | Owns |
|---|---|---|---|
| `guard.py` | On the boat | 10 s | drag, swing, wind, gust, apparent wind angle, AIS |
| Cloudflare worker | Off the boat | 60 s | boat-gone-dark, scope, depth, daily all-clear, track recording, drag as backup |

They share `state/harvest-moon`, so whichever notices a drag first latches the
flag and the other stays quiet. Neither can be silenced by the other failing.

### Why the Pi does the alerting

ntfy limits messages **per IP address, not per account** (`"basis": "ip"` in
your account JSON). The boat has its own address. Cloudflare Workers share
egress addresses with thousands of customers, so the worker kept hitting a
250-per-12-hours quota that strangers had used up. That was the cause of months
of alerts arriving hours late or not at all — not a bug in the alarm logic.

### The dead man's switch

The boat-gone-dark alert is **not** sent by anything noticing the boat is quiet.
While armed, `guard.py` schedules an ntfy message for `deadmanDelayMin` in the
future and keeps pushing it further out. If the Pi, the Vesper, or the internet
dies, nothing pushes it back and ntfy delivers it on its own. **Nothing needs to
be working for that alert to reach you.**

A clean shutdown (`systemctl stop`, reboot, Ctrl-C) cancels it. An unclean one —
power cut, panic, water — deliberately does not.

The refresh interval is *derived* from the delay (a third of it, floored at
5 min). Never make them independent: a refresh slower than the delay fires the
alert on a healthy boat every time.

---

## Services on the Pi

| Unit | File | Purpose |
|---|---|---|
| `frame` | `frame.py` | e-ink display: photos / dashboard / info screen |
| `sensors` | `sensors.py` | NMEA → Firebase telemetry + position + bow position |
| `weather` | `weather.py` | NWS forecast, NOAA tides, sun/moon → Firebase |
| `guard` | `guard.py` | fast anchor watcher + AIS + dead man's switch |

All three of `sensors`, `weather`, `guard` open their own read-only socket to
the Vesper at `192.168.1.167:39150`. Multiple readers are fine.

### Secrets

`guard.py` needs the ntfy credentials, kept out of the unit file:

    sudo mkdir -p /etc/harvest-moon
    sudo nano /etc/harvest-moon/ntfy.env      # NTFY_TOPIC= and NTFY_TOKEN=
    sudo chmod 600 /etc/harvest-moon/ntfy.env
    sudo chown harvestmoon /etc/harvest-moon/ntfy.env

### Deploying a change

Copy **from the Mac**, not from inside an SSH session — running `scp` on the Pi
copies the Pi's own stale copy over itself. And check you're sending the file
you think you are; browsers add suffixes rather than overwriting:

    cd ~/Downloads && grep -c deadman guard.py     # confirm it's the new one
    scp guard.py harvestmoon@inkyframe.local:/home/harvestmoon/sensors/
    sudo systemctl restart guard

---

## Firestore collections

| Path | Written by | Holds |
|---|---|---|
| `vessels/` | sensors.py | GPS position, **bow position**, timestamp |
| `telemetry/` | sensors.py | wind, depth, temp, speed, heading (mag + true) |
| `weather/` | weather.py | forecast, tides (string **and** numbers), sun, moon |
| `alarms/` | watch.html | anchor, radius, every alert setting, snooze |
| `state/` | worker + guard | shared alert latches |
| `tracks/` | worker | swing track |
| `health/` | worker + guard | heartbeats, push outcomes, ntfy quota |
| `ais/` | guard.py | nearby AIS targets |
| `modes/` | frame.html | e-ink display mode |

Rules are in `firestore-rules.txt`.

---

## Geometry that matters

**The bow offset.** The anchor is dropped at the bow; the GPS antenna is ~30 ft
aft. Measuring drag from the GPS overstated it by a boat length — 130 ft showing
on a calm night with 100 ft of chain down and nothing wrong. `sensors.py` now
publishes `bowLat`/`bowLon`, projected from the GPS along **true** heading, and
the watch page, worker and guard all measure from there. Projected, not
subtracted: subtracting is only right while the bow points at the anchor.

**Scope.** `rode ÷ (water depth + bow roller height)`. The sounder reads depth
*below the transducer*, so the transducer offset must be right or every scope
number is wrong by the same amount.

**Alarm radius.** `√(rode² − (depth + roller)²) + margin`. The square root is how
far the bow can reach with the chain bar-taut — a true worst case, since catenary
only holds you closer. Boat length is deliberately excluded: the alarm measures
to the bow, so including a hull length counts it twice. Margin is fixed (25 ft
default), not a percentage, because GPS error doesn't scale with rode.

**Chain marks**, every 25 ft: red, orange, yellow, green, blue, purple, light
green, white, pink, light blue → 250 ft total.

---

## Still untested

1. **The dead man's switch.** Arm it, confirm a `deadman: offline alert
   rescheduled` line in the guard log, then pull power to the Pi and wait for
   the offline alert. Until that has been seen arrive, it is a belief.
2. **ntfy quota under the switch.** Watch `ntfy quota:` in the guard log over a
   day. Dropping ~144/day means replaced scheduled messages count against the
   budget and the intervals should lengthen. Barely moving means they don't.

## Housekeeping

- Rotate the ntfy token (it was exposed in a screenshot). New token on ntfy.sh,
  update `/etc/harvest-moon/ntfy.env` **and** the Cloudflare variable.
- `transmit.html` is retired — the Pi is the transmitter now.
