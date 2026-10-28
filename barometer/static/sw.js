// Barometer service worker: caches the app shell so it opens instantly.
// API responses are never cached; private data only ever lives in memory.
const CACHE = "barometer-shell-v1";
const SHELL = [
  "/static/app.css",
  "/static/js/main.js",
  "/static/js/api.js",
  "/static/js/ui.js",
  "/static/js/views/common.js",
  "/static/js/views/today.js",
  "/static/js/views/capture.js",
  "/static/js/views/people.js",
  "/static/js/views/brief.js",
  "/static/js/views/map.js",
  "/static/js/views/reflect.js",
  "/static/vendor/cytoscape.min.js",
  "/static/vendor/fonts/fraunces-latin-wght-normal.woff2",
  "/static/vendor/fonts/inter-latin-wght-normal.woff2",
  "/static/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin || !url.pathname.startsWith("/static/")) return;
  // Network first so updates land; fall back to the cached shell when the Pi is unreachable.
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy));
        return res;
      })
      .catch(() => caches.match(event.request)),
  );
});
