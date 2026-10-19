"""Land map for Harvest Watch routing: 1 arc-minute (~1 nm) cells over the
forecast region, from Natural Earth 1:10m land + minor islands. A cell counts
as land if any part of it is land (checked at half-minute resolution), so
barrier islands like the Outer Banks stay solid; narrow harbour channels close,
so routes should start and finish in open water. For weather strategy, not
pilotage: shoals and reefs are not in it.
Row 0 is the SOUTH edge, matching the forecast maps. White = land.

Made once; rerun only to change the region or resolution. Download the two
files from github.com/nvkelso/natural-earth-vector (folder geojson), then:
    python3 make_land.py ne_10m_land.geojson ne_10m_minor_islands.geojson
and copy land.png into netlify/ beside index.html."""
import json, sys
import numpy as np
from PIL import Image, ImageDraw
S, W, N, E = 5.0, -100.0, 52.0, -40.0
SUB = 2                       # half-minute rendering, then 2x2 -> 1 minute
R = 60 * SUB
w, h = int((E - W) * R), int((N - S) * R)
img = Image.new('1', (w, h), 0); d = ImageDraw.Draw(img)
def px(ring):
    return [((lon - W) * R, (N - lat) * R) for lon, lat in ring]      # drawn north-up, flipped below
n = 0
for f in sys.argv[1:]:
    for feat in json.load(open(f))['features']:
        g = feat['geometry']; polys = g['coordinates'] if g['type'] == 'MultiPolygon' else [g['coordinates']]
        for poly in polys:
            xs = [p[0] for p in poly[0]]; ys = [p[1] for p in poly[0]]
            if max(xs) < W or min(xs) > E or max(ys) < S or min(ys) > N: continue
            d.polygon(px(poly[0]), fill=1); n += 1
            for hole in poly[1:]: d.polygon(px(hole), fill=0)
a = np.array(img, dtype=np.uint8)[::-1]                              # row 0 = south
a = a.reshape(h // SUB, SUB, w // SUB, SUB).max(axis=(1, 3))         # any land in the cell makes it land
out = Image.fromarray((a * 255).astype(np.uint8), 'L').convert('1')
out.save('land.png', optimize=True)
print(f'{n} polygons; grid {a.shape[1]}x{a.shape[0]}; land {a.mean():.1%}')
