const urlInput = document.getElementById("directarrUrl");
const saveBtn = document.getElementById("save");
const scanBtn = document.getElementById("scan");
const qualitiesDiv = document.getElementById("qualities");
const statusDiv = document.getElementById("status");
const subsCheck = document.getElementById("includeSubs");

chrome.storage.local.get(["directarrUrl", "includeSubs"], (data) => {
  if (data.directarrUrl) urlInput.value = data.directarrUrl;
  subsCheck.checked = !!data.includeSubs;
});

saveBtn.onclick = () => {
  chrome.storage.local.set({ directarrUrl: urlInput.value.trim(), includeSubs: subsCheck.checked });
  statusDiv.textContent = "Saved";
};

subsCheck.onchange = () => {
  chrome.storage.local.set({ includeSubs: subsCheck.checked });
};

scanBtn.onclick = async () => {
  statusDiv.textContent = "Scanning…";
  qualitiesDiv.innerHTML = "";
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  chrome.tabs.sendMessage(tab.id, { action: "scan" }, (res) => {
    if (chrome.runtime.lastError) {
      statusDiv.textContent = "Reload the page and try again";
      return;
    }
    if (!res || !res.media || !res.media.length) {
      statusDiv.textContent = "No media found (play the video first)";
      return;
    }
    const base = urlInput.value.trim().replace(/\/$/, "");
    if (!base) { statusDiv.textContent = "Set Directarr URL first"; return; }
    const title = res.title || "episode";
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
          // Optional: close popup after a moment
          setTimeout(() => window.close(), 1200);
        } catch (e) {
          btn.disabled = false;
          btn.textContent = "Retry";
          statusDiv.textContent = "Send failed";
        }
      };
      row.appendChild(btn);
      qualitiesDiv.appendChild(row);
    });
    statusDiv.textContent = res.media.length + " found — " + title.slice(0, 50);
  });
};
