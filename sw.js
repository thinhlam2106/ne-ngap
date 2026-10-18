// Né Ngập: nhận thông báo đẩy. Không lưu bộ đệm trang (không có fetch), nên bản mới luôn được tải ngay.
self.addEventListener('install', function () { self.skipWaiting(); });
self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });
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
