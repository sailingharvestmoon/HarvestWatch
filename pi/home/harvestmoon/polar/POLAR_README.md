# Harvest Moon polar logging

Two programs. `polar_logger.py` runs on the Pi whenever you're sailing and
writes a file. `polar_build.py` runs whenever you feel like it and turns those
files into a polar. You don't touch the second one until you have data.

---

## Why it works the way it does

Your masthead unit measures **apparent wind** — a blend of the real wind and
your own motion. A polar needs **true wind**, referenced to the water.

Converting one to the other needs **speed through water**. Not SOG. GPS speed
includes current, and in Penobscot Bay a 1.5 knot set is a third of a light-air
reading. That was the flaw in the numbers you'd collected by hand: 4.2 knots of
boat speed in 4.6 knots of wind on a beam reach isn't something a 21,000 pound
full-keel boat does. It was the tide.

So: apparent wind + speed through water + heading, once a second, converted
here rather than trusted from the instrument, and saved locally.

---

## Step 1 — Find your data (do this first)

```bash
python3 polar_logger.py --probe
```

Thirty seconds of listening. It tries the usual Vesper endpoints, then prints
which NMEA sentences are on the wire and whether the fields you need are among
them. If it finds nothing, point it somewhere:

```bash
python3 polar_logger.py --probe --kind tcp --host 192.168.4.1 --port 39150
python3 polar_logger.py --probe --kind udp --port 2000
python3 polar_logger.py --probe --kind serial --dev /dev/ttyUSB0
```

**What you want:** `ok` beside apparent wind angle, apparent wind speed, and
speed through water.

**What I expect to go wrong:** wind arrives and speed through water doesn't.
Your DST810 has STW; the wind data is on the Raymarine side; whether both reach
one stream depends on how the Vesper is bridged. If STW is missing, fix that
before logging anything — a log without it records current.

You can watch the whole chain work at the slip:

```bash
python3 polar_logger.py --simulate --probe --probe-seconds 10
```

## Step 2 — Log

```bash
python3 polar_logger.py
```

Writes `~/polar_logs/polar_YYYY-MM-DD.csv`, one row a second, roughly 4 MB a
day. Set it and forget it. To run at boot, copy `harvest-moon-polar.service`
to `/etc/systemd/system/` and:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now harvest-moon-polar
journalctl -u harvest-moon-polar -f
```

**Motoring.** Engine hours poison everything. If your tach puts RPM on the
NMEA bus the logger catches it. Otherwise flag it by hand:

```bash
touch /run/harvestmoon.motoring    # engine on
rm    /run/harvestmoon.motoring    # engine off
```

You will forget. Worth wiring to something that already knows — an oil pressure
switch on a GPIO, or engine state off the Cerbo.

**Not Firebase.** One row a second is 86,400 writes a day against a 20,000 free
tier. Log locally, rsync when you have signal.

## Step 3 — Build

```bash
python3 polar_build.py
python3 polar_build.py --since 2026-11-08     # the passage only
python3 polar_build.py --percentile 70        # honest, for passage planning
python3 polar_build.py --min-samples 5        # looser, while data is thin
```

---

## Reading the output

**Coverage map.** Steady segments per cell; dots are cells you haven't sailed.
The light-air upwind corner may never fill, which is fine — you'd be motoring.

**Port / starboard check.** The important one early on. If she's consistently
faster on one tack, she isn't: the masthead unit is rotated. The script solves
for the offset that makes both tacks agree, rather than guessing from a rule of
thumb. Put the number in `AWA_OFFSET_DEG` at the top of `polar_logger.py` and
every cell improves at once. `--apply-offset` shows you the corrected build
before you commit to it.

To feed this check, sail a few deliberate pairs: hold a steady angle two
minutes on starboard, tack, hold two minutes on port, same conditions.

**Measured vs estimated.** A ratio of 1.08 means she's 8% quicker than the
design estimate, and the cells you haven't filled get rescaled to match.

**M vs e.** M came from your sailing, e is still my guess. Watch the M count
climb.

---

## How much data

Roughly 8 wind-speed bins × 15 angle bins, and a cell wants ~8 steady segments
before it means anything. Steady state is a modest fraction of real sailing
time, and conditions don't distribute evenly — you'll bank a hundred hours of
12–18 knot reaching and three hours of light-air beating. Call it **100–150
hours** to fill the cells that matter. The passage south should do most of the
downwind half in one go, which is the half that decides your routing anyway.

---

## Two judgment calls, not settled facts

**The 85th percentile.** A polar should describe the boat sailed well, not
sailed average, or the router plans on mediocrity. But for landfall timing and
fuel, `--percentile 70` is arguably the more honest number. You get both from
the same data.

**Leeway and heel.** The true wind conversion ignores both. Neither is on your
bus. The error is real but small next to the current contamination this is
built to remove. If you ever add a heel sensor the logger already records XDR
roll if it sees it.
