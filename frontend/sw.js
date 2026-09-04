/* WeatherGPT service worker.
 *
 * Scope is deliberately narrow. It caches the APP SHELL -- the page chrome,
 * icons and manifest -- so the installed app opens instantly and still shows
 * a usable screen with no network.
 *
 * It does NOT cache /api responses, and that is a correctness decision rather
 * than an oversight. This project's whole claim is that every number on screen
 * carries a live source and issue time, and that it says so when it is
 * degraded. A service worker replaying yesterday's forecast would put a real
 * number on screen with no indication it was stale, from a layer that sits
 * BELOW the degradation machinery in main.py and could not annotate it even if
 * it wanted to. Silently serving stale weather is the one failure this system
 * exists to prevent, so offline means "the app says it cannot reach the API",
 * not "the app shows you an old number".
 *
 * Navigation is network-first for the same reason plus a practical one: the
 * root HTML has the demo token injected into it at request time, so a cached
 * copy would go stale the moment that token is rotated.
 */
const VERSION = 'wgpt-v1';
const SHELL = `shell-${VERSION}`;

// Cached on install. Kept minimal: everything here must be safe to serve
// offline and must not contain a grounded weather value.
const SHELL_ASSETS = [
  // The two documents themselves. Safe to precache only because neither ships
  // a grounded weather value in its markup: every figure renders as an em dash
  // until it is fetched, and the dashboard repaints from its own last-known
  // reading in localStorage with the time it was taken. A cached shell must
  // never be able to show a stale number as if it were current -- during a
  // cyclone the network is exactly what fails.
  '/',
  '/app',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/icon-maskable-512.png',
  '/icons/apple-touch-icon.png',
  '/manifest.webmanifest',
];

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL);
    // addAll is atomic: one 404 would fail the whole install, so assets are
    // added individually and a missing one degrades instead of bricking the
    // worker.
    await Promise.all(SHELL_ASSETS.map(url =>
      cache.add(new Request(url, { cache: 'reload' })).catch(() => {})
    ));
    self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    // Drop caches from older versions so a redeploy cannot leave a stale
    // shell behind.
    const keys = await caches.keys();
    await Promise.all(
      keys.filter(k => k !== SHELL).map(k => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const req = event.request;

  // Only GET is cacheable, and only same-origin. POSTs to /api/chat and the
  // dissemination endpoints must always hit the network.
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Never intercept the API or the websocket. Live data stays live, and any
  // failure surfaces through the app's own degradation path.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/')) return;

  // Navigations: network first, cached shell only as an offline fallback.
  if (req.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        const cache = await caches.open(SHELL);
        cache.put('/offline-shell', fresh.clone());
        return fresh;
      } catch (e) {
        const cached = await caches.match('/offline-shell');
        return cached || new Response(
          '<!doctype html><meta charset="utf-8"><title>WeatherGPT offline</title>' +
          '<body style="font:16px system-ui;background:#0b1628;color:#f0f9ff;' +
          'padding:2rem"><h1>WeatherGPT is offline</h1><p>No connection, so no ' +
          'live weather. Nothing is shown from cache, because an old forecast ' +
          'presented as current is worse than none.</p>',
          { headers: { 'Content-Type': 'text/html; charset=utf-8' }, status: 503 }
        );
      }
    })());
    return;
  }

  // Static shell assets: cache first, they are versioned by VERSION.
  event.respondWith((async () => {
    const cached = await caches.match(req);
    if (cached) return cached;
    try {
      const fresh = await fetch(req);
      if (fresh.ok) {
        const cache = await caches.open(SHELL);
        cache.put(req, fresh.clone());
      }
      return fresh;
    } catch (e) {
      return cached || Response.error();
    }
  })());
});
