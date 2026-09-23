import os, socket, time, math, random
import json, urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont, ImageOps
from inky.inky_e673 import Inky

VESPER_IP = '192.168.1.167'
VESPER_PORT = 39150
PHOTO_DIR = '/home/harvestmoon/inky/examples/7color/slideshow_photos'
AVG_WINDOW = 300         # seconds of data to average (5 min)
RENDER_EVERY = 300       # redraw the dashboard this often (5 min)
GUST_WINDOW = 3600       # remember the peak wind over this long (1 hour)
MODE_CHECK_EVERY = 20    # check the photo/dashboard flag this often
PHOTO_INTERVAL = 600
POLL_WHEN_OFFLINE = 30
PHOTO_SATURATION = 0.5   # lower to 0.3 for softer color

# Display mode is set from the frame.html web page, stored in Firebase.
MODE_URL = ('https://firestore.googleapis.com/v1/projects/harvest-moon-watch'
            '/databases/(default)/documents/modes/harvest-moon')
WEATHER_URL = ('https://firestore.googleapis.com/v1/projects/harvest-moon-watch'
              '/databases/(default)/documents/weather/harvest-moon')

# Spectra 6 (E673) palette indices
BLACK, WHITE, YELLOW, RED, BLUE, GREEN = 0, 1, 2, 3, 5, 6

# Rolling peak-wind history: list of (epoch, true_wind_kt, true_wind_dir)
gust_history = []
GUST_MAX_KT = 70          # readings above this are treated as garbage
_recent_tws = deque(maxlen=5)   # short buffer to median-filter single-sample spikes

def compass_point(deg):
    dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE',
            'S','SSW','SW','WSW','W','WNW','NW','NNW']
    return dirs[int((deg + 11.25) / 22.5) % 16]

def true_wind(awa_deg, aws, boat_speed):
    awa = math.radians(awa_deg)
    x = aws * math.cos(awa) - boat_speed
    y = aws * math.sin(awa)
    return math.degrees(math.atan2(y, x)) % 360, math.hypot(x, y)

def scalar_mean(vals):
    return sum(vals) / len(vals) if vals else None

def circular_mean(vals):
    # Vector average of angles, so 350 and 10 average to 0, not 180.
    if not vals:
        return None
    x = sum(math.cos(math.radians(v)) for v in vals)
    y = sum(math.sin(math.radians(v)) for v in vals)
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return None
    return math.degrees(math.atan2(y, x)) % 360

def conv_lat(raw, hemi):
    return f"{int(raw[:2])}\u00b0{float(raw[2:]):.3f}'{hemi}"

def conv_lon(raw, hemi):
    return f"{int(raw[:3])}\u00b0{float(raw[3:]):.3f}'{hemi}"

def parse(sentence, d):
    try:
        p = sentence.strip().split('*')[0].split(',')
        t = p[0]
        if t.endswith('MWV') and p[2] == 'R':
            d['awa'] = float(p[1]); d['aws'] = float(p[3])
        elif t.endswith('DPT') and p[1]:
            d['depth'] = float(p[1])
        elif t.endswith('MTW') and p[1]:
            d['temp'] = float(p[1])
        elif t.endswith('VHW') and p[5]:
            d['boatspd'] = float(p[5])
        elif t.endswith('HDG') and p[1]:
            d['hdg_mag'] = float(p[1])
        elif t.endswith('RMC') and p[2] == 'A':
            d['sog'] = float(p[7]) if p[7] else None
            d['cog'] = float(p[8]) if p[8] else None
            d['lat'] = conv_lat(p[3], p[4])
            d['lon'] = conv_lon(p[5], p[6])
            if p[10]:
                d['var'] = float(p[10]) * (-1 if p[11] == 'W' else 1)
            try:
                dt = datetime.strptime(p[9] + p[1].split('.')[0], "%d%m%y%H%M%S")
                d['time'] = dt.replace(tzinfo=timezone.utc).astimezone().strftime("%H:%M")
            except: pass
    except: pass
    return d

# Keys we average over the window
AVG_KEYS = ('awa', 'aws', 'boatspd', 'depth', 'temp', 'cog', 'sog', 'hdg_mag')

