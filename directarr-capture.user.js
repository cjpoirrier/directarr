// ==UserScript==
// @name         Directarr Stream Capture
// @namespace    https://github.com/cjpoirrier/directarr
// @version      0.3
// @description  Capture media and subtitle requests and send them to Directarr
// @match        *://*/*
// @include      *
// @grant        GM_xmlhttpRequest
// @connect      *
// @run-at       document-start
// ==/UserScript==

(function () {
  "use strict";

  console.log("[Directarr] script loaded on", location.href);
  // Tiny on-page marker so you can see it ran
  document.addEventListener("DOMContentLoaded", () => {
    const mark = document.createElement("div");
    mark.textContent = "D";
    mark.title = "Directarr capture active";
    mark.style.cssText = "position:fixed;top:8px;right:8px;background:#3b82f6;color:#fff;width:22px;height:22px;border-radius:50%;text-align:center;line-height:22px;font:bold 12px sans-serif;z-index:999999";
    document.body.appendChild(mark);
  });

  const DIRECTARR = "http://10.0.0.93:8787"; // <-- your Directarr URL
  const API_KEY = ""; // optional

  const seen = new Set();

  function isMedia(url) {
    if (!url) return false;
    const u = url.toLowerCase();
    // Reject obvious non-media
    if (u.endsWith(".js") || u.endsWith(".css") || u.endsWith(".woff") || u.endsWith(".svg") || u.endsWith(".png")) return false;
    if (u.includes("subtitlemanager")) return false;
    return (
      u.includes(".m3u8") ||
      u.includes(".mp4") ||
      u.includes(".mkv") ||
      u.includes("/cdn/hls/") ||
      u.includes("master.txt") ||
      u.includes("do=getvideo") ||
      /\/(playlist|master|index)\.m3u8/.test(u)
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
    const title = (document.title || "Captured") + (kind === "sub" ? " [sub]" : "");
    const body = JSON.stringify({
      title: title.slice(0, 200),
      url: url,
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
      onload: function (res) {
        console.log("[Directarr]", kind, url, res.status);
        notify((kind === "sub" ? "Subtitle: " : "Queued: ") + title.slice(0, 50));
      },
      onerror: function () {
        notify("Directarr error");
      },
    });
  }

  function check(url) {
    if (!url) return;
    if (url.includes("neocdn") || url.includes("m3u8") || url.includes(".mp4") || url.includes("getVideo") || url.includes("/hls/")) {
      console.log("[Directarr] saw:", url);
    }
    if (isMedia(url)) {
      // If it's the getVideo API, fetch it and look for a real playlist
      if (url.includes("do=getVideo") || url.includes("do=getvideo")) {
        fetch(url, { credentials: "include" })
          .then(r => r.text())
          .then(text => {
            const m = text.match(/https?:\/\/[^"'\s]+\.m3u8[^"'\s]*/i) ||
                      text.match(/https?:\/\/[^"'\s]+\/master\.txt[^"'\s]*/i) ||
                      text.match(/https?:\/\/[^"'\s]+\/cdn\/hls\/[^"'\s]+/i);
            if (m) {
              console.log("[Directarr] resolved playlist:", m[0]);
              send(m[0], "media");
            } else {
              console.log("[Directarr] getVideo response (first 300):", text.slice(0, 300));
            }
          })
          .catch(() => {});
        return;
      }
      send(url, "media");
    } else if (isSubtitle(url)) {
      send(url, "sub");
    }
  }

  function notify(msg) {
    const el = document.createElement("div");
    el.textContent = msg;
    el.style.cssText =
      "position:fixed;bottom:12px;right:12px;background:#111;color:#fff;" +
      "padding:8px 12px;border-radius:6px;z-index:999999;font:13px sans-serif;" +
      "box-shadow:0 2px 8px rgba(0,0,0,.4);max-width:320px";
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3500);
  }

  // fetch
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : (input && input.url);
      check(url);
    } catch (e) {}
    return origFetch.apply(this, arguments);
  };

  // XHR
  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    try { check(url); } catch (e) {}
    return origOpen.apply(this, arguments);
  };

  // Performance observer as a backup (catches some players that bypass hooks)
  try {
    const po = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        if (entry.name) check(entry.name);
      }
    });
    po.observe({ type: "resource", buffered: true });
  } catch (e) {}

  console.log("[Directarr] capture active (media + subtitles)");
})();
