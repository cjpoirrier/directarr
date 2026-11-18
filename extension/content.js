// Quietly capture media and subtitle URLs in the background.
// No on-page UI. Popup reads the captured list when opened.
const media = [];
const subtitles = [];
const seen = new Set();

function isMedia(url) {
  if (!url) return false;
  const u = url.toLowerCase();
  // Reject obvious non-media and embed/page URLs
  if (u.endsWith(".js") || u.endsWith(".css") || u.endsWith(".woff") || u.endsWith(".svg") || u.endsWith(".png") || u.endsWith(".jpg")) return false;
  if (u.includes("subtitlemanager")) return false;
  if (u.includes("/embed-") && !u.includes(".m3u8") && !u.includes(".mp4") && !u.includes("getsources")) return false;
  if (u.includes("autoplay=") || u.includes("autostart=")) return false;

  return (
    u.includes(".m3u8") ||
    u.includes(".mp4") ||
    u.includes(".mkv") ||
    u.includes("/cdn/hls/") ||
    u.includes("master.txt") ||
    u.includes("do=getvideo") ||
    u.includes("getvideo") ||
    u.includes("getsources") ||
    /\/(playlist|master|index)\.m3u8/.test(u) ||
    u.includes("savedly.net") ||
    u.includes("winghoster") ||
    (u.includes("/cdn/") && (u.includes(".mp4") || u.includes(".m3u8") || u.includes(".ts")))
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

function guessQuality(url) {
  const u = url.toLowerCase();
  // Common resolution tokens
  if (/\b(1080|fhd|1920)\b/.test(u)) return "1080p";
  if (/\b(720|hd|1280)\b/.test(u)) return "720p";
  if (/\b(480|sd|854)\b/.test(u)) return "480p";
  if (/\b(360|640)\b/.test(u)) return "360p";
  if (/\b(2160|4k|uhd)\b/.test(u)) return "2160p";
  // Query-param style
  const q = u.match(/[?&](?:q|quality|res|resolution)=(\d{3,4})/i);
  if (q) return q[1] + "p";
  return "unknown";
}

function addMedia(url) {
  if (seen.has(url)) return;
  seen.add(url);
  media.push({ url, quality: guessQuality(url) });
  // Notify background so the extension icon shows a badge
  try {
    chrome.runtime.sendMessage({ action: "mediaFound", count: media.length });
  } catch (e) {}
}

function check(url) {
  if (!url || seen.has(url)) return;
  if (isSubtitle(url)) {
    seen.add(url);
    subtitles.push(url);
    return;
  }
  if (!isMedia(url)) return;

  // Resolve getVideo-style APIs the same way the userscript does
  if (url.includes("do=getVideo") || url.includes("do=getvideo") || url.includes("getvideo")) {
    fetch(url, { credentials: "include" })
      .then(r => r.text())
      .then(text => {
        const m = text.match(/https?:\/\/[^"'\s]+\.m3u8[^"'\s]*/i) ||
                  text.match(/https?:\/\/[^"'\s]+\/master\.txt[^"'\s]*/i) ||
                  text.match(/https?:\/\/[^"'\s]+\/cdn\/hls\/[^"'\s]+/i);
        if (m) addMedia(m[0]);
      })
      .catch(() => {});
    return;
  }
  addMedia(url);
}

// Hooks (run early)
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

function extractSonarrTitle() {
  const site = location.hostname.replace(/^www\./, "").split(".")[0];
  let raw = (document.title || "").trim();

  // AniWaves-style: "Show Name (2026) — Episode 1: ..."
  let m = raw.match(/^(.+?)\s*\(\d{4}\)\s*[—–-]\s*Episode\s*(\d+)/i);
  if (m) {
    const show = m[1].trim();
    const ep = parseInt(m[2], 10);
    return `${show} - S01E${String(ep).padStart(2, "0")} [${site}]`;
  }

  // Generic "Episode N" / "Ep N" / SxxExx
  m = raw.match(/(?:episode|ep\.?)\s*(\d+)/i) || raw.match(/S(\d+)E(\d+)/i);
  let season = 1, episode = null, show = raw;
  if (m) {
    if (m[2]) {
      season = parseInt(m[1], 10);
      episode = parseInt(m[2], 10);
      show = raw.replace(m[0], "").trim();
    } else {
      episode = parseInt(m[1], 10);
      show = raw.replace(m[0], "").replace(/[-–—]\s*$/, "").trim();
    }
  }

  // Prefer cleaner name from page
  const h1 = document.querySelector("h1, .title, .anime-title, [itemprop='name']");
  if (h1) {
    const t = h1.textContent.trim();
    if (t.length > 3 && t.length < 100 && !t.toLowerCase().includes(site.toLowerCase())) {
      show = t.replace(/\s*(episode|ep)\s*\d+.*/i, "").replace(/\(\d{4}\)/, "").trim() || show;
    }
  }
  const og = document.querySelector('meta[property="og:title"]');
  if (og?.content) {
    const ogt = og.content.replace(/\s*[|_-].*$/, "").replace(/\(\d{4}\)/, "").trim();
    if (ogt.length > 3 && ogt.length < 120) show = ogt;
  }

  // Drop common site suffixes
  show = show.replace(/\s*[|_-]\s*(AniWave|AniWaves|KissAnime|Gogoanime|9anime|Zoro|Anix|Animepahe|HiAnime|AllAnime|MyAsiantv).*$/i, "");
  show = show.replace(/\s*(English Subbed|English Dubbed|Subbed|Dubbed)\s*/gi, " ").replace(/\s+/g, " ").trim() || "Unknown Show";

  if (episode) {
    const se = `S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`;
    return `${show} - ${se} [${site}]`;
  }
  return `${show} [${site}]`;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "getCaptured") {
    sendResponse({
      title: extractSonarrTitle(),
      pageUrl: location.href,
      media: media.slice(),
      subtitles: subtitles.slice(),
      site: location.hostname
    });
  }
  return true;
});