def nmea_ok(line):
    # Validate the NMEA 0183 checksum so corrupted sentences (which produce
    # garbage wind spikes) are rejected before they reach the gust/averages.
    line = line.strip()
    if not line or line[0] != '$':
        return False
    if '*' not in line:
        return True                     # some talkers omit it; accept
    body, _, ck = line[1:].partition('*')
    try:
        want = int(ck[:2], 16)
    except ValueError:
        return False
    got = 0
    for ch in body:
        got ^= ord(ch)
    return got == want

def _median(vals):
    s = sorted(vals); n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

def ingest(line, samples, latest, now):
    if not nmea_ok(line):
        return                              # drop corrupted sentences
    cur = {}
    parse(line, cur)
    for k, v in cur.items():
        if v is None:
            continue
        # Plausibility bounds: reject values this boat can't actually produce,
        # so a checksum-passing-but-glitchy reading can't poison the data.
        if k == 'aws' and not (0 <= v <= GUST_MAX_KT): continue
        if k == 'boatspd' and not (0 <= v <= 30): continue
        if k == 'sog' and not (0 <= v <= 30): continue
        if k == 'depth' and not (0 < v < 150): continue
        if k == 'temp' and not (-5 < v < 45): continue
        latest[k] = v                       # newest value of everything
        if k in AVG_KEYS:
            samples[k].append((now, v))     # timestamped history for averaging
    # Gust: median-filter the last few true-wind samples so a single spike
    # (a real anemometer glitch) can't define the hour's peak.
    if 'awa' in cur and 'aws' in cur and 0 <= cur['aws'] <= GUST_MAX_KT:
        twa, tws = true_wind(cur['awa'], cur['aws'], latest.get('boatspd', 0.0))
        twd = (latest.get('hdg_mag', 0.0) + latest.get('var', 0.0) + twa) % 360
        _recent_tws.append(tws)
        if len(_recent_tws) >= 3:
            g = _median(_recent_tws)
            if 0 <= g <= GUST_MAX_KT:
                gust_history.append((now, g, twd))

def compute_display(samples, latest, now):
    d = {}
    d['time'] = latest.get('time', '--:--')
    d['lat'] = latest.get('lat', '--')
    d['lon'] = latest.get('lon', '--')
    d['var'] = latest.get('var', 0.0)

    def vals(k):
        return [v for (t, v) in samples.get(k, [])]

    if vals('aws'):     d['aws'] = scalar_mean(vals('aws'))
    if vals('awa'):     d['awa'] = circular_mean(vals('awa'))
    if vals('boatspd'): d['boatspd'] = scalar_mean(vals('boatspd'))
    if vals('hdg_mag'): d['hdg_mag'] = circular_mean(vals('hdg_mag'))
    if vals('depth'):   d['depth'] = scalar_mean(vals('depth'))
    if vals('temp'):    d['temp'] = scalar_mean(vals('temp'))
    if vals('cog'):     d['cog'] = circular_mean(vals('cog'))
    if vals('sog'):     d['sog'] = scalar_mean(vals('sog'))

    # Gust: peak true wind over the last hour.
    global gust_history
    cutoff = now - GUST_WINDOW
    gust_history = [g for g in gust_history if g[0] >= cutoff]
    if gust_history:
        gt, gtws, gtwd = max(gust_history, key=lambda g: g[1])
        d['gust'] = (gtws, gtwd, datetime.fromtimestamp(gt).strftime("%H:%M"))
    return d

def collect_and_average(display):
    # Returns an averaged dict, None (Vesper offline), or 'PHOTO' (mode changed).
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((VESPER_IP, VESPER_PORT))
    except Exception:
        return None
    sock.settimeout(6)
    samples = defaultdict(list)
    latest = {}
    buf = ''
    start = time.time()
    last_mode_check = start
    try:
        while time.time() - start < RENDER_EVERY:
            try:
                chunk = sock.recv(1024).decode('ascii', errors='ignore')
            except socket.timeout:
                chunk = ''
            if chunk:
                buf += chunk
                lines = buf.split('\n')
                buf = lines[-1]
                for line in lines[:-1]:
                    ingest(line, samples, latest, time.time())
            now = time.time()
            if now - last_mode_check >= MODE_CHECK_EVERY:
                last_mode_check = now
                if get_mode() == 'photo':
                    return 'PHOTO'
    except Exception:
        pass
    finally:
        try: sock.close()
        except Exception: pass
    return compute_display(samples, latest, time.time()) if samples else None

