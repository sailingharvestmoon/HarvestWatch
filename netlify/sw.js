// Harvest Watch - offline weather (service worker)
// =================================================
// Does nothing until "Save for offline" (Weather tab) has saved a copy.
// After that, when the internet can't be reached, the weather screens are
// served from that copy: the app itself, the pre-built maps, the forecast
// table, "Now", NOAA's fronts charts and what the passage router needs.
//
// Rules it keeps:
//   - Online, everything comes from the network exactly as before. The saved
//     copy is used only when a request fails or hangs.
//   - It only ever looks at the app's own files and weather sources (below).
//     The anchor watch, alarms, telemetry, sign-in and every write go straight
//     to the network without passing through here, so they can never be
//     answered from an old copy.
//   - Saved files are matched exactly: a newer model run's maps are never
//     answered with an older run's pictures, and NOAA's fronts charts come
//     from the copy only when the app asks for the saved set (?v=off), which
//     it labels with the times they had when saved.
//   - Nothing is saved here. The app saves the copy (index.html, offSave) into
//     a cache named hw-offline-<time saved>; this only reads it.

const PREFIX = 'hw-offline-';
const WAIT_MS = 5000;            // a saved file whose network request takes longer than this comes from the copy
const OFFLINE_HOLD_MS = 30000;   // after a failure, use the copy straight away for this long before trying again

let offlineUntil = 0;            // network known to be down until then
let savingUntil = 0;             // the app is saving a new copy: stay out of the way
let told = null;                 // last offline/online state sent to the app

// How this worker treats a request: null = leave it alone.
function rule(u) {
  if (u.origin === self.location.origin)                                          // the app itself
    return u.pathname.endsWith('/sw.js') ? null : { loose: true };
  if (u.hostname === 'sailingharvestmoon.github.io') return { loose: false };    // pre-built maps
  if (u.hostname === 'cdnjs.cloudflare.com')
    return u.pathname.includes('/leaflet/') ? { loose: false } : null;
  if (u.hostname === 'www.wpc.ncep.noaa.gov')                                     // fronts charts
    return /[?&]v=off(&|$)/.test(u.search) ? { loose: true, copyOnly: true } : null;
  if (u.hostname === 'firestore.googleapis.com')                                  // weather docs only
    return /\/documents\/weather\/[^/]+$/.test(u.pathname) ? { loose: false } : null;
  return null;
}

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('message', e => {
  if (e.data && e.data.hw === 'saving') savingUntil = e.data.on ? Date.now() + 15 * 60000 : 0;
});

async function savedCopy() {
  const name = (await caches.keys()).filter(n => n.startsWith(PREFIX)).sort().pop();
  return name ? { cache: await caches.open(name), savedAt: +name.slice(PREFIX.length) || 1 } : null;
}

function tell(on) {
  if (told === on) return;
  told = on;
  self.clients.matchAll({ includeUncontrolled: true }).then(cs => cs.forEach(c => c.postMessage({ hw: 'offline', on })));
}

function fromCopy(hit, savedAt) {
  tell(true);
  if (hit.type === 'opaque') return hit;          // NOAA's charts: handed over as saved
  const h = new Headers(hit.headers);
  h.set('x-hw-offline', String(savedAt));         // lets the app know it's looking at the saved copy
  return new Response(hit.body, { status: hit.status, statusText: hit.statusText, headers: h });
}

async function answer(req, how) {
  let copy = null, hit = null;
  try {
    copy = await savedCopy();
    if (copy) hit = await copy.cache.match(req, { ignoreSearch: how.loose, ignoreVary: true });
  } catch (e) { hit = null; }
  if (!hit) return fetch(req);                    // not in the saved copy: the network, as always
  if (how.copyOnly || Date.now() < offlineUntil) return fromCopy(hit, copy.savedAt);
  let timer;
  try {
    const r = await Promise.race([
      fetch(req),
      new Promise((_, no) => { timer = setTimeout(() => no(new Error('slow')), WAIT_MS); }),
    ]);
    clearTimeout(timer);
    if (r.status >= 500) throw new Error('HTTP ' + r.status);
    tell(false);
    return r;                                     // the network answered: use it, whatever it said
  } catch (e) {
    clearTimeout(timer);
    offlineUntil = Date.now() + OFFLINE_HOLD_MS;
    return fromCopy(hit, copy.savedAt);
  }
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || Date.now() < savingUntil) return;
  let u;
  try { u = new URL(req.url); } catch (err) { return; }
  const how = rule(u);
  if (!how) return;                               // not ours: untouched
  e.respondWith(answer(req, how).catch(() => fetch(req)));
});
