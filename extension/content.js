// Capture media and subtitle URLs (hooks always active for capture, but scan is on-demand via popup)
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

function extractSonarrTitle() {
  // Prefer a clean Sonarr-style title: Show Name - SxxExx - Episode Title
  // Fall back to cleaned document.title, with site as source.
  const site = location.hostname.replace(/^www\./, "").split(".")[0];
  let raw = document.title || "";
  // Common site suffixes
  raw = raw.replace(/\s*[|_-]\s*(MyAsiantv|AniWave|KissAnime|Gogoanime|9anime|Zoro|Anix|Animepahe|HiAnime|AllAnime).*$/i, "");
  raw = raw.replace(/\s*(English Subbed|English Dubbed|Subbed|Dubbed)\s*/gi, " ").trim();

  // Try to find episode number
  let epMatch = raw.match(/(?:episode|ep\.?|e)\s*(\d+)/i) || raw.match(/S(\d+)E(\d+)/i);
  let season = 1, episode = null, show = raw, epTitle = "";

  if (epMatch) {
    if (epMatch[2]) { // SxxExx
      season = parseInt(epMatch[1], 10);
      episode = parseInt(epMatch[2], 10);
      show = raw.replace(epMatch[0], "").trim();
    } else {
      episode = parseInt(epMatch[1], 10);
      show = raw.replace(epMatch[0], "").replace(/[-–]\s*$/, "").trim();
    }
  }

  // Look for better show name in page (h1, og:title, etc.)
  const h1 = document.querySelector("h1, .title, .anime-title, [itemprop='name']");
  if (h1 && h1.textContent.trim().length > 3 && h1.textContent.trim().length < 80) {
    const h1text = h1.textContent.trim();
    if (!h1text.toLowerCase().includes(site.toLowerCase())) {
      show = h1text.replace(/\s*(episode|ep)\s*\d+.*/i, "").trim() || show;
    }
  }

  const og = document.querySelector('meta[property="og:title"]');
  if (og && og.content) {
    const ogt = og.content.replace(/\s*[|_-].*$/, "").trim();
    if (ogt.length > 3 && ogt.length < 100) show = ogt;
  }

  show = show.replace(/\s+/g, " ").trim() || "Unknown Show";
  if (episode) {
    const se = `S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`;
    return `${show} - ${se}${epTitle ? " - " + epTitle : ""} [${site}]`;
  }
  return `${show} [${site}]`;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "scan") {
    sendResponse({
      title: extractSonarrTitle(),
      pageUrl: location.href,
      media,
      subtitles,
      site: location.hostname
    });
  }
  return true; // keep channel open if needed
});
