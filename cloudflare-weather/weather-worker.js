// Harvest Watch - Weather Worker (Cloudflare)
// ===========================================
// All of the weather that used to run on the Pi, running off the boat:
//
//   every 30 min  "Now" summary -> weather/<vessel>
//                 (NWS observation + text forecast, NOAA tides, sun, moon,
//                  pressure trend; Open-Meteo "current" when outside NWS land)
//   every hour    7-model point forecast -> weather/<vessel>-forecast
//   on request    the same, within a minute of the app's Refresh button
//   every 30 min  valid times of NOAA WPC's day 0-7 fronts charts (the app
//                 shows WPC's own pictures) -> weather/<vessel>-wpc
//   by hand only  ?run=fronts (coded fronts -> weather/<vessel>-fronts) and
//                 ?run=wx (rain areas -> weather/<vessel>-fronts-wx); the app
//                 no longer uses these, so they are not scheduled
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
// Manual:  https://<worker-url>/?run=now | ?run=forecast | ?run=charts | ?run=fronts | ?run=wx | ?status=1

const FALLBACK = { lat: 44.10, lon: -69.10 };            // midcoast Maine
const NOW_EVERY_MIN = 30, FC_EVERY_MIN = 60, FC_MIN_GAP_MIN = 2, FRONTS_EVERY_MIN = 30, WX_EVERY_MIN = 60, CHARTS_EVERY_MIN = 30;

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
    if (u.pathname === '/wpc') return wpcRelay(request, u);
    try {
      if (u.searchParams.get('run') === 'now') return json(await jobNow(e, await getState(e)));
      if (u.searchParams.get('run') === 'forecast') return json(await jobForecast(e, await getState(e)));
      if (u.searchParams.get('run') === 'fronts') return json(await jobFronts(e, await getState(e)));
      if (u.searchParams.get('run') === 'wx') return json(await jobWx(e, await getState(e)));
      if (u.searchParams.get('run') === 'charts') return json(await jobCharts(e, await getState(e)));
      const st = await getState(e);
      return json({ ok: true, lastNow: ago(st.lastNow), lastForecast: ago(st.lastFc), station: st.station,
                    hint: 'Add ?run=now or ?run=forecast to run a job by hand.' });
    } catch (err) { return json({ ok: false, error: String(err && err.stack || err) }, 500); }
  }
};

// Relay for NOAA WPC chart files (fronts & pressure). WPC doesn't answer
// GitHub's build machines, so the map build fetches through here.
// Only WPC /kml/ files are passed through.
async function wpcRelay(request, u) {
  let t;
  try { t = new URL(u.searchParams.get('u') || ''); } catch { return new Response('bad url', { status: 400 }); }
  if (t.protocol !== 'https:' || !/(^|\.)wpc\.ncep\.noaa\.gov$/.test(t.hostname) || !t.pathname.startsWith('/kml/'))
    return new Response('not allowed', { status: 403 });
  const r = await fetch(t.toString(), { method: request.method === 'HEAD' ? 'HEAD' : 'GET',
    headers: { 'User-Agent': 'Mozilla/5.0 (compatible; harvest-watch)' } });
  const h = new Headers();
  for (const k of ['content-type', 'last-modified']) { const v = r.headers.get(k); if (v) h.set(k, v); }
  return new Response(request.method === 'HEAD' ? null : r.body, { status: r.status, headers: h });
}

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
  return { lastNow: num(f.lastNow) || 0, lastFc: num(f.lastFc) || 0, lastFronts: num(f.lastFronts) || 0, lastWx: num(f.lastWx) || 0, lastCharts: num(f.lastCharts) || 0,
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
  if (now - st.lastCharts > CHARTS_EVERY_MIN * 60000) return jobCharts(e, st);
  return { idle: true };
}

