// EventBooking Service Worker v1.6
const CACHE_NAME = 'eventbooking-v1.6';
const STATIC_ASSETS = [
  '/static/css/style.css',
  '/static/js/main.js',
  '/static/js/calendar.js',
  '/static/js/booking.js',
  '/static/js/admin.js',
  '/static/manifest.json'
];

// Installa: cache asset statici
self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return cache.addAll(STATIC_ASSETS);
    }).then(function() {
      return self.skipWaiting();
    })
  );
});

// Attiva: pulisci cache vecchie
self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys().then(function(cacheNames) {
      return Promise.all(
        cacheNames.filter(function(name) {
          return name !== CACHE_NAME;
        }).map(function(name) {
          return caches.delete(name);
        })
      );
    }).then(function() {
      return self.clients.claim();
    })
  );
});

// Fetch: cache-first per statici, network-first per API
self.addEventListener('fetch', function(event) {
  var url = new URL(event.request.url);

  // Non intercettare richieste API o esterne
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/init-db')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // Non intercettare richieste POST/PUT/DELETE
  if (event.request.method !== 'GET') {
    event.respondWith(fetch(event.request));
    return;
  }

  // Pagine HTML (navigazione): sempre dalla rete, mai dalla cache.
  // Sono dinamiche e contengono dati (elenco prenotazioni, nomi): servirle dalla cache
  // mostrerebbe dati vecchi dopo una modifica. La cache resta solo come ultima spiaggia offline.
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(function() {
        return caches.match(event.request).then(function(cached) {
          return cached || caches.match('/');
        });
      })
    );
    return;
  }

  // Cache-first per asset statici
  event.respondWith(
    caches.match(event.request).then(function(cached) {
      if (cached) {
        // Aggiorna in background
        fetch(event.request).then(function(response) {
          if (response && response.status === 200) {
            caches.open(CACHE_NAME).then(function(cache) {
              cache.put(event.request, response);
            });
          }
        }).catch(function() {});
        return cached;
      }

      return fetch(event.request).then(function(response) {
        if (!response || response.status !== 200 || response.type !== 'basic') {
          return response;
        }
        var responseToCache = response.clone();
        caches.open(CACHE_NAME).then(function(cache) {
          cache.put(event.request, responseToCache);
        });
        return response;
      }).catch(function() {
        // Offline: mostra pagina fallback se disponibile
        if (event.request.mode === 'navigate') {
          return caches.match('/');
        }
      });
    })
  );
});
