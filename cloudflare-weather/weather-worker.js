// Harvest Watch - Weather Worker (Cloudflare)
// ===========================================
// All of the weather that used to run on the Pi, running off the boat:
//
//   every 30 min  "Now" summary -> weather/<vessel>
//                 (NWS observation + text forecast, NOAA tides, sun, moon,
//                  pressure trend; Open-Meteo "current" when outside NWS land)
//   every hour    7-model point forecast -> weather/<vessel>-forecast
//   on request    the same, within a minute of the app's Refresh button
//
// It is deliberately a SEPARATE worker from the anchor watcher, so nothing
// here can slow down or break a drag alarm.
//
// Position: the "Now" summary is always for the boat (vessels/<vessel>, the
// last fix the Pi published - still works while the Pi is off, it just
// stops moving). The model forecast is for the place chosen in the app
// (weather/<vessel>-places "active"), or the boat when none is chosen.
//
// Each cron run does at most ONE job, to stay inside the free plan's small
// CPU allowance. Bookkeeping lives in weather/<vessel>-wxstate.
//
// Variables (wrangler.toml [vars]): PROJECT_ID, VESSEL_ID, NWS_UA
// Manual:  https://<worker-url>/?run=now | ?run=forecast | ?status=1

const FALLBACK = { lat: 44.10, lon: -69.10 };            // midcoast Maine
const NOW_EVERY_MIN = 30, FC_EVERY_MIN = 60, FC_MIN_GAP_MIN = 2;

const FC_MODELS = [
  ['ecmwf', ['ecmwf_ifs025', 'ecmwf_ifs']],
  ['gfs',   ['gfs_global', 'gfs_seamless']],                 // pure runs, not the "seamless" blends
  ['ukmo',  ['ukmo_global_deterministic_10km', 'ukmo_seamless']],
  ['icon',  ['icon_global', 'icon_seamless']],
  ['nam',   ['ncep_nam_conus']],
  ['hrrr',  ['ncep_hrrr_conus', 'gfs_hrrr']],
  ['aifs',  ['ecmwf_aifs025_single', 'ecmwf_aifs025']],
];
const FC_VARS = ['weather_code', 'is_day', 'wind_speed_10m', 'wind_gusts_10m', 'wind_direction_10m',
                 'precipitation', 'cape', 'cloud_cover', 'temperature_2m', 'pressure_msl'];
const FC_ROUND = { precipitation: 1, pressure_msl: 1, wind_speed_10m: 1, wind_gusts_10m: 1 };
const FC_DAYS = 7;

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(cfg(env)).catch(e => console.log('tick failed:', String(e && e.stack || e))));
  },
  async fetch(request, env) {
    const e = cfg(env), u = new URL(request.url);
    try {
      if (u.searchParams.get('run') === 'now') return json(await jobNow(e, await getState(e)));
      if (u.searchParams.get('run') === 'forecast') return json(await jobForecast(e, await getState(e)));
      const st = await getState(e);
      return json({ ok: true, lastNow: ago(st.lastNow), lastForecast: ago(st.lastFc), station: st.station,
                    hint: 'Add ?run=now or ?run=forecast to run a job by hand.' });
    } catch (err) { return json({ ok: false, error: String(err && err.stack || err) }, 500); }
  }
};

function cfg(env) {
  const t = v => typeof v === 'string' ? v.trim() : v;
  return { PROJECT_ID: t(env.PROJECT_ID) || 'harvest-moon-watch', VESSEL_ID: t(env.VESSEL_ID) || 'harvest-moon',
           NWS_UA: t(env.NWS_UA) || 'harvest-watch (sailingharvestmoon@gmail.com)' };
}
const json = (o, s = 200) => new Response(JSON.stringify(o, null, 2), { status: s, headers: { 'content-type': 'application/json' } });
const ago = ms => ms ? Math.round((Date.now() - ms) / 60000) + ' min ago' : 'never';

