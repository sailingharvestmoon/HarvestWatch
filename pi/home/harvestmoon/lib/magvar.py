#!/usr/bin/env python3
"""
Magnetic variation (declination) from the World Magnetic Model
--------------------------------------------------------------
The Vesper sends heading as MAGNETIC only and leaves the variation field in its
RMC sentence empty, so without this every "true" direction the Pi works out
(true heading, true wind direction, gust direction, the bow position the anchor
watch measures from) was really magnetic, or used a number typed into the watch
page that went stale as the boat moved: about 15 W in midcoast Maine, 11 W at
Norfolk, 9 W in the Bahamas.

This works it out from the boat's position and today's date with WMM2025, the
same model NOAA and chart plotters use. No internet, nothing to install.

    import magvar
    magvar.variation(36.95, -76.30)     # -> about -10.8  (degrees; West is negative)

Sign: East positive, West negative - the same as the watch page setting.
True = magnetic + variation.

Check it by hand on the Pi:   python3 ~/lib/magvar.py 36.95 -76.30

Model: WMM2025 (NOAA NCEI, public domain), good from 2025.0 to 2030.0. NOAA
publishes the next model (WMM2030) around December 2029: paste its .COF file
over _COF below and change EPOCH. Until then the answer drifts by only a few
hundredths of a degree a year after 2030.

The maths is a direct port of NOAA's geomag.c (spherical harmonic expansion
to degree 12, geodetic to geocentric conversion on the WGS-84 ellipsoid).
Standard library only.
"""

import math, sys, time
from datetime import datetime, timezone

EPOCH = 2025.0
VALID_UNTIL = 2030.0
MAXORD = 12

# WMM2025 coefficients: n  m  g  h  g-dot  h-dot  (nT, nT/yr)
_COF = """
  1  0  -29351.8       0.0       12.0        0.0
  1  1   -1410.8    4545.4        9.7      -21.5
  2  0   -2556.6       0.0      -11.6        0.0
  2  1    2951.1   -3133.6       -5.2      -27.7
  2  2    1649.3    -815.1       -8.0      -12.1
  3  0    1361.0       0.0       -1.3        0.0
  3  1   -2404.1     -56.6       -4.2        4.0
  3  2    1243.8     237.5        0.4       -0.3
  3  3     453.6    -549.5      -15.6       -4.1
  4  0     895.0       0.0       -1.6        0.0
  4  1     799.5     278.6       -2.4       -1.1
  4  2      55.7    -133.9       -6.0        4.1
  4  3    -281.1     212.0        5.6        1.6
  4  4      12.1    -375.6       -7.0       -4.4
  5  0    -233.2       0.0        0.6        0.0
  5  1     368.9      45.4        1.4       -0.5
  5  2     187.2     220.2        0.0        2.2
  5  3    -138.7    -122.9        0.6        0.4
  5  4    -142.0      43.0        2.2        1.7
  5  5      20.9     106.1        0.9        1.9
  6  0      64.4       0.0       -0.2        0.0
  6  1      63.8     -18.4       -0.4        0.3
  6  2      76.9      16.8        0.9       -1.6
  6  3    -115.7      48.8        1.2       -0.4
  6  4     -40.9     -59.8       -0.9        0.9
  6  5      14.9      10.9        0.3        0.7
  6  6     -60.7      72.7        0.9        0.9
  7  0      79.5       0.0       -0.0        0.0
  7  1     -77.0     -48.9       -0.1        0.6
  7  2      -8.8     -14.4       -0.1        0.5
  7  3      59.3      -1.0        0.5       -0.8
  7  4      15.8      23.4       -0.1        0.0
  7  5       2.5      -7.4       -0.8       -1.0
  7  6     -11.1     -25.1       -0.8        0.6
  7  7      14.2      -2.3        0.8       -0.2
  8  0      23.2       0.0       -0.1        0.0
  8  1      10.8       7.1        0.2       -0.2
  8  2     -17.5     -12.6        0.0        0.5
  8  3       2.0      11.4        0.5       -0.4
  8  4     -21.7      -9.7       -0.1        0.4
  8  5      16.9      12.7        0.3       -0.5
  8  6      15.0       0.7        0.2       -0.6
  8  7     -16.8      -5.2       -0.0        0.3
  8  8       0.9       3.9        0.2        0.2
  9  0       4.6       0.0       -0.0        0.0
  9  1       7.8     -24.8       -0.1       -0.3
  9  2       3.0      12.2        0.1        0.3
  9  3      -0.2       8.3        0.3       -0.3
  9  4      -2.5      -3.3       -0.3        0.3
  9  5     -13.1      -5.2        0.0        0.2
  9  6       2.4       7.2        0.3       -0.1
  9  7       8.6      -0.6       -0.1       -0.2
  9  8      -8.7       0.8        0.1        0.4
  9  9     -12.9      10.0       -0.1        0.1
 10  0      -1.3       0.0        0.1        0.0
 10  1      -6.4       3.3        0.0        0.0
 10  2       0.2       0.0        0.1       -0.0
 10  3       2.0       2.4        0.1       -0.2
 10  4      -1.0       5.3       -0.0        0.1
 10  5      -0.6      -9.1       -0.3       -0.1
 10  6      -0.9       0.4        0.0        0.1
 10  7       1.5      -4.2       -0.1        0.0
 10  8       0.9      -3.8       -0.1       -0.1
 10  9      -2.7       0.9       -0.0        0.2
 10 10      -3.9      -9.1       -0.0       -0.0
 11  0       2.9       0.0        0.0        0.0
 11  1      -1.5       0.0       -0.0       -0.0
 11  2      -2.5       2.9        0.0        0.1
 11  3       2.4      -0.6        0.0       -0.0
 11  4      -0.6       0.2        0.0        0.1
 11  5      -0.1       0.5       -0.1       -0.0
 11  6      -0.6      -0.3        0.0       -0.0
 11  7      -0.1      -1.2       -0.0        0.1
 11  8       1.1      -1.7       -0.1       -0.0
 11  9      -1.0      -2.9       -0.1        0.0
 11 10      -0.2      -1.8       -0.1        0.0
 11 11       2.6      -2.3       -0.1        0.0
 12  0      -2.0       0.0        0.0        0.0
 12  1      -0.2      -1.3        0.0       -0.0
 12  2       0.3       0.7       -0.0        0.0
 12  3       1.2       1.0       -0.0       -0.1
 12  4      -1.3      -1.4       -0.0        0.1
 12  5       0.6      -0.0       -0.0       -0.0
 12  6       0.6       0.6        0.1       -0.0
 12  7       0.5      -0.1       -0.0       -0.0
 12  8      -0.1       0.8        0.0        0.0
 12  9      -0.4       0.1        0.0       -0.0
 12 10      -0.2      -1.0       -0.1       -0.0
 12 11      -1.3       0.1       -0.0        0.0
 12 12      -0.7       0.2       -0.1       -0.1
"""

