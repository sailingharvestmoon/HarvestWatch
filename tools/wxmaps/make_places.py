#!/usr/bin/env python3
"""
Harvest Watch - place names for the weather maps (netlify/places.json)
======================================================================
The Maps view draws town names over the map colours itself (wmPlaces in
index.html): no tile server, no key, and it works offline. This makes the
list it draws from. Run it again only to change the places or the spacing.

Source: GeoNames (CC BY 4.0) - every place of 1000+ people - as packed in
the "all-the-cities" npm package (cities.pbf). Read here directly; none of
that package's code is run:

  npm pack all-the-cities@3.1.0 && tar xzf all-the-cities-3.1.0.tgz
  python3 tools/wxmaps/make_places.py package/cities.pbf netlify/places.json

Each place gets the lowest map zoom (3..12) at which its label fits without
overlapping a more important one; places that never fit are left out.
Output: [[name, lat, lon, zoom, big], ...] sorted by zoom, then importance.
big = 1 for the larger lettering (capitals and cities of 250 000+).
"""
import sys, json, math

REGION = (5.0, -100.0, 52.0, -40.0)          # south, west, north, east (as build_maps.py)
ZOOMS = range(3, 13)                          # the Maps view's zoom range
EXTRA = [                                     # small settlements GeoNames' 1000+ list leaves out
    # (name, lat, lon) - add cruising-ground places here as needed
]

def read_pbf(path):
    """all-the-cities' cities.pbf: length-prefixed protobuf records."""
    b = open(path, "rb").read()
    def varint(i):
        r = s = 0
        while True:
            c = b[i]; i += 1; r |= (c & 0x7f) << s; s += 7
            if c < 128: return r, i
    zz = lambda n: (n >> 1) ^ -(n & 1)
    i, lat, lon, out = 0, 0, 0, []
    names = {2: "name", 3: "cc", 4: "alt", 5: "muni", 6: "msub", 7: "fc", 8: "adm"}
    while i < len(b):
        n, i = varint(i); end = i + n; c = {"pop": 0}
        while i < end:
            key, i = varint(i); tag, wt = key >> 3, key & 7
            if wt == 0:
                v, i = varint(i)
                if tag == 9: c["pop"] = v
                elif tag == 10: lon += zz(v); c["lon"] = lon / 1e5
                elif tag == 11: lat += zz(v); c["lat"] = lat / 1e5
            elif wt == 2:
                l, i = varint(i); c[names.get(tag, str(tag))] = b[i:i + l].decode("utf-8", "replace"); i += l
            else:
                raise SystemExit(f"unexpected wire type {wt}")
        out.append(c)
    return out

def world_px(lat, lon, z):
    w = 256 * 2 ** z
    s = math.sin(math.radians(max(-85, min(85, lat))))
    return (lon + 180) / 360 * w, (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * w

def label_box(x, y, name, big):
    # Same layout as wmPlaces: name centred just above a dot. Widths are
    # estimated (bold system font ~0.6 em a letter), with a little margin.
    fs = 13 if big else 12
    w = len(name) * fs * 0.6 + 8
    return (x - w / 2, y - fs - 8, x + w / 2, y + 4)

def main():
    src, dst = sys.argv[1], sys.argv[2]
    s, w, n, e = REGION
    P = [c for c in read_pbf(src) if s <= c["lat"] <= n and w <= c["lon"] <= e and c.get("name")]
    P += [{"name": nm, "lat": la, "lon": lo, "pop": 1000, "fc": "PPL"} for nm, la, lo in EXTRA]
    for c in P:
        cap = c.get("fc") == "PPLC"
        c["big"] = 1 if cap or c["pop"] >= 250000 else 0
        c["score"] = c["pop"] * (8 if cap else 2 if c.get("fc") == "PPLA" else 1)
    P.sort(key=lambda c: -c["score"])
    minz = {}
    for z in ZOOMS:
        cell, grid = 128, {}
        def hits(bx):
            for gx in range(int(bx[0] // cell), int(bx[2] // cell) + 1):
                for gy in range(int(bx[1] // cell), int(bx[3] // cell) + 1):
                    for o in grid.get((gx, gy), ()):
                        if bx[0] < o[2] and o[0] < bx[2] and bx[1] < o[3] and o[1] < bx[3]: return True
            return False
        def put(bx):
            for gx in range(int(bx[0] // cell), int(bx[2] // cell) + 1):
                for gy in range(int(bx[1] // cell), int(bx[3] // cell) + 1):
                    grid.setdefault((gx, gy), []).append(bx)
        for k, c in enumerate(P):                  # already showing from a lower zoom: keep their room
            if k in minz: put(label_box(*world_px(c["lat"], c["lon"], z), c["name"], c["big"]))
        for k, c in enumerate(P):
            if k in minz: continue
            bx = label_box(*world_px(c["lat"], c["lon"], z), c["name"], c["big"])
            if not hits(bx): put(bx); minz[k] = z
    out = sorted(((minz[k], k) for k in minz))
    rows = [[P[k]["name"], round(P[k]["lat"], 4), round(P[k]["lon"], 4), z, P[k]["big"]] for z, k in out]
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write("[\n" + ",\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows) + "\n]\n")
    per = {z: sum(1 for r in rows if r[3] == z) for z in ZOOMS}
    print(f"{len(P)} places in the region, {len(rows)} kept; first shown at zoom: {per}")

if __name__ == "__main__":
    main()