// ─── Firestore (REST, same open rules as everything else) ─────────
const FS = e => `https://firestore.googleapis.com/v1/projects/${e.PROJECT_ID}/databases/(default)/documents`;
async function fsGet(e, path, mask) {
  const q = mask ? '?' + mask.map(m => 'mask.fieldPaths=' + m).join('&') : '';
  const r = await fetch(`${FS(e)}/${path}${q}`);
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`GET ${path} -> ${r.status}`);
  return (await r.json()).fields || {};
}
async function fsWrite(e, path, obj, masked) {
  const fields = {};
  for (const [k, v] of Object.entries(obj)) {
    if (v === null || v === undefined || v === '') continue;
    fields[k] = typeof v === 'boolean' ? { booleanValue: v }
      : Number.isInteger(v) ? { integerValue: String(v) }
      : typeof v === 'number' ? { doubleValue: v } : { stringValue: String(v) };
  }
  // masked: touch only these fields. Unmasked: replace the whole document
  // (as weather.py did) so a field that failed this time is not left behind
  // looking current.
  const q = masked ? '?' + Object.keys(fields).map(k => 'updateMask.fieldPaths=' + k).join('&') : '';
  const r = await fetch(`${FS(e)}/${path}${q}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ fields }) });
  if (!r.ok) throw new Error(`PATCH ${path} -> ${r.status}: ${(await r.text()).slice(0, 200)}`);
}
const num = f => f ? (f.doubleValue !== undefined ? Number(f.doubleValue) : f.integerValue !== undefined ? Number(f.integerValue) : null) : null;
const str = f => f && f.stringValue !== undefined ? f.stringValue : '';
const parse = (s, d) => { try { return JSON.parse(s); } catch (e) { return d; } };

async function getState(e) {
  const f = await fsGet(e, `weather/${e.VESSEL_ID}-wxstate`) || {};
  return { lastNow: num(f.lastNow) || 0, lastFc: num(f.lastFc) || 0,
           station: parse(str(f.station), null), pressLog: parse(str(f.pressLog), []) };
}
async function boatPosition(e) {
  try {
    const f = await fsGet(e, `vessels/${e.VESSEL_ID}`, ['lat', 'lon']);
    const lat = num(f && f.lat), lon = num(f && f.lon);
    if (lat !== null && lon !== null) return { lat, lon };
  } catch (err) {}
  return { ...FALLBACK };
}
async function forecastPosition(e) {
  try {
    const f = await fsGet(e, `weather/${e.VESSEL_ID}-places`, ['active']);
    const a = parse(str(f && f.active), null);
    if (a && a.lat !== undefined && a.lon !== undefined) return { lat: +a.lat, lon: +a.lon, place: a.name || '' };
  } catch (err) {}
  return { ...(await boatPosition(e)), place: '' };
}

// ─── the scheduler ────────────────────────────────────────────────
async function tick(e) {
  const st = await getState(e), now = Date.now();
  let req = 0;
  try { req = num((await fsGet(e, `weather/${e.VESSEL_ID}-forecast`, ['requestAt']) || {}).requestAt) || 0; } catch (err) {}
  const fcDue = now - st.lastFc > FC_EVERY_MIN * 60000 || (req > st.lastFc && now - st.lastFc > FC_MIN_GAP_MIN * 60000);
  if (fcDue) return jobForecast(e, st);
  if (now - st.lastNow > NOW_EVERY_MIN * 60000) return jobNow(e, st);
  return { idle: true };
}

// ═══ JOB: 7-model point forecast ══════════════════════════════════
const round = (a, nd) => (a || []).map(v => v === null || v === undefined ? null : nd ? Math.round(v * 10 ** nd) / 10 ** nd : Math.round(v));
async function omJson(url) {
  const r = await fetch(url); const j = await r.json().catch(() => null);
  if (!r.ok || !j || j.error) throw new Error((j && j.reason) || 'HTTP ' + r.status);
  return j;
}
async function fcModel(apis, lat, lon) {
  let last = 'failed';
  for (const name of apis) {
    try {
      const j = await omJson(`https://api.open-meteo.com/v1/forecast?latitude=${lat.toFixed(4)}&longitude=${lon.toFixed(4)}`
        + `&hourly=${FC_VARS.join(',')}&daily=sunrise,sunset&models=${name}&wind_speed_unit=kn&temperature_unit=fahrenheit`
        + `&precipitation_unit=mm&timezone=auto&forecast_days=${FC_DAYS}`);
      const h = j.hourly || {};
      const ok = (h.wind_speed_10m || []).some(v => v !== null);
      return { j, meta: { api: name, ok, err: ok ? '' : 'no coverage here' } };
    } catch (err) { last = String(err.message || err).slice(0, 120); }
  }
  return { j: null, meta: { ok: false, err: last } };
}
async function jobForecast(e, st) {
  const p = await forecastPosition(e);
  const [res, marine] = await Promise.all([
    Promise.all(FC_MODELS.map(([, apis]) => fcModel(apis, p.lat, p.lon))),
    omJson(`https://marine-api.open-meteo.com/v1/marine?latitude=${p.lat.toFixed(4)}&longitude=${p.lon.toFixed(4)}`
      + `&hourly=wave_height,wave_direction,wave_period&length_unit=imperial&timezone=auto&forecast_days=${FC_DAYS}`).catch(() => null)
  ]);
  const base = res.find(r => r.j && r.j.hourly && r.j.hourly.time);
  if (!base) { await fsWrite(e, `weather/${e.VESSEL_ID}-wxstate`, { lastFc: Date.now() - (FC_EVERY_MIN - 10) * 60000 }, true); return { ok: false, note: 'no model answered' }; }
  const T = base.j.hourly.time, models = {};
  FC_MODELS.forEach(([id], i) => {
    const { j, meta } = res[i];
    if (j && j.hourly) {
      const h = j.hourly, idx = new Map((h.time || []).map((t, k) => [t, k]));
      meta.h = {};
      for (const v of FC_VARS) meta.h[v] = round(T.map(t => { const k = idx.get(t); return k === undefined || !h[v] ? null : h[v][k]; }), FC_ROUND[v] || 0);
    }
    models[id] = meta;
  });
  let mar = null;
  if (marine && marine.hourly && marine.hourly.time) {
    const m = marine.hourly;
    mar = { time: m.time, wave_height: round(m.wave_height, 1), wave_direction: round(m.wave_direction, 0), wave_period: round(m.wave_period, 0) };
  }
  const at = Date.now();
  const data = { at, lat: +p.lat.toFixed(4), lon: +p.lon.toFixed(4), place: p.place,
    off: base.j.utc_offset_seconds || 0, tz: base.j.timezone_abbreviation || '', tzName: base.j.timezone || '',
    time: T, daily: base.j.daily, models, marine: mar };
  // Whole-document write on purpose: it also clears the app's requestAt.
  await fsWrite(e, `weather/${e.VESSEL_ID}-forecast`, { data: JSON.stringify(data), updatedAt: at }, false);
  await fsWrite(e, `weather/${e.VESSEL_ID}-wxstate`, { lastFc: at }, true);
  return { ok: true, models: Object.fromEntries(Object.entries(models).map(([k, v]) => [k, v.ok ? v.api : v.err])) };
}

