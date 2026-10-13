// BallMetrics service worker：畫面離線可開；資料一律走網路（失敗時顯示最後一次的資料）
const SHELL = 'bm-shell-v1';
const DATA = 'bm-data-v1';
const SHELL_FILES = ['/', '/static/icons/icon-192.png', '/manifest.webmanifest'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => ![SHELL, DATA].includes(k)).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;              // CDN 交給瀏覽器快取
  if (url.pathname.endsWith('.csv') || url.pathname === '/login') return;
  const isApi = url.pathname.startsWith('/api/');
  e.respondWith(
    fetch(req).then(res => {
      if (res.ok && (isApi ? ['/api/analysis', '/api/stats', '/api/bets'].includes(url.pathname) : true)) {
        const copy = res.clone();
        caches.open(isApi ? DATA : SHELL).then(c => c.put(req, copy));
      }
      return res;
    }).catch(() => caches.match(req).then(r => r || (isApi
      ? new Response(JSON.stringify({ detail: '目前離線，顯示的是最後一次的資料' }), { status: 503, headers: { 'Content-Type': 'application/json' } })
      : caches.match('/'))))
  );
});
