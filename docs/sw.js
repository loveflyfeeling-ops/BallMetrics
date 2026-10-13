// 網頁離線可開；加密資料一律先抓網路，失敗才用上次的（資料本身是加密的）
const CACHE = 'bm-pages-v1';
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(['./', 'icons/icon-192.png', 'manifest.webmanifest'])).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  e.respondWith(fetch(req).then(res => {
    if (res.ok) { const c = res.clone(); const key = url.pathname.endsWith('data.enc.json') ? new Request(url.pathname) : req; caches.open(CACHE).then(x => x.put(key, c)); }
    return res;
  }).catch(() => caches.match(url.pathname.endsWith('data.enc.json') ? new Request(url.pathname) : req).then(r => r || caches.match('./'))));
});