def _setup():
    # Read the coefficients and convert them from Schmidt semi-normalised to
    # unnormalised form, once, at import (geomag.c's E0000 block).
    N = MAXORD + 1
    c = [[0.0] * N for _ in range(N)]     # g in c[m][n], h in c[n][m-1]
    cd = [[0.0] * N for _ in range(N)]    # their yearly change
    for line in _COF.strip().splitlines():
        f = line.split()
        n, m = int(f[0]), int(f[1])
        g, h, gd, hd = (float(x) for x in f[2:6])
        c[m][n], cd[m][n] = g, gd
        if m != 0:
            c[n][m - 1], cd[n][m - 1] = h, hd
    snorm = [[0.0] * N for _ in range(N)]
    k = [[0.0] * N for _ in range(N)]
    fn = [0.0] * N
    fm = [0.0] * N
    snorm[0][0] = 1.0
    for n in range(1, N):
        snorm[0][n] = snorm[0][n - 1] * (2 * n - 1) / n
        j = 2
        for m in range(0, n + 1):
            k[m][n] = ((n - 1) * (n - 1) - m * m) / ((2 * n - 1) * (2 * n - 3))
            if m > 0:
                snorm[m][n] = snorm[m - 1][n] * math.sqrt((n - m + 1) * j / (n + m))
                j = 1
                c[n][m - 1] *= snorm[m][n]
                cd[n][m - 1] *= snorm[m][n]
            c[m][n] *= snorm[m][n]
            cd[m][n] *= snorm[m][n]
        fn[n] = n + 1.0
        fm[n] = float(n)
    k[1][1] = 0.0
    return c, cd, k, fn, fm

_C, _CD, _K, _FN, _FM = _setup()