// ═══ JOB: surface fronts (WPC coded bulletins) ════════════════════
// WPC's "Coded Surface Frontal Positions": the analysis every 3 h (CODSUS,
// high-res ASUS02 at 0.1 deg) and the 12-48 h forecasts (CODSRP). The app
// draws them as sharp lines. Written to weather/<vessel>-fronts.
export function codCoord(tok) {
  if (!/^\d{4,7}$/.test(tok)) return null;
  const hi = tok.length >= 6;
  const la = hi ? +tok.slice(0, 3) / 10 : +tok.slice(0, 2), lo = hi ? +tok.slice(3) / 10 : +tok.slice(2);
  if (la > 90 || lo > 360) return null;
  return [Math.round(la * 10) / 10, -Math.round((lo > 180 ? lo - 360 : lo) * 10) / 10];
}
const COD_KEYS = new Set(['HIGHS', 'LOWS', 'COLD', 'WARM', 'STNRY', 'OCFNT', 'TROF', 'DRYLINE']);
export function parseCOD(text, issuedMs) {
  const iss = new Date(issuedMs), frames = [];
  let fr = null, feat = null;
  const close = () => { if (feat && fr) {
      if (feat.k === 'HIGHS' || feat.k === 'LOWS') {
        for (let i = 0; i + 1 < feat.nums.length; i += 2) { const c = codCoord(feat.nums[i + 1]); if (c) fr.hl.push([feat.k[0], +feat.nums[i], c[0], c[1]]); }
      } else {
        const pts = feat.nums.map(codCoord).filter(Boolean);
        if (pts.length > 1) fr.fr.push([feat.k, feat.q, pts.flat()]);
      } }
    feat = null; };
  // MMDDHH (analysis) / DDHHMM (forecast) -> ms, year/month from the issue time
  const when = (mo, d, h, mi) => {
    let t = Date.UTC(iss.getUTCFullYear(), mo, d, h, mi);
    if (t - issuedMs > 200 * 86400e3) t = Date.UTC(iss.getUTCFullYear() - 1, mo, d, h, mi);
    if (issuedMs - t > 200 * 86400e3) t = Date.UTC(iss.getUTCFullYear() + 1, mo, d, h, mi);
    return t;
  };
  for (const raw of String(text).split('\n')) {
    const line = raw.trim(); if (!line) continue;
    if (line.startsWith('$$')) { close(); break; }
    let m = line.match(/^(\d+)\s*HR PROG VALID (\d{2})(\d{2})(\d{2})Z/);
    if (m) {
      close();
      let mo = iss.getUTCMonth(); const d = +m[2];
      if (d < iss.getUTCDate() - 7) mo += 1;                        // crossed into next month
      fr = { t: when(mo, d, +m[3], +m[4]), kind: 'forecast', lead: +m[1], hl: [], fr: [] }; frames.push(fr); continue;
    }
    m = line.match(/^VALID (\d{2})(\d{2})(\d{2})Z/);
    if (m) { close(); fr = { t: when(+m[1] - 1, +m[2], +m[3], 0), kind: 'analysis', hl: [], fr: [] }; frames.push(fr); continue; }
    if (!fr) continue;
    const tok = line.split(/\s+/);
    if (COD_KEYS.has(tok[0])) {
      close(); feat = { k: tok[0], q: '', nums: [] };
      for (const x of tok.slice(1)) { if (/^\d+$/.test(x)) feat.nums.push(x); else if (!feat.nums.length) feat.q += (feat.q ? ' ' : '') + x; }
    } else if (feat && /^\d/.test(tok[0])) { for (const x of tok) if (/^\d+$/.test(x)) feat.nums.push(x); }
    else close();
  }
  close();
  return frames.filter(f => f.fr.length || f.hl.length);
}
// National Forecast Chart (mapservices.weather.noaa.gov): WPC's day 1-3 fronts
// and highs/lows as vectors. Layers per day: 1 + 12(d-1) highs/lows, 2 + 12(d-1) fronts.
const NFC = 'https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/natl_fcst_wx_chart/MapServer';
const NFC_KIND = [[/cold/i, 'COLD'], [/warm/i, 'WARM'], [/stationary/i, 'STNRY'], [/occlu/i, 'OCFNT'], [/dry/i, 'DRYLINE'], [/trough|squall|outflow/i, 'TROF']];
const MON3 = { JAN: 0, FEB: 1, MAR: 2, APR: 3, MAY: 4, JUN: 5, JUL: 6, AUG: 7, SEP: 8, OCT: 9, NOV: 10, DEC: 11 };
export function nfcTime(txt) {
  const m = String(txt).match(/Valid:\s*\w+\s+(Morning|Afternoon|Evening|Night)\s+(\w{3})\w*\s+(\d{1,2})\s+(\d{4})/i);
  if (!m || MON3[m[2].toUpperCase()] === undefined) return null;
  const h = { morning: 12, afternoon: 18, evening: 24, night: 30 }[m[1].toLowerCase()];
  return Date.UTC(+m[4], MON3[m[2].toUpperCase()], +m[3]) + h * 3600e3;
}
async function nfcFrames() {
  const q = async id => {
    const r = await fetch(`${NFC}/${id}/query?where=1%3D1&outFields=popupconte,feat&returnGeometry=true&outSR=4326&f=geojson`);
    if (!r.ok) throw new Error(`layer ${id} HTTP ${r.status}`);
    return (await r.json()).features || [];
  };
  const frames = [];
  for (const d of [2, 3]) {
    const [hl, fr] = await Promise.all([q(1 + 12 * (d - 1)), q(2 + 12 * (d - 1))]);
    const t = [...fr, ...hl].map(f => nfcTime((f.properties || {}).popupconte)).find(Boolean);
    if (!t) continue;
    const F = { t, kind: 'forecast', src: 'nfc', day: d, hl: [], fr: [] };
    for (const f of hl) {
      const c = (f.geometry || {}).coordinates, k = /high/i.test((f.properties || {}).feat) ? 'H' : /low/i.test((f.properties || {}).feat) ? 'L' : null;
      if (k && c) F.hl.push([k, '', Math.round(c[1] * 10) / 10, Math.round(c[0] * 10) / 10]);
    }
    for (const f of fr) {
      const g = f.geometry || {}, feat = (f.properties || {}).feat || '';
      const k = (NFC_KIND.find(([re]) => re.test(feat)) || [null, 'TROF'])[1];
      const lines = g.type === 'LineString' ? [g.coordinates] : g.type === 'MultiLineString' ? g.coordinates : [];
      for (const ln of lines) if (ln.length > 1) F.fr.push([k, '', ln.flatMap(([lo, la]) => [Math.round(la * 100) / 100, Math.round(lo * 100) / 100])]);
    }
    if (F.fr.length || F.hl.length) frames.push(F);
  }
  return frames;
}
async function jobFronts(e, st) {
  const hdr = { 'User-Agent': e.NWS_UA, 'Accept': 'application/ld+json' };
  // NWS API product ids are 3+3: type COD, location SUS (analysis) / SRP (forecast)
  const list = async type => ((await getJson(`https://api.weather.gov/products/types/${type.slice(0, 3)}/locations/${type.slice(3)}`, hdr)) || {})['@graph'] || [];
  const text = async id => (await getJson(`https://api.weather.gov/products/${id}`, hdr)).productText || '';
  const out = [], notes = [];
  try {
    const all = (await list('CODSUS')).sort((a, b) => Date.parse(b.issuanceTime) - Date.parse(a.issuanceTime));
    notes.push(`CODSUS: ${all.length} listed`);
    let pick = all.filter(p => p.wmoCollectiveId === 'ASUS02');
    if (!pick.length) pick = all;
    pick = pick.filter(p => Date.now() - Date.parse(p.issuanceTime) < 30 * 3600e3).slice(0, 10);
    for (const p of pick) {
      try { const got = parseCOD(await text(p.id), Date.parse(p.issuanceTime)); if (!got.length) notes.push('CODSUS ' + p.id + ': nothing decoded'); out.push(...got); } catch (err) { notes.push('CODSUS ' + p.id + ': ' + err.message); }
    }
  } catch (err) { notes.push('CODSUS list: ' + err.message); }
  try {
    const srp = await list('CODSRP'); notes.push(`CODSRP: ${srp.length} listed`);
    const p = srp.sort((a, b) => Date.parse(b.issuanceTime) - Date.parse(a.issuanceTime))[0];
    if (p) out.push(...parseCOD(await text(p.id), Date.parse(p.issuanceTime)).map(f => ({ ...f, issued: Date.parse(p.issuanceTime) })));
  } catch (err) { notes.push('CODSRP: ' + err.message); }
  // Days 2-3 from the National Forecast Chart map service (vector fronts, no pressures).
  try { const nfc = await nfcFrames(); notes.push(`NFC: ${nfc.length} frames`); out.push(...nfc); } catch (err) { notes.push('NFC: ' + err.message); }
  // one frame per valid time: analyses win over forecasts, coded bulletins over the NFC
  const byT = new Map();
  const rank = f => f.kind === 'analysis' ? 2 : f.src === 'nfc' ? 0 : 1;
  for (const f of out) { const k = f.t, cur = byT.get(k); if (!cur || rank(f) > rank(cur)) byT.set(k, f); }
  // NFC frames only extend past the coded forecasts
  const lastCoded = Math.max(0, ...out.filter(f => f.src !== 'nfc').map(f => f.t));
  const frames = [...byT.values()].filter(f => f.src !== 'nfc' || f.t > lastCoded).sort((a, b) => a.t - b.t);
  const at = Date.now();
  if (frames.length) await fsWrite(e, `weather/${e.VESSEL_ID}-fronts`, { data: JSON.stringify({ at, frames }), updatedAt: at });
  await fsWrite(e, `weather/${e.VESSEL_ID}-wxstate`, { lastFronts: frames.length ? at : at - (FRONTS_EVERY_MIN - 10) * 60000 }, true);
  return { ok: frames.length > 0, frames: frames.map(f => `${new Date(f.t).toISOString().slice(5, 16)} ${f.kind}${f.lead ? ' +' + f.lead + 'h' : ''}: ${f.fr.length} fronts, ${f.hl.length} H/L`), notes };
}

