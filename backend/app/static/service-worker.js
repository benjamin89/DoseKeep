self.addEventListener("install", event => event.waitUntil(caches.open("dosekeep-v3").then(cache => cache.addAll(["/", "/styles.css", "/app.js", "/manifest.webmanifest"]))));
self.addEventListener("fetch", event => event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request))));
