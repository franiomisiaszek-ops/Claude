/* Cramless service worker: offline app shell, offline "Today", and push reminders. */
const CACHE = 'cramless-v1';
const SHELL = ['/', '/manifest.webmanifest', '/favicon.svg', '/icons/icon-192.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

const networkFirst = async (request, cacheKey) => {
  const cache = await caches.open(CACHE);
  try {
    const res = await fetch(request);
    if (res.ok) cache.put(cacheKey ?? request, res.clone());
    return res;
  } catch (e) {
    const hit = await cache.match(cacheKey ?? request);
    if (hit) return hit;
    throw e;
  }
};

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== self.location.origin) return;
  // Last known plan, so "Today" opens offline.
  if (url.pathname === '/api/state') return event.respondWith(networkFirst(req, '/api/state'));
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/oauth/')) return;
  if (url.pathname.startsWith('/assets/')) {
    return event.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => (caches.open(CACHE).then((c) => c.put(req, res.clone())), res))));
  }
  if (req.mode === 'navigate') return event.respondWith(networkFirst(req, '/'));
});

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { title: 'Cramless', body: event.data ? event.data.text() : '' };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || 'Cramless', {
      body: data.body || '',
      icon: '/icons/icon-192.png',
      badge: '/icons/badge-72.png',
      tag: data.tag,
      data: { url: data.url || '/' },
    }),
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ('focus' in client) {
          client.navigate(target);
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    }),
  );
});

self.addEventListener('message', (event) => {
  if (event.data === 'clear-data') event.waitUntil(caches.open(CACHE).then((c) => c.delete('/api/state')));
});