// ═══ JOB: rain & weather areas (National Forecast Chart, days 1-3) ═══
// WPC's precipitation areas as polygons, drawn under the fronts in the app.
// Its own job so it gets its own budget of fetches (about 30 here; the free
// plan allows 50 per run). Same map service and query as nfcFrames above.
// Layer ids: 12 per day; these are the offsets within a day.
const WX_LAYERS = [[4, 'RA'], [3, 'TSRA'], [5, 'MIX'], [6, 'SN'], [10, 'FZRA'], [11, 'HSN'], [8, 'FF'], [7, 'SVR']];
async function nfcQuery(id) {
  const r = await fetch(`${NFC}/${id}/query?where=1%3D1&outFields=popupconte,feat&returnGeometry=true&outSR=4326&f=geojson`);
  if (!r.ok) throw new Error(`layer ${id} HTTP ${r.status}`);
  return (await r.json()).features || [];
}
// One ring [[lon, lat], ...] -> flat [lat, lon, ...], thinned to points at least
// `tol` degrees apart. Rings that thin to under 3 points are dropped.
export function wxRing(pts, tol) {
  const out = []; let pa = null, po = null;
  for (const p of pts || []) {
    const a = Math.round(p[1] * 100) / 100, o = Math.round(p[0] * 100) / 100;
    if (pa !== null && Math.abs(a - pa) < tol && Math.abs(o - po) < tol) continue;
    out.push(a, o); pa = a; po = o;
  }
  return out.length >= 6 ? out : null;
}
export function wxAreas(raw, tol) {
  const areas = [];
  for (const [k, g] of raw) {
    const polys = g.type === 'Polygon' ? [g.coordinates] : g.type === 'MultiPolygon' ? g.coordinates : [];
    for (const p of polys) { const rings = (p || []).map(r => wxRing(r, tol)).filter(Boolean); if (rings.length) areas.push([k, rings]); }
  }
  return areas;
}
async function jobWx(e, st) {
  const raw = [], notes = []; let good = 0;
  for (const d of [1, 2, 3]) {
    const base = 12 * (d - 1), geo = []; let t = null;
    for (const [off, k] of WX_LAYERS) {
      try {
        const feats = await nfcQuery(base + off); good++;
        for (const f of feats) { t = t || nfcTime((f.properties || {}).popupconte); if (f.geometry) geo.push([k, f.geometry]); }
      } catch (err) { notes.push(`day ${d} layer ${base + off}: ${err.message}`); }
    }
    // the areas' own popups may not carry the valid time; the day's highs/lows do
    if (geo.length && !t) { try { t = (await nfcQuery(base + 1)).map(f => nfcTime((f.properties || {}).popupconte)).find(Boolean) || null; } catch (err) {} }
    if (geo.length && !t) notes.push(`day ${d}: ${geo.length} areas but no valid time, skipped`);
    if (geo.length && t) raw.push({ day: d, t, geo });
    else if (!geo.length) notes.push(`day ${d}: no areas`);
  }
  const at = Date.now();
  if (!good) {   // service down: keep the last good areas, try again in ~10 min
    await fsWrite(e, `weather/${e.VESSEL_ID}-wxstate`, { lastWx: at - (WX_EVERY_MIN - 10) * 60000 }, true);
    return { ok: false, notes };
  }
  // Firestore caps a field near 1 MB: thin the outlines further until it fits.
  let data = '', days = [];
  for (const tol of [0.04, 0.1, 0.25, 0.5]) {
    days = raw.map(x => ({ day: x.day, t: x.t, areas: wxAreas(x.geo, tol) })).filter(x => x.areas.length);
    data = JSON.stringify({ at, days });
    if (data.length < 800000) { if (tol > 0.04) notes.push(`outlines thinned to ${tol} deg to fit`); break; }
  }
  if (data.length >= 800000) {
    notes.push(`too large (${data.length} chars), not saved`);
    await fsWrite(e, `weather/${e.VESSEL_ID}-wxstate`, { lastWx: at }, true);
    return { ok: false, notes };
  }
  await fsWrite(e, `weather/${e.VESSEL_ID}-fronts-wx`, { data, updatedAt: at });
  await fsWrite(e, `weather/${e.VESSEL_ID}-wxstate`, { lastWx: at }, true);
  const count = a => Object.entries(a.reduce((m, [k]) => (m[k] = (m[k] || 0) + 1, m), {})).map(([k, n]) => `${n} ${k}`).join(', ');
  return { ok: true, days: days.map(x => `day ${x.day} valid ${new Date(x.t).toISOString().slice(5, 16)}: ${count(x.areas)}`),
           size: data.length, notes };
}