def tile(draw, x, y, label, value, sub, fonts, val_color=BLACK):
    f_lbl, f_val, f_sub = fonts
    draw.text((x, y), label, font=f_lbl, fill=BLUE)
    draw.text((x, y + 26), value, font=f_val, fill=val_color)
    if sub:
        draw.text((x, y + 84), sub, font=f_sub, fill=BLACK)

def render_dashboard(display, d):
    img = Image.new('P', display.resolution, WHITE)
    draw = ImageDraw.Draw(img)
    F = '/usr/share/fonts/truetype/dejavu/'
    f_hdr = ImageFont.truetype(F + 'DejaVuSans-Bold.ttf', 30)
    f_lbl = ImageFont.truetype(F + 'DejaVuSans.ttf', 22)
    f_val = ImageFont.truetype(F + 'DejaVuSans-Bold.ttf', 50)
    f_sub = ImageFont.truetype(F + 'DejaVuSans.ttf', 26)
    fonts = (f_lbl, f_val, f_sub)

    draw.text((20, 12), d.get('time', '--:--'), font=f_hdr, fill=BLACK)
    pos = f"{d.get('lat','--')}   {d.get('lon','--')}"
    draw.text((150, 18), pos, font=f_lbl, fill=BLACK)
    draw.line([(20, 62), (780, 62)], fill=BLACK, width=2)

    if 'awa' in d and 'aws' in d:
        twa, tws = true_wind(d['awa'], d['aws'], d.get('boatspd', 0.0))
        hdg_true = d.get('hdg_mag', 0) + d.get('var', 0)
        twd = (hdg_true + twa) % 360
        wind_val = f"{tws:.1f} kt"                            # big: speed
        wind_sub = f"{compass_point(twd)}  {twd:.0f}\u00b0"   # small: direction
    else:
        wind_val, wind_sub = '--', ''

    col = [20, 290, 555]
    row1, row2 = 85, 265

    temp_f = f"{d['temp'] * 9/5 + 32:.0f}\u00b0F" if 'temp' in d else '--'
    depth_ft = f"{d['depth'] * 3.28084:.0f} ft" if 'depth' in d else '--'

    tile(draw, col[0], row1, "TRUE WIND", wind_val, wind_sub, fonts, val_color=RED)
    tile(draw, col[1], row1, "DEPTH", depth_ft, '', fonts)
    tile(draw, col[2], row1, "WATER TEMP", temp_f, '', fonts)
    tile(draw, col[0], row2, "COG",
         f"{d['cog']:.0f}\u00b0" if d.get('cog') is not None else '--', '', fonts)
    tile(draw, col[1], row2, "SOG",
         f"{d['sog']:.1f} kt" if d.get('sog') is not None else '--', '', fonts)
    tile(draw, col[2], row2, "BOAT SPD",
         f"{d.get('boatspd',0):.1f} kt" if 'boatspd' in d else '--', '', fonts)

    # Gust line along the bottom
    g = d.get('gust')
    if g:
        gtws, gtwd, gtime = g
        draw.text((20, 428), "GUST", font=f_lbl, fill=BLUE)
        draw.text((130, 426),
                  f"{gtws:.1f} kt   {compass_point(gtwd)} {gtwd:.0f}\u00b0   @ {gtime}",
                  font=f_sub, fill=RED)

    display.set_image(img)
    display.show()

