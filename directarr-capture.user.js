// ==UserScript==
// @name         Directarr Stream Capture
// @namespace    https://github.com/cjpoirrier/directarr
// @version      0.4
// @description  Quietly capture media streams and send them to Directarr
// @match        *://*/*
// @include      *
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      10.0.0.93
// @connect      localhost
// @connect      127.0.0.1
// @connect      *
// @run-at       document-start
// ==/UserScript==

(function () {
  "use strict";

  // Configurable via Tampermonkey menu (right-click the script)
  let DIRECTARR = GM_getValue("directarrUrl", "http://localhost:8787");
  const API_KEY = GM_getValue("apiKey", "");

  GM_registerMenuCommand("Set Directarr URL", () => {
    const url = prompt("Directarr base URL", DIRECTARR);
    if (url) {
      DIRECTARR = url.replace(/\/$/, "");
      GM_setValue("directarrUrl", DIRECTARR);
      alert("Saved: " + DIRECTARR);
    }
  });

  const seen = new Set();

  function isMedia(url) {
    if (!url) return false;
    const u = url.toLowerCase();
    if (u.endsWith(".js") || u.endsWith(".css") || u.endsWith(".woff") || u.endsWith(".svg") || u.endsWith(".png")) return false;
    if (u.includes("subtitlemanager")) return false;
    return (
      u.includes(".m3u8") ||
      u.includes(".mp4") ||
      u.includes(".mkv") ||
      u.includes("/cdn/hls/") ||
      u.includes("master.txt") ||
      u.includes("do=getvideo") ||
      u.includes("getvideo") ||
      /\/(playlist|master|index)\.m3u8/.test(u) ||
      u.includes("getsources") ||
      u.includes("/ajax/sources")
    );
  }

  function isSubtitle(url) {
    if (!url) return false;
    const u = url.toLowerCase();
    return (
      u.includes(".vtt") ||
      u.includes(".srt") ||
      u.includes(".ass") ||
      u.includes("subtitle") ||
      (u.includes("neocdn") && u.endsWith(".html") && /\[.*\]/.test(url))
    );
  }

  function send(url, kind) {
    if (seen.has(url)) return;
    seen.add(url);
    const title = (document.title || "Captured").slice(0, 180) + (kind === "sub" ? " [sub]" : "");
    const body = JSON.stringify({
      title,
      url,
      headers: {
        Referer: location.href,
        Origin: location.origin,
        "User-Agent": navigator.userAgent,
      },
    });
    GM_xmlhttpRequest({
      method: "POST",
      url: DIRECTARR + "/api/v1/streams/download",
      headers: {
        "Content-Type": "application/json",
        ...(API_KEY ? { "X-Api-Key": API_KEY } : {}),
      },
      data: body,
      onload: () => console.log("[Directarr]", kind, url),
      onerror: () => console.warn("[Directarr] send failed", url),
    });
  }

  function check(url) {
    if (!url) return;
    if (isMedia(url)) {
      if (url.includes("do=getVideo") || url.includes("do=getvideo") || url.includes("getvideo")) {
        fetch(url, { credentials: "include" })
          .then(r => r.text())
          .then(text => {
            const m = text.match(/https?:\/\/[^"'\s]+\.m3u8[^"'\s]*/i) ||
                      text.match(/https?:\/\/[^"'\s]+\/master\.txt[^"'\s]*/i) ||
                      text.match(/https?:\/\/[^"'\s]+\/cdn\/hls\/[^"'\s]+/i);
            if (m) send(m[0], "media");
          })
          .catch(() => {});
        return;
      }
      send(url, "media");
    } else if (isSubtitle(url)) {
      send(url, "sub");
    }
  }

  // Hooks
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : input?.url;
      check(url);
    } catch (e) {}
    return origFetch.apply(this, arguments);
  };

  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    try { check(url); } catch (e) {}
    return origOpen.apply(this, arguments);
  };

  try {
    const po = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) if (e.name) check(e.name);
    });
    po.observe({ type: "resource", buffered: true });
  } catch (e) {}
})();