// ═══ JOB: valid times for NOAA WPC's day 0-7 charts ═══════════════
// The app's Fronts view shows WPC's own chart pictures, exactly as on
// wpc.ncep.noaa.gov/basicwx/day0-7loop.html. The pictures carry their valid
// time only as printed text, so this job works the times out for the app:
//   days 1/2-2 1/2 (fronts + NDFD rain/snow/ice/storms): read from WPC's own
//     page, which lists each chart with its valid day and hour
//   days 3-7: valid 12Z; the issue day comes from the picture's Last-Modified
//     time (same rule the old GitHub map build used)
// Analyses need nothing: their file names carry the hour.
const WPCB = 'https://www.wpc.ncep.noaa.gov';
const WPC_UA = { 'User-Agent': 'Mozilla/5.0 (compatible; harvest-watch)' };
const DOW = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
// The next time (from 18 h ago on) that falls on weekday `dow` at `hh` UTC.
export function dowTime(dow, hh, now) {
  const D = 86400e3, from = now - 18 * 3600e3;
  let t = Math.floor(from / D) * D + hh * 3600e3;
  for (let k = 0; k < 9; k++, t += D) if (t >= from && new Date(t).getUTCDay() === DOW[dow]) return t;
  return null;
}
// WPC's short-range page: nav links carry arrval (order) + vtime ("Mon_00Z");
// the pictures are 9Nfndfd.gif, numbered in the same (time) order.
export function ndfdTimes(html, now) {
  const imgs = [...new Set([...String(html).matchAll(/(9\d)fndfd\.gif/g)].map(m => m[1]))].sort();
  const byArr = new Map();
  for (const m of String(html).matchAll(/arrval=(\d+)(?:&amp;|&)vtime=(\w{3})_(\d{2})Z/g)) if (!byArr.has(+m[1])) byArr.set(+m[1], [m[2], +m[3]]);
  const vt = [...byArr.entries()].sort((a, b) => a[0] - b[0]).map(x => x[1]);
  if (!imgs.length || imgs.length !== vt.length) return [];
  const out = imgs.map((n, i) => ({ img: `/basicwx/${n}fndfd.gif`, t: dowTime(vt[i][0], vt[i][1], now) }));
  return out.every((x, i) => x.t && (!i || x.t > out[i - 1].t)) ? out : [];
}
async function lastModified(url) {
  for (const method of ['HEAD', 'GET']) {
    try {
      const r = await fetch(url, { method, headers: WPC_UA });
      const lm = Date.parse(r.headers.get('last-modified') || '');
      if (r.ok && lm) return lm;
    } catch (err) {}
  }
  return null;
}
export function medrTime(lm, day) {
  const H = 3600e3, D = 24 * H;
  return Math.floor((lm - 12 * H) / D) * D + 12 * H + day * D;
}
async function jobCharts(e, st) {
  const at = Date.now(), notes = [], out = { at, ndfd: [], medr: [] };
  try {
    const r = await fetch(`${WPCB}/basicwx/basicwx_ndfd.php`, { headers: WPC_UA });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    out.ndfd = ndfdTimes(await r.text(), at);
    notes.push(`NDFD: ${out.ndfd.length} charts`);
  } catch (err) { notes.push('NDFD page: ' + err.message); }
  for (const [i, c] of ['j', 'k', 'l', 'm', 'n'].entries()) {
    const img = `/medr/9${c}hwbg_conus.gif`, lm = await lastModified(WPCB + img);
    if (lm) out.medr.push({ img, day: 3 + i, t: medrTime(lm, 3 + i) }); else notes.push(`day ${3 + i}: no Last-Modified`);
  }
  if (!out.ndfd.length && !out.medr.length) {   // WPC unreachable: keep the last good times, retry in ~10 min
    await fsWrite(e, `weather/${e.VESSEL_ID}-wxstate`, { lastCharts: at - (CHARTS_EVERY_MIN - 10) * 60000 }, true);
    return { ok: false, notes };
  }
  await fsWrite(e, `weather/${e.VESSEL_ID}-wpc`, { data: JSON.stringify(out), updatedAt: at });
  await fsWrite(e, `weather/${e.VESSEL_ID}-wxstate`, { lastCharts: at }, true);
  const iso = t => new Date(t).toISOString().slice(5, 16);
  return { ok: true, ndfd: out.ndfd.map(x => `${x.img} valid ${iso(x.t)}`), medr: out.medr.map(x => `day ${x.day} valid ${iso(x.t)}`), notes };
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
