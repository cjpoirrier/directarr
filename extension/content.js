// Capture media and subtitle URLs
const media = [];
const subtitles = [];
const seen = new Set();

function check(url) {
  if (!url || seen.has(url)) return;
  seen.add(url);
  const u = url.toLowerCase();
  if (u.includes(".vtt") || u.includes(".srt") || u.includes(".ass") || u.includes("subtitle")) {
    subtitles.push(url);
    return;
  }
  if (u.includes(".m3u8") || u.includes(".mp4") || u.includes(".mkv") || u.includes("/cdn/") || u.includes("savedly.net")) {
    let quality = "unknown";
    if (u.includes("1080") || u.includes("fhd")) quality = "1080p";
    else if (u.includes("720") || u.includes("hd")) quality = "720p";
    else if (u.includes("480") || u.includes("sd")) quality = "480p";
    media.push({ url, quality });
  }
}

// Hooks
const origFetch = window.fetch;
window.fetch = function(input, init) {
  try { check(typeof input === "string" ? input : input?.url); } catch(e) {}
  return origFetch.apply(this, arguments);
};
const origOpen = XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open = function(method, url) {
  try { check(url); } catch(e) {}
  return origOpen.apply(this, arguments);
};
try {
  const po = new PerformanceObserver((list) => {
    for (const e of list.getEntries()) if (e.name) check(e.name);
  });
  po.observe({ type: "resource", buffered: true });
} catch(e) {}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "scan") {
    sendResponse({
      title: document.title,
      pageUrl: location.href,
      media,
      subtitles
    });
  }
});