def get_weather():
    try:
        with urllib.request.urlopen(WEATHER_URL, timeout=6) as r:
            f = json.load(r).get('fields', {})
    except Exception:
        return None
    def s(k):
        return f.get(k, {}).get('stringValue')
    def n(k):
        v = f.get(k, {})
        if 'integerValue' in v: return int(v['integerValue'])
        if 'doubleValue' in v: return v['doubleValue']
        return None
    return {
        'city': s('city'), 'state': s('state'),
        'nowTemp': n('nowTemp'), 'nowWind': s('nowWind'), 'nowSky': s('nowSky'),
        'pressureMb': n('pressureMb'), 'pressureTrend': s('pressureTrend'),
        'sunrise': s('sunrise'), 'sunset': s('sunset'), 'moon': s('moon'),
        'tideStation': s('tideStation'), 'tides': s('tides'), 'forecast': s('forecast'),
        # Provenance: which point the forecast actually came from and when it
        # last succeeded. A Firestore PATCH never deletes, so a stale forecast
        # otherwise sits here looking current.
        'forecastFrom': s('forecastFrom'), 'forecastAt': n('forecastAt'),
        'tideStationNm': n('tideStationNm'),
        'tideNowFt': n('tideNowFt'), 'tideRiseToMaxFt': n('tideRiseToMaxFt'),
        'tideMaxAt': s('tideMaxAt'),
    }

def wx_category(sky):
    s = (sky or '').lower()
    if 'thunder' in s or 'storm' in s: return 'storm'
    if 'snow' in s or 'sleet' in s or 'flurr' in s: return 'snow'
    if 'rain' in s or 'shower' in s or 'drizzle' in s: return 'rain'
    if 'fog' in s or 'haze' in s or 'mist' in s or 'smoke' in s: return 'fog'
    if 'partly' in s or 'few' in s or 'mostly sunny' in s: return 'partly'
    if 'overcast' in s or 'cloud' in s: return 'cloud'
    if 'sunny' in s or 'clear' in s or 'fair' in s: return 'sun'
    return 'cloud'

def _sun(draw, ox, oy, rr):
    w = max(2, int(rr * 0.35))
    for a in range(0, 360, 45):
        ca, sa = math.cos(math.radians(a)), math.sin(math.radians(a))
        draw.line([(ox + ca*rr*1.5, oy + sa*rr*1.5), (ox + ca*rr*2.15, oy + sa*rr*2.15)], fill=YELLOW, width=w)
    draw.ellipse([ox-rr, oy-rr, ox+rr, oy+rr], fill=YELLOW)

def _cloud(draw, ox, oy, s, fill=WHITE):
    w = s*0.5; h = s*0.3; t = max(2, int(s*0.05))
    bumps = [(-w*0.4, h*0.05, h*0.62), (0.0, -h*0.4, h*0.85), (w*0.42, h*0.0, h*0.68)]
    base = [ox-w*0.62, oy+h*0.1, ox+w*0.66, oy+h*0.95]
    for bx, by, br in bumps:                              # black silhouette
        draw.ellipse([ox+bx-br, oy+by-br, ox+bx+br, oy+by+br], fill=BLACK)
    draw.rectangle(base, fill=BLACK)
    for bx, by, br in bumps:                              # white interior (outline effect)
        draw.ellipse([ox+bx-br+t, oy+by-br+t, ox+bx+br-t, oy+by+br-t], fill=fill)
    draw.rectangle([base[0]+t, base[1]+t, base[2]-t, base[3]-t], fill=fill)

def draw_wx_icon(draw, cx, cy, s, sky):
    cat = wx_category(sky)
    if cat == 'sun':
        _sun(draw, cx, cy, s*0.28)
    elif cat == 'partly':
        _sun(draw, cx - s*0.18, cy - s*0.16, s*0.2)
        _cloud(draw, cx + s*0.08, cy + s*0.08, s*0.85)
    elif cat == 'cloud':
        _cloud(draw, cx, cy, s)
    elif cat == 'rain':
        _cloud(draw, cx, cy - s*0.08, s)
        for i in range(3):
            x = cx - s*0.22 + i*s*0.22
            draw.line([(x, cy+s*0.28), (x - s*0.08, cy+s*0.5)], fill=BLUE, width=max(2, int(s*0.06)))
    elif cat == 'storm':
        _cloud(draw, cx, cy - s*0.08, s)
        draw.polygon([(cx-s*0.02, cy+s*0.22), (cx+s*0.12, cy+s*0.22),
                      (cx+s*0.02, cy+s*0.40), (cx+s*0.14, cy+s*0.40),
                      (cx-s*0.06, cy+s*0.64), (cx+s*0.0, cy+s*0.44),
                      (cx-s*0.10, cy+s*0.44)], fill=RED)
    elif cat == 'snow':
        _cloud(draw, cx, cy - s*0.08, s)
        for i in range(3):
            x = cx - s*0.22 + i*s*0.22
            draw.ellipse([x-s*0.04, cy+s*0.34, x+s*0.04, cy+s*0.42], fill=BLACK)
    elif cat == 'fog':
        for i in range(4):
            y = cy - s*0.24 + i*s*0.16
            draw.line([(cx-s*0.4, y), (cx+s*0.4, y)], fill=BLACK, width=max(2, int(s*0.06)))

