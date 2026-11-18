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
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  chrome.tabs.sendMessage(tab.id, { action: "scan" }, (res) => {
    if (!res || !res.media) {
      statusDiv.textContent = "No media found";
      return;
    }
    qualitiesDiv.innerHTML = "";
    const base = urlInput.value.trim().replace(/\/$/, "");
    if (!base) { statusDiv.textContent = "Set Directarr URL first"; return; }
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
            title: res.title || "episode",
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
        } catch (e) {
          btn.disabled = false;
          btn.textContent = "Retry";
          statusDiv.textContent = "Send failed";
        }
      };
      row.appendChild(btn);
      qualitiesDiv.appendChild(row);
    });
    statusDiv.textContent = res.media.length + " found";
  });
};
