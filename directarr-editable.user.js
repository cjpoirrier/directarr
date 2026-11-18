// ==UserScript==
// @name         Directarr Stream Capture (Editable)
// @namespace    https://github.com/cjpoirrier/directarr
// @version      0.5
// @description  Capture media streams. Edit the isMedia / patterns below. Does NOT auto-send — use the extension badge + popup to download.
// @match        *://*/*
// @include      *
// @grant        none
// @run-at       document-start
// ==/UserScript==

(function () {
  "use strict";

  // ========== EDIT THIS SECTION ==========
  // Add or remove URL patterns that should be treated as media.
  function isMedia(url) {
    if (!url) return false;
    const u = url.toLowerCase();
    if (u.endsWith(".js") || u.endsWith(".css") || u.endsWith(".woff") || u.endsWith(".svg") || u.endsWith(".png")) return false;
    return (
      u.includes(".m3u8") ||
      u.includes(".mp4") ||
      u.includes(".mkv") ||
      u.includes("/cdn/hls/") ||
      u.includes("master.txt") ||
      u.includes("do=getvideo") ||
      u.includes("getvideo") ||
      u.includes("getsources") ||
      u.includes("/ajax/sources") ||
      /\/(playlist|master|index)\.m3u8/.test(u)
      // Add more patterns here, e.g.:
      // || u.includes("your-cdn-domain")
    );
  }

  function guessQuality(url) {
    const u = url.toLowerCase();
    if (/\b(1080|fhd|1920)\b/.test(u)) return "1080p";
    if (/\b(720|hd|1280)\b/.test(u)) return "720p";
    if (/\b(480|sd)\b/.test(u)) return "480p";
    if (/\b(360)\b/.test(u)) return "360p";
    if (/\b(2160|4k)\b/.test(u)) return "2160p";
    return "unknown";
  }
  // ========== END EDIT SECTION ==========

  const media = [];
  const seen = new Set();

  function add(url) {
    if (seen.has(url)) return;
    seen.add(url);
    const entry = { url, quality: guessQuality(url) };
    media.push(entry);
    console.log("[Directarr userscript] captured", entry.quality, url);
    // If you later want to talk to the extension, you can postMessage here.
  }

  function check(url) {
    if (!url || !isMedia(url)) return;
    if (url.includes("getvideo") || url.includes("do=getVideo")) {
      fetch(url, { credentials: "include" })
        .then(r => r.text())
        .then(text => {
          const m = text.match(/https?:\/\/[^"'\s]+\.m3u8[^"'\s]*/i);
          if (m) add(m[0]);
        })
        .catch(() => {});
      return;
    }
    add(url);
  }

  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try { check(typeof input === "string" ? input : input?.url); } catch (e) {}
    return origFetch.apply(this, arguments);
  };
  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    try { check(url); } catch (e) {}
    return origOpen.apply(this, arguments);
  };
  try {
    const po = new PerformanceObserver(list => {
      for (const e of list.getEntries()) if (e.name) check(e.name);
    });
    po.observe({ type: "resource", buffered: true });
  } catch (e) {}

  // Expose for debugging in console
  window.__directarrCaptured = media;
})();