// ═══ JOB: "Now" summary for the boat and the cabin display ════════
const MPH_TO_KT = 0.868976;
async function getJson(url, headers, tries = 2) {
  let last;
  for (let k = 0; k <= tries; k++) {
    try {
      const r = await fetch(url, { headers: headers || {} });
      if (r.ok) return await r.json();
      last = new Error('HTTP ' + r.status);
    } catch (err) { last = err; }
    await new Promise(res => setTimeout(res, 1500 * (k + 1)));
  }
  throw last;
}
function fmtWind(dir, speed) {
  if (!speed) return dir || '';
  const nums = (String(speed).match(/\d+/g) || []).map(Number);
  const kt = nums.length ? Math.round(Math.max(...nums) * MPH_TO_KT) : 0;
  return `${dir || ''} ${kt}kt`.trim();
}
function shortSky(s) {
  s = String(s || '').split(' then ')[0].replace('Slight Chance ', '').replace('Chance ', '')
    .replace('Showers And Thunderstorms', 'T-storms').replace('Thunderstorms', 'T-storms');
  return s.trim().slice(0, 18);
}
function havKm(a, b, c, d) {
  const R = 6371, r = Math.PI / 180, x = Math.sin((c - a) * r / 2) ** 2 + Math.cos(a * r) * Math.cos(c * r) * Math.sin((d - b) * r / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(x), Math.sqrt(1 - x));
}
// "4:42pm" style, in the boat's time zone, like the Pi's strftime("%-I:%M%p").
function hm(ms, tz, dropZero) {
  const s = new Intl.DateTimeFormat('en-US', { timeZone: tz, hour: 'numeric', minute: '2-digit', hour12: true }).format(new Date(ms))
    .replace(/\s/g, '').toLowerCase();
  return dropZero ? s.replace(':00', '') : s;
}

