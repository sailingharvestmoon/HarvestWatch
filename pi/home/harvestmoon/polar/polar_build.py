#!/usr/bin/env python3
"""
Harvest Moon polar builder.

Reads the CSVs written by polar_logger.py and produces a polar. The work is
mostly in deciding what to throw away:

  * motoring rows go
  * anything not in steady state goes, because a boat accelerating out of a
    tack is not evidence about how fast she sails
  * what survives is binned by true wind speed and angle, and each cell takes
    a high percentile of the surviving boat speeds -- a polar should describe
    the boat sailed well, not sailed averagely

It also checks port against starboard. If she is consistently faster on one
tack, she isn't; the masthead unit is rotated.

    python3 polar_build.py
    python3 polar_build.py --since 2026-11-08      # just the passage
    python3 polar_build.py --percentile 70         # honest passage-planning
"""

import argparse
import csv
import glob
import math
import os
import statistics
import sys
from collections import defaultdict

TWS_BINS = [6, 8, 10, 12, 14, 16, 20, 25]
TWA_BINS = [45, 52, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150, 160, 170, 180]

# Estimated polar for the IP380, from design numbers: LWL 32 ft, 21,000 lb
# displacement, 42.9% ballast, 885 sq ft working sail, D/L 286, hull speed
# 7.58 kt, PHRF ~165. This is the prior. Cells you fill with real sailing
# replace it; cells you don't get rescaled by the bias your data reveals.
ESTIMATED = {
    6:  {45: 2.8, 52: 3.6, 60: 4.1, 70: 4.4, 80: 4.7, 90: 4.9, 100: 5.0,
         110: 4.9, 120: 4.7, 130: 4.4, 140: 4.1, 150: 3.7, 160: 3.4, 170: 3.1, 180: 3.0},
    8:  {45: 3.6, 52: 4.8, 60: 5.2, 70: 5.5, 80: 5.8, 90: 5.9, 100: 6.0,
         110: 5.9, 120: 5.7, 130: 5.4, 140: 5.0, 150: 4.6, 160: 4.2, 170: 3.9, 180: 3.8},
    10: {45: 4.3, 52: 5.5, 60: 5.9, 70: 6.1, 80: 6.4, 90: 6.5, 100: 6.6,
         110: 6.6, 120: 6.4, 130: 6.2, 140: 5.8, 150: 5.4, 160: 5.0, 170: 4.7, 180: 4.6},
    12: {45: 4.7, 52: 5.9, 60: 6.3, 70: 6.5, 80: 6.8, 90: 6.9, 100: 7.0,
         110: 7.0, 120: 6.9, 130: 6.7, 140: 6.4, 150: 6.0, 160: 5.6, 170: 5.3, 180: 5.2},
    14: {45: 5.0, 52: 6.2, 60: 6.5, 70: 6.7, 80: 7.0, 90: 7.1, 100: 7.2,
         110: 7.3, 120: 7.2, 130: 7.0, 140: 6.7, 150: 6.4, 160: 6.0, 170: 5.7, 180: 5.6},
    16: {45: 5.2, 52: 6.4, 60: 6.7, 70: 6.9, 80: 7.2, 90: 7.3, 100: 7.4,
         110: 7.5, 120: 7.4, 130: 7.3, 140: 7.0, 150: 6.7, 160: 6.4, 170: 6.1, 180: 6.0},
    20: {45: 5.3, 52: 6.5, 60: 6.8, 70: 7.1, 80: 7.4, 90: 7.5, 100: 7.6,
         110: 7.7, 120: 7.7, 130: 7.6, 140: 7.4, 150: 7.1, 160: 6.8, 170: 6.5, 180: 6.4},
    25: {45: 5.2, 52: 6.4, 60: 6.7, 70: 7.1, 80: 7.4, 90: 7.6, 100: 7.7,
         110: 7.8, 120: 7.8, 130: 7.7, 140: 7.5, 150: 7.3, 160: 7.0, 170: 6.7, 180: 6.6},
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load(log_dir, since=None, until=None):
    paths = sorted(glob.glob(os.path.join(os.path.expanduser(log_dir), "polar_*.csv")))
    rows = []
    for path in paths:
        day = os.path.basename(path)[6:16]
        if since and day < since:
            continue
        if until and day > until:
            continue
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    if int(r.get("motoring") or 0):
                        continue
                    rows.append({
                        "utc": r["utc"],
                        "stw": float(r["stw"]),
                        "twa": float(r["twa"]),
                        "tws": float(r["tws"]),
                        "awa": float(r["awa"]),
                        "aws": float(r["aws"]),
                        "hdg": float(r["hdg_true"]) if r.get("hdg_true") else None,
                    })
                except (KeyError, TypeError, ValueError):
                    continue
    return paths, rows


# ---------------------------------------------------------------------------
# Steady state
# ---------------------------------------------------------------------------

def circ_std(degrees):
    """Circular standard deviation, degrees. Handles the 359/001 wrap."""
    if len(degrees) < 2:
        return 0.0
    xs = sum(math.cos(math.radians(d)) for d in degrees) / len(degrees)
    ys = sum(math.sin(math.radians(d)) for d in degrees) / len(degrees)
    r = math.hypot(xs, ys)
    if r >= 1.0:
        return 0.0
    if r <= 1e-9:
        return 180.0
    return math.degrees(math.sqrt(-2.0 * math.log(r)))


def circ_mean(degrees):
    xs = sum(math.cos(math.radians(d)) for d in degrees) / len(degrees)
    ys = sum(math.sin(math.radians(d)) for d in degrees) / len(degrees)
    return math.degrees(math.atan2(ys, xs)) % 360.0


def segments(rows, window, gates, stride=None):
    """Sliding non-overlapping windows that pass every stability gate."""
    stride = stride or window
    out = []
    i = 0
    n = len(rows)
    while i + window <= n:
        chunk = rows[i:i + window]
        # a gap in the log breaks the window
        if not contiguous(chunk):
            i += 1
            continue
        twas = [abs(r["twa"]) for r in chunk]
        twss = [r["tws"] for r in chunk]
        stws = [r["stw"] for r in chunk]
        hdgs = [r["hdg"] for r in chunk if r["hdg"] is not None]
        signs = set(1 if r["twa"] >= 0 else -1 for r in chunk)

        if len(signs) != 1:                       # tacked or gybed mid-window
            i += 1
            continue
        if statistics.pstdev(twas) > gates["twa"]:
            i += 1
            continue
        if statistics.pstdev(twss) > gates["tws"]:
            i += 1
            continue
        if statistics.pstdev(stws) > gates["stw"]:
            i += 1
            continue
        if hdgs and circ_std(hdgs) > gates["hdg"]:
            i += 1
            continue

        out.append({
            "twa": statistics.mean(twas),
            "tws": statistics.mean(twss),
            "stw": statistics.mean(stws),
            "awa": statistics.mean([abs(r["awa"]) for r in chunk]),
            "aws": statistics.mean([r["aws"] for r in chunk]),
            "tack": 1 if chunk[0]["twa"] >= 0 else -1,
            "utc": chunk[0]["utc"],
        })
        i += stride
    return out


def contiguous(chunk):
    """Reject a window that spans a gap in logging."""
    try:
        from datetime import datetime
        t0 = datetime.fromisoformat(chunk[0]["utc"])
        t1 = datetime.fromisoformat(chunk[-1]["utc"])
    except (ValueError, KeyError):
        return True
    span = (t1 - t0).total_seconds()
    return span <= len(chunk) * 1.6


# ---------------------------------------------------------------------------
# Binning
# ---------------------------------------------------------------------------

def nearest(value, bins):
    return min(bins, key=lambda b: abs(b - value))


def bin_segments(segs, tol_tws=1.5, tol_twa=6.0):
    """Assign each segment to the nearest cell, rejecting loose matches."""
    cells = defaultdict(list)
    for s in segs:
        tws_b = nearest(s["tws"], TWS_BINS)
        twa_b = nearest(s["twa"], TWA_BINS)
        if abs(s["tws"] - tws_b) > tol_tws or abs(s["twa"] - twa_b) > tol_twa:
            continue
        cells[(tws_b, twa_b)].append(s)
    return cells


def percentile(values, pct):
    if not values:
        return None
    vs = sorted(values)
    if len(vs) == 1:
        return vs[0]
    k = (len(vs) - 1) * pct / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return vs[int(k)]
    return vs[lo] * (hi - k) + vs[hi] * (k - lo)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def coverage_map(cells, min_samples):
    print("\nCoverage — steady segments per cell")
    print("  TWA:  " + "".join("{:>5}".format(a) for a in TWA_BINS))
    for tws in TWS_BINS:
        line = "  {:>3} kt".format(tws)
        for twa in TWA_BINS:
            n = len(cells.get((tws, twa), []))
            line += "{:>5}".format(n if n else ".")
        print(line)
    filled = sum(1 for c in cells.values() if len(c) >= min_samples)
    total = len(TWS_BINS) * len(TWA_BINS)
    print("  {}/{} cells at >={} segments ({:.0f}%)".format(
        filled, total, min_samples, 100.0 * filled / total))


def true_wind(awa, aws, stw):
    """Same conversion the logger does, so an offset can be re-applied here."""
    a = math.radians(awa)
    x = aws * math.cos(a) - stw
    y = aws * math.sin(a)
    return abs(math.degrees(math.atan2(y, x))), math.hypot(x, y)


def asymmetry(segs, offset):
    """Mean port-vs-starboard speed difference if the masthead were off by
    `offset` degrees. The true offset is the one that makes the boat
    symmetric, because real boats very nearly are."""
    cells = defaultdict(lambda: ([], []))
    for s in segs:
        # offset is signed in the boat frame; on port tack it flips
        awa = s["awa"] - offset * s["tack"]
        if awa <= 0 or awa >= 180:
            continue
        twa, tws = true_wind(awa, s["aws"], s["stw"])
        key = (nearest(tws, TWS_BINS), nearest(twa, TWA_BINS))
        if abs(tws - key[0]) > 1.5 or abs(twa - key[1]) > 6.0:
            continue
        cells[key][0 if s["tack"] > 0 else 1].append(s["stw"])
    diffs = []
    for stbd, port in cells.values():
        if len(stbd) >= 3 and len(port) >= 3:
            a, b = statistics.mean(stbd), statistics.mean(port)
            diffs.append(abs(a - b) / ((a + b) / 2))
    if len(diffs) < 3:
        return None, 0
    return statistics.mean(diffs) * 100, len(diffs)


def tack_check(segs, search=15.0):
    """Port vs starboard. Asymmetry means the masthead unit is rotated."""
    print("\nPort / starboard check")
    base, n = asymmetry(segs, 0.0)
    if base is None:
        print("  Not enough paired cells yet. Needs both tacks in the same")
        print("  conditions — sail a few deliberate tack-and-hold pairs.")
        return None
    print("  {} paired cells, mean tack-to-tack speed difference: {:.1f}%".format(n, base))
    if base < 1.5:
        print("  Symmetric. No masthead correction indicated.")
        return 0.0

    best, best_val = 0.0, base
    o = -search
    while o <= search + 1e-9:
        val, pairs = asymmetry(segs, o)
        if val is not None and pairs >= 3 and val < best_val:
            best, best_val = o, val
        o += 0.25
    if abs(best) < 0.25:
        print("  No offset in ±{:.0f}° improves it. Look at calibration of the".format(search))
        print("  speed log instead, or at a genuine sail-trim difference.")
        return 0.0
    print("  Solved: an offset of {:+.2f}° cuts that to {:.1f}%.".format(best, best_val))
    print("  Set AWA_OFFSET_DEG = {:+.2f} in polar_logger.py and rebuild.".format(best))
    print("  (Solved by searching for the offset that makes both tacks agree,")
    print("   not estimated from a rule of thumb.)")
    return best


def bias_vs_estimate(table):
    ratios = []
    for tws, row in table.items():
        for twa, val in row.items():
            if val and val[1] == "M":
                est = ESTIMATED[tws][twa]
                if est:
                    ratios.append(val[0] / est)
    if not ratios:
        return None
    return statistics.median(ratios)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_table(cells, min_samples, pct):
    table = {tws: {} for tws in TWS_BINS}
    for tws in TWS_BINS:
        for twa in TWA_BINS:
            segs = cells.get((tws, twa), [])
            if len(segs) >= min_samples:
                table[tws][twa] = (percentile([s["stw"] for s in segs], pct), "M")
            else:
                table[tws][twa] = None
    return table


def fill_from_estimate(table, ratio):
    scale = ratio or 1.0
    for tws in TWS_BINS:
        for twa in TWA_BINS:
            if table[tws][twa] is None:
                table[tws][twa] = (ESTIMATED[tws][twa] * scale, "e")
    return table


def write_pol(table, path):
    with open(path, "w") as fh:
        fh.write("twa/tws\t" + "\t".join(str(w) for w in TWS_BINS) + "\n")
        for twa in TWA_BINS:
            fh.write(str(twa))
            for tws in TWS_BINS:
                fh.write("\t{:.2f}".format(table[tws][twa][0]))
            fh.write("\n")


def print_table(table):
    print("\nPolar (M = measured, e = estimated)")
    print("  TWA " + "".join("{:>9}".format(str(w) + "kt") for w in TWS_BINS))
    for twa in TWA_BINS:
        line = "  {:>3}".format(twa)
        for tws in TWS_BINS:
            val, src = table[tws][twa]
            line += "{:>8.2f}{}".format(val, src)
        print(line)


def best_vmg(table):
    print("\nBest VMG angles")
    print("  TWS    beat      up-VMG      run     down-VMG")
    for tws in TWS_BINS:
        up = max(((twa, table[tws][twa][0] * math.cos(math.radians(twa)))
                  for twa in TWA_BINS if twa <= 90), key=lambda x: x[1])
        dn = max(((twa, -table[tws][twa][0] * math.cos(math.radians(twa)))
                  for twa in TWA_BINS if twa >= 90), key=lambda x: x[1])
        print("  {:>3}   {:>4}°   {:>6.2f} kt   {:>4}°   {:>6.2f} kt".format(
            tws, up[0], up[1], dn[0], dn[1]))


def main():
    p = argparse.ArgumentParser(description="Build a polar from logged data")
    p.add_argument("--log-dir", default="~/polar_logs")
    p.add_argument("--out", default="harvest_moon_measured.pol")
    p.add_argument("--since", help="YYYY-MM-DD")
    p.add_argument("--until", help="YYYY-MM-DD")
    p.add_argument("--window", type=int, default=60,
                   help="steady-state window, seconds (don't go below ~30)")
    p.add_argument("--percentile", type=float, default=85)
    p.add_argument("--min-samples", type=int, default=8,
                   help="segments needed before a cell counts as measured")
    p.add_argument("--gate-twa", type=float, default=5.0)
    p.add_argument("--gate-tws", type=float, default=1.5)
    p.add_argument("--gate-stw", type=float, default=0.25)
    p.add_argument("--gate-hdg", type=float, default=5.0)
    p.add_argument("--offset-search", type=float, default=15.0,
                   help="range of masthead offsets to search, degrees")
    p.add_argument("--apply-offset", action="store_true",
                   help="rebuild using the solved offset instead of just reporting it")
    args = p.parse_args()

    paths, rows = load(args.log_dir, args.since, args.until)
    if not paths:
        print("No logs in {}. Nothing to build.".format(args.log_dir))
        return 1
    print("{} files, {} sailing rows ({:.1f} hours under sail).".format(
        len(paths), len(rows), len(rows) / 3600.0))
    if len(rows) < args.window * 2:
        print("Too little data to analyse yet.")
        return 1

    gates = {"twa": args.gate_twa, "tws": args.gate_tws,
             "stw": args.gate_stw, "hdg": args.gate_hdg}
    segs = segments(rows, args.window, gates)
    print("{} steady-state segments of {}s ({:.0f}% of logged time).".format(
        len(segs), args.window, 100.0 * len(segs) * args.window / max(len(rows), 1)))
    if not segs:
        print("Nothing steady. Loosen with --window 40, or check the data.")
        return 1

    solved = tack_check(segs, args.offset_search)
    if args.apply_offset and solved:
        print("\nRebuilding with the solved offset applied.")
        for s_ in segs:
            awa = s_["awa"] - solved * s_["tack"]
            s_["twa"], s_["tws"] = true_wind(awa, s_["aws"], s_["stw"])

    cells = bin_segments(segs)
    coverage_map(cells, args.min_samples)

    table = build_table(cells, args.min_samples, args.percentile)
    measured = sum(1 for tws in TWS_BINS for twa in TWA_BINS if table[tws][twa])
    ratio = bias_vs_estimate(table)
    if ratio:
        print("\nMeasured vs estimated: ratio {:.3f} across {} cells.".format(
            ratio, measured))
        print("  She is {:.1f}% {} than the design estimate; unfilled cells".format(
            abs(ratio - 1) * 100, "quicker" if ratio > 1 else "slower"))
        print("  are rescaled to match.")
    table = fill_from_estimate(table, ratio)

    print_table(table)
    best_vmg(table)
    write_pol(table, args.out)
    print("\nWrote {} ({}/{} cells measured, {:.0f}th percentile).".format(
        args.out, measured, len(TWS_BINS) * len(TWA_BINS),
        args.percentile))
    return 0


if __name__ == "__main__":
    sys.exit(main())
