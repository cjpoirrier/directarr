(() => {
  const KEY = "directarr_api_key";
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  let currentView = "dashboard";
  let downloadPollTimer = null;
  let resolveShareUrl = "";
  let resolveFiles = [];

  function apiKey() {
    return localStorage.getItem(KEY) || ($("#api-key") && $("#api-key").value.trim()) || "";
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
    if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, { ...opts, headers, credentials: "same-origin" });
    let data = null;
    const text = await res.text();
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
    if (res.status === 401) {
      showLogin(true);
      throw new Error((data && data.detail) || "Authentication required");
    }
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

  function showLogin(show) {
    const overlay = $("#login-overlay");
    if (!overlay) return;
    overlay.classList.toggle("hidden", !show);
  }

  async function checkAuth() {
    try {
      const st = await fetch("/api/v1/auth/status", { credentials: "same-origin" }).then((r) =>
        r.json()
      );
      if (!st.auth_enabled || st.authenticated) {
        showLogin(false);
        $("#logout-btn").classList.toggle("hidden", !st.auth_enabled);
        $("#conn-status").textContent = st.auth_enabled
          ? `Signed in as ${st.username || "user"}`
          : "Auth disabled";
        $("#conn-status").className = "pill ok";
        return true;
      }
      showLogin(true);
      $("#conn-status").textContent = "Login required";
      $("#conn-status").className = "pill warn";
      return false;
    } catch {
      showLogin(true);
      return false;
    }
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
    if (name === "dashboard") loadDashboard();
    if (name === "releases") loadReleases();
    if (name === "downloads") loadDownloads();
    if (name === "sources") loadSources();
    if (name === "gdrive") loadGdrive();
    if (name === "settings") updateSettings();
    manageDownloadPolling();
  }

  let lastDownloads = [];

  function downloadsAreActive(rows) {
    return (rows || []).some((r) => {
      const s = (r.status || "").toLowerCase();
      return s === "queued" || s === "downloading";
    });
  }

  function updateAutoRefreshLabel(active) {
    const label = $("#auto-refresh-label");
    if (!label || currentView !== "downloads") return;
    label.textContent = active ? "Auto-refresh on (live)" : "Auto-refresh on (idle)";
  }

  function manageDownloadPolling() {
    if (downloadPollTimer) {
      clearTimeout(downloadPollTimer);
      downloadPollTimer = null;
    }
    if (currentView !== "downloads" && currentView !== "dashboard") return;

    const tick = async () => {
      try {
        if (currentView === "downloads") await loadDownloads({ silent: true });
        else if (currentView === "dashboard") await loadDashboard({ silent: true });
      } finally {
        scheduleNext();
      }
    };

    const scheduleNext = () => {
      if (downloadPollTimer) {
        clearTimeout(downloadPollTimer);
        downloadPollTimer = null;
      }
      if (currentView !== "downloads" && currentView !== "dashboard") return;
      const active = downloadsAreActive(lastDownloads);
      updateAutoRefreshLabel(active);
      let delay = 5000;
      if (currentView === "downloads") delay = active ? 1500 : 4000;
      else if (currentView === "dashboard") delay = active ? 2500 : 8000;
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
      lastDownloads = downloads || [];
      renderDownloadsTable($("#dash-downloads"), lastDownloads.slice(0, 8));
    } catch (e) {
      if (!silent) {
        $("#conn-status").textContent = e.message;
        $("#conn-status").className = "pill err";
      }
    }
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
          .map(
            (r) => `<tr>
          <td class="title">${esc(r.title)}</td>
          <td>${esc(r.source || "")}</td>
          <td>${esc(r.resolution || "—")}</td>
          <td>${esc(r.codec || "—")}</td>
          <td class="actions">
            ${
              queue
                ? `<button class="btn sm primary" data-queue-title="${esc(r.title)}" data-queue-url="${esc(
                    r.url
                  )}">Queue</button>`
                : ""
            }
            <a class="btn sm" href="${esc(r.url)}" target="_blank" rel="noopener">Open</a>
          </td>
        </tr>`
          )
          .join("")}
      </tbody></table>`;
    el.querySelectorAll("[data-queue-url]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await api("/api/v1/downloads", {
            method: "POST",
            body: { title: btn.dataset.queueTitle, url: btn.dataset.queueUrl },
          });
          toast("Queued download");
        } catch (e) {
          toast(e.message, true);
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
          .map(
            (r) => `<tr>
          <td class="title">${esc(r.title)}</td>
          <td>${statusPill(r.status)}</td>
          <td>${progressCell(r)}</td>
          <td class="title muted">${esc(r.error || r.path || "")}</td>
          <td class="actions">
            ${
              (r.status || "").toLowerCase() === "failed"
                ? `<button class="btn sm primary" data-retry-id="${esc(r.id)}">Retry</button>`
                : ""
            }
          </td>
        </tr>`
          )
          .join("")}
      </tbody></table>`;
    el.querySelectorAll("[data-retry-id]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          const res = await api(
            `/api/v1/downloads/${encodeURIComponent(btn.dataset.retryId)}/retry`,
            { method: "POST" }
          );
          toast(
            res.finalize_only
              ? "Retrying (finalizing existing file)"
              : "Retry queued"
          );
          loadDownloads({ silent: true });
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
          ${rows
            .map(
              (s) => `<tr>
            <td>${esc(s.name)}</td>
            <td><code>${esc(s.adapter)}</code></td>
            <td>${
              s.enabled
                ? '<span class="pill ok">enabled</span>'
                : '<span class="pill muted">disabled</span>'
            }</td>
            <td class="actions">
              <button class="btn sm" data-toggle-id="${esc(s.id)}" data-enabled="${
                s.enabled ? "1" : "0"
              }">
                ${s.enabled ? "Disable" : "Enable"}
              </button>
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
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function loadGdrive() {
    const panel = $("#gdrive-auth-panel");
    try {
      const st = await api("/api/v1/gdrive/status");
      let statusHtml = "";
      if (!st.client_secrets_present) {
        statusHtml =
          '<div><span class="pill err">Not configured</span></div>' +
          '<p class="hint" style="margin-top:0.5rem">Place a Desktop OAuth client JSON at <code>' +
          esc(st.client_secrets_path) +
          "</code>, then connect.</p>";
      } else if (st.authorized && st.token_valid) {
        statusHtml =
          '<div><span class="pill ok">Connected</span> Google Drive is authorized and the token is valid.</div>' +
          '<p class="hint" style="margin-top:0.5rem">Resolve share links below. Use Disconnect to switch accounts.</p>';
      } else if (st.needs_reauth || st.token_present) {
        statusHtml =
          '<div><span class="pill warn">Reauthorization needed</span></div>' +
          '<p class="hint" style="margin-top:0.5rem">' +
          esc(st.error || "Stored token is expired or revoked.") +
          "</p>";
      } else {
        statusHtml =
          '<div><span class="pill muted">Not authorized</span></div>' +
          '<p class="hint" style="margin-top:0.5rem">Connect a Google account that can open the shared Drive links.</p>';
      }
      $("#gdrive-status").innerHTML = statusHtml;
      if (panel) panel.classList.remove("hidden");
    } catch (e) {
      $("#gdrive-status").textContent = e.message;
      if (panel) panel.classList.remove("hidden");
    }
  }

  async function updateSettings() {
    try {
      const data = await api("/api/v1/settings/security");
      const key = data.api_key || "";
      const host = location.origin;
      if ($("#api-key")) $("#api-key").value = key;
      if ($("#torznab-url")) {
        $("#torznab-url").textContent = `${host}${data.torznab_path || "/api"}?apikey=${key || "YOUR_KEY"}`;
      }
      const info = $("#settings-auth-info");
      if (info) {
        info.textContent = data.auth_enabled
          ? `Forms login enabled (user: ${data.username || "admin"}).`
          : "Forms login disabled (DIRECTARR_PASSWORD not set).";
      }
      // Keep a browser copy so optional X-Api-Key header still works if session expires.
      if (key) localStorage.setItem(KEY, key);
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function copyText(value, label) {
    try {
      await navigator.clipboard.writeText(value);
      toast(`${label} copied`);
    } catch {
      toast("Could not copy — select and copy manually", true);
    }
  }

  function renderResolveResults(url, files) {
    resolveShareUrl = url;
    resolveFiles = (files || []).filter((f) => !f.is_folder);
    const actions = $("#resolve-actions");
    if (!resolveFiles.length) {
      actions.classList.add("hidden");
      $("#resolve-results").innerHTML = `<p class="muted">No downloadable files found in this share.</p>`;
      return;
    }
    actions.classList.remove("hidden");
    $("#resolve-count").textContent = `${resolveFiles.length} file(s)`;
    $("#resolve-results").innerHTML = `<table>
      <thead><tr>
        <th><input type="checkbox" id="resolve-check-all" checked /></th>
        <th>Name</th><th>Type</th><th>Size</th><th></th>
      </tr></thead>
      <tbody>
        ${resolveFiles
          .map(
            (f, i) => `<tr>
          <td><input type="checkbox" class="resolve-check" data-idx="${i}" checked /></td>
          <td class="title">${esc(f.name)}</td>
          <td>${esc((f.mime_type || "file").split("/").pop())}</td>
          <td>${f.size != null ? Math.round(f.size / 1048576) + " MiB" : "—"}</td>
          <td class="actions">
            <button class="btn sm primary" data-dfid="${esc(f.id)}" data-dname="${esc(f.name)}">Queue</button>
          </td>
        </tr>`
          )
          .join("")}
      </tbody></table>`;

    const master = $("#resolve-check-all");
    if (master) {
      master.addEventListener("change", () => {
        $$(".resolve-check").forEach((c) => {
          c.checked = master.checked;
        });
      });
    }
    $("#resolve-results").querySelectorAll("[data-dfid]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await api("/api/v1/downloads", {
            method: "POST",
            body: {
              title: btn.dataset.dname,
              url: resolveShareUrl,
              drive_file_id: btn.dataset.dfid,
            },
          });
          toast("Queued Drive file download");
        } catch (e) {
          toast(e.message, true);
        }
      });
    });
  }

  async function queueSelected(all) {
    const indices = all
      ? resolveFiles.map((_, i) => i)
      : $$(".resolve-check")
          .filter((c) => c.checked)
          .map((c) => Number(c.dataset.idx));
    const files = indices
      .map((i) => resolveFiles[i])
      .filter(Boolean)
      .map((f) => ({ title: f.name, drive_file_id: f.id }));
    if (!files.length) return toast("No files selected", true);
    try {
      const res = await api("/api/v1/downloads/bulk", {
        method: "POST",
        body: { url: resolveShareUrl, files },
      });
      const parts = [`Queued ${res.count} download(s)`];
      if (res.skipped_count) parts.push(`${res.skipped_count} skipped (already in queue)`);
      toast(parts.join(" · "));
      if (res.errors && res.errors.length) {
        toast(`${res.errors.length} failed to queue`, true);
      }
      if (res.count > 0) setView("downloads");
    } catch (e) {
      toast(e.message, true);
    }
  }

  // Navigation
  $$(".nav-btn").forEach((btn) => btn.addEventListener("click", () => setView(btn.dataset.view)));

  // Login
  $("#login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    $("#login-error").classList.add("hidden");
    try {
      await api("/api/v1/auth/login", {
        method: "POST",
        body: {
          username: $("#login-user").value.trim(),
          password: $("#login-pass").value,
        },
      });
      showLogin(false);
      toast("Signed in");
      loadDashboard();
      checkAuth();
    } catch (e) {
      $("#login-error").textContent = e.message;
      $("#login-error").classList.remove("hidden");
    }
  });

  $("#logout-btn").addEventListener("click", async () => {
    try {
      await api("/api/v1/auth/logout", { method: "POST" });
    } catch {
      /* ignore */
    }
    showLogin(true);
    $("#conn-status").textContent = "Logged out";
    $("#conn-status").className = "pill muted";
  });

  // Settings: API key (Sonarr-style, Settings page only)
  const saved = localStorage.getItem(KEY);
  if (saved && $("#api-key")) $("#api-key").value = saved;

  const copyApiBtn = $("#copy-api-key");
  if (copyApiBtn) {
    copyApiBtn.addEventListener("click", () => {
      const key = ($("#api-key") && $("#api-key").value.trim()) || "";
      if (!key) return toast("No API key loaded", true);
      copyText(key, "API key");
    });
  }

  const copyTorzBtn = $("#copy-torznab");
  if (copyTorzBtn) {
    copyTorzBtn.addEventListener("click", () => {
      const url = ($("#torznab-url") && $("#torznab-url").textContent.trim()) || "";
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
        if ($("#api-key")) $("#api-key").value = data.api_key || "";
        if (data.api_key) localStorage.setItem(KEY, data.api_key);
        const host = location.origin;
        if ($("#torznab-url")) {
          $("#torznab-url").textContent = `${host}${data.torznab_path || "/api"}?apikey=${data.api_key || "YOUR_KEY"}`;
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
  $("#refresh-downloads").addEventListener("click", () => loadDownloads());
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
    const feedback = $("#gdrive-auth-feedback");
    const pasteBox = $("#gdrive-paste-fallback");
    try {
      const r = await api("/api/v1/gdrive/auth-url");
      window.open(r.auth_url, "_blank", "noopener");
      if (r.mode === "callback") {
        toast("Google sign-in opened — finish there; this page will update when you return");
        if (pasteBox) pasteBox.classList.add("hidden");
        if (feedback) {
          feedback.innerHTML =
            "Complete sign-in in the other tab. Google redirects back to Directarr and the backend saves the token automatically.";
        }
      } else {
        toast("Google sign-in opened — after Allow, paste the localhost URL below");
        if (pasteBox) pasteBox.classList.remove("hidden");
        if (feedback) {
          feedback.innerHTML =
            "No <code>DIRECTARR_PUBLIC_URL</code> configured, so paste mode is active. Copy the full <code>http://localhost/?code=...</code> URL after Allow.";
        }
      }
    } catch (e) {
      toast(e.message, true);
      if (feedback) feedback.textContent = e.message;
    }
  });

  $("#gdrive-auth-code").addEventListener("click", async () => {
    const code = $("#gdrive-code").value.trim();
    const feedback = $("#gdrive-auth-feedback");
    if (!code) return toast("Paste the full localhost URL (or the code) first", true);
    try {
      await api("/api/v1/gdrive/auth-code", { method: "POST", body: { code } });
      toast("Google Drive connected");
      $("#gdrive-code").value = "";
      if (feedback) feedback.innerHTML = '<span class="pill ok">Connected</span> Authorization succeeded.';
      loadGdrive();
    } catch (e) {
      toast(e.message, true);
      if (feedback) feedback.innerHTML = '<span class="pill err">Failed</span> ' + esc(e.message);
    }
  });

  const disconnectBtn = $("#gdrive-disconnect");
  if (disconnectBtn) {
    disconnectBtn.addEventListener("click", async () => {
      if (!confirm("Disconnect Google Drive? You will need to authorize again to resolve shares.")) return;
      try {
        await api("/api/v1/gdrive/disconnect", { method: "POST" });
        toast("Google Drive disconnected");
        const feedback = $("#gdrive-auth-feedback");
        if (feedback) feedback.textContent = "Disconnected. Connect again when ready.";
        loadGdrive();
      } catch (e) {
        toast(e.message, true);
      }
    });
  }

  $("#resolve-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const url = $("#resolve-url").value.trim();
    $("#resolve-results").innerHTML = `<p class="muted">Resolving (including nested folders)…</p>`;
    $("#resolve-actions").classList.add("hidden");
    try {
      const data = await api("/api/v1/gdrive/resolve", { method: "POST", body: { url } });
      renderResolveResults(url, data.files || []);
      if (data.file_count != null) {
        toast(`Found ${data.file_count} downloadable file(s)`);
      }
    } catch (e) {
      $("#resolve-results").innerHTML = "";
      toast(e.message, true);
    }
  });

  $("#resolve-select-all").addEventListener("click", () => {
    $$(".resolve-check").forEach((c) => {
      c.checked = true;
    });
    const m = $("#resolve-check-all");
    if (m) m.checked = true;
  });
  $("#resolve-select-none").addEventListener("click", () => {
    $$(".resolve-check").forEach((c) => {
      c.checked = false;
    });
    const m = $("#resolve-check-all");
    if (m) m.checked = false;
  });
  $("#resolve-download-selected").addEventListener("click", () => queueSelected(false));
  $("#resolve-download-all").addEventListener("click", () => queueSelected(true));

  // Boot
  updateSettings();
  (async () => {
    const ok = await checkAuth();
    if (ok) {
      loadDashboard();
      manageDownloadPolling();
    }
  })();

  // Handle OAuth callback return (?gdrive=connected|error)
  (function handleGdriveReturn() {
    const params = new URLSearchParams(location.search);
    const gdrive = params.get("gdrive");
    if (!gdrive) return;
    const feedback = () => $("#gdrive-auth-feedback");
    if (gdrive === "connected") {
      toast("Google Drive connected");
      setView("gdrive");
      loadGdrive();
      if (feedback()) feedback().innerHTML = '<span class="pill ok">Connected</span> Authorization completed by the server.';
    } else if (gdrive === "error") {
      const msg = params.get("message") || "Authorization failed";
      toast(msg, true);
      setView("gdrive");
      if (feedback()) feedback().innerHTML = '<span class="pill err">Failed</span> ' + esc(msg);
    }
    // Clean query string without reloading
    try {
      const url = new URL(location.href);
      url.searchParams.delete("gdrive");
      url.searchParams.delete("message");
      history.replaceState({}, "", url.pathname + url.search + url.hash);
    } catch (_) {}
  })();

})();