def render_info(display, w):
    img = Image.new('P', display.resolution, WHITE)
    draw = ImageDraw.Draw(img)
    F = '/usr/share/fonts/truetype/dejavu/'
    f_hdr = ImageFont.truetype(F + 'DejaVuSans-Bold.ttf', 30)
    f_lbl = ImageFont.truetype(F + 'DejaVuSans.ttf', 20)
    f_big = ImageFont.truetype(F + 'DejaVuSans-Bold.ttf', 44)
    f_med = ImageFont.truetype(F + 'DejaVuSans.ttf', 24)
    f_sub = ImageFont.truetype(F + 'DejaVuSans.ttf', 22)

    place = w.get('city') or 'Harvest Moon'
    if w.get('state'): place += ', ' + w['state']
    draw.text((20, 12), place, font=f_hdr, fill=BLACK)
    draw.text((650, 18), datetime.now().strftime("%-I:%M%p").lower(), font=f_lbl, fill=BLACK)
    draw.line([(20, 58), (780, 58)], fill=BLACK, width=2)

    LX, RX = 20, 415

    # ── LEFT column: NOW, then FORECAST ──
    draw.text((LX, 72), "NOW", font=f_lbl, fill=BLUE)
    t = w.get('nowTemp')
    draw.text((LX, 96), (f"{t}°F" if t is not None else "--"), font=f_big, fill=RED)
    draw_wx_icon(draw, LX + 205, 120, 54, w.get('nowSky'))
    line2 = " ".join(v for v in [w.get('nowWind'), w.get('nowSky')] if v)
    draw.text((LX, 150), line2, font=f_sub, fill=BLACK)
    pr, tr = w.get('pressureMb'), (w.get('pressureTrend') or '')
    arrow = {'rising': '↑', 'falling': '↓', 'steady': '→'}.get(tr, '')
    if pr is not None:
        draw.text((LX, 180), f"{pr:.0f} mb  {arrow} {tr}".rstrip(), font=f_sub, fill=BLACK)

    f_note = ImageFont.truetype(F + 'DejaVuSans.ttf', 17)

    # FORECAST heading carries its source: when we are out on the water there
    # is no NWS grid at the boat, so this is the shore point it fell back to.
    src = (w.get('forecastFrom') or '')
    head = "FORECAST"
    if src and src != 'boat position':
        head += " \u00b7 " + src[:22]
    draw.text((LX, 245), head, font=f_lbl, fill=BLUE)
    yy = 273
    for d in [x.strip() for x in (w.get('forecast') or '').split(' | ') if x.strip()][:3]:
        draw.text((LX + 10, yy), d, font=f_sub, fill=BLACK)
        yy += 28
    # Age, shown only when the forecast is old enough to mislead.
    fa = w.get('forecastAt')
    if fa:
        hrs = (time.time() - fa / 1000.0) / 3600.0
        if hrs >= 3:
            draw.text((LX + 10, yy), f"forecast {int(hrs)}h old", font=f_note, fill=RED)

    # ── RIGHT column: SUN & MOON, then TIDES ──
    draw.text((RX, 72), "SUN & MOON", font=f_lbl, fill=BLUE)
    draw.text((RX, 100), f"Sunrise  {w.get('sunrise','--')}", font=f_med, fill=BLACK)
    draw.text((RX, 130), f"Sunset   {w.get('sunset','--')}", font=f_med, fill=BLACK)
    draw.text((RX, 162), w.get('moon') or '', font=f_sub, fill=BLACK)

    station = (w.get('tideStation') or '').title()
    nm = w.get('tideStationNm')
    label = ("TIDES \u00b7 " + station).strip(' \u00b7')
    if nm is not None:
        label += f" ({nm:.0f} nm)" if nm >= 1 else " (here)"
    draw.text((RX, 220), label, font=f_lbl, fill=BLUE)
    yy = 250
    for e in [x.strip() for x in (w.get('tides') or '').split(' / ') if x.strip()][:4]:
        color = BLUE if e.upper().startswith('HIGH') else BLACK
        draw.text((RX + 10, yy), e, font=f_med, fill=color)
        yy += 32
    # Where the water is right now and what is still to come - the number the
    # scope calculation actually turns on.
    tn, rise = w.get('tideNowFt'), w.get('tideRiseToMaxFt')
    if tn is not None:
        line = f"now {tn:.1f} ft"
        if rise is not None and rise >= 0.1:
            line += f"  \u2191 {rise:.1f} ft by {w.get('tideMaxAt') or 'high'}"
        draw.text((RX + 10, yy + 2), line, font=f_note, fill=BLACK)

    display.set_image(img)
    display.show()

