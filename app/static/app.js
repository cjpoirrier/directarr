(() => {
  const KEY = "directarr_api_key";
  let downloadPollTimer = null;
  let downloadPollGen = 0;
  let lastDownloads = [];
  let currentView = "dashboard";
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
    const headers = Object.assign({}, opts.headers || {});
    const key = apiKey();
    if (key) headers["X-Api-Key"] = key;
    let body = opts.body;
    if (body && typeof body === "object" && !(body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers,
      body,
      credentials: "same-origin",
    });
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
    if (!res.ok) {
      if (res.status === 401 && path.indexOf("/api/v1/auth/") !== 0) {
        try { localStorage.removeItem(KEY); } catch (e) {}
        showLogin();
      }
      let msg = data && data.detail ? data.detail : text;
      if (typeof msg !== "string") msg = JSON.stringify(msg);
      throw new Error(msg || res.statusText || "Request failed");
    }
    return data;
  }

  function statusPill(status) {
    const s = (status || "").toLowerCase();
    let cls = "muted";
    if (s === "completed") cls = "ok";
    else if (s === "failed" || s === "cancelled") cls = "err";
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

  function showLogin() {
    const screen = $("#login-screen");
    const shell = $("#app-shell");
    if (screen) screen.classList.remove("hidden");
    if (shell) shell.hidden = true;
  }

  function hideLogin() {
    const screen = $("#login-screen");
    const shell = $("#app-shell");
    if (screen) screen.classList.add("hidden");
    if (shell) shell.hidden = false;
  }

  async function ensureAuth() {
    try {
      const res = await fetch("/api/v1/auth/status", { credentials: "same-origin" });
      const st = await res.json();
      if (st && st.authenticated) {
        hideLogin();
        return true;
      }
      showLogin();
      return false;
    } catch (e) {
      showLogin();
      return false;
    }
  }


  function setMobileNavOpen(open) {
    const shell = $("#app-shell");
    const backdrop = $("#nav-backdrop");
    if (shell) shell.classList.toggle("nav-open", !!open);
    if (backdrop) backdrop.hidden = !open;
    ["#menu-toggle", "#topbar-menu"].forEach((sel) => {
      const btn = $(sel);
      if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  function setView(name) {
    currentView = name;
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
    $$(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
    const titles = {
      dashboard: "Dashboard",
      search: "Search",
      releases: "Releases",
      downloads: "Downloads",
      history: "History",
      stats: "Stats",
      sources: "Indexers",
      applications: "Applications",
      gdrive: "Google Drive",
      settings: "Settings",
      system: "System",
    };
    $("#view-title").textContent = titles[name] || name;
    if (name === "dashboard") loadDashboard({ silent: false });
    if (name === "releases") loadReleases();
    if (name === "downloads") loadDownloads({ silent: false });
    if (name === "history") loadHistory();
    if (name === "stats") loadStats();
    if (name === "sources") loadSources();
    if (name === "applications") loadApplications();
    if (name === "gdrive") loadGdrive();
    if (name === "settings") {
      updateSettings();
      loadSyncProfiles();
    }
    if (name === "system") loadSystemStatus();
    // Restart (or stop) the download poller for the new view
    manageDownloadPolling();
    setMobileNavOpen(false);
  }

  function setSettingsTab(tab) {
    $$(".settings-tab").forEach((b) => b.classList.toggle("active", b.dataset.settingsTab === tab));
    $$(".settings-panel").forEach((p) => p.classList.toggle("active", p.id === `settings-panel-${tab}`));
  }

  const loginForm = $("#login-form");
  if (loginForm) {
    loginForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const err = $("#login-error");
      if (err) err.textContent = "";
      const userEl = $("#login-user");
      const passEl = $("#login-pass");
      const username = (userEl && userEl.value.trim()) || "";
      const password = (passEl && passEl.value) || "";
      if (!username || !password) {
        if (err) err.textContent = "Enter username and password";
        return;
      }
      try {
        // Do not send a stale X-Api-Key during login
        try { localStorage.removeItem(KEY); } catch (e) {}
        const res = await fetch("/api/v1/auth/login", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ username, password }),
        });
        const text = await res.text();
        let data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) {}
        if (!res.ok) {
          let msg = "Invalid username or password";
          if (data && data.detail) {
            msg = typeof data.detail === "string" ? data.detail : "Login failed";
          }
          if (err) err.textContent = msg;
          return;
        }
        if (passEl) passEl.value = "";
        // Confirm cookie session before showing app
        const stRes = await fetch("/api/v1/auth/status", { credentials: "same-origin" });
        const st = await stRes.json();
        if (!st.authenticated) {
          if (err) err.textContent = "Login saved but session cookie was blocked. Check browser privacy settings.";
          return;
        }
        hideLogin();
        try {
          await loadDashboard({ silent: true });
        } catch (e) {}
        manageDownloadPolling();
      } catch (e) {
        if (err) err.textContent = e.message || "Login failed";
      }
    });
  }

  ensureAuth().then((ok) => {
    if (ok) manageDownloadPolling();
  });

  function downloadsAreActive(rows) {
    return (rows || []).some((r) => {
      const s = (r.status || "").toLowerCase();
      return s === "queued" || s === "downloading";
    });
  }

  function updateAutoRefreshLabel(active) {
    const label = $("#auto-refresh-label");
    if (!label) return;
    if (currentView !== "downloads") return;
    label.textContent = active ? "Auto-refresh on (live)" : "Auto-refresh on (idle)";
  }

  function manageDownloadPolling() {
    if (downloadPollTimer) {
      clearTimeout(downloadPollTimer);
      downloadPollTimer = null;
    }
    const gen = ++downloadPollGen;
    if (currentView !== "downloads" && currentView !== "dashboard") {
      updateAutoRefreshLabel(false);
      return;
    }

    const tick = async () => {
      if (gen !== downloadPollGen) return;
      try {
        if (currentView === "downloads") await loadDownloads({ silent: true });
        else if (currentView === "dashboard") await loadDashboard({ silent: true });
      } finally {
        if (gen === downloadPollGen) scheduleNext();
      }
    };

    const scheduleNext = () => {
      if (gen !== downloadPollGen) return;
      if (downloadPollTimer) {
        clearTimeout(downloadPollTimer);
        downloadPollTimer = null;
      }
      if (currentView !== "downloads" && currentView !== "dashboard") return;
      const active = downloadsAreActive(lastDownloads);
      updateAutoRefreshLabel(active);
      let delay = 5000;
      if (currentView === "downloads") delay = active ? 1500 : 4000;
      else delay = active ? 2500 : 8000;
      downloadPollTimer = setTimeout(tick, delay);
    };

    tick();
  }

  async function loadDashboard({ silent } = {}) {
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
      lastDownloads = downloads || [];
      renderDownloadsTable($("#dash-downloads"), lastDownloads.slice(0, 8));
    } catch (e) {
      if (!silent) {
        $("#conn-status").textContent = e.message;
        $("#conn-status").className = "pill err";
      }
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

  function isGoIndexUrl(url) {
    try {
      const u = new URL(url);
      const host = u.hostname.toLowerCase();
      if (host === "drive.kayoani.online") return true;
      // path like /0:/Folder/
      return /^\/\d+:\//.test(u.pathname || "");
    } catch {
      return false;
    }
  }

  function isResolvableUrl(url) {
    return isDriveUrl(url) || isGoIndexUrl(url) || /\/open\/?\?id=/i.test(url || "");
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
    const selected = indices.map((i) => queueModalFiles[i]).filter(Boolean);
    if (!selected.length) return toast("No files selected", true);

    // Google Drive shares use bulk + drive_file_id; GoIndex uses per-file HTTPS URLs
    const useDriveApi = isDriveUrl(queueModalShareUrl);
    try {
      if (useDriveApi) {
        const files = selected.map((f) => ({ title: f.name, drive_file_id: f.id }));
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
        return;
      }
      let count = 0;
      for (const f of selected) {
        await api("/api/v1/downloads", {
          method: "POST",
          body: { title: f.name, url: f.url || queueModalShareUrl },
        });
        count += 1;
      }
      toast(`Queued ${count} download(s)`);
      closeQueueModal();
      setView("downloads");
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function queueRelease(title, url) {
    // Plain direct HTTPS: queue as-is
    if (!isResolvableUrl(url)) {
      await api("/api/v1/downloads", {
        method: "POST",
        body: { title, url },
      });
      toast("Queued download");
      return;
    }
    toast("Resolving share link…");
    const data = await api("/api/v1/resolve", {
      method: "POST",
      body: { url },
    });
    const files = (data.files || []).filter((f) => !f.is_folder);
    if (!files.length) {
      throw new Error(data.error || "No downloadable files found in this share");
    }
    const shareUrl = data.input_url || url;
    if (files.length === 1) {
      const f = files[0];
      const body = {
        title: f.name || title,
        url: f.url || shareUrl,
      };
      // Google Drive file ids only for real Drive API downloads
      if (isDriveUrl(shareUrl) || isDriveUrl(f.url || "")) {
        body.drive_file_id = f.id;
        body.url = shareUrl;
      }
      await api("/api/v1/downloads", { method: "POST", body });
      toast("Queued file");
      setView("downloads");
      return;
    }
    openQueueModal(data.name || title, shareUrl, files);
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
            const resolvable = isResolvableUrl(r.url || "");
            return `<tr>
          <td class="title">${esc(r.title)}</td>
          <td>${esc(r.source || "")}</td>
          <td>${esc(r.resolution || "—")}</td>
          <td>${esc(r.codec || "—")}</td>
          <td class="actions">
            ${
              queue
                ? `<button class="btn sm primary" data-queue-title="${esc(r.title)}" data-queue-url="${esc(r.url)}">${
                    resolvable ? "Queue / Resolve" : "Queue"
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

  function formatBytes(n) {
    const v = Number(n);
    if (!Number.isFinite(v) || v < 0) return "—";
    if (v < 1024) return `${Math.round(v)} B`;
    if (v < 1048576) return `${(v / 1024).toFixed(1)} KiB`;
    if (v < 1073741824) return `${(v / 1048576).toFixed(1)} MiB`;
    return `${(v / 1073741824).toFixed(2)} GiB`;
  }

  function sizeCell(r) {
    const total = r.size_bytes != null ? Number(r.size_bytes) : null;
    const done = r.bytes_done != null ? Number(r.bytes_done) : null;
    const st = (r.status || "").toLowerCase();
    if (st === "downloading" && total && total > 0 && done != null) {
      return `${formatBytes(done)} / ${formatBytes(total)}`;
    }
    if (st === "downloading" && done != null && done > 0 && !total) {
      return `${formatBytes(done)} / ?`;
    }
    if (total && total > 0) return formatBytes(total);
    if (done && done > 0) return formatBytes(done);
    return "—";
  }

  function progressCell(r) {
    const pct = Math.max(0, Math.min(100, Number(r.progress) || 0));
    const active = (r.status || "").toLowerCase() === "downloading";
    if (active || (pct > 0 && pct < 100)) {
      return `<span class="bar" title="${pct}%"><span style="width:${pct}%"></span></span> ${pct}%`;
    }
    return `${pct}%`;
  }

  function selectedDownloadIds() {
    return $$("#downloads-table .dl-check:checked")
      .map((c) => c.value)
      .filter(Boolean);
  }

  function renderDownloadsTable(el, rows) {
    if (!rows.length) {
      el.innerHTML = `<p class="muted">No download jobs yet.</p>`;
      return;
    }
    const prevSelected = new Set(selectedDownloadIds());
    el.innerHTML = `<table>
      <thead><tr>
        <th style="width:2rem"><input type="checkbox" id="dl-check-all" title="Select all" /></th>
        <th>Title</th><th>Status</th><th>Size</th><th>Progress</th><th>Error / path</th><th></th>
      </tr></thead>
      <tbody>
        ${rows
          .map((r) => {
            const st = (r.status || "").toLowerCase();
            const canCancel = st === "queued" || st === "downloading";
            const hasFile = !!(r.path && String(r.path).trim());
            const actions = [];
            if (canCancel) {
              actions.push(
                `<button class="btn sm danger" data-cancel-id="${esc(r.id)}">Cancel</button>`
              );
            } else {
              actions.push(
                `<button class="btn sm" data-remove-id="${esc(r.id)}">Remove</button>`
              );
            }
            if (hasFile) {
              actions.push(
                `<button class="btn sm danger" data-delete-file-id="${esc(r.id)}" title="Delete file from disk">Delete file</button>`
              );
            }
            const checked = prevSelected.has(r.id) ? "checked" : "";
            return `<tr>
          <td><input type="checkbox" class="dl-check" value="${esc(r.id)}" ${checked} /></td>
          <td class="title">${esc(r.title)}</td>
          <td>${statusPill(r.status)}</td>
          <td class="muted">${sizeCell(r)}</td>
          <td>${progressCell(r)}</td>
          <td class="title muted">${esc(r.error || r.path || "")}</td>
          <td class="actions">${actions.join(" ")}</td>
        </tr>`;
          })
          .join("")}
      </tbody></table>`;
    const master = $("#dl-check-all");
    if (master) {
      master.addEventListener("change", () => {
        $$("#downloads-table .dl-check").forEach((c) => {
          c.checked = master.checked;
        });
      });
    }
    el.querySelectorAll("[data-cancel-id]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await api(`/api/v1/downloads/${encodeURIComponent(btn.dataset.cancelId)}/cancel`, {
            method: "POST",
          });
          toast("Download cancelled");
          await loadDownloads({ silent: true });
        } catch (e) {
          toast(e.message, true);
          btn.disabled = false;
        }
      });
    });
    el.querySelectorAll("[data-remove-id]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await api(`/api/v1/downloads/${encodeURIComponent(btn.dataset.removeId)}`, {
            method: "DELETE",
          });
          toast("Removed from list");
          await loadDownloads({ silent: true });
        } catch (e) {
          toast(e.message, true);
          btn.disabled = false;
        }
      });
    });
    el.querySelectorAll("[data-delete-file-id]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!confirm("Delete this file from disk? The list entry will stay.")) return;
        btn.disabled = true;
        try {
          const res = await api("/api/v1/downloads/delete-files", {
            method: "POST",
            body: { ids: [btn.dataset.deleteFileId] },
          });
          toast(
            res.deleted_count
              ? "File deleted from disk"
              : res.missing && res.missing.length
                ? "File not found on disk"
                : "No file deleted"
          );
          await loadDownloads({ silent: true });
        } catch (e) {
          toast(e.message, true);
          btn.disabled = false;
        }
      });
    });
  }

  async function loadReleases() {
    try {
      const rows = await api("/api/v1/releases?limit=200");
      renderReleasesTable($("#releases-table"), rows, { queue: true });
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function loadDownloads({ silent } = {}) {
    try {
      const rows = await api("/api/v1/downloads");
      lastDownloads = rows || [];
      renderDownloadsTable($("#downloads-table"), lastDownloads);
      updateAutoRefreshLabel(downloadsAreActive(lastDownloads));
    } catch (e) {
      if (!silent) toast(e.message, true);
    }
  }

  let adapterCatalog = [];

  function categoryPills(cats) {
    const list = Array.isArray(cats) && cats.length ? cats : ["TV", "Anime"];
    return list.map((c) => `<span class="cat-pill">${esc(c)}</span>`).join(" ");
  }

  async function loadSources() {
    try {
      const rows = await api("/api/v1/sources");
      const enabledCount = (rows || []).filter((s) => s.enabled).length;
      const stats = $("#indexer-stats");
      if (stats) {
        stats.textContent = `Indexers ${rows.length} · Enabled ${enabledCount}`;
      }
      if (!rows.length) {
        $("#sources-table").innerHTML = `<p class="muted">No indexers yet. Click <strong>+ Add Indexer</strong> to install one from the catalog.</p>`;
        return;
      }
      // Sort: enabled first, then priority asc, then name
      const sorted = [...rows].sort((a, b) => {
        if (!!b.enabled !== !!a.enabled) return a.enabled ? -1 : 1;
        const pa = Number(a.priority) || 25;
        const pb = Number(b.priority) || 25;
        if (pa !== pb) return pa - pb;
        return String(a.name || "").localeCompare(String(b.name || ""));
      });
      $("#sources-table").innerHTML = `<table class="indexer-table">
        <thead><tr>
          <th style="width:2.5rem"></th>
          <th>Indexer Name</th>
          <th>Protocol</th>
          <th>Privacy</th>
          <th>Priority</th>
          <th>Categories</th>
          <th>Enabled</th>
          <th></th>
        </tr></thead>
        <tbody>
          ${sorted
            .map((s) => {
              const priv = (s.privacy || "public").toLowerCase();
              const proto = (s.protocol || "direct").toLowerCase();
              return `<tr class="${s.enabled ? "indexer-row-on" : "indexer-row-off"}">
            <td><span class="status-dot ${s.enabled ? "ok" : "muted"}" title="${s.enabled ? "Enabled" : "Disabled"}"></span></td>
            <td>
              <strong>${esc(s.name || s.id)}</strong>
              <div class="hint"><code>${esc(s.adapter || "")}</code>${s.base_url ? ` · ${esc(s.base_url)}` : ""}</div>
            </td>
            <td><span class="proto-pill ${esc(proto)}">${esc(proto)}</span></td>
            <td><span class="privacy-pill ${esc(priv)}">${esc(priv)}</span></td>
            <td>${esc(String(s.priority != null ? s.priority : 25))}</td>
            <td>${categoryPills(s.categories)}</td>
            <td>
              <label class="switch" title="Enable / disable">
                <input type="checkbox" class="src-enable" data-toggle-id="${esc(s.id)}" ${s.enabled ? "checked" : ""} />
                <span class="slider"></span>
              </label>
            </td>
            <td class="actions">
              <button class="btn sm" data-edit-id="${esc(s.id)}">Edit</button>
              <button class="btn sm danger" data-delete-id="${esc(s.id)}">Remove</button>
            </td>
          </tr>`;
            })
            .join("")}
        </tbody></table>`;
      $("#sources-table").querySelectorAll(".src-enable").forEach((el) => {
        el.addEventListener("change", async () => {
          const id = el.dataset.toggleId;
          const next = !!el.checked;
          try {
            await api(`/api/v1/sources/${encodeURIComponent(id)}`, {
              method: "PATCH",
              body: { enabled: next },
            });
            toast(next ? "Indexer enabled" : "Indexer disabled");
            loadSources();
          } catch (e) {
            toast(e.message, true);
            el.checked = !next;
          }
        });
      });
      $("#sources-table").querySelectorAll("[data-delete-id]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          if (!confirm("Remove this indexer? This does not delete downloaded files.")) return;
          try {
            await api(`/api/v1/sources/${encodeURIComponent(btn.dataset.deleteId)}`, {
              method: "DELETE",
            });
            toast("Indexer removed");
            loadSources();
          } catch (e) {
            toast(e.message, true);
          }
        });
      });
      $("#sources-table").querySelectorAll("[data-edit-id]").forEach((btn) => {
        btn.addEventListener("click", () => editSource(btn.dataset.editId));
      });
    } catch (e) {
      toast(e.message, true);
    }
  }

  function flattenSettingsForForm(settings) {
    const flat = { ...(settings || {}) };
    const sel = flat.selectors || {};
    if (sel.result) flat.selector_result = sel.result;
    if (sel.title) flat.selector_title = sel.title;
    if (sel.url) flat.selector_url = sel.url;
    if (sel.date) flat.selector_date = sel.date;
    if (sel.drive_link) flat.selector_drive_link = sel.drive_link;
    return flat;
  }

  async function editSource(sourceId) {
    try {
      if (!adapterCatalog.length) {
        adapterCatalog = await api("/api/v1/adapters");
      }
      const source = await api(`/api/v1/sources/${encodeURIComponent(sourceId)}`);
      const entry =
        adapterCatalog.find((a) => a.adapter === source.adapter) ||
        adapterCatalog.find((a) => a.id === source.adapter) ||
        adapterCatalog[0];
      if (!entry) return toast("No adapter schema available to edit", true);
      showAddSourcePanel(true);
      const form = $("#adapter-settings-form");
      const values = flattenSettingsForForm(source.settings || source);
      form.classList.remove("hidden");
      const fields = entry.settings || [];
      form.innerHTML = `
        <h3 style="margin:0 0 0.75rem">Edit ${esc(source.name || sourceId)}</h3>
        <form id="adapter-config-form">
          ${fields
            .map((f) => {
              const id = `cfg-${f.name}`;
              let val = values[f.name];
              if (val == null) val = f.default == null ? "" : f.default;
              if (f.type === "boolean") {
                return `<label class="row" style="gap:0.5rem;margin:0.5rem 0">
                  <input type="checkbox" id="${id}" data-fname="${esc(f.name)}" ${val ? "checked" : ""} />
                  <span>${esc(f.label || f.name)}</span>
                </label>`;
              }
              const inputType = f.type === "number" ? "number" : f.type === "url" ? "url" : "text";
              return `<label>${esc(f.label || f.name)}
                <input id="${id}" data-fname="${esc(f.name)}" type="${inputType}" value="${esc(String(val))}" />
                ${f.help ? `<span class="hint">${esc(f.help)}</span>` : ""}
              </label>`;
            })
            .join("")}
          <div class="row" style="margin-top:1rem">
            <button class="btn primary" type="submit">Save changes</button>
          </div>
        </form>`;
      $("#adapter-config-form").addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const settings = {};
        form.querySelectorAll("[data-fname]").forEach((input) => {
          const name = input.dataset.fname;
          if (input.type === "checkbox") settings[name] = input.checked;
          else if (input.type === "number")
            settings[name] = input.value === "" ? null : Number(input.value);
          else settings[name] = input.value.trim();
        });
        try {
          await api(`/api/v1/sources/${encodeURIComponent(sourceId)}`, {
            method: "PATCH",
            body: { settings },
          });
          toast("Indexer updated");
          showAddSourcePanel(false);
          loadSources();
        } catch (e) {
          toast(e.message, true);
        }
      });
    } catch (e) {
      toast(e.message, true);
    }
  }

  function showAddSourcePanel(show) {
    const modal = $("#add-indexer-modal");
    if (!modal) return;
    modal.classList.toggle("hidden", !show);
    if (show) {
      const form = $("#adapter-settings-form");
      if (form) {
        form.classList.add("hidden");
        form.innerHTML = "";
      }
      const search = $("#catalog-search");
      if (search) search.value = "";
      loadAdapterCatalog();
    }
  }

  function catalogFilterValues() {
    return {
      q: (($("#catalog-search") && $("#catalog-search").value) || "").trim().toLowerCase(),
      protocol: (($("#catalog-filter-protocol") && $("#catalog-filter-protocol").value) || "").toLowerCase(),
      language: (($("#catalog-filter-language") && $("#catalog-filter-language").value) || "").toLowerCase(),
      privacy: (($("#catalog-filter-privacy") && $("#catalog-filter-privacy").value) || "").toLowerCase(),
      category: (($("#catalog-filter-category") && $("#catalog-filter-category").value) || "").toLowerCase(),
    };
  }

  function filterCatalog(rows) {
    const f = catalogFilterValues();
    return (rows || []).filter((a) => {
      if (f.protocol && (a.protocol || "direct").toLowerCase() !== f.protocol) return false;
      if (f.language && (a.language || "en-US").toLowerCase() !== f.language) return false;
      if (f.privacy && (a.privacy || "public").toLowerCase() !== f.privacy) return false;
      if (f.category) {
        const cats = (a.categories || []).map((c) => String(c).toLowerCase());
        if (!cats.includes(f.category)) return false;
      }
      if (f.q) {
        const hay = `${a.name || ""} ${a.adapter || ""} ${a.description || ""}`.toLowerCase();
        if (!hay.includes(f.q)) return false;
      }
      return true;
    });
  }

  function renderCatalogTable(rows) {
    const el = $("#adapter-catalog");
    if (!el) return;
    const list = filterCatalog(rows);
    const count = $("#catalog-count");
    if (count) count.textContent = `${list.length} indexer(s) available`;
    if (!list.length) {
      el.innerHTML = `<p class="muted">No indexers match your filters.</p>`;
      return;
    }
    el.innerHTML = `<table class="indexer-table catalog-table">
      <thead><tr>
        <th>Protocol</th><th>Name</th><th>Language</th><th>Description</th><th>Privacy</th><th>Categories</th><th></th>
      </tr></thead>
      <tbody>
        ${list
          .map((a) => {
            const proto = (a.protocol || "direct").toLowerCase();
            const priv = (a.privacy || "public").toLowerCase();
            return `<tr class="catalog-row" data-adapter-id="${esc(a.id)}" style="cursor:pointer">
            <td><span class="proto-pill ${esc(proto)}">${esc(proto)}</span></td>
            <td><strong>${esc(a.name)}</strong><div class="hint"><code>${esc(a.adapter)}</code></div></td>
            <td class="muted">${esc(a.language || "en-US")}</td>
            <td class="muted">${esc(a.description || "")}</td>
            <td><span class="privacy-pill ${esc(priv)}">${esc(priv)}</span></td>
            <td>${categoryPills(a.categories)}</td>
            <td class="actions"><button class="btn sm primary" data-adapter-id="${esc(a.id)}">Select</button></td>
          </tr>`;
          })
          .join("")}
      </tbody></table>`;
    el.querySelectorAll("[data-adapter-id]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        openAdapterSettings(btn.dataset.adapterId);
      });
    });
    el.querySelectorAll("tr.catalog-row").forEach((row) => {
      row.addEventListener("click", () => openAdapterSettings(row.dataset.adapterId));
    });
  }

  async function loadAdapterCatalog() {
    try {
      adapterCatalog = await api("/api/v1/adapters");
      const form = $("#adapter-settings-form");
      if (form) {
        form.classList.add("hidden");
        form.innerHTML = "";
      }
      renderCatalogTable(adapterCatalog);
    } catch (e) {
      toast(e.message, true);
    }
  }

  function openAdapterSettings(adapterId) {
    const entry = adapterCatalog.find((a) => a.id === adapterId);
    const form = $("#adapter-settings-form");
    if (!entry || !form) return;
    const fields = entry.settings || [];
    form.classList.remove("hidden");
    form.innerHTML = `
      <h3 style="margin:0 0 0.75rem">Configure ${esc(entry.name)}</h3>
      <p class="hint">${esc(entry.description || "")}</p>
      <form id="adapter-config-form">
        ${fields
          .map((f) => {
            const id = `cfg-${f.name}`;
            const val = f.default == null ? "" : f.default;
            if (f.type === "boolean") {
              return `<label class="row" style="gap:0.5rem;margin:0.5rem 0">
                <input type="checkbox" id="${id}" data-fname="${esc(f.name)}" ${val ? "checked" : ""} />
                <span>${esc(f.label || f.name)}</span>
              </label>`;
            }
            const inputType = f.type === "number" ? "number" : f.type === "url" ? "url" : "text";
            return `<label>${esc(f.label || f.name)}
              <input id="${id}" data-fname="${esc(f.name)}" type="${inputType}" value="${esc(String(val))}" ${
              f.required ? "required" : ""
            } />
              ${f.help ? `<span class="hint">${esc(f.help)}</span>` : ""}
            </label>`;
          })
          .join("")}
        <div class="row" style="margin-top:1rem">
          <button class="btn primary" type="submit">Add & enable</button>
          <button class="btn" type="button" id="cfg-back-catalog">Back to list</button>
        </div>
      </form>`;
    form.scrollIntoView({ behavior: "smooth", block: "nearest" });
    const back = $("#cfg-back-catalog");
    if (back) {
      back.addEventListener("click", () => {
        form.classList.add("hidden");
        form.innerHTML = "";
      });
    }
    $("#adapter-config-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const settings = {};
      form.querySelectorAll("[data-fname]").forEach((input) => {
        const name = input.dataset.fname;
        if (input.type === "checkbox") settings[name] = input.checked;
        else if (input.type === "number") settings[name] = input.value === "" ? null : Number(input.value);
        else settings[name] = input.value.trim();
      });
      try {
        const created = await api("/api/v1/sources", {
          method: "POST",
          body: { adapter: entry.id, settings, enabled: true },
        });
        toast(`Added ${created.name || entry.name}`);
        showAddSourcePanel(false);
        loadSources();
      } catch (e) {
        toast(e.message, true);
      }
    });
  }

  async function loadSystemStatus() {
    const el = $("#system-status");
    const healthEl = $("#system-health");
    if (!el) return;
    try {
      const st = await api("/api/v1/system/status");
      const rows = [
        ["App", `${st.app || "Directarr"} ${st.version || ""}`],
        ["Health", st.health || "ok"],
        ["Python", st.python || "—"],
        ["Platform", st.platform || "—"],
        ["Auth mode", st.auth_mode || "—"],
        ["Indexers", `${st.indexers_enabled || 0} enabled / ${st.indexers_total || 0} total`],
        ["Releases indexed", String(st.releases ?? "—")],
        ["Downloads", `${st.downloads_active || 0} active / ${st.downloads || 0} total`],
        ["Google Drive", st.gdrive_authorized ? "Authorized" : "Not authorized"],
        ["Config dir", st.config_dir || "—"],
        ["Download dir", st.download_dir || "—"],
        ["Staging dir", st.staging_dir || "—"],
        ["Max download", `${st.max_download_mb || "—"} MB`],
        ["HTTP connections", String(st.download_connections ?? "—")],
        ["Torznab", st.torznab_path || "/api"],
      ];
      el.innerHTML = rows
        .map(
          ([label, val]) =>
            `<div class="settings-label">${esc(label)}</div><div class="settings-field"><code>${esc(val)}</code></div>`
        )
        .join("");
      if (healthEl) {
        healthEl.innerHTML =
          st.health === "ok"
            ? `<span class="pill ok">Healthy</span> · Directarr is responding`
            : `<span class="pill err">${esc(st.health || "unknown")}</span>`;
      }
    } catch (e) {
      if (el) el.innerHTML = `<div class="muted">${esc(e.message)}</div>`;
      if (healthEl) healthEl.textContent = e.message;
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
      const setVal = (sel, val) => {
        const el = $(sel);
        if (el) el.value = val ?? "";
      };
      setVal("#settings-bind", data.bind_address || "0.0.0.0");
      setVal("#settings-port", data.port != null ? data.port : location.port || 8787);
      setVal("#settings-url-base", data.url_base || "");
      setVal("#settings-allowed-hosts", data.allowed_hosts || "");
      setVal("#settings-auth-user", data.auth_username || "admin");
      const authMode = $("#settings-auth-mode");
      if (authMode) authMode.value = (data.auth_mode || "forms").toLowerCase() === "none" ? "none" : "forms";
      const authRequired = $("#settings-auth-required");
      if (authRequired) {
        authRequired.checked = (data.auth_mode || "forms").toLowerCase() !== "none";
      }
      const np = $("#settings-new-password");
      if (np) np.value = "";
      const cp = $("#settings-current-password");
      if (cp) cp.value = "";
      if (key) localStorage.setItem(KEY, key);
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function saveGeneralSettings(ev) {
    if (ev) ev.preventDefault();
    const body = {
      bind_address: ($("#settings-bind") && $("#settings-bind").value.trim()) || "0.0.0.0",
      port: Number(($("#settings-port") && $("#settings-port").value) || 8787),
      url_base: ($("#settings-url-base") && $("#settings-url-base").value.trim()) || "",
      allowed_hosts: ($("#settings-allowed-hosts") && $("#settings-allowed-hosts").value.trim()) || "",
      auth_mode: ($("#settings-auth-mode") && $("#settings-auth-mode").value) || "forms",
      auth_username: ($("#settings-auth-user") && $("#settings-auth-user").value.trim()) || "admin",
    };
    const newPw = $("#settings-new-password") && $("#settings-new-password").value;
    const curPw = $("#settings-current-password") && $("#settings-current-password").value;
    if (newPw) {
      body.new_password = newPw;
      body.current_password = curPw || "";
    }
    try {
      await api("/api/v1/settings/general", { method: "PUT", body });
      toast("Settings saved");
      await updateSettings();
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function loadApplications() {
    try {
      const data = await api("/api/v1/applications");
      const items = data.items || [];
      const el = $("#applications-table");
      if (!el) return;
      if (!items.length) {
        el.innerHTML = `<div class="empty-state"><p class="muted">No applications linked yet. Add Sonarr or Radarr to sync indexers.</p></div>`;
        return;
      }
      el.innerHTML =
        `<table><thead><tr>
          <th>Name</th><th>Type</th><th>URL</th><th>Sync</th><th>Status</th><th></th>
        </tr></thead><tbody>` +
        items
          .map((a) => {
            const en = a.enabled ? '<span class="pill ok">On</span>' : '<span class="pill muted">Off</span>';
            const st = a.last_sync_status
              ? a.last_sync_status.startsWith("ok") || a.last_sync_status === "ok"
                ? `<span class="pill ok">${esc(a.last_sync_status)}</span>`
                : `<span class="pill err">${esc(a.last_sync_status)}</span>`
              : '<span class="pill muted">—</span>';
            return `<tr data-id="${esc(a.id)}">
              <td><strong>${esc(a.name)}</strong> ${en}</td>
              <td><span class="proto-pill">${esc(a.app_type)}</span></td>
              <td class="muted" style="max-width:220px;overflow:hidden;text-overflow:ellipsis">${esc(a.base_url)}</td>
              <td class="muted">${esc(a.sync_level || "fullSync")}</td>
              <td>${st}</td>
              <td class="row" style="gap:0.35rem">
                <button type="button" class="btn sm" data-app-test="${esc(a.id)}">Test</button>
                <button type="button" class="btn sm" data-app-edit="${esc(a.id)}">Edit</button>
                <button type="button" class="btn sm danger" data-app-del="${esc(a.id)}">Delete</button>
              </td>
            </tr>`;
          })
          .join("") +
        `</tbody></table>`;
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function loadSyncProfiles() {
    try {
      const data = await api("/api/v1/sync-profiles");
      const items = data.items || [];
      const el = $("#sync-profiles-table");
      if (el) {
        if (!items.length) {
          el.innerHTML = `<p class="muted">No profiles.</p>`;
        } else {
          el.innerHTML =
            `<table><thead><tr><th>Name</th><th>RSS</th><th>Auto</th><th>Interactive</th><th>Seeders</th><th></th></tr></thead><tbody>` +
            items
              .map(
                (p) => `<tr>
                <td>${esc(p.name)}</td>
                <td>${p.enable_rss ? "✓" : "—"}</td>
                <td>${p.enable_automatic_search ? "✓" : "—"}</td>
                <td>${p.enable_interactive_search ? "✓" : "—"}</td>
                <td>${p.minimum_seeders ?? 1}</td>
                <td class="row" style="gap:0.35rem">
                  <button type="button" class="btn sm" data-profile-edit="${esc(p.id)}">Edit</button>
                  ${
                    p.id === "default"
                      ? ""
                      : `<button type="button" class="btn sm danger" data-profile-del="${esc(p.id)}">Delete</button>`
                  }
                </td>
              </tr>`
              )
              .join("") +
            `</tbody></table>`;
        }
      }
      const sel = $("#app-sync-profile");
      if (sel) {
        const cur = sel.value;
        sel.innerHTML = items
          .map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`)
          .join("");
        if (cur) sel.value = cur;
      }
      return items;
    } catch (e) {
      toast(e.message, true);
      return [];
    }
  }

  async function loadHistory() {
    try {
      const filter = ($("#history-filter") && $("#history-filter").value) || "";
      const q = filter ? `?event_type=${encodeURIComponent(filter)}&limit=150` : "?limit=150";
      const data = await api("/api/v1/history" + q);
      const items = data.items || [];
      const el = $("#history-table");
      if (!el) return;
      if (!items.length) {
        el.innerHTML = `<div class="empty-state"><p class="muted">No history events yet.</p></div>`;
        return;
      }
      el.innerHTML =
        `<table><thead><tr><th>When</th><th>Type</th><th>Message</th><th>Details</th></tr></thead><tbody>` +
        items
          .map(
            (h) => `<tr>
            <td class="muted" style="white-space:nowrap">${esc(h.created_at || "")}</td>
            <td><span class="pill muted">${esc(h.event_type)}</span></td>
            <td>${esc(h.message)}</td>
            <td class="muted" style="max-width:280px;overflow:hidden;text-overflow:ellipsis">${esc(h.details || "")}</td>
          </tr>`
          )
          .join("") +
        `</tbody></table>`;
    } catch (e) {
      toast(e.message, true);
    }
  }

  function openApplicationModal(app) {
    $("#app-edit-id").value = app ? app.id : "";
    $("#application-modal-title").textContent = app ? "Edit Application" : "Add Application";
    $("#app-type").value = (app && app.app_type) || "sonarr";
    $("#app-name").value = (app && app.name) || "";
    $("#app-base-url").value = (app && app.base_url) || "";
    $("#app-api-key").value = (app && app.api_key) || "";
    $("#app-sync-level").value = (app && app.sync_level) || "fullSync";
    $("#app-enabled").checked = !app || !!app.enabled;
    $("#app-tags").value = (app && app.tags) || "";
    loadSyncProfiles().then(() => {
      if (app && app.sync_profile_id) $("#app-sync-profile").value = app.sync_profile_id;
    });
    $("#application-modal").classList.remove("hidden");
  }

  function closeApplicationModal() {
    $("#application-modal").classList.add("hidden");
  }

  function openSyncProfileModal(profile) {
    $("#profile-edit-id").value = profile ? profile.id : "";
    $("#sync-profile-modal-title").textContent = profile ? "Edit Sync Profile" : "Add Sync Profile";
    $("#profile-name").value = (profile && profile.name) || "";
    $("#profile-rss").checked = !profile || !!profile.enable_rss;
    $("#profile-auto").checked = !profile || !!profile.enable_automatic_search;
    $("#profile-interactive").checked = !profile || !!profile.enable_interactive_search;
    $("#profile-seeders").value = profile && profile.minimum_seeders != null ? profile.minimum_seeders : 1;
    $("#sync-profile-modal").classList.remove("hidden");
  }

  function closeSyncProfileModal() {
    $("#sync-profile-modal").classList.add("hidden");
  }

  async function loadStats() {
    try {
      const data = await api("/api/v1/stats");
      const byStatus = data.downloads_by_status || {};
      const total = data.downloads_total ?? 0;
      const completed = byStatus.completed || 0;
      const failed = (byStatus.failed || 0) + (byStatus.cancelled || 0);
      const setText = (id, val) => {
        const el = $(id);
        if (el) el.textContent = String(val);
      };
      setText("#stats-total-dl", total);
      setText("#stats-completed", completed);
      setText("#stats-failed", failed);
      setText("#stats-releases", data.releases_total ?? 0);

      const statusOrder = ["completed", "downloading", "queued", "failed", "cancelled"];
      const statusRows = statusOrder
        .filter((s) => (byStatus[s] || 0) > 0 || s === "completed")
        .concat(Object.keys(byStatus).filter((s) => !statusOrder.includes(s)))
        .filter((s, i, arr) => arr.indexOf(s) === i);
      const maxStatus = Math.max(1, ...statusRows.map((s) => byStatus[s] || 0));
      const statusCls = (s) => {
        if (s === "completed") return "ok";
        if (s === "failed" || s === "cancelled") return "err";
        if (s === "downloading" || s === "queued") return "warn";
        return "";
      };
      const statusHtml = statusRows.length
        ? statusRows
            .map((s) => {
              const n = byStatus[s] || 0;
              const pct = Math.round((n / maxStatus) * 100);
              return `<div class="stat-bar-row">
                <span>${esc(s)}</span>
                <div class="stat-bar-track"><div class="stat-bar-fill ${statusCls(s)}" style="width:${pct}%"></div></div>
                <span class="muted">${n}</span>
              </div>`;
            })
            .join("")
        : `<p class="muted">No download jobs yet.</p>`;
      const byStatusEl = $("#stats-by-status");
      if (byStatusEl) byStatusEl.innerHTML = statusHtml;

      const sources = data.releases_by_source || [];
      const maxSrc = Math.max(1, ...sources.map((r) => r.count || 0));
      const srcHtml = sources.length
        ? `<table><thead><tr><th>Indexer / source</th><th>Releases</th><th></th></tr></thead><tbody>` +
          sources
            .map((r) => {
              const pct = Math.round(((r.count || 0) / maxSrc) * 100);
              return `<tr>
                <td>${esc(r.source || "unknown")}</td>
                <td>${r.count || 0}</td>
                <td style="min-width:120px"><div class="stat-bar-track"><div class="stat-bar-fill" style="width:${pct}%"></div></div></td>
              </tr>`;
            })
            .join("") +
          `</tbody></table>`
        : `<p class="muted">No indexed releases yet. Run a search to populate sources.</p>`;
      const bySourceEl = $("#stats-by-source");
      if (bySourceEl) bySourceEl.innerHTML = srcHtml;

      const recent = data.recent_downloads || [];
      const recentHtml = recent.length
        ? `<table><thead><tr><th>Title</th><th>Status</th><th>When</th></tr></thead><tbody>` +
          recent
            .map(
              (d) => `<tr>
              <td>${esc(d.title || "—")}</td>
              <td>${statusPill(d.status)}</td>
              <td class="muted">${esc(d.created_at || "—")}</td>
            </tr>`
            )
            .join("") +
          `</tbody></table>`
        : `<p class="muted">No recent downloads.</p>`;
      const recentEl = $("#stats-recent");
      if (recentEl) recentEl.innerHTML = recentHtml;
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

  // Settings sub-tabs
  $$(".settings-tab").forEach((btn) => {
    btn.addEventListener("click", () => setSettingsTab(btn.dataset.settingsTab));
  });

  const refreshStatsBtn = $("#refresh-stats");
  if (refreshStatsBtn) {
    refreshStatsBtn.addEventListener("click", () => {
      loadStats();
      toast("Stats refreshed");
    });
  }

  // General settings form
  const generalForm = $("#general-settings-form");
  if (generalForm) {
    generalForm.addEventListener("submit", saveGeneralSettings);
  }
  const authModeSel = $("#settings-auth-mode");
  const authReqChk = $("#settings-auth-required");
  if (authModeSel && authReqChk) {
    authModeSel.addEventListener("change", () => {
      authReqChk.checked = authModeSel.value !== "none";
    });
    authReqChk.addEventListener("change", () => {
      authModeSel.value = authReqChk.checked ? "forms" : "none";
    });
  }

  // Applications
  const addAppBtn = $("#add-application-btn");
  if (addAppBtn) addAppBtn.addEventListener("click", () => openApplicationModal(null));
  ["#application-modal-close", "#application-modal-cancel"].forEach((sel) => {
    const b = $(sel);
    if (b) b.addEventListener("click", closeApplicationModal);
  });
  const appSave = $("#application-modal-save");
  if (appSave) {
    appSave.addEventListener("click", async () => {
      const id = ($("#app-edit-id") && $("#app-edit-id").value) || "";
      const body = {
        app_type: $("#app-type").value,
        name: $("#app-name").value.trim() || $("#app-type").value,
        base_url: $("#app-base-url").value.trim(),
        api_key: $("#app-api-key").value.trim(),
        sync_level: $("#app-sync-level").value,
        sync_profile_id: ($("#app-sync-profile") && $("#app-sync-profile").value) || "default",
        enabled: $("#app-enabled").checked,
        tags: ($("#app-tags") && $("#app-tags").value.trim()) || "",
      };
      try {
        if (id) await api(`/api/v1/applications/${encodeURIComponent(id)}`, { method: "PUT", body });
        else await api("/api/v1/applications", { method: "POST", body });
        toast(id ? "Application updated" : "Application added");
        closeApplicationModal();
        loadApplications();
      } catch (e) {
        toast(e.message, true);
      }
    });
  }
  const appTestBtn = $("#app-test-btn");
  if (appTestBtn) {
    appTestBtn.addEventListener("click", async () => {
      const id = ($("#app-edit-id") && $("#app-edit-id").value) || "";
      const body = {
        app_type: $("#app-type").value,
        name: $("#app-name").value.trim() || $("#app-type").value,
        base_url: $("#app-base-url").value.trim(),
        api_key: $("#app-api-key").value.trim(),
      };
      try {
        const res = id
          ? await api(`/api/v1/applications/${encodeURIComponent(id)}/test`, { method: "POST" })
          : await api("/api/v1/applications/test", { method: "POST", body });
        toast(res.ok ? `Connected: ${res.message || "ok"}` : `Failed: ${res.message || res.status_code}`, !res.ok);
      } catch (e) {
        toast(e.message, true);
      }
    });
  }
  document.addEventListener("click", async (ev) => {
    const t = ev.target;
    if (!(t instanceof HTMLElement)) return;
    const testId = t.getAttribute("data-app-test");
    const editId = t.getAttribute("data-app-edit");
    const delId = t.getAttribute("data-app-del");
    if (testId) {
      try {
        const res = await api(`/api/v1/applications/${encodeURIComponent(testId)}/test`, { method: "POST" });
        toast(res.ok ? `Connected: ${res.message || "ok"}` : `Failed: ${res.message || res.status_code}`, !res.ok);
        loadApplications();
      } catch (e) {
        toast(e.message, true);
      }
    }
    if (editId) {
      try {
        const app = await api(`/api/v1/applications/${encodeURIComponent(editId)}`);
        openApplicationModal(app);
      } catch (e) {
        toast(e.message, true);
      }
    }
    if (delId) {
      if (!confirm("Delete this application?")) return;
      try {
        await api(`/api/v1/applications/${encodeURIComponent(delId)}`, { method: "DELETE" });
        toast("Application removed");
        loadApplications();
      } catch (e) {
        toast(e.message, true);
      }
    }
    const pe = t.getAttribute("data-profile-edit");
    const pd = t.getAttribute("data-profile-del");
    if (pe) {
      try {
        const data = await api("/api/v1/sync-profiles");
        const profile = (data.items || []).find((p) => p.id === pe);
        if (profile) openSyncProfileModal(profile);
      } catch (e) {
        toast(e.message, true);
      }
    }
    if (pd) {
      if (!confirm("Delete this sync profile?")) return;
      try {
        await api(`/api/v1/sync-profiles/${encodeURIComponent(pd)}`, { method: "DELETE" });
        toast("Profile deleted");
        loadSyncProfiles();
      } catch (e) {
        toast(e.message, true);
      }
    }
  });

  // Sync profiles
  const addProfileBtn = $("#add-sync-profile-btn");
  if (addProfileBtn) addProfileBtn.addEventListener("click", () => openSyncProfileModal(null));
  ["#sync-profile-modal-close", "#sync-profile-modal-cancel"].forEach((sel) => {
    const b = $(sel);
    if (b) b.addEventListener("click", closeSyncProfileModal);
  });
  const profileSave = $("#sync-profile-modal-save");
  if (profileSave) {
    profileSave.addEventListener("click", async () => {
      const id = ($("#profile-edit-id") && $("#profile-edit-id").value) || "";
      const body = {
        name: $("#profile-name").value.trim(),
        enable_rss: $("#profile-rss").checked,
        enable_automatic_search: $("#profile-auto").checked,
        enable_interactive_search: $("#profile-interactive").checked,
        minimum_seeders: Number($("#profile-seeders").value || 1),
      };
      try {
        if (id) await api(`/api/v1/sync-profiles/${encodeURIComponent(id)}`, { method: "PUT", body });
        else await api("/api/v1/sync-profiles", { method: "POST", body });
        toast("Sync profile saved");
        closeSyncProfileModal();
        loadSyncProfiles();
      } catch (e) {
        toast(e.message, true);
      }
    });
  }

  // History
  const refreshHistoryBtn = $("#refresh-history");
  if (refreshHistoryBtn) refreshHistoryBtn.addEventListener("click", () => loadHistory());
  const historyFilter = $("#history-filter");
  if (historyFilter) historyFilter.addEventListener("change", () => loadHistory());
  const clearHistoryBtn = $("#clear-history");
  if (clearHistoryBtn) {
    clearHistoryBtn.addEventListener("click", async () => {
      if (!confirm("Clear all history?")) return;
      try {
        await api("/api/v1/history", { method: "DELETE" });
        toast("History cleared");
        loadHistory();
      } catch (e) {
        toast(e.message, true);
      }
    });
  }

  ["#menu-toggle", "#topbar-menu"].forEach((sel) => {
    const btn = $(sel);
    if (!btn) return;
    btn.addEventListener("click", () => {
      const shell = $("#app-shell");
      const open = !(shell && shell.classList.contains("nav-open"));
      setMobileNavOpen(open);
    });
  });
  const navBackdrop = $("#nav-backdrop");
  if (navBackdrop) {
    navBackdrop.addEventListener("click", () => setMobileNavOpen(false));
  }
  window.addEventListener("resize", () => {
    if (window.innerWidth > 900) setMobileNavOpen(false);
  });

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

  const FILTER_QUERY_TERMS = {
    "1080p": "1080p",
    "720p": "720p",
    "2160p": "2160p",
    "480p": "480p",
    dual: "dual audio",
    multi: "multi audio",
    hevc: "x265",
    av1: "AV1",
    x264: "x264",
  };

  function selectedSearchFilters() {
    return $$("#search-filters input[data-filter]:checked").map((el) => el.dataset.filter);
  }

  function buildSearchQuery(base) {
    const parts = [base.trim()];
    for (const key of selectedSearchFilters()) {
      const term = FILTER_QUERY_TERMS[key];
      if (term && !parts.join(" ").toLowerCase().includes(term.toLowerCase())) {
        parts.push(term);
      }
    }
    return parts.join(" ").trim();
  }

  function releaseMatchesFilters(r, filters) {
    if (!filters.length) return true;
    const title = (r.title || "").toLowerCase();
    const res = (r.resolution || "").toLowerCase();
    const codec = (r.codec || "").toLowerCase();
    const dual =
      !!r.dual_audio ||
      /dual[\s._-]*audio|multi[\s._-]*audio|\bdual\b|\bmulti\b/i.test(r.title || "");

    const resFilters = filters.filter((f) => ["1080p", "720p", "2160p", "480p"].includes(f));
    if (resFilters.length) {
      const ok = resFilters.some((f) => res === f || title.includes(f));
      if (!ok) return false;
    }
    if (filters.includes("dual") || filters.includes("multi")) {
      if (!dual && !title.includes("dual") && !title.includes("multi")) return false;
    }
    if (filters.includes("hevc")) {
      if (
        !(
          codec.includes("hevc") ||
          codec.includes("x265") ||
          codec.includes("h.265") ||
          /x265|hevc|h\.?265/i.test(title)
        )
      ) {
        return false;
      }
    }
    if (filters.includes("av1")) {
      if (!(codec.includes("av1") || title.includes("av1"))) return false;
    }
    if (filters.includes("x264")) {
      if (!(codec.includes("x264") || codec.includes("h.264") || /x264|h\.?264/i.test(title))) {
        return false;
      }
    }
    return true;
  }

  $("#search-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const base = $("#search-q").value.trim();
    if (!base) return;
    const filters = selectedSearchFilters();
    const q = buildSearchQuery(base);
    $("#search-results").innerHTML = `<p class="muted">Searching…</p>`;
    try {
      const data = await api(`/api/v1/search?q=${encodeURIComponent(q)}`);
      let rows = data.releases || [];
      const before = rows.length;
      rows = rows.filter((r) => releaseMatchesFilters(r, filters));
      renderReleasesTable($("#search-results"), rows, { queue: true });
      if (filters.length) {
        toast(`Found ${before} · showing ${rows.length} after filters`);
      } else {
        toast(`Found ${data.count ?? before} releases`);
      }
    } catch (e) {
      $("#search-results").innerHTML = "";
      toast(e.message, true);
    }
  });

  $("#refresh-releases").addEventListener("click", loadReleases);
  $("#refresh-downloads").addEventListener("click", () => loadDownloads({ silent: false }));

  const clearFinishedBtn = $("#clear-finished-downloads");
  if (clearFinishedBtn) {
    clearFinishedBtn.addEventListener("click", async () => {
      clearFinishedBtn.disabled = true;
      try {
        const res = await api("/api/v1/downloads/clear", {
          method: "POST",
          body: { finished_only: true },
        });
        toast(`Cleared ${res.deleted || 0} finished job(s)`);
        await loadDownloads({ silent: true });
      } catch (e) {
        toast(e.message, true);
      } finally {
        clearFinishedBtn.disabled = false;
      }
    });
  }

  const clearAllBtn = $("#clear-all-downloads");
  if (clearAllBtn) {
    clearAllBtn.addEventListener("click", async () => {
      if (!confirm("Cancel any active downloads and clear the entire list?")) return;
      clearAllBtn.disabled = true;
      try {
        const res = await api("/api/v1/downloads/clear", {
          method: "POST",
          body: { finished_only: false },
        });
        toast(`Cleared ${res.deleted || 0} job(s)`);
        await loadDownloads({ silent: true });
      } catch (e) {
        toast(e.message, true);
      } finally {
        clearAllBtn.disabled = false;
      }
    });
  }

  const dlSelectAll = $("#dl-select-all");
  if (dlSelectAll) {
    dlSelectAll.addEventListener("click", () => {
      $$("#downloads-table .dl-check").forEach((c) => {
        c.checked = true;
      });
      const master = $("#dl-check-all");
      if (master) master.checked = true;
    });
  }
  const dlSelectNone = $("#dl-select-none");
  if (dlSelectNone) {
    dlSelectNone.addEventListener("click", () => {
      $$("#downloads-table .dl-check").forEach((c) => {
        c.checked = false;
      });
      const master = $("#dl-check-all");
      if (master) master.checked = false;
    });
  }
  const dlRemoveSelected = $("#dl-remove-selected");
  if (dlRemoveSelected) {
    dlRemoveSelected.addEventListener("click", async () => {
      const ids = selectedDownloadIds();
      if (!ids.length) return toast("Select at least one download", true);
      if (!confirm(`Remove ${ids.length} item(s) from the list? Files on disk are kept.`)) return;
      dlRemoveSelected.disabled = true;
      try {
        const res = await api("/api/v1/downloads/bulk-remove", {
          method: "POST",
          body: { ids },
        });
        toast(`Removed ${res.removed || ids.length} from list`);
        await loadDownloads({ silent: true });
      } catch (e) {
        toast(e.message, true);
      } finally {
        dlRemoveSelected.disabled = false;
      }
    });
  }
  const dlDeleteFiles = $("#dl-delete-files");
  if (dlDeleteFiles) {
    dlDeleteFiles.addEventListener("click", async () => {
      const ids = selectedDownloadIds();
      if (!ids.length) return toast("Select at least one download", true);
      if (
        !confirm(
          `Delete physical files for ${ids.length} selected job(s)? This cannot be undone. List entries stay.`
        )
      )
        return;
      dlDeleteFiles.disabled = true;
      try {
        const res = await api("/api/v1/downloads/delete-files", {
          method: "POST",
          body: { ids },
        });
        toast(
          `Deleted ${res.deleted_count || 0} file(s)` +
            (res.missing && res.missing.length ? ` · ${res.missing.length} missing` : "")
        );
        await loadDownloads({ silent: true });
      } catch (e) {
        toast(e.message, true);
      } finally {
        dlDeleteFiles.disabled = false;
      }
    });
  }

  const logoutBtn = $("#logout-btn");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", async () => {
      try {
        await fetch("/api/v1/auth/logout", {
          method: "POST",
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
      } catch (e) {}
      try {
        localStorage.removeItem(KEY);
      } catch (e) {}
      showLogin();
      const passEl = $("#login-pass");
      if (passEl) passEl.value = "";
      toast("Logged out");
    });
  }

  $("#reload-sources").addEventListener("click", async () => {
    try {
      const r = await api("/api/v1/sources/reload", { method: "POST" });
      toast(`Reloaded ${r.loaded} sources (${r.enabled} enabled)`);
      loadSources();
    } catch (e) {
      toast(e.message, true);
    }
  });

  const addSourceBtn = $("#add-source-btn");
  if (addSourceBtn) addSourceBtn.addEventListener("click", () => showAddSourcePanel(true));
  ["#add-indexer-close", "#add-indexer-close-2"].forEach((sel) => {
    const btn = $(sel);
    if (btn) btn.addEventListener("click", () => showAddSourcePanel(false));
  });
  const addIndexerModal = $("#add-indexer-modal");
  if (addIndexerModal) {
    addIndexerModal.addEventListener("click", (ev) => {
      if (ev.target === addIndexerModal) showAddSourcePanel(false);
    });
  }
  ["catalog-search", "catalog-filter-protocol", "catalog-filter-language", "catalog-filter-privacy", "catalog-filter-category"].forEach(
    (id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.addEventListener("input", () => renderCatalogTable(adapterCatalog));
      el.addEventListener("change", () => renderCatalogTable(adapterCatalog));
    }
  );
  const refreshSystem = $("#refresh-system");
  if (refreshSystem) refreshSystem.addEventListener("click", () => loadSystemStatus());

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
  manageDownloadPolling();
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
