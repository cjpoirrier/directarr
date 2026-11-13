(() => {
  const KEY = "directarr_api_key";
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  function apiKey() {
    const el = $("#api-key");
    return localStorage.getItem(KEY) || (el && el.value.trim()) || "";
  }

  function toast(msg, isErr) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.toggle("err", !!isErr);
    el.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.add("hidden"), 4000);
  }

  async function api(path, opts = {}) {
    const headers = Object.assign({ "X-Api-Key": apiKey() }, opts.headers || {});
    if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, { ...opts, headers });
    let data = null;
    const text = await res.text();
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!res.ok) {
      const detail = (data && data.detail) || res.statusText || "Request failed";
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function statusPill(status) {
    const s = (status || "").toLowerCase();
    let cls = "muted";
    if (s === "completed") cls = "ok";
    else if (s === "failed") cls = "err";
    else if (s === "downloading" || s === "queued") cls = "warn";
    return `<span class="pill ${cls}">${status || "—"}</span>`;
  }

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setView(name) {
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
    $$(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
    const titles = {
      dashboard: "Dashboard",
      search: "Search",
      releases: "Releases",
      downloads: "Downloads",
      sources: "Sources",
      gdrive: "Google Drive",
      settings: "Settings",
    };
    $("#view-title").textContent = titles[name] || name;
    if (name === "dashboard") loadDashboard();
    if (name === "releases") loadReleases();
    if (name === "downloads") loadDownloads();
    if (name === "sources") loadSources();
    if (name === "gdrive") loadGdrive();
    if (name === "settings") updateSettings();
  }

  async function loadDashboard() {
    try {
      const [sources, releases, downloads, gdrive] = await Promise.all([
        api("/api/v1/sources"),
        api("/api/v1/releases?limit=5"),
        api("/api/v1/downloads"),
        api("/api/v1/gdrive/status"),
      ]);
      $("#stat-sources").textContent = `${sources.filter((s) => s.enabled).length}/${sources.length} enabled`;
      $("#stat-releases").textContent = String(releases.length);
      $("#stat-downloads").textContent = String(downloads.length);
      $("#stat-gdrive").textContent = gdrive.authorized ? "Authorized" : "Not authorized";
      $("#conn-status").textContent = "Connected";
      $("#conn-status").className = "pill ok";
      renderDownloadsTable($("#dash-downloads"), downloads.slice(0, 8));
    } catch (e) {
      $("#conn-status").textContent = e.message;
      $("#conn-status").className = "pill err";
    }
  }

  let queueModalShareUrl = "";
  let queueModalFiles = [];

  function isDriveUrl(url) {
    try {
      const host = new URL(url).hostname.toLowerCase();
      return host === "drive.google.com" || host === "docs.google.com";
    } catch {
      return false;
    }
  }

  function closeQueueModal() {
    const modal = $("#queue-modal");
    if (modal) modal.classList.add("hidden");
    queueModalShareUrl = "";
    queueModalFiles = [];
  }

  function openQueueModal(title, shareUrl, files) {
    queueModalShareUrl = shareUrl;
    queueModalFiles = (files || []).filter((f) => !f.is_folder);
    const modal = $("#queue-modal");
    if (!modal) return;
    $("#queue-modal-title").textContent = title || "Drive files";
    $("#queue-modal-hint").textContent = `${queueModalFiles.length} downloadable file(s) found. Select what to queue.`;
    $("#queue-modal-list").innerHTML = `<table>
      <thead><tr>
        <th><input type="checkbox" id="queue-modal-check-all" checked /></th>
        <th>Name</th><th>Size</th>
      </tr></thead>
      <tbody>
        ${queueModalFiles
          .map(
            (f, i) => `<tr>
          <td><input type="checkbox" class="queue-modal-check" data-idx="${i}" checked /></td>
          <td class="title">${esc(f.name)}</td>
          <td>${f.size != null ? Math.round(f.size / 1048576) + " MiB" : "—"}</td>
        </tr>`
          )
          .join("")}
      </tbody></table>`;
    const master = $("#queue-modal-check-all");
    if (master) {
      master.addEventListener("change", () => {
        $$(".queue-modal-check").forEach((c) => {
          c.checked = master.checked;
        });
      });
    }
    modal.classList.remove("hidden");
  }

  async function queueDriveFiles(all) {
    const indices = all
      ? queueModalFiles.map((_, i) => i)
      : $$(".queue-modal-check")
          .filter((c) => c.checked)
          .map((c) => Number(c.dataset.idx));
    const files = indices
      .map((i) => queueModalFiles[i])
      .filter(Boolean)
      .map((f) => ({ title: f.name, drive_file_id: f.id }));
    if (!files.length) return toast("No files selected", true);
    try {
      const res = await api("/api/v1/downloads/bulk", {
        method: "POST",
        body: { url: queueModalShareUrl, files },
      });
      const parts = [`Queued ${res.count} download(s)`];
      if (res.skipped_count) parts.push(`${res.skipped_count} already in queue`);
      toast(parts.join(" · "));
      if (res.errors && res.errors.length) toast(`${res.errors.length} failed`, true);
      closeQueueModal();
      if (res.count > 0) setView("downloads");
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function queueRelease(title, url) {
    // Non-Drive: queue the URL as-is.
    if (!isDriveUrl(url)) {
      await api("/api/v1/downloads", {
        method: "POST",
        body: { title, url },
      });
      toast("Queued download");
      return;
    }
    // Drive: resolve on the backend, then queue or show file picker.
    toast("Resolving Drive link…");
    const data = await api("/api/v1/gdrive/resolve", {
      method: "POST",
      body: { url },
    });
    const files = (data.files || []).filter((f) => !f.is_folder);
    if (!files.length) {
      throw new Error(data.error || "No downloadable files found in this Drive share");
    }
    if (files.length === 1) {
      await api("/api/v1/downloads", {
        method: "POST",
        body: {
          title: files[0].name || title,
          url,
          drive_file_id: files[0].id,
        },
      });
      toast("Queued Drive file");
      setView("downloads");
      return;
    }
    openQueueModal(data.name || title, url, files);
  }

  function renderReleasesTable(el, rows, { queue = false } = {}) {
    if (!rows.length) {
      el.innerHTML = `<p class="muted">No releases yet. Run a search.</p>`;
      return;
    }
    el.innerHTML = `<table>
      <thead><tr><th>Title</th><th>Source</th><th>Res</th><th>Codec</th><th></th></tr></thead>
      <tbody>
        ${rows
          .map((r) => {
            const drive = isDriveUrl(r.url || "");
            return `<tr>
          <td class="title">${esc(r.title)}</td>
          <td>${esc(r.source || "")}</td>
          <td>${esc(r.resolution || "—")}</td>
          <td>${esc(r.codec || "—")}</td>
          <td class="actions">
            ${
              queue
                ? `<button class="btn sm primary" data-queue-title="${esc(r.title)}" data-queue-url="${esc(r.url)}">${
                    drive ? "Queue / Resolve" : "Queue"
                  }</button>`
                : ""
            }
            <a class="btn sm" href="${esc(r.url)}" target="_blank" rel="noopener">Open</a>
          </td>
        </tr>`;
          })
          .join("")}
      </tbody></table>`;
    el.querySelectorAll("[data-queue-url]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await queueRelease(btn.dataset.queueTitle, btn.dataset.queueUrl);
        } catch (e) {
          toast(e.message, true);
        } finally {
          btn.disabled = false;
        }
      });
    });
  }

  function renderDownloadsTable(el, rows) {
    if (!rows.length) {
      el.innerHTML = `<p class="muted">No download jobs yet.</p>`;
      return;
    }
    el.innerHTML = `<table>
      <thead><tr><th>Title</th><th>Status</th><th>Progress</th><th>Error / path</th></tr></thead>
      <tbody>
        ${rows.map((r) => `<tr>
          <td class="title">${esc(r.title)}</td>
          <td>${statusPill(r.status)}</td>
          <td>${r.progress ?? 0}%</td>
          <td class="title muted">${esc(r.error || r.path || "")}</td>
        </tr>`).join("")}
      </tbody></table>`;
  }

  async function loadReleases() {
    try {
      const rows = await api("/api/v1/releases?limit=200");
      renderReleasesTable($("#releases-table"), rows, { queue: true });
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function loadDownloads() {
    try {
      const rows = await api("/api/v1/downloads");
      renderDownloadsTable($("#downloads-table"), rows);
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function loadSources() {
    try {
      const rows = await api("/api/v1/sources");
      if (!rows.length) {
        $("#sources-table").innerHTML = `<p class="muted">No sources in config/sources.json</p>`;
        return;
      }
      $("#sources-table").innerHTML = `<table>
        <thead><tr><th>Name</th><th>Adapter</th><th>Status</th><th></th></tr></thead>
        <tbody>
          ${rows.map((s) => `<tr>
            <td>${esc(s.name)}</td>
            <td><code>${esc(s.adapter)}</code></td>
            <td>${s.enabled ? '<span class="pill ok">enabled</span>' : '<span class="pill muted">disabled</span>'}</td>
            <td class="actions">
              <button class="btn sm" data-toggle-id="${esc(s.id)}" data-enabled="${s.enabled ? "1" : "0"}">
                ${s.enabled ? "Disable" : "Enable"}
              </button>
            </td>
          </tr>`).join("")}
        </tbody></table>`;
      $("#sources-table").querySelectorAll("[data-toggle-id]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          try {
            const next = btn.dataset.enabled !== "1";
            await api(`/api/v1/sources/${encodeURIComponent(btn.dataset.toggleId)}`, {
              method: "PATCH",
              body: { enabled: next },
            });
            toast(next ? "Source enabled" : "Source disabled");
            loadSources();
          } catch (e) {
            toast(e.message, true);
          }
        });
      });
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function loadGdrive() {
    try {
      const st = await api("/api/v1/gdrive/status");
      $("#gdrive-status").innerHTML = `
        <div>Client secrets: ${st.client_secrets_present ? '<span class="pill ok">present</span>' : '<span class="pill err">missing</span>'}</div>
        <div style="margin-top:0.35rem">Token: ${st.token_present ? '<span class="pill ok">present</span>' : '<span class="pill warn">missing</span>'}</div>
        <div style="margin-top:0.35rem">Authorized: ${st.authorized ? '<span class="pill ok">yes</span>' : '<span class="pill muted">no</span>'}</div>
        <div class="hint">${esc(st.client_secrets_path)}</div>`;
    } catch (e) {
      $("#gdrive-status").textContent = e.message;
    }
  }

  async function updateSettings() {
    try {
      const data = await api("/api/v1/settings/security");
      const key = data.api_key || "";
      const host = location.origin;
      const keyEl = $("#api-key");
      if (keyEl) keyEl.value = key;
      const urlEl = $("#torznab-url");
      if (urlEl) {
        urlEl.textContent = `${host}${data.torznab_path || "/api"}?apikey=${key || "YOUR_KEY"}`;
      }
      if (key) localStorage.setItem(KEY, key);
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function copyText(value, label) {
    try {
      await navigator.clipboard.writeText(value);
      toast(label + " copied");
    } catch {
      toast("Could not copy — select and copy manually", true);
    }
  }

  // Navigation
  $$(".nav-btn").forEach((btn) => btn.addEventListener("click", () => setView(btn.dataset.view)));

  // API key
  const saved = localStorage.getItem(KEY);
  const keyEl = $("#api-key");
  if (saved && keyEl) keyEl.value = saved;

  const copyApiBtn = $("#copy-api-key");
  if (copyApiBtn) {
    copyApiBtn.addEventListener("click", () => {
      const key = (keyEl && keyEl.value.trim()) || apiKey();
      if (!key) return toast("No API key loaded", true);
      copyText(key, "API key");
    });
  }

  const copyTorzBtn = $("#copy-torznab");
  if (copyTorzBtn) {
    copyTorzBtn.addEventListener("click", () => {
      const urlEl = $("#torznab-url");
      const url = (urlEl && urlEl.textContent.trim()) || "";
      if (!url) return toast("No Torznab URL", true);
      copyText(url, "Torznab URL");
    });
  }

  const refreshSettingsBtn = $("#refresh-settings");
  if (refreshSettingsBtn) {
    refreshSettingsBtn.addEventListener("click", () => {
      updateSettings();
      toast("Settings refreshed");
    });
  }

  const regenBtn = $("#regen-api-key");
  if (regenBtn) {
    regenBtn.addEventListener("click", async () => {
      if (!confirm("Regenerate API key? Sonarr/Radarr indexers must be updated with the new key.")) return;
      try {
        const data = await api("/api/v1/settings/api-key/regenerate", { method: "POST" });
        if (keyEl) keyEl.value = data.api_key || "";
        if (data.api_key) localStorage.setItem(KEY, data.api_key);
        const host = location.origin;
        const urlEl = $("#torznab-url");
        if (urlEl) {
          urlEl.textContent = `${host}${data.torznab_path || "/api"}?apikey=${data.api_key || "YOUR_KEY"}`;
        }
        toast("API key regenerated");
      } catch (e) {
        toast(e.message, true);
      }
    });
  }

  $("#search-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const q = $("#search-q").value.trim();
    if (!q) return;
    $("#search-results").innerHTML = `<p class="muted">Searching…</p>`;
    try {
      const data = await api(`/api/v1/search?q=${encodeURIComponent(q)}`);
      renderReleasesTable($("#search-results"), data.releases || [], { queue: true });
      toast(`Found ${data.count ?? (data.releases || []).length} releases`);
    } catch (e) {
      $("#search-results").innerHTML = "";
      toast(e.message, true);
    }
  });

  $("#refresh-releases").addEventListener("click", loadReleases);
  $("#refresh-downloads").addEventListener("click", loadDownloads);
  $("#reload-sources").addEventListener("click", async () => {
    try {
      const r = await api("/api/v1/sources/reload", { method: "POST" });
      toast(`Reloaded ${r.loaded} sources (${r.enabled} enabled)`);
      loadSources();
    } catch (e) {
      toast(e.message, true);
    }
  });

  $("#gdrive-auth-url").addEventListener("click", async () => {
    try {
      const r = await api("/api/v1/gdrive/auth-url");
      window.open(r.auth_url, "_blank", "noopener");
      toast("Opened auth URL — paste the code when done");
    } catch (e) {
      toast(e.message, true);
    }
  });

  $("#gdrive-auth-code").addEventListener("click", async () => {
    const code = $("#gdrive-code").value.trim();
    if (!code) return toast("Paste the OAuth code first", true);
    try {
      await api("/api/v1/gdrive/auth-code", { method: "POST", body: { code } });
      toast("Google Drive authorized");
      loadGdrive();
    } catch (e) {
      toast(e.message, true);
    }
  });

  $("#resolve-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const url = $("#resolve-url").value.trim();
    $("#resolve-results").innerHTML = `<p class="muted">Resolving…</p>`;
    try {
      const data = await api("/api/v1/gdrive/resolve", { method: "POST", body: { url } });
      const files = data.files || [];
      if (!files.length) {
        $("#resolve-results").innerHTML = `<p class="muted">No files returned.</p>`;
        return;
      }
      $("#resolve-results").innerHTML = `<table>
        <thead><tr><th>Name</th><th>Type</th><th>Size</th><th></th></tr></thead>
        <tbody>
          ${files.map((f) => `<tr>
            <td class="title">${esc(f.name)}</td>
            <td>${f.is_folder ? "folder" : esc(f.mime_type || "file")}</td>
            <td>${f.size != null ? Math.round(f.size / 1048576) + " MiB" : "—"}</td>
            <td class="actions">
              ${f.is_folder ? "" : `<button class="btn sm primary" data-dfid="${esc(f.id)}" data-dname="${esc(f.name)}">Queue</button>`}
            </td>
          </tr>`).join("")}
        </tbody></table>`;
      $("#resolve-results").querySelectorAll("[data-dfid]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          try {
            await api("/api/v1/downloads", {
              method: "POST",
              body: {
                title: btn.dataset.dname,
                url,
                drive_file_id: btn.dataset.dfid,
              },
            });
            toast("Queued Drive file download");
          } catch (e) {
            toast(e.message, true);
          }
        });
      });
    } catch (e) {
      $("#resolve-results").innerHTML = "";
      toast(e.message, true);
    }
  });

  // Boot
  updateSettings();
  if (apiKey()) loadDashboard();

  const qClose = $("#queue-modal-close");
  if (qClose) qClose.addEventListener("click", closeQueueModal);
  const qAll = $("#queue-modal-all");
  if (qAll) qAll.addEventListener("click", () => {
    $$(".queue-modal-check").forEach((c) => { c.checked = true; });
    const m = $("#queue-modal-check-all");
    if (m) m.checked = true;
  });
  const qNone = $("#queue-modal-none");
  if (qNone) qNone.addEventListener("click", () => {
    $$(".queue-modal-check").forEach((c) => { c.checked = false; });
    const m = $("#queue-modal-check-all");
    if (m) m.checked = false;
  });
  const qDl = $("#queue-modal-download");
  if (qDl) qDl.addEventListener("click", () => queueDriveFiles(false));
  const qDlAll = $("#queue-modal-download-all");
  if (qDlAll) qDlAll.addEventListener("click", () => queueDriveFiles(true));
  const qModal = $("#queue-modal");
  if (qModal) qModal.addEventListener("click", (ev) => {
    if (ev.target === qModal) closeQueueModal();
  });
})();
