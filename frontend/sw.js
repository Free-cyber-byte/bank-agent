/* ZeroStep service worker —— 零依赖，仅缓存静态外壳；/api/ 一律走网络，不缓存。
 * 版本号：升级静态资源时改 VERSION，旧缓存会在 activate 阶段被清掉。
 */
'use strict';

var VERSION = '0.5.1';
var CACHE_NAME = 'zerostep-shell-v' + VERSION;

var SHELL = [
  '/',
  '/index.html',
  '/styles.css',
  '/app.js',
  '/manifest.webmanifest',
  '/assets/icons/icon-192.png',
  '/assets/icons/icon-512.png',
  '/assets/icons/icon-192-maskable.png',
  '/assets/icons/icon-512-maskable.png',
  '/assets/icons/apple-touch-icon.png'
];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(SHELL.map(function (url) {
        // 每次安装都重新拉取，避免把旧文件写进新缓存
        return new Request(url, { cache: 'reload' });
      }));
    }).then(function () {
      return self.skipWaiting();
    })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (key) {
        if (key !== CACHE_NAME && key.indexOf('zerostep-') === 0) return caches.delete(key);
        return null;
      }));
    }).then(function () {
      return self.clients.claim();
    })
  );
});

function isApi(url) {
  return url.pathname.indexOf('/api/') === 0 || url.pathname === '/api';
}

self.addEventListener('fetch', function (event) {
  var request = event.request;
  if (request.method !== 'GET') return;
  var url;
  try {
    url = new URL(request.url);
  } catch (e) {
    return;
  }
  if (url.origin !== self.location.origin) return;

  // 业务接口：永远取实时数据（审批/余额/审计不能看到旧状态）
  if (isApi(url)) {
    event.respondWith(fetch(request));
    return;
  }

  // 导航请求：网络优先，失败回落外壳，保证离线也能打开 App
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(function () {
        return caches.match('/index.html').then(function (hit) {
          return hit || caches.match('/');
        });
      })
    );
    return;
  }

  // 静态资源：缓存优先，后台静默更新
  event.respondWith(
    caches.match(request).then(function (hit) {
      var fetching = fetch(request).then(function (res) {
        if (res && res.ok && res.type === 'basic') {
          var copy = res.clone();
          caches.open(CACHE_NAME).then(function (cache) { cache.put(request, copy); });
        }
        return res;
      }).catch(function () { return hit || Response.error(); });
      return hit || fetching;
    })
  );
});
