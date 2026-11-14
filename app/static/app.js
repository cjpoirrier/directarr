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
      sources: "Sources",
      gdrive: "Google Drive",
      settings: "Settings",
    };
    $("#view-title").textContent = titles[name] || name;
    if (name === "dashboard") loadDashboard({ silent: false });
    if (name === "releases") loadReleases();
    if (name === "downloads") loadDownloads({ silent: false });
    if (name === "sources") loadSources();
    if (name === "gdrive") loadGdrive();
    if (name === "settings") updateSettings();
    // Restart (or stop) the download poller for the new view
    manageDownloadPolling();
    setMobileNavOpen(false);
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

  function progressCell(r) {
    const pct = Math.max(0, Math.min(100, Number(r.progress) || 0));
    const active = (r.status || "").toLowerCase() === "downloading";
    if (active || (pct > 0 && pct < 100)) {
      return `<span class="bar" title="${pct}%"><span style="width:${pct}%"></span></span> ${pct}%`;
    }
    return `${pct}%`;
  }

  function renderDownloadsTable(el, rows) {
    if (!rows.length) {
      el.innerHTML = `<p class="muted">No download jobs yet.</p>`;
      return;
    }
    el.innerHTML = `<table>
      <thead><tr><th>Title</th><th>Status</th><th>Progress</th><th>Error / path</th><th></th></tr></thead>
      <tbody>
        ${rows
          .map((r) => {
            const st = (r.status || "").toLowerCase();
            const canCancel = st === "queued" || st === "downloading";
            const actions = canCancel
              ? `<button class="btn sm danger" data-cancel-id="${esc(r.id)}">Cancel</button>`
              : `<button class="btn sm" data-remove-id="${esc(r.id)}">Remove</button>`;
            return `<tr>
          <td class="title">${esc(r.title)}</td>
          <td>${statusPill(r.status)}</td>
          <td>${progressCell(r)}</td>
          <td class="title muted">${esc(r.error || r.path || "")}</td>
          <td class="actions">${actions}</td>
        </tr>`;
          })
          .join("")}
      </tbody></table>`;
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

  async function loadSources() {
    try {
      const rows = await api("/api/v1/sources");
      if (!rows.length) {
        $("#sources-table").innerHTML = `<p class="muted">No sources installed. Click <strong>Add source</strong> to pick an adapter.</p>`;
        return;
      }
      $("#sources-table").innerHTML = `<table>
        <thead><tr><th>Name</th><th>Adapter</th><th>URL</th><th>Status</th><th></th></tr></thead>
        <tbody>
          ${rows
            .map(
              (s) => `<tr>
            <td>${esc(s.name)}</td>
            <td><code>${esc(s.adapter)}</code></td>
            <td class="title muted">${esc(s.base_url || "")}</td>
            <td>${
              s.enabled
                ? '<span class="pill ok">enabled</span>'
                : '<span class="pill muted">disabled</span>'
            }</td>
            <td class="actions">
              <button class="btn sm" data-edit-id="${esc(s.id)}">Edit</button>
              <button class="btn sm" data-toggle-id="${esc(s.id)}" data-enabled="${s.enabled ? "1" : "0"}">
                ${s.enabled ? "Disable" : "Enable"}
              </button>
              <button class="btn sm danger" data-delete-id="${esc(s.id)}">Remove</button>
            </td>
          </tr>`
            )
            .join("")}
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
      $("#sources-table").querySelectorAll("[data-delete-id]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          if (!confirm("Remove this source?")) return;
          try {
            await api(`/api/v1/sources/${encodeURIComponent(btn.dataset.deleteId)}`, {
              method: "DELETE",
            });
            toast("Source removed");
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
          toast("Source updated");
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
    const panel = $("#add-source-panel");
    if (!panel) return;
    panel.classList.toggle("hidden", !show);
    if (show) loadAdapterCatalog();
  }

  async function loadAdapterCatalog() {
    try {
      adapterCatalog = await api("/api/v1/adapters");
      const el = $("#adapter-catalog");
      const form = $("#adapter-settings-form");
      if (form) {
        form.classList.add("hidden");
        form.innerHTML = "";
      }
      el.innerHTML = `<table>
        <thead><tr><th>Adapter</th><th>Description</th><th></th></tr></thead>
        <tbody>
          ${adapterCatalog
            .map(
              (a) => `<tr>
            <td><strong>${esc(a.name)}</strong><div class="hint"><code>${esc(a.adapter)}</code></div></td>
            <td class="muted">${esc(a.description || "")}</td>
            <td class="actions"><button class="btn sm primary" data-adapter-id="${esc(a.id)}">Configure</button></td>
          </tr>`
            )
            .join("")}
        </tbody></table>`;
      el.querySelectorAll("[data-adapter-id]").forEach((btn) => {
        btn.addEventListener("click", () => openAdapterSettings(btn.dataset.adapterId));
      });
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
        </div>
      </form>`;
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
  const addSourceCancel = $("#add-source-cancel");
  if (addSourceCancel) addSourceCancel.addEventListener("click", () => showAddSourcePanel(false));

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
