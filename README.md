# Directarr

Anime-focused direct-download indexer scaffold for self-hosting.

## Architecture
- FastAPI: REST API plus Torznab-shaped `caps`, `search`, and `tvsearch` endpoints.
- Adapters: pluggable site parsers; a generic CSS-selector adapter is included.
- Normalizer: extracts season, episode, resolution, and codec tags.
- SQLite: stores indexed releases and download jobs.
- Internal downloader: queues approved HTTPS URLs and streams them into the downloads volume.
- Docker Compose: persistent config, staging, and completed-download directories.

## Important *Arr integration limitation
The Torznab indexer API can be added to Sonarr as a Torznab custom indexer. However, Sonarr/Radarr do not expose a general-purpose public API for third-party download-client plugins. A Torznab result containing a Google Drive/direct URL does not, by itself, make Sonarr download that file. This MVP provides the indexer API and a separate internal download queue. A true automatic *Arr grab handoff needs a supported, maintained download-client bridge; do not pretend a qBittorrent/SABnzbd endpoint can accept arbitrary files (those protocols expect torrent/NZB jobs).

## Run
1. Copy `.env.example` to `.env` and set a long random `DIRECTARR_API_KEY`.
2. `docker compose up -d --build`
3. Open the web UI at `http://YOUR-SERVER-IP:8787/` (enter your API key in the top bar).
4. API docs: `http://YOUR-SERVER-IP:8787/docs`.

Local (no Docker): from the project root, with deps installed:
`DIRECTARR_API_KEY=your-key python -m uvicorn app.main:app --host 0.0.0.0 --port 8787`

Torznab URL: `http://YOUR-SERVER-IP:8787/api?apikey=YOUR_KEY`

## Configure sources
Edit `config/sources.json`. Built-in adapters:

- **kayoanime** — searches [kayoanime.com](https://kayoanime.com/), follows post pages, and extracts Google Drive share links (`a.shortc-button`).
- **example_html** — generic CSS-selector adapter for other authorized HTML sources.

Example `kayoanime` entry (enabled in the default config):
```json
[
  {
    "id": "kayoanime",
    "name": "KayoAnime",
    "adapter": "kayoanime",
    "enabled": true,
    "base_url": "https://kayoanime.com",
    "max_posts": 5,
    "follow_posts": true,
    "timeout": 25
  }
]
```

Only automate sources and files you are authorized to access. Do not bypass login, paywall, CAPTCHA, DRM, anti-bot controls, or Google Drive permissions. Drive links still require your own Google account access.

## API
- `GET /api?apikey=KEY&t=caps`
- `GET /api?apikey=KEY&t=search&q=...`
- `GET /api?apikey=KEY&t=tvsearch&q=...&season=...&ep=...`
- `GET /api/v1/sources` (X-Api-Key)
- `POST /api/v1/sources/reload` (X-Api-Key)
- `GET /api/v1/releases` (X-Api-Key)
- `GET /api/v1/search?q=...` (X-Api-Key) — JSON search for the web UI
- `PATCH /api/v1/sources/{id}` JSON `{"enabled":true}` (X-Api-Key)
- `POST /api/v1/downloads` JSON `{"title":"...","url":"https://...","drive_file_id":"optional"}` (X-Api-Key)
- `GET /api/v1/downloads` and `/api/v1/downloads/{id}` (X-Api-Key)
- `GET /api/v1/gdrive/status` — OAuth credential presence
- `GET /api/v1/gdrive/auth-url` — start OAuth (requires `config/credentials.json`)
- `POST /api/v1/gdrive/auth-code` JSON `{"code":"..."}` — finish OAuth, writes `config/token.json`
- `POST /api/v1/gdrive/resolve` JSON `{"url":"https://drive.google.com/..."}` — list folder/file metadata

## Google Drive authorized resolution
Directarr does **not** bypass Drive permissions. It uses the official Drive API with *your* Google account.

1. In [Google Cloud Console](https://console.cloud.google.com/): create a project, enable **Google Drive API**, configure OAuth consent, create an OAuth client of type **Desktop app**, download the JSON.
   - Authorized redirect URI must include `http://localhost` (Google’s modern loopback requirement; the old OOB flow is blocked).
2. Save it as `config/credentials.json` (or set `DIRECTARR_GDRIVE_CREDENTIALS`).
3. `GET /api/v1/gdrive/auth-url` (or use the **Google Drive** page in the web UI) → open the returned `auth_url` in a browser while signed into the Google account that can access the shared files.
4. After you click Allow, the browser will try to load `http://localhost/?code=...` and fail (connection refused). That is expected. Copy the **entire address-bar URL** and paste it (or just the `code` value) into the UI, or:
   ```
   POST /api/v1/gdrive/auth-code
   {"code": "http://localhost/?code=4/0A...&state=..."}
   ```
   The backend automatically extracts the code. This writes `config/token.json`.
5. `POST /api/v1/gdrive/resolve` with a KayoAnime Drive folder URL to list episode files.
6. `POST /api/v1/downloads` with the folder/file URL and `drive_file_id` set to the chosen file id.

Scope default: `drive.readonly`.

## Volumes
- `./config`: sources, SQLite database, optional `credentials.json` + `token.json`
- `./downloads`: completed downloads
- `./staging`: partial downloads

## Current boundaries
- KayoAnime returns Drive share URLs; resolve + authorized download require your OAuth token.
- Folder URLs cannot be downloaded as a single job — resolve first, then pass `drive_file_id`.
- Web UI covers dashboard, search, releases, downloads, source toggles, and Drive auth/resolve.