def declination(lat, lon, year, alt_km=0.0):
    """Declination in degrees (East +) at a geodetic lat/lon, decimal year, height in km."""
    c, cd, k, fn, fm = _C, _CD, _K, _FN, _FM
    N = MAXORD + 1
    dt = year - EPOCH
    a, b, re = 6378.137, 6356.7523142, 6371.2       # WGS-84 axes, reference radius (km)
    a2, b2 = a * a, b * b
    c2 = a2 - b2
    a4, b4 = a2 * a2, b2 * b2
    c4 = a4 - b4

    rlat, rlon = math.radians(lat), math.radians(lon)
    srlat, crlat = math.sin(rlat), math.cos(rlat)
    srlon, crlon = math.sin(rlon), math.cos(rlon)
    srlat2, crlat2 = srlat * srlat, crlat * crlat
    sp = [0.0] * N
    cp = [0.0] * N
    cp[0] = 1.0
    sp[1], cp[1] = srlon, crlon

    # Geodetic -> spherical (geocentric) coordinates
    alt = alt_km
    q = math.sqrt(a2 - c2 * srlat2)
    q1 = alt * q
    q2 = ((q1 + a2) / (q1 + b2)) ** 2
    ct = srlat / math.sqrt(q2 * crlat2 + srlat2)
    st = math.sqrt(max(0.0, 1.0 - ct * ct))
    r = math.sqrt(alt * alt + 2.0 * q1 + (a4 - c4 * srlat2) / (q * q))
    d = math.sqrt(a2 * crlat2 + b2 * srlat2)
    ca = (alt + d) / r
    sa = c2 * crlat * srlat / (r * d)

    for m in range(2, N):
        sp[m] = sp[1] * cp[m - 1] + cp[1] * sp[m - 1]
        cp[m] = cp[1] * cp[m - 1] - sp[1] * sp[m - 1]

    aor = re / r
    ar = aor * aor
    br = bt = bp = bpp = 0.0
    p = [[0.0] * N for _ in range(N)]
    dp = [[0.0] * N for _ in range(N)]
    pp = [0.0] * N
    p[0][0] = 1.0
    pp[0] = 1.0
    for n in range(1, N):
        ar *= aor
        for m in range(0, n + 1):
            # Unnormalised associated Legendre functions and derivatives
            if n == m:
                p[m][n] = st * p[m - 1][n - 1]
                dp[m][n] = st * dp[m - 1][n - 1] + ct * p[m - 1][n - 1]
            elif n == 1 and m == 0:
                p[m][n] = ct * p[m][n - 1]
                dp[m][n] = ct * dp[m][n - 1] - st * p[m][n - 1]
            else:
                if m > n - 2:
                    p[m][n - 2] = 0.0
                    dp[m][n - 2] = 0.0
                p[m][n] = ct * p[m][n - 1] - k[m][n] * p[m][n - 2]
                dp[m][n] = ct * dp[m][n - 1] - st * p[m][n - 1] - k[m][n] * dp[m][n - 2]
            # Coefficients moved on to the date asked for
            g = c[m][n] + dt * cd[m][n]
            par = ar * p[m][n]
            if m == 0:
                temp1 = g * cp[m]
                temp2 = g * sp[m]
            else:
                h = c[n][m - 1] + dt * cd[n][m - 1]
                temp1 = g * cp[m] + h * sp[m]
                temp2 = g * sp[m] - h * cp[m]
            bt -= ar * temp1 * dp[m][n]
            bp += fm[m] * temp2 * par
            br += fn[n] * temp1 * par
            if st == 0.0 and m == 1:          # exactly at a geographic pole
                pp[n] = pp[n - 1] if n == 1 else ct * pp[n - 1] - k[m][n] * pp[n - 2]
                bpp += fm[m] * temp2 * ar * pp[n]
    bp = bpp if st == 0.0 else bp / st

    # Spherical -> geodetic field components; declination from north and east
    bx = -bt * ca - br * sa
    by = bp
    return math.degrees(math.atan2(by, bx))

def decimal_year(t=None):
    """A Unix time (default now) as a decimal year, e.g. 2026.85."""
    dt = datetime.fromtimestamp(time.time() if t is None else t, tz=timezone.utc)
    start = datetime(dt.year, 1, 1, tzinfo=timezone.utc)
    end = datetime(dt.year + 1, 1, 1, tzinfo=timezone.utc)
    return dt.year + (dt - start).total_seconds() / (end - start).total_seconds()

_cache = {}

def variation(lat, lon, t=None):
    """
    Magnetic variation in degrees for a boat at lat/lon (East +, West -).
    Raises ValueError for an impossible position.

    Cached on a 0.1 degree grid (about 6 nm) and a few days, so calling it for
    every NMEA sentence costs a dictionary lookup. Variation changes by well
    under 0.1 degree across one grid square here.
    """
    lat, lon = float(lat), float(lon)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or lat != lat or lon != lon:
        raise ValueError(f"bad position {lat}, {lon}")
    key = (round(lat, 1), round(lon, 1), round(decimal_year(t), 2))
    v = _cache.get(key)
    if v is None:
        if len(_cache) > 256:
            _cache.clear()
        v = declination(key[0], key[1], key[2])
        _cache[key] = v
    return v

if __name__ == "__main__":
    if len(sys.argv) == 3:
        la, lo = float(sys.argv[1]), float(sys.argv[2])
        v = variation(la, lo)
        print(f"{la:.4f}, {lo:.4f}: variation {abs(v):.1f} {'W' if v < 0 else 'E'} ({v:+.1f})")
    else:
        for name, la, lo in [("Midcoast Maine", 44.10, -69.10), ("Norfolk", 36.95, -76.30),
                             ("Nassau", 25.08, -77.35)]:
            v = variation(la, lo)
            print(f"{name:15s} {abs(v):4.1f} {'W' if v < 0 else 'E'}")
        print("Usage: python3 magvar.py LAT LON   (decimal degrees, West longitude negative)")
