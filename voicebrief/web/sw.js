/* VoiceBrief 서비스워커
 * 앱 셸만 캐시한다. API 응답은 항상 네트워크(요약 결과가 실시간이어야 하므로).
 */
const CACHE = 'voicebrief-v1';
const SHELL = ['/', '/static/styles.css', '/static/app.js', '/manifest.webmanifest'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;                      // 업로드는 통과
  if (new URL(request.url).pathname.startsWith('/api/')) return; // API는 항상 네트워크

  event.respondWith(
    caches.match(request).then((cached) =>
      cached || fetch(request).then((res) => {
        if (res.ok && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(request, copy));
        }
        return res;
      }).catch(() => cached)
    )
  );
});
