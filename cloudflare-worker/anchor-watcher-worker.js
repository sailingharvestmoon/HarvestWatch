// Harvest Moon - Cloud Anchor Watcher (Cloudflare Worker)  v2
// ============================================================
// Runs every minute (cron). This is the OFF-BOAT watcher. Its most important
// job is the one the Pi structurally cannot do: notice that the boat has gone
// dark. It also acts as a slower backup for drag, and handles the alerts that
// move on tide timescales (scope, depth).
//
// The fast alerts (drag at 10s, swing, wind, gust, AWA, AIS) live in guard.py
// on the Pi. The two watchers share the state/ document, so whichever one
// notices a drag first latches the flag and the other stays quiet.
//
// WHAT CHANGED FROM v1 (and why your alerts were arriving hours late):
//
//  1. LATCH ONLY ON A CONFIRMED SEND. v1 did this:
//         await notify(...);            // swallowed all errors
//         s.dragAlerted = true;         // ...and latched anyway
//     A push that failed was marked as delivered and never retried. Now
//     sendNtfy returns true/false and the flag is only set on a real 2xx.
//     A failed push is retried on the very next cycle.
//
//  2. HEALTH DOC. Every run writes health/<vessel> with a timestamp and the
//     outcome of the last push attempt. If an alert doesn't arrive you can now
//     tell "the cron never fired" apart from "the push was dropped" by looking
//     at the watch page instead of guessing.
//
//  3. DAILY OK PING. Once a day at DAILY_PING_HOUR local time it pushes
//     "watchers healthy". Silence is otherwise indistinguishable from safety;
//     this makes a missing notification something you actually notice.
//
//  Note: ntfy forwards to Firebase asynchronously, so a 200 here still does
//  not guarantee the notification reached the phone. That is exactly why the
//  daily ping and the Pi's independent push path both exist.
//
// Environment variables (Cloudflare dashboard -> Settings -> Variables):
//   PROJECT_ID       Firebase project id
//   VESSEL_ID        harvest-moon
//   NTFY_TOPIC       secret ntfy topic (treat like a password)
//   NTFY_TOKEN       ntfy account token  <-- strongly recommended, see above
// Optional:
//   NTFY_SERVER      default https://ntfy.sh
//   RENOTIFY_MIN     default 10
//   TZ_NAME          default America/New_York
//   DAILY_PING_HOUR  default 8   (local hour for the "all healthy" ping)
//
// Manual testing:
//   https://<worker-url>/          -> run one check, return JSON
//   https://<worker-url>/?test=1   -> fire a test push
//   https://<worker-url>/?health=1 -> show the health doc without running
//   https://<worker-url>/?debug=1  -> show what it asks Firestore for

const DEFAULTS = {
  radiusFt: 150,
  confirmN: 2,
  staleMin: 8,
  targetScope: 5.0,
  minScope: 4.0,
  bowRollerFt: 5.0,
  transducerOffsetFt: 2.0,
  rodeOutFt: 0,          // 0 = unknown, scope checks skipped
  minDepthFt: 8.0,
  dailyPingHour: 8
};

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runCheck(clean(env)).catch(e => console.log('run failed:', String(e))));
  },
  async fetch(request, env) {
    try {
      env = clean(env);
      const missing = ['PROJECT_ID', 'VESSEL_ID', 'NTFY_TOPIC'].filter(k => !env[k]);
      if (missing.length) {
        return json({ ok: false, error: 'Missing variables: ' + missing.join(', '),
          fix: 'Add them under Settings -> Variables and Secrets, then Deploy.' });
      }
      const url = new URL(request.url);

      if (url.searchParams.get('health') === '1') {
        const h = await fsGet(env, 'health', env.VESSEL_ID);
        const now = Date.now();
        const age = (f) => { const v = num(f); return v ? Math.round((now - v) / 60000) + ' min ago' : 'never'; };
        return json({
          workerLastRun:  h ? age(h.workerLastRun) : 'never',
          guardLastRun:   h ? age(h.guardLastRun)  : 'never (Pi guard not deployed yet)',
          lastPushOk:     h ? age(h.lastPushOkTs)  : 'never',
          lastPushFail:   h ? age(h.lastPushFailTs): 'never',
          lastPushError:  h && h.lastPushError ? h.lastPushError.stringValue : '',
          tokenConfigured: !!env.NTFY_TOKEN
        });
      }

      if (url.searchParams.get('debug') === '1') {
        const raw = (v) => JSON.stringify(String(v ?? ''));
        const target = `${FS(env)}/alarms/${env.VESSEL_ID}`;
        let status = 'n/a', body = '';
        try { const r = await fetch(target); status = r.status; body = (await r.text()).slice(0, 400); }
        catch (e) { body = String(e); }
        return json({
          urlTheWorkerIsFetching: target, firestoreStatus: status, firestoreReply: body,
          valuesAsStored: { PROJECT_ID: raw(env.PROJECT_ID), VESSEL_ID: raw(env.VESSEL_ID),
                            NTFY_TOPIC: raw(env.NTFY_TOPIC), NTFY_TOKEN_set: !!env.NTFY_TOKEN },
          hint: 'Extra spaces inside the quotes mean the value needs retyping. 404 = no document at that path.'
        });
      }

      if (url.searchParams.get('test') === '1') {
        const ok = await sendNtfy(env, 'Harvest Moon \u2014 watcher test',
          'Manual test from the Cloudflare watcher. The push chain works.', 4, 'white_check_mark');
        // Report the real reason inline. A fetch invocation and a cron
        // invocation do not share memory, so pointing at the health doc here
        // was useless - the outcome would never be written by this path.
        await writeHealth(env);
        return json({
          sent: ok,
          error: ok ? '' : pushOutcome.error,
          topicAsStored: JSON.stringify(env.NTFY_TOPIC || ''),
          serverAsStored: JSON.stringify(env.NTFY_SERVER || '(default https://ntfy.sh)'),
          tokenLength: env.NTFY_TOKEN ? String(env.NTFY_TOKEN).length : 0,
          tokenPrefix: env.NTFY_TOKEN ? String(env.NTFY_TOKEN).slice(0, 3) : '',
          note: ok
            ? 'ntfy accepted the message. If nothing arrives on the phone, the drop is downstream of ntfy - check the ntfy app subscription.'
            : 'ntfy REJECTED the message. The error field below is the reason.'
        });
      }

      return json(await runCheck(env));
    } catch (e) {
      return json({ ok: false, error: String((e && e.stack) || e) });
    }
  }
};

function clean(env) {
  const t = (v) => (typeof v === 'string' ? v.trim() : v);
  return { ...env,
    PROJECT_ID: t(env.PROJECT_ID), VESSEL_ID: t(env.VESSEL_ID), NTFY_TOPIC: t(env.NTFY_TOPIC),
    NTFY_SERVER: t(env.NTFY_SERVER), NTFY_TOKEN: t(env.NTFY_TOKEN), TZ_NAME: t(env.TZ_NAME) };
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj, null, 2), {
    status, headers: { 'content-type': 'application/json; charset=utf-8' } });
}

// --- FIRESTORE ---------------------------------------------------
const FS = (env) => `https://firestore.googleapis.com/v1/projects/${env.PROJECT_ID}/databases/(default)/documents`;

async function fsGet(env, coll, doc) {
  const res = await fetch(`${FS(env)}/${coll}/${doc}`);
  if (res.status === 404) return null;
  if (!res.ok) {
    let detail = '';
    try { const e = await res.json(); detail = e.error?.message || JSON.stringify(e); }
    catch (_) { try { detail = await res.text(); } catch (_) {} }
    throw new Error(`Firestore GET ${coll}/${doc} -> ${res.status}: ${detail}`);
  }
  const data = await res.json();
  return data.fields || null;
}