async function nws(e, lat, lon, shore) {
  const hdr = { 'User-Agent': e.NWS_UA, 'Accept': 'application/geo+json' };
  const out = {};
  let p;
  try { p = (await getJson(`https://api.weather.gov/points/${lat.toFixed(4)},${lon.toFixed(4)}`, hdr)).properties; }
  catch (err) { return out; }                                     // outside NWS land - caller falls back
  const rel = (p.relativeLocation || {}).properties || {};
  out.city = rel.city || ''; out.state = rel.state || ''; out.tzName = p.timeZone || '';
  try {
    let fc, src = 'boat position';
    try { fc = await getJson(p.forecast, hdr, 1); }
    catch (err) {                                                  // on the water: no grid here, ask the shore
      if (!shore) throw err;
      const sp = (await getJson(`https://api.weather.gov/points/${shore.lat.toFixed(4)},${shore.lon.toFixed(4)}`, hdr)).properties;
      fc = await getJson(sp.forecast, hdr, 1); src = shore.name || 'nearby shore';
    }
    const periods = fc.properties.periods || [];
    out.forecastFrom = src;
    if (periods.length) {
      out.nowTemp = periods[0].temperature; out.nowSky = periods[0].shortForecast || '';
      out.nowWind = fmtWind(periods[0].windDirection, periods[0].windSpeed);
    }
    const days = [];
    for (const pd of periods) {
      if (pd.isDaytime && days.length < 3) {
        const lbl = new Intl.DateTimeFormat('en-US', { weekday: 'short', timeZone: out.tzName || 'UTC' }).format(new Date(pd.startTime));
        days.push(`${lbl}: ${pd.temperature ?? '--'}° ${fmtWind(pd.windDirection, pd.windSpeed)} ${shortSky(pd.shortForecast)}`);
      }
    }
    out.forecast = days.join(' | ');
    out.forecastAt = Date.now();
  } catch (err) { console.log('nws forecast failed:', String(err)); }
  try {
    const stns = await getJson(p.observationStations, hdr, 1);
    const sid = stns.features[0].properties.stationIdentifier;
    const ob = (await getJson(`https://api.weather.gov/stations/${sid}/observations/latest`, hdr, 1)).properties;
    const tC = ob.temperature && ob.temperature.value;
    if (tC !== null && tC !== undefined) out.nowTemp = Math.round(tC * 9 / 5 + 32);
    if (ob.textDescription) out.nowSky = ob.textDescription;
    let pr = ob.barometricPressure && ob.barometricPressure.value;
    if (pr === null || pr === undefined) pr = ob.seaLevelPressure && ob.seaLevelPressure.value;
    if (pr !== null && pr !== undefined) out.pressureMb = Math.round(pr / 10) / 10;
  } catch (err) { console.log('nws obs failed:', String(err)); }
  return out;
}

// Outside NWS coverage (Bahamas, Caribbean...): current conditions from Open-Meteo.
const WMO = { 0: 'Clear', 1: 'Mostly Clear', 2: 'Partly Cloudy', 3: 'Overcast', 45: 'Fog', 48: 'Fog', 51: 'Light Drizzle', 53: 'Drizzle', 55: 'Drizzle',
  61: 'Light Rain', 63: 'Rain', 65: 'Heavy Rain', 71: 'Light Snow', 73: 'Snow', 75: 'Heavy Snow', 80: 'Rain Showers', 81: 'Rain Showers', 82: 'Heavy Showers', 95: 'Thunderstorms', 96: 'Thunderstorms', 99: 'Thunderstorms' };
const COMPASS = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
async function omCurrent(lat, lon) {
  const j = await omJson(`https://api.open-meteo.com/v1/forecast?latitude=${lat.toFixed(4)}&longitude=${lon.toFixed(4)}`
    + `&current=temperature_2m,weather_code,wind_speed_10m,wind_direction_10m,pressure_msl&wind_speed_unit=kn&temperature_unit=fahrenheit&timezone=auto`);
  const c = j.current || {};
  return { nowTemp: c.temperature_2m !== undefined ? Math.round(c.temperature_2m) : null, nowSky: WMO[c.weather_code] || '',
           nowWind: c.wind_speed_10m !== undefined ? `${COMPASS[Math.round((c.wind_direction_10m || 0) / 22.5) % 16]} ${Math.round(c.wind_speed_10m)}kt` : '',
           pressureMb: c.pressure_msl !== undefined ? Math.round(c.pressure_msl * 10) / 10 : null,
           tzName: j.timezone || '', forecastFrom: 'Open-Meteo', forecastAt: Date.now() };
}

