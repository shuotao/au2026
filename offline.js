/* AU 2026 選課工具 —— 「離線閱覽」開關
 *
 * 六個工具頁共用這一支。做四件事：
 *   1. 在導覽列右端放一個開關，讓每個人自己決定要不要下載離線資料
 *   2. 開啟時把六個工具頁抓下來存進 Cache，並顯示下載進度
 *   3. 關閉時把 Service Worker 註銷、快取刪乾淨（不留東西在別人裝置上）
 *   4. 沒網路時給明確提示，並把需要網路的站外連結（AI 選課顧問）標出來
 *
 * 為什麼這個站適合離線：六頁的議程資料、CSS、JS 全部內嵌在 HTML 裡，
 * 執行期沒有任何 fetch／XHR，所以把 HTML 檔本身存起來就等於整個工具可用。
 *
 * 樣式自己注入，不依賴各頁的 CSS —— 六頁的樣式表是各自獨立的。
 */
(function () {
  "use strict";

  var CACHE = "au2026-v1";          // 必須與 sw.js 的 CACHE 一致
  var SW_URL = "./sw.js";
  var LS_KEY = "au2026offline";

  // 要抓下來的檔案。全部是同網域的靜態檔。
  var ASSETS = [
    "./index.html",
    "./planner.html",
    "./onsite.html",
    "./digital-guide.html",
    "./planner-tw.html",
    "./AU2026_%E6%8C%91%E8%AA%B2%E5%B7%A5%E5%85%B7.html",
    "./offline.js",
    "./manifest.json",
    "./icon-192.png",
    "./icon-512.png",
    "./icon-maskable-512.png",
  ];

  var supported =
    "serviceWorker" in navigator &&
    "caches" in window &&
    (location.protocol === "https:" || location.hostname === "localhost");

  /* ---------------- 樣式 ---------------- */
  var css =
    /* 導覽列右端的開關 */
    ".au-sw{margin-left:auto;display:inline-flex;align-items:center;gap:7px;" +
    "white-space:nowrap;font-size:12px;color:#dbe6f2}" +
    ".au-sw button{display:inline-flex;align-items:center;gap:7px;cursor:pointer;" +
    "font:inherit;color:inherit;background:transparent;border:1px solid rgba(255,255,255,.22);" +
    "border-radius:999px;padding:3px 10px 3px 8px}" +
    ".au-sw button:hover{background:rgba(255,255,255,.12);color:#fff}" +
    ".au-sw button:disabled{opacity:.6;cursor:progress}" +
    ".au-sw .lever{position:relative;width:26px;height:14px;border-radius:999px;flex:none;" +
    "background:rgba(255,255,255,.25);transition:background .15s}" +
    ".au-sw .lever::after{content:'';position:absolute;top:2px;left:2px;width:10px;height:10px;" +
    "border-radius:50%;background:#fff;transition:transform .15s}" +
    ".au-sw[data-on='1'] .lever{background:#4ade80}" +
    ".au-sw[data-on='1'] .lever::after{transform:translateX(12px)}" +
    ".au-sw[data-on='1'] button{border-color:rgba(74,222,128,.65)}" +
    ".au-sw .dot{font-size:10.5px;opacity:.8}" +
    /* 底部提示列 */
    ".au-off-bar{position:fixed;left:0;right:0;bottom:0;z-index:9999;display:none;" +
    "align-items:center;gap:10px;justify-content:center;flex-wrap:wrap;padding:9px 14px;" +
    "font-size:12.5px;line-height:1.5;background:#10233b;color:#dbe6f2;" +
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans TC','Microsoft JhengHei',sans-serif;" +
    "box-shadow:0 -1px 6px rgba(0,0,0,.18)}" +
    ".au-off-bar.show{display:flex}" +
    ".au-off-bar b{color:#fff}" +
    ".au-off-bar button{font:inherit;font-weight:700;cursor:pointer;padding:4px 12px;" +
    "border-radius:999px;border:1px solid rgba(255,255,255,.45);background:transparent;color:#fff}" +
    ".au-off-bar button:hover{background:rgba(255,255,255,.14)}" +
    ".au-off-bar .au-off-x{border-color:transparent;font-weight:400;opacity:.75}" +
    /* 離線時，站外服務的連結要一眼看得出來用不了 */
    "html.au-is-offline a[data-au-needs-net]{opacity:.45;text-decoration:line-through}" +
    "html.au-is-offline a[data-au-needs-net]::after{content:'（離線中無法使用）';text-decoration:none;" +
    "display:inline;white-space:nowrap;font-size:.85em;opacity:.9}";

  var style = document.createElement("style");
  style.appendChild(document.createTextNode(css));
  document.head.appendChild(style);

  /* ---------------- 底部提示列 ---------------- */
  var bar, dismissed = {};

  function ensureBar() {
    if (bar) return bar;
    bar = document.createElement("div");
    bar.className = "au-off-bar";
    bar.setAttribute("role", "status");
    bar.setAttribute("aria-live", "polite");
    document.body.appendChild(bar);
    return bar;
  }

  function showBar(html, actionLabel, onAction, kind) {
    if (dismissed[kind]) return;
    var b = ensureBar();
    b.innerHTML = "";
    var msg = document.createElement("span");
    msg.innerHTML = html;
    b.appendChild(msg);
    if (actionLabel) {
      var go = document.createElement("button");
      go.textContent = actionLabel;
      go.onclick = onAction;
      b.appendChild(go);
    }
    var x = document.createElement("button");
    x.className = "au-off-x";
    x.textContent = "關閉";
    x.onclick = function () { dismissed[kind] = true; b.classList.remove("show"); };
    b.appendChild(x);
    b.classList.add("show");
  }

  function hideBar() { if (bar) bar.classList.remove("show"); }

  /* ---------------- 開關 ---------------- */
  var wrap, btn, label, lever;

  function buildSwitch(nav) {
    wrap = document.createElement("span");
    wrap.className = "au-sw";
    wrap.setAttribute("data-on", "0");

    btn = document.createElement("button");
    btn.type = "button";
    btn.setAttribute("role", "switch");
    btn.setAttribute("aria-checked", "false");

    lever = document.createElement("span");
    lever.className = "lever";
    lever.setAttribute("aria-hidden", "true");

    label = document.createElement("span");
    label.textContent = "離線閱覽";

    btn.appendChild(lever);
    btn.appendChild(label);
    wrap.appendChild(btn);
    nav.appendChild(wrap);

    btn.addEventListener("click", function () {
      if (wrap.getAttribute("data-on") === "1") disable();
      else enable();
    });
  }

  function setState(on, text, busy) {
    wrap.setAttribute("data-on", on ? "1" : "0");
    btn.setAttribute("aria-checked", on ? "true" : "false");
    btn.disabled = !!busy;
    label.textContent = text || "離線閱覽";
    btn.title = on
      ? "已下載，沒網路時六個工具頁都打得開。點一下可關閉並刪除已下載的資料。"
      : "點一下把六個挑課工具下載到這台裝置，之後沒網路也能用（約 1.8 MB）。";
  }

  /* ---------------- 開啟：下載 ---------------- */
  async function enable() {
    setState(false, "準備中…", true);
    try {
      await navigator.serviceWorker.register(SW_URL);

      var cache = await caches.open(CACHE);
      var done = 0, failed = [];

      for (var i = 0; i < ASSETS.length; i++) {
        var url = ASSETS[i];
        try {
          // cache:"reload" 確保抓的是伺服器上的新版，不是瀏覽器自己的舊快取
          await cache.add(new Request(url, { cache: "reload" }));
        } catch (e) {
          failed.push(url);
        }
        done++;
        setState(false, "下載中 " + done + "/" + ASSETS.length, true);
      }

      if (failed.length === ASSETS.length) throw new Error("全部下載失敗");

      localStorage.setItem(LS_KEY, "1");
      setState(true, "可離線", false);

      if (failed.length) {
        showBar(
          "<b>離線資料已下載</b>，但有 " + failed.length + " 個檔案沒抓到，" +
          "那幾頁離線時可能打不開。回到有網路的地方再開關一次就會補齊。",
          null, null, "partial"
        );
      } else {
        showBar(
          "<b>已可離線使用</b> —— 六個挑課工具都存到這台裝置了，飛機上、會場沒訊號都打得開。" +
          "站外的 AI 選課顧問仍需要網路。",
          null, null, "enabled"
        );
      }
    } catch (e) {
      console.warn("[au2026] 離線下載失敗：", e);
      localStorage.removeItem(LS_KEY);
      setState(false, "離線閱覽", false);
      showBar("<b>離線資料下載失敗</b> —— 請確認網路後再試一次。", null, null, "failed");
    }
  }

  /* ---------------- 關閉：清乾淨 ---------------- */
  async function disable() {
    setState(true, "清除中…", true);
    try {
      var keys = await caches.keys();
      await Promise.all(
        keys.filter(function (k) { return k.indexOf("au2026-") === 0; })
            .map(function (k) { return caches.delete(k); })
      );
      var regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map(function (r) { return r.unregister(); }));
    } catch (e) {
      console.warn("[au2026] 清除離線資料時出錯：", e);
    }
    localStorage.removeItem(LS_KEY);
    setState(false, "離線閱覽", false);
    showBar("<b>已關閉離線閱覽</b>，下載的資料都從這台裝置刪掉了。", null, null, "disabled");
  }

  /* ---------------- 上／離線狀態 ---------------- */
  function markExternalLinks() {
    var links = document.querySelectorAll("a[href]");
    for (var i = 0; i < links.length; i++) {
      var a = links[i], href = a.getAttribute("href") || "";
      if (!/^https?:\/\//i.test(href)) continue;      // 站內相對連結，離線可用
      if (a.hasAttribute("data-au-needs-net")) continue;
      try {
        if (new URL(href, location.href).origin !== location.origin) {
          a.setAttribute("data-au-needs-net", "");
        }
      } catch (e) { /* 壞掉的 href，略過 */ }
    }
  }

  function onOffline() {
    document.documentElement.classList.add("au-is-offline");
    var ready = localStorage.getItem(LS_KEY) === "1";
    showBar(
      ready
        ? "<b>離線中</b> —— 六個挑課工具都能照常使用；標成刪除線的站外服務需要網路。"
        : "<b>離線中</b> —— 你還沒開啟「離線閱覽」，所以只有目前這頁能看。回到有網路時記得打開右上角的開關。",
      null, null, "offline"
    );
  }

  function onOnline() {
    document.documentElement.classList.remove("au-is-offline");
    dismissed.offline = false;
    hideBar();
  }

  /* ---------------- 啟動 ---------------- */
  function init() {
    markExternalLinks();

    var nav = document.querySelector("nav.aunav");
    if (nav && supported) {
      buildSwitch(nav);
      // 還原先前的選擇
      if (localStorage.getItem(LS_KEY) === "1") {
        setState(true, "可離線", false);
        navigator.serviceWorker.register(SW_URL).catch(function () {});
      } else {
        setState(false, "離線閱覽", false);
      }
    }

    if (!navigator.onLine) onOffline();
  }

  window.addEventListener("offline", onOffline);
  window.addEventListener("online", onOnline);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