# Alternate photo / info on the slideshow so the info screen comes around
# every other slide. Falls back to a photo if weather isn't available yet.
slide_toggle = 1
def show_slide(display):
    global slide_toggle
    slide_toggle ^= 1
    if slide_toggle == 1:
        w = get_weather()
        if w:
            render_info(display, w)
            return
    show_photo(display)

def show_photo(display):
    photos = [f for f in os.listdir(PHOTO_DIR)
              if f.lower().endswith(('.jpg', '.jpeg'))]
    if not photos:
        return
    photo_path = os.path.join(PHOTO_DIR, random.choice(photos))
    img = Image.open(photo_path).convert("RGB")
    img = ImageOps.pad(img, display.resolution, color="black")
    display.set_image(img, saturation=PHOTO_SATURATION)
    display.show()

def get_mode():
    # Reads the mode set from the frame.html control page. Any network
    # trouble returns 'auto', so a hiccup just keeps the current behavior.
    try:
        with urllib.request.urlopen(MODE_URL, timeout=4) as r:
            m = json.load(r).get('fields', {}).get('mode', {}).get('stringValue', 'auto')
        return m if m in ('auto', 'photo', 'dashboard', 'info') else 'auto'
    except Exception:
        return 'auto'

display = Inky()
mode = None
last_photo = 0
print("Frame controller started.")

while True:
    wanted = get_mode()

    # Forced photo mode: slideshow with the info screen interspersed.
    if wanted == 'photo':
        now = time.time()
        if mode != 'photo' or (now - last_photo) >= PHOTO_INTERVAL:
            print("Mode=photo - slide.", flush=True)
            show_slide(display)
            last_photo = now
        mode = 'photo'
        time.sleep(POLL_WHEN_OFFLINE)
        continue

    # Forced info mode: just the weather/tide screen.
    if wanted == 'info':
        now = time.time()
        if mode != 'info' or (now - last_photo) >= PHOTO_INTERVAL:
            print("Mode=info.", flush=True)
            w = get_weather()
            render_info(display, w) if w else show_photo(display)
            last_photo = now
        mode = 'info'
        time.sleep(POLL_WHEN_OFFLINE)
        continue

    # 'auto' or 'dashboard': gather ~5 min of data, average it, and draw.
    result = collect_and_average(display)
    if result == 'PHOTO':
        continue
    if result:
        print("Dashboard (5-min avg).", flush=True)
        render_dashboard(display, result)
        mode = 'dash'
    else:
        now = time.time()
        if mode != 'photo' or (now - last_photo) >= PHOTO_INTERVAL:
            print("Vesper offline - slide.", flush=True)
            show_slide(display)
            last_photo = now
        mode = 'photo'
        time.sleep(POLL_WHEN_OFFLINE)
