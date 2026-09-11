/* AU 2026 選課工具 —— Service Worker
 *
 * 職責很窄：**只負責在沒網路時把已經存好的檔案供應出來**。
 * 「要不要下載、下載哪些」由頁面上的「離線閱覽」開關決定（見 offline.js），
 * 這樣使用者才看得到下載進度，也才不會有人在沒同意的情況下被灌 1.8 MB。
 *
 * 版本規則：改過任何被快取的檔案，就把 VERSION 加一。
 * 舊版快取會在 activate 時清掉，頁面那邊會提示使用者重新下載。
 */

const VERSION = "v1";
const CACHE = "au2026-" + VERSION;   // 必須與 offline.js 的 CACHE 一致

self.addEventListener("install", () => {
  // 不預抓任何東西 —— 交給頁面的開關。
  // 也不 skipWaiting，避免有人正在排課表時被換版。
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((k) => k.startsWith("au2026-") && k !== CACHE).map((k) => caches.delete(k))
      );
      await self.clients.claim();
    })()
  );
});

self.addEventListener("message", (event) => {
  if (event.data === "skip-waiting") self.skipWaiting();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  // 跨網域（AI 選課顧問、Google Fonts）一律不攔，讓它們照瀏覽器原本行為走：
  // 離線時自然失敗，這是正確的 —— 那些服務本來就需要網路。
  let url;
  try { url = new URL(req.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return;

  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE);
      const cached = await cache.match(req, { ignoreSearch: true });

      if (cached) {
        // 快取優先 —— 會場網路慢的時候差別很有感。
        // 同時在背景抓新版，下次進來就是新的。
        event.waitUntil(
          (async () => {
            try {
              const fresh = await fetch(req);
              if (fresh && fresh.ok) await cache.put(req, fresh.clone());
            } catch (e) { /* 離線，維持用快取 */ }
          })()
        );
        return cached;
      }

      try {
        return await fetch(req);
      } catch (e) {
        // 離線又沒存過：導覽請求就退回首頁，至少讓人有東西可看
        if (req.mode === "navigate") {
          const fallback = await cache.match("./index.html");
          if (fallback) return fallback;
        }
        return new Response("離線中，而且這個檔案沒有被下載下來。", {
          status: 504,
          headers: { "Content-Type": "text/plain; charset=utf-8" },
        });
      }
    })()
  );
});