// ─── tides (NOAA CO-OPS) ──────────────────────────────────────────
async function nearestStation(lat, lon) {
  // Nearby search first (small); the full station list only if that fails.
  let list = null;
  try {
    const j = await getJson(`https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/tidepredstations.json?lat=${lat.toFixed(4)}&lon=${lon.toFixed(4)}&radius=50`, null, 1);
    list = (j.stationList || j.stations || []).map(s => ({ id: s.stationId || s.id, name: s.stationName || s.name, lat: +(s.lat), lng: +(s.lon ?? s.lng) }));
  } catch (err) {}
  if (!list || !list.length) {
    const j = await getJson('https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type=tidepredictions', null, 1);
    list = (j.stations || []).map(s => ({ id: s.id, name: s.name, lat: +s.lat, lng: +s.lng }));
  }
  let best = null, bd = 1e9;
  for (const s of list) { if (!isFinite(s.lat) || !isFinite(s.lng)) continue; const d = havKm(lat, lon, s.lat, s.lng); if (d < bd) { bd = d; best = s; } }
  return best ? { ...best, forLat: lat, forLon: lon } : null;
}
function tideHeightAt(rows, t) {
  let prev = null, next = null;
  for (const r of rows) { if (r.t <= t) prev = r; else { next = r; break; } }
  if (prev && next) {
    const f = (t - prev.t) / (next.t - prev.t), mid = (prev.v + next.v) / 2, amp = (prev.v - next.v) / 2;
    return mid + amp * Math.cos(Math.PI * f);
  }
  return prev ? prev.v : next ? next.v : null;
}
async function tides(lat, lon, station, tz) {
  const out = {};
  if (!station) return out;
  out.tideStation = station.name; out.tideStationId = String(station.id);
  out.tideStationNm = Math.round(havKm(lat, lon, station.lat, station.lng) / 1.852 * 10) / 10;
  const y = new Date(Date.now() - 86400000), begin = y.toISOString().slice(0, 10).replace(/-/g, '');
  const j = await getJson(`https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?product=predictions&datum=MLLW&interval=hilo`
    + `&units=english&time_zone=gmt&format=json&begin_date=${begin}&range=72&station=${station.id}`, null, 1);
  const rows = (j.predictions || []).map(p => ({ t: Date.parse(p.t.replace(' ', 'T') + ':00Z'), v: parseFloat(p.v), k: p.type }))
    .filter(r => isFinite(r.t) && isFinite(r.v)).sort((a, b) => a.t - b.t);
  const now = Date.now();
  out.tides = rows.filter(r => r.t >= now - 30 * 60000).slice(0, 4)
    .map(r => `${r.k === 'H' ? 'HIGH' : 'low'} ${hm(r.t, tz, true)} ${Math.round(r.v * 10) / 10}ft`).join(' / ');
  const cur = tideHeightAt(rows, now);
  if (cur !== null && rows.length >= 2) {
    const ahead = rows.filter(r => r.t >= now && r.t <= now + 12 * 3600000).concat([{ t: now, v: cur }]);
    const hi = ahead.reduce((a, b) => b.v > a.v ? b : a), lo = ahead.reduce((a, b) => b.v < a.v ? b : a);
    Object.assign(out, { tideNowFt: +cur.toFixed(2), tideMaxNext12Ft: +hi.v.toFixed(2), tideMinNext12Ft: +lo.v.toFixed(2),
      tideRiseToMaxFt: +Math.max(0, hi.v - cur).toFixed(2), tideFallToMinFt: +Math.max(0, cur - lo.v).toFixed(2) });
    if (hi.t > now) out.tideMaxAt = hm(hi.t, tz);
    if (lo.t > now) out.tideMinAt = hm(lo.t, tz);
  }
  return out;
}

