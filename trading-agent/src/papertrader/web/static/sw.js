// Keeps the app's look (styles, script, icons) on the phone, and shows a short "can't reach
// your PC" page when the PC can't be reached. Pages with data are never stored on the phone.
const V = "__ASSET_V__"; // filled in by the server: changes with every new style or script
const CACHE = "papertrader-" + V;
const ASSETS = ["/static/app.css?v=" + V, "/static/app.js?v=" + V, "/static/icons/icon-192.png", "/offline"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE)
    .then((cache) => cache.addAll(ASSETS.map((u) => new Request(u, { cache: "reload" }))))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {
    // No browser bar in the home-screen app: never hang on a blank screen when the PC is off.
    const timeout = new Promise((_, reject) => setTimeout(reject, 12000));
    event.respondWith(Promise.race([fetch(request), timeout]).catch(() => caches.match("/offline")));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(caches.match(request).then((hit) => hit || fetch(request).then((response) => {
      if (response.ok && url.searchParams.get("v") === V) {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
      }
      return response;
    })));
  }
});
