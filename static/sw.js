// GreenScope Service Worker - Offline caching + Push notifications for watering reminders
const CACHE_NAME = 'greenscope-v2';
const STATIC_ASSETS = [
  '/',
  '/index.html',
  '/report.html',
  '/impact.html',
  '/manifest.json'
];

// Install event - cache static assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_ASSETS);
    })
  );
  self.skipWaiting();
});

// Activate event - clean up old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      );
    })
  );
  self.clients.claim();
});

// Fetch event - serve from cache, fallback to network
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  
  // Skip non-GET requests
  if (event.request.method !== 'GET') {
    return;
  }
  
  // Skip external requests (APIs, etc.)
  if (url.origin !== location.origin) {
    return;
  }
  
  // Handle API requests - network first, cache fallback
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirstStrategy(event.request));
    return;
  }
  
  // Handle report pages - cache first for offline viewing
  if (url.pathname.startsWith('/report/') || url.pathname === '/impact') {
    event.respondWith(cacheFirstStrategy(event.request));
    return;
  }
  
  // Handle static assets - cache first
  event.respondWith(cacheFirstStrategy(event.request));
});

// Cache first strategy for static assets and report pages
async function cacheFirstStrategy(request) {
  const cached = await caches.match(request);
  if (cached) {
    return cached;
  }
  
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    // Return offline page for navigation requests
    if (request.mode === 'navigate') {
      return caches.match('/index.html');
    }
    throw error;
  }
}

// Network first strategy for API requests
async function networkFirstStrategy(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await caches.match(request);
    if (cached) {
      // Add header to indicate stale content
      const response = cached.clone();
      response.headers.set('X-GreenScope-Offline', 'true');
      return response;
    }
    // Return offline response for report API
    if (request.url.includes('/api/v1/reports/')) {
      return new Response(
        JSON.stringify({
          error: 'Offline',
          message: 'This report is not available offline. Please connect to the internet to load the latest data.'
        }),
        {
          status: 503,
          headers: { 'Content-Type': 'application/json' }
        }
      );
    }
    throw error;
  }
}

// Background sync for offline actions
self.addEventListener('sync', (event) => {
  if (event.tag === 'sync-purchases' || event.tag === 'sync-sales' || event.tag === 'sync-watering') {
    event.waitUntil(syncOfflineData(event.tag));
  }
});

async function syncOfflineData(tag) {
  console.log('Background sync triggered:', tag);
  // Future: sync offline purchases/sales/watering when back online
}

// Push notifications for watering reminders
self.addEventListener('push', (event) => {
  if (event.data) {
    try {
      const data = event.data.json();
      const options = {
        body: data.body || 'Time to water your plants!',
        icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">🌿</text></svg>',
        badge: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">🌿</text></svg>',
        vibrate: [200, 100, 200],
        tag: data.tag || 'watering-reminder',
        renotify: true,
        data: data.data || {},
        actions: [
          { action: 'done', title: '✅ Done' },
          { action: 'snooze', title: '⏰ Snooze 1h' }
        ]
      };
      
      event.waitUntil(
        self.registration.showNotification(data.title || '🌿 GreenScope Watering Reminder', options)
      );
    } catch (e) {
      console.error('Push notification error:', e);
    }
  }
});

// Notification click handling
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  
  if (event.action === 'done') {
    // Mark watering as done - could sync to server
    console.log('Watering marked as done');
    event.waitUntil(
      clients.matchAll({ type: 'window' }).then(clientList => {
        for (const client of clientList) {
          client.postMessage({ type: 'watering-done', data: event.notification.data });
        }
      })
    );
  } else if (event.action === 'snooze') {
    // Snooze for 1 hour - reschedule notification
    console.log('Snoozed for 1 hour');
    // In a real implementation, this would reschedule via the server
  } else {
    // Default click - open the report page
    const reportId = event.notification.data?.reportId;
    const url = reportId ? `/report/${reportId}` : '/';
    event.waitUntil(
      clients.matchAll({ type: 'window' }).then(clientList => {
        for (const client of clientList) {
          if (client.url.includes(reportId) && 'focus' in client) {
            return client.focus();
          }
        }
        if (clients.openWindow) {
          return clients.openWindow(url);
        }
      })
    );
  }
});

// Periodic background sync for watering schedule (requires Periodic Background Sync API)
self.addEventListener('periodicsync', (event) => {
  if (event.tag === 'check-watering-schedule') {
    event.waitUntil(checkWateringSchedule());
  }
});

async function checkWateringSchedule() {
  // This would check local IndexedDB for watering schedules
  // and show notifications for due waterings
  console.log('Checking watering schedule...');
}

console.log('GreenScope SW v2 loaded - offline caching + push notifications enabled');