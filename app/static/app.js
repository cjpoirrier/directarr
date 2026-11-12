(() => {
  const KEY = "directarr_api_key";
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  function apiKey() {
    return localStorage.getItem(KEY) || $("#api-key").value.trim();
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

  function renderReleasesTable(el, rows, { queue = false } = {}) {
    if (!rows.length) {
      el.innerHTML = `<p class="muted">No releases yet. Run a search.</p>`;
      return;
    }
    el.innerHTML = `<table>
      <thead><tr><th>Title</th><th>Source</th><th>Res</th><th>Codec</th><th></th></tr></thead>
      <tbody>
        ${rows.map((r) => `<tr>
          <td class="title">${esc(r.title)}</td>
          <td>${esc(r.source || "")}</td>
          <td>${esc(r.resolution || "—")}</td>
          <td>${esc(r.codec || "—")}</td>
          <td class="actions">
            ${queue ? `<button class="btn sm primary" data-queue-title="${esc(r.title)}" data-queue-url="${esc(r.url)}">Queue</button>` : ""}
            <a class="btn sm" href="${esc(r.url)}" target="_blank" rel="noopener">Open</a>
          </td>
        </tr>`).join("")}
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

  function updateSettings() {
    const key = apiKey() || "YOUR_KEY";
    const host = location.origin;
    $("#torznab-url").textContent = `${host}/api?apikey=${key}`;
  }

  // Navigation
  $$(".nav-btn").forEach((btn) => btn.addEventListener("click", () => setView(btn.dataset.view)));

  // API key
  const saved = localStorage.getItem(KEY);
  if (saved) $("#api-key").value = saved;
  $("#save-key").addEventListener("click", () => {
    localStorage.setItem(KEY, $("#api-key").value.trim());
    toast("API key saved in this browser");
    loadDashboard();
    updateSettings();
  });

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
      toast("Browser opened - after Allow, copy the full address bar and paste it below");
      const hint = $("#gdrive-hint");
      if (hint) {
        hint.innerHTML =
          'After you click <strong>Allow</strong>, the browser will show "localhost refused to connect". ' +
          "That is expected. Copy the <strong>entire address bar</strong> " +
          "(it starts with <code>http://localhost/?code=...</code>) and paste it into the box, then click Submit.";
      }
    } catch (e) {
      toast(e.message, true);
    }
  });

  $("#gdrive-auth-code").addEventListener("click", async () => {
    const code = $("#gdrive-code").value.trim();
    if (!code) return toast("Paste the full localhost URL (or the code) first", true);
    try {
      await api("/api/v1/gdrive/auth-code", { method: "POST", body: { code } });
      toast("Google Drive authorized");
      $("#gdrive-code").value = "";
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
})();
