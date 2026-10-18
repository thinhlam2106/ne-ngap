// Né Ngập: nhận thông báo đẩy, và nhớ sẵn các file dữ liệu lớn (đường phố, sông nước, địa điểm).
// Các file dữ liệu có mã phiên bản trong tên nên nhớ mãi không sợ cũ; trang chính (index.html) luôn tải mới.
var DATA_CACHE = 'nn-du-lieu-v1';
self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', function (e) {
  var u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== self.location.origin || !/\/du-lieu-[a-z]+-[0-9a-f]{8}\.json$/.test(u.pathname)) return;
  e.respondWith(caches.open(DATA_CACHE).then(function (c) {
    return c.match(u.pathname).then(function (hit) {
      if (hit) return hit;
      return fetch(e.request).then(function (r) {
        if (r.ok) {
          c.put(u.pathname, r.clone());
          // bỏ bản dữ liệu cũ cùng loại (khác mã phiên bản)
          var kind = u.pathname.match(/du-lieu-([a-z]+)-/)[1];
          c.keys().then(function (ks) { ks.forEach(function (k) { var p = new URL(k.url).pathname; if (p !== u.pathname && p.indexOf('du-lieu-' + kind + '-') >= 0) c.delete(k); }); });
        }
        return r;
      });
    });
  }));
});
self.addEventListener('push', function (e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = { title: 'Né Ngập', body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || 'Né Ngập', {
    body: d.body || '', icon: 'icon-192.png', badge: 'badge-72.png', lang: 'vi',
    tag: d.tag || undefined, renotify: !!d.tag, data: { url: d.url || './' }
  }));
});
self.addEventListener('notificationclick', function (e) {
  e.notification.close();
  var url = new URL((e.notification.data && e.notification.data.url) || './', self.registration.scope).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (ws) {
    for (var i = 0; i < ws.length; i++) {
      if (ws[i].url.indexOf(self.registration.scope) === 0 && 'focus' in ws[i]) { if (ws[i].navigate) ws[i].navigate(url); return ws[i].focus(); }
    }
    return self.clients.openWindow(url);
  }));
});