// Field-masked PATCH. Without the mask, a PATCH replaces the whole document -
// which matters now that the Pi guard writes to the same state/ and health/
// docs. Each writer must touch only its own fields.
async function fsPatch(env, coll, doc, fields) {
  const mask = Object.keys(fields).map(k => `updateMask.fieldPaths=${encodeURIComponent(k)}`).join('&');
  const res = await fetch(`${FS(env)}/${coll}/${doc}?${mask}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fields })
  });
  if (!res.ok) {
    let detail = '';
    try { const e = await res.json(); detail = e.error?.message || JSON.stringify(e); }
    catch (_) { try { detail = await res.text(); } catch (_) {} }
    throw new Error(`Firestore PATCH ${coll}/${doc} -> ${res.status}: ${detail}`);
  }
}

const num  = (f) => f ? (f.doubleValue !== undefined ? f.doubleValue
                       : (f.integerValue !== undefined ? parseInt(f.integerValue) : undefined)) : undefined;
const bool = (f) => f ? !!f.booleanValue : false;
const str  = (f) => f && f.stringValue !== undefined ? f.stringValue : '';
// Read a config number, falling back to a default when absent or nonsense.
const cfgNum = (f, dflt) => { const v = num(f); return (v === undefined || !isFinite(v)) ? dflt : v; };

// --- NTFY --------------------------------------------------------
// Returns TRUE only on a real 2xx. Callers latch their alert flag on this
// return value and on nothing else. Never throws - a push failure must not
// abort track recording or state persistence further down the run.
let pushOutcome = { ok: null, error: '' };

async function sendNtfy(env, title, message, priority, tags) {
  const server = (env.NTFY_SERVER || 'https://ntfy.sh').replace(/\/+$/, '');
  try {
    if (!env.NTFY_TOPIC) throw new Error('NTFY_TOPIC not set');
    // JSON body, not headers: HTTP header values cannot carry characters above
    // Latin-1, so an emoji in the title throws before the request is sent.
    const payload = {
      topic: env.NTFY_TOPIC, title, message,
      priority: Number(priority) || 4,
      tags: String(tags || 'anchor').split(',').map(s => s.trim()).filter(Boolean)
    };
    const headers = { 'Content-Type': 'application/json' };
    if (env.NTFY_TOKEN) headers['Authorization'] = 'Bearer ' + String(env.NTFY_TOKEN).trim();
    const res = await fetch(server, { method: 'POST', headers, body: JSON.stringify(payload) });
    if (!res.ok) {
      let detail = '';
      try { detail = (await res.text()).slice(0, 200); } catch (_) {}
      if (res.status === 429) {
        throw new Error('ntfy rate limit (429). If NTFY_TOKEN is set, the account itself is over quota; '
          + 'if not, anonymous sends are limited per IP and Cloudflare IPs are shared. Detail: ' + detail);
      }
      throw new Error(`ntfy POST -> ${res.status}: ${detail}`);
    }
    pushOutcome = { ok: true, error: '' };
    return true;
  } catch (e) {
    pushOutcome = { ok: false, error: String(e).slice(0, 300) };
    console.log('ntfy push FAILED (will retry next cycle):', String(e));
    return false;
  }
}

// --- GEO ---------------------------------------------------------
function haversine(lat1, lon1, lat2, lon2) {
  const R = 6371000, rad = Math.PI / 180;
  const p1 = lat1 * rad, p2 = lat2 * rad;
  const dp = (lat2 - lat1) * rad, dl = (lon2 - lon1) * rad;
  const a = Math.sin(dp/2)**2 + Math.cos(p1)*Math.cos(p2)*Math.sin(dl/2)**2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

// --- TRACK -------------------------------------------------------
// Capped at ~33h rather than v1's 5000 points. At 5000 the document reached
// roughly 125 KB, and the worker was reading and rewriting all of it every
// single minute, which is a lot of work to do on the same invocation that has
// to fire your drag alarm.
const TRACK_MAX_POINTS = 2000;

function encodeTrack(pts) { return pts.map(p => `${p[0].toFixed(5)},${p[1].toFixed(5)},${p[2]}`).join(';'); }
function decodeTrack(s) {
  if (!s) return [];
  return s.split(';').filter(Boolean).map(c => {
    const a = c.split(',');
    return [parseFloat(a[0]), parseFloat(a[1]), parseInt(a[2]) || 0];
  }).filter(p => isFinite(p[0]) && isFinite(p[1]));
}
const anchorSessionKey = (la, lo) => la.toFixed(5) + ',' + lo.toFixed(5);

async function recordTrackPoint(env, sessionKey, lat, lon, ts) {
  const existing = await fsGet(env, 'tracks', env.VESSEL_ID);
  let pts = [];
  if (existing && str(existing.session) === sessionKey) pts = decodeTrack(str(existing.pts));
  const tsSec = Math.round(ts / 1000);
  if (pts.length && pts[pts.length - 1][2] === tsSec) return;   // same fix twice
  pts.push([lat, lon, tsSec]);
  if (pts.length > TRACK_MAX_POINTS) pts = pts.slice(-TRACK_MAX_POINTS);
  await fsPatch(env, 'tracks', env.VESSEL_ID, {
    pts: { stringValue: encodeTrack(pts) },
    session: { stringValue: sessionKey },
    count: { integerValue: String(pts.length) },
    updatedAt: { integerValue: String(Date.now()) }
  });
}

// --- STATE -------------------------------------------------------
// dragAlerted / lastDragTs are SHARED with guard.py on the Pi, so whichever
// watcher sees the drag first suppresses a duplicate alert from the other.
// breachCountWorker is this watcher's own counter (the Pi counts faster ticks).
async function writeState(env, s) {
  await fsPatch(env, 'state', env.VESSEL_ID, {
    breachCountWorker: { integerValue: String(s.breachCount) },
    dragAlerted:       { booleanValue: s.dragAlerted },
    staleAlerted:      { booleanValue: s.staleAlerted },
    scopeAlerted:      { booleanValue: s.scopeAlerted },
    depthAlerted:      { booleanValue: s.depthAlerted },
    lastDragTs:        { integerValue: String(s.lastDragTs) },
    lastPingDay:       { stringValue: s.lastPingDay }
  });
}

async function writeHealth(env, extra) {
  const fields = { workerLastRun: { integerValue: String(Date.now()) } };
  if (pushOutcome.ok === true) fields.lastPushOkTs = { integerValue: String(Date.now()) };
  if (pushOutcome.ok === false) {
    fields.lastPushFailTs = { integerValue: String(Date.now()) };
    fields.lastPushError  = { stringValue: pushOutcome.error };
  }
  Object.assign(fields, extra || {});
  try { await fsPatch(env, 'health', env.VESSEL_ID, fields); }
  catch (e) { console.log('health write failed:', String(e)); }
}

// Local calendar day, for the once-a-day healthy ping.
function localParts(env) {
  const tz = env.TZ_NAME || 'America/New_York';
  const f = new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit',
                                               day: '2-digit', hour: '2-digit', hour12: false });
  const p = {};
  for (const part of f.formatToParts(new Date())) p[part.type] = part.value;
  return { day: `${p.year}-${p.month}-${p.day}`, hour: parseInt(p.hour) };
}

// --- MAIN --------------------------------------------------------
async function runCheck(env) {
  const cfg = await fsGet(env, 'alarms', env.VESSEL_ID);
  if (!cfg) { await writeHealth(env); return { ok: true, note: 'no alarm config - set an anchor from the watch page' }; }

  const st = (await fsGet(env, 'state', env.VESSEL_ID)) || {};
  const s = {
    breachCount:  num(st.breachCountWorker) || 0,
    dragAlerted:  bool(st.dragAlerted),
    staleAlerted: bool(st.staleAlerted),
    scopeAlerted: bool(st.scopeAlerted),
    depthAlerted: bool(st.depthAlerted),
    lastDragTs:   num(st.lastDragTs) || 0,
    lastPingDay:  str(st.lastPingDay)
  };
  const before = JSON.stringify(s);

  const armed = bool(cfg.armed);
  const snoozeUntil = num(cfg.snoozeUntil) || 0;
  const snoozed = Date.now() < snoozeUntil;
  const result = { ok: true, armed, snoozed };

  // Disarmed: clear every latch so the next arming starts clean.
  if (!armed) {
    Object.assign(s, { breachCount: 0, dragAlerted: false, staleAlerted: false,
                       scopeAlerted: false, depthAlerted: false, lastDragTs: 0 });
    if (JSON.stringify(s) !== before) await writeState(env, s);
    await writeHealth(env);
    return result;
  }

  const anchorLat = num(cfg.anchorLat), anchorLon = num(cfg.anchorLon);
  // Armed but with no anchor should never happen now that cancelling deletes
  // the coordinates - but if it does, every distance becomes NaN and the drag
  // logic silently stops meaning anything. Say so instead.
  if (anchorLat === undefined || anchorLon === undefined) {
    await writeHealth(env);
    return { ...result, note: 'armed but no anchor position set - nothing to measure against' };
  }
  const radiusFt  = cfgNum(cfg.radius,   DEFAULTS.radiusFt);
  const confirmN  = cfgNum(cfg.confirmN, DEFAULTS.confirmN);
  const staleMin  = cfgNum(cfg.staleMin, DEFAULTS.staleMin);
  const renotifyMs = (parseInt(env.RENOTIFY_MIN) || 10) * 60000;
  result.radiusFt = radiusFt;

  const pos = await fsGet(env, 'vessels', env.VESSEL_ID);
  const now = Date.now();

  // ---- HEARTBEAT: has the boat gone dark? ------------------------
  if (!pos) {
    if (!s.staleAlerted && !snoozed) {
      if (await sendNtfy(env, 'Harvest Moon \u2014 no data',
            'The watcher has no position from the boat at all.', 4, 'warning,anchor')) {
        s.staleAlerted = true;
      }
    }
    if (JSON.stringify(s) !== before) await writeState(env, s);
    await writeHealth(env);
    return { ...result, note: 'no position document' };
  }

  // Measure from the BOW where the boat publishes it. The anchor is marked at
  // the bow, so measuring from the GPS - which sits aft - reports the boat as
  // a boat-length further out than it is, on every fix, forever.
  const gpsLat = num(pos.lat), gpsLon = num(pos.lon);
  const bowLat = num(pos.bowLat), bowLon = num(pos.bowLon);
  const usingBow = (bowLat !== undefined && bowLon !== undefined);
  const lat = usingBow ? bowLat : gpsLat;
  const lon = usingBow ? bowLon : gpsLon;
  const ts  = num(pos.timestamp) || 0;
  const ageMin = Math.round((now - ts) / 60000);
  const stale = (now - ts) > staleMin * 60000;
  result.ageMin = ageMin;

  if (stale) {
    if (!s.staleAlerted && !snoozed) {
      if (await sendNtfy(env, 'Harvest Moon \u2014 lost contact',
            `No position from the boat for ${ageMin} min. The Pi, the Vesper, or Starlink may be down `
            + `- the anchor watch is NOT running until this clears.`, 5, 'warning,electric_plug')) {
        s.staleAlerted = true;
      }
    }
    result.note = 'stale - not judging drag on old data';
    if (JSON.stringify(s) !== before) await writeState(env, s);
    await writeHealth(env);
    return result;
  }

  if (s.staleAlerted) {
    if (await sendNtfy(env, 'Harvest Moon \u2014 contact restored',
          'Position updates have resumed. The watch is live again.', 3, 'white_check_mark')) {
      s.staleAlerted = false;
    }
  }

  // ---- DRAG (backup to the Pi; shared latch prevents doubles) -----
  const distFt = haversine(anchorLat, anchorLon, lat, lon) * 3.28084;
  result.dist = Math.round(distFt);
  result.measuredFrom = usingBow ? 'bow' : 'gps';
  const breach = distFt > radiusFt;
  s.breachCount = breach ? s.breachCount + 1 : 0;

  if (s.breachCount >= confirmN && !snoozed) {
    if (!s.dragAlerted) {
      if (await sendNtfy(env, 'ANCHOR DRAGGING',
            `Harvest Moon is ${Math.round(distFt)} ft from anchor (limit ${radiusFt} ft). Check the boat now.`,
            5, 'rotating_light,anchor')) {
        s.dragAlerted = true; s.lastDragTs = now;
      }
    } else if (now - s.lastDragTs > renotifyMs) {
      if (await sendNtfy(env, 'STILL DRAGGING',
            `Harvest Moon ${Math.round(distFt)} ft from anchor (limit ${radiusFt} ft).`, 5, 'rotating_light,anchor')) {
        s.lastDragTs = now;
      }
    }
    result.state = 'DRAGGING';
  } else if (s.dragAlerted && distFt < radiusFt * 0.9) {
    if (await sendNtfy(env, 'Harvest Moon \u2014 back inside',
          `Boat is ${Math.round(distFt)} ft from anchor, within the ${radiusFt} ft limit.`, 3, 'white_check_mark')) {
      s.dragAlerted = false;
    }
    result.state = 'recovered';
  } else {
    result.state = breach ? 'watching (unconfirmed)' : 'ok';
  }

  // ---- SCOPE + DEPTH (tide timescale, so the worker owns these) ---
  const tel = await fsGet(env, 'telemetry', env.VESSEL_ID);
  if (tel && !snoozed) {
    const telAgeMin = (now - (num(tel.updatedAt) || 0)) / 60000;
    if (telAgeMin < 10) {
      const depthBelowXducer = num(tel.depthFt);
      const xducerOffset = cfgNum(cfg.transducerOffsetFt, DEFAULTS.transducerOffsetFt);
      const bowRoller    = cfgNum(cfg.bowRollerFt, DEFAULTS.bowRollerFt);
      const rodeOut      = cfgNum(cfg.rodeOutFt, DEFAULTS.rodeOutFt);
      const minScope     = cfgNum(cfg.minScope, DEFAULTS.minScope);
      const minDepth     = cfgNum(cfg.minDepthFt, DEFAULTS.minDepthFt);

      if (depthBelowXducer !== undefined) {
        // Depth below the waterline, which is what both of these actually want.
        const waterDepth = depthBelowXducer + xducerOffset;
        result.waterDepthFt = Math.round(waterDepth * 10) / 10;

        if (bool(cfg.depthEnabled)) {
          if (waterDepth < minDepth) {
            if (!s.depthAlerted) {
              if (await sendNtfy(env, 'SHALLOW \u2014 Harvest Moon',
                    `Water depth ${waterDepth.toFixed(1)} ft, below your ${minDepth} ft limit. Falling tide?`,
                    5, 'rotating_light,ocean')) s.depthAlerted = true;
            }
          } else if (s.depthAlerted && waterDepth > minDepth * 1.15) {
            if (await sendNtfy(env, 'Harvest Moon \u2014 depth recovered',
                  `Depth back to ${waterDepth.toFixed(1)} ft.`, 3, 'white_check_mark')) s.depthAlerted = false;
          }
        }

        // Scope = rode deployed / (water depth + height of bow roller above water)
        if (bool(cfg.scopeEnabled) && rodeOut > 0) {
          const scope = rodeOut / (waterDepth + bowRoller);
          result.scope = Math.round(scope * 10) / 10;
          if (scope < minScope) {
            if (!s.scopeAlerted) {
              if (await sendNtfy(env, 'SCOPE LOW \u2014 Harvest Moon',
                    `Scope is down to ${scope.toFixed(1)}:1 (you set ${minScope}:1 as the floor). `
                    + `${Math.round(rodeOut)} ft of chain out in ${waterDepth.toFixed(1)} ft of water. `
                    + `The tide has come up - consider letting out more.`, 4, 'warning,anchor')) s.scopeAlerted = true;
            }
          } else if (s.scopeAlerted && scope > minScope * 1.1) {
            if (await sendNtfy(env, 'Harvest Moon \u2014 scope recovered',
                  `Scope back to ${scope.toFixed(1)}:1.`, 3, 'white_check_mark')) s.scopeAlerted = false;
          }
        }
      }
    } else {
      result.telemetryNote = 'telemetry stale - skipping scope/depth';
    }
  }

  // ---- DAILY HEALTHY PING ----------------------------------------
  // Without this, "no notification" means either "all is well" or "the alert
  // chain is broken", and you cannot tell which. With it, a missing morning
  // ping is itself the alarm.
  const { day, hour } = localParts(env);
  const pingHour = cfgNum(cfg.dailyPingHour, DEFAULTS.dailyPingHour);
  if (bool(cfg.dailyPingEnabled) && hour >= pingHour && s.lastPingDay !== day) {
    const h = await fsGet(env, 'health', env.VESSEL_ID);
    const guardAge = h ? Math.round((now - (num(h.guardLastRun) || 0)) / 60000) : null;
    const guardTxt = (guardAge === null || !h || !num(h.guardLastRun))
      ? 'Pi guard: not reporting'
      : (guardAge < 5 ? 'Pi guard: healthy' : `Pi guard: last seen ${guardAge} min ago`);
    if (await sendNtfy(env, 'Harvest Moon \u2014 watchers healthy',
          `${Math.round(distFt)} ft from anchor of ${radiusFt} ft. Position ${ageMin} min old. ${guardTxt}.`,
          2, 'white_check_mark,anchor')) {
      s.lastPingDay = day;
    }
  }

  // ---- TRACK (last, so nothing above it can be skipped) ----------
  // Track records the BOW, matching the watch page's own trail. Recording the
  // GPS here would make cloud-supplied points sit a boat-length outside the
  // phone-supplied ones in the same swing trail.
  try { await recordTrackPoint(env, anchorSessionKey(anchorLat, anchorLon), lat, lon, ts); }
  catch (e) { console.log('track recording failed (alarm unaffected):', String(e)); }

  if (JSON.stringify(s) !== before) await writeState(env, s);
  await writeHealth(env);
  return result;
}
