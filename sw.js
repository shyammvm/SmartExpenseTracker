// Ledger Service Worker - Cache App Shell for Instant Loading
const CACHE_NAME = 'ledger-cache-v1';
const STATIC_ASSETS = [
  './styles.css',
  './config.js',
  './index.html',
  './dashboard.html',
  './history.html',
  './budgets.html',
  './instructions.html',
  './categories.html',
  './conflicts.html',
  './manifest.json'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_ASSETS).catch((err) => {
        console.warn('Some assets failed to cache:', err);
      });
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  // Only cache GET requests that are not API calls
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);

  // Skip localhost, 127.0.0.1, and API backend URLs completely
  if (url.hostname === 'localhost' || url.hostname === '127.0.0.1' || url.origin.includes('onrender.com') || url.pathname.includes('/summary/') || url.pathname.includes('/expenses/')) {
    return;
  }

  // Network-first with cache fallback for HTML/CSS/JS assets
  event.respondWith(
    fetch(event.request)
      .then((networkResponse) => {
        if (networkResponse && networkResponse.status === 200 && networkResponse.type === 'basic') {
          const responseToCache = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, responseToCache);
          });
        }
        return networkResponse;
      })
      .catch(() => caches.match(event.request))
  );
});
