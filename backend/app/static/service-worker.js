const CACHE = "dosekeep-v5";
const ASSETS = ["/", "/styles.css?v=0.3.2", "/app.js?v=0.3.2", "/manifest.webmanifest?v=0.3.2"];

self.addEventListener("install", event => {
  self.skipWaiting();
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)));
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

// Always prefer the deployed version. The cache is only an offline fallback,
// so Portainer redeploys are visible without manually clearing browser data.
self.addEventListener("fetch", event => {
  if (event.request.method !== "GET") return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});