// ─── sun and moon (computed, no API) - ported from weather.py ─────
function sunTimes(lat, lon, tz) {
  const now = new Date(), N = Math.floor((Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) - Date.UTC(now.getUTCFullYear(), 0, 0)) / 86400000);
  const rad = Math.PI / 180;
  const ut = rising => {
    const lngHour = lon / 15, t = N + ((rising ? 6 : 18) - lngHour) / 24, M = 0.9856 * t - 3.289;
    let L = (M + 1.916 * Math.sin(M * rad) + 0.020 * Math.sin(2 * M * rad) + 282.634) % 360; if (L < 0) L += 360;
    let RA = (Math.atan(0.91764 * Math.tan(L * rad)) / rad) % 360; if (RA < 0) RA += 360;
    RA += Math.floor(L / 90) * 90 - Math.floor(RA / 90) * 90; RA /= 15;
    const sinDec = 0.39782 * Math.sin(L * rad), cosDec = Math.cos(Math.asin(sinDec));
    const cosH = (Math.cos(90.833 * rad) - sinDec * Math.sin(lat * rad)) / (cosDec * Math.cos(lat * rad));
    if (cosH > 1 || cosH < -1) return null;
    const H = (rising ? 360 - Math.acos(cosH) / rad : Math.acos(cosH) / rad) / 15;
    let UT = (H + RA - 0.06571 * t - 6.622 - lngHour) % 24; if (UT < 0) UT += 24;
    return UT;
  };
  const day0 = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const f = u => u === null ? '--' : hm(day0 + u * 3600000, tz);
  return [f(ut(true)), f(ut(false))];
}
function moonPhase() {
  const days = (Date.now() - Date.UTC(2000, 0, 6, 18, 14)) / 86400000, syn = 29.530588853, age = ((days % syn) + syn) % syn;
  const illum = Math.round(50 * (1 - Math.cos(2 * Math.PI * age / syn)));
  const names = [[1.85, 'New'], [5.5, 'Waxing crescent'], [9.2, 'First quarter'], [12.9, 'Waxing gibbous'], [16.6, 'Full'],
                 [20.3, 'Waning gibbous'], [23.9, 'Last quarter'], [27.6, 'Waning crescent'], [30, 'New']];
  for (const [lim, nm] of names) if (age < lim) return `${nm} (${illum}%)`;
  return `New (${illum}%)`;
}
function pressureTrend(log, mb) {
  const now = Date.now();
  log = (log || []).filter(h => now - h[0] <= 6 * 3600000);
  let ref = null;
  if (log.length) {
    const c = log.reduce((a, b) => Math.abs(b[0] - (now - 3 * 3600000)) < Math.abs(a[0] - (now - 3 * 3600000)) ? b : a);
    if (now - c[0] >= 1.5 * 3600000) ref = c[1];
  }
  log.push([now, mb]);
  const d = ref === null ? null : mb - ref;
  return { log, trend: d === null ? '' : d >= 1 ? 'rising' : d <= -1 ? 'falling' : 'steady' };
}

async function jobNow(e, st) {
  const { lat, lon } = await boatPosition(e);
  // Tide station: re-found only when the boat has moved more than 5 nm.
  let station = st.station;
  if (!station || havKm(lat, lon, station.forLat, station.forLon) > 5 * 1.852) {
    try { station = await nearestStation(lat, lon); } catch (err) { console.log('station lookup failed:', String(err)); }
  }
  const shore = station ? { lat: station.lat, lon: station.lng, name: station.name } : null;
  let d = await nws(e, lat, lon, shore);
  if (d.nowTemp === undefined) {
    try { d = Object.assign(await omCurrent(lat, lon), d.city ? { city: d.city, state: d.state } : {}); } catch (err) { console.log('open-meteo current failed:', String(err)); }
  }
  const tz = d.tzName || 'America/New_York';
  try { Object.assign(d, await tides(lat, lon, station, tz)); } catch (err) { console.log('tides failed:', String(err)); }
  [d.sunrise, d.sunset] = sunTimes(lat, lon, tz);
  d.moon = moonPhase();
  let log = st.pressLog;
  if (d.pressureMb !== null && d.pressureMb !== undefined) { const r = pressureTrend(log, d.pressureMb); log = r.log; if (r.trend) d.pressureTrend = r.trend; }
  const at = Date.now();
  d.updatedAt = at; d.source = 'cloud';
  delete d.tzName;
  await fsWrite(e, `weather/${e.VESSEL_ID}`, d, false);
  await fsWrite(e, `weather/${e.VESSEL_ID}-wxstate`, { lastNow: at, station: station ? JSON.stringify(station) : '', pressLog: JSON.stringify(log) }, true);
  return { ok: true, wrote: Object.keys(d) };
}
