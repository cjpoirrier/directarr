const urlInput = document.getElementById("directarrUrl");
const saveBtn = document.getElementById("save");
const qualitiesDiv = document.getElementById("qualities");
const statusDiv = document.getElementById("status");
const titleDiv = document.getElementById("title");
const subsCheck = document.getElementById("includeSubs");
const toggleSettings = document.getElementById("toggleSettings");
const settingsPanel = document.getElementById("settingsPanel");

chrome.storage.local.get(["directarrUrl", "includeSubs"], (data) => {
  if (data.directarrUrl) urlInput.value = data.directarrUrl;
  subsCheck.checked = !!data.includeSubs;
  loadCaptured();
});

saveBtn.onclick = () => {
  chrome.storage.local.set({ directarrUrl: urlInput.value.trim(), includeSubs: subsCheck.checked });
  statusDiv.textContent = "Saved";
};

subsCheck.onchange = () => {
  chrome.storage.local.set({ includeSubs: subsCheck.checked });
};

toggleSettings.onclick = () => {
  settingsPanel.classList.toggle("hidden");
};

async function loadCaptured() {
  statusDiv.textContent = "Reading captured streams…";
  qualitiesDiv.innerHTML = "";
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  chrome.tabs.sendMessage(tab.id, { action: "getCaptured" }, (res) => {
    if (chrome.runtime.lastError || !res) {
      titleDiv.textContent = "No data";
      statusDiv.textContent = "Reload the page, play the video, then reopen this popup.";
      return;
    }
    const title = res.title || "episode";
    titleDiv.textContent = title;
    if (!res.media || !res.media.length) {
      statusDiv.textContent = "No media captured yet. Play the video on the page, then reopen the popup.";
      return;
    }
    const base = (urlInput.value || "").trim().replace(/\/$/, "");
    if (!base) {
      statusDiv.textContent = "Set Directarr URL in Settings first";
      settingsPanel.classList.remove("hidden");
      return;
    }
    res.media.forEach((m) => {
      const row = document.createElement("div");
      row.className = "quality";
      row.innerHTML = `<span style="flex:1">${m.quality || "video"}</span>`;
      const btn = document.createElement("button");
      btn.textContent = "Send";
      btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = "…";
        try {
          const body = {
            title: title,
            url: m.url,
            headers: { Referer: res.pageUrl }
          };
          if (subsCheck.checked && res.subtitles?.length) {
            body.subtitles = res.subtitles;
          }
          await fetch(base + "/api/v1/streams/download", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
          });
          btn.textContent = "Sent";
          statusDiv.textContent = "Sent: " + title.slice(0, 40);
          setTimeout(() => window.close(), 1000);
        } catch (e) {
          btn.disabled = false;
          btn.textContent = "Retry";
          statusDiv.textContent = "Send failed";
        }
      };
      row.appendChild(btn);
      qualitiesDiv.appendChild(row);
    });
    statusDiv.textContent = res.media.length + " stream(s) captured";
  });
}
