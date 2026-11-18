# Setup

See [SETTINGS.md](SETTINGS.md) for every environment variable.

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Required | Notes |
|----------|----------|-------|
| `DIRECTARR_API_KEY` | yes | Long random string (`openssl rand -hex 32`). Used by Torznab and API clients. |
| `DIRECTARR_AUTH_PASSWORD` | yes | Web UI password. Change after first login if you used a temporary value. |
| `DIRECTARR_AUTH_USERNAME` | no | Default `admin`. |
| `DIRECTARR_FLARESOLVERR_URL` | no | e.g. `http://flaresolverr:8191/v1`. Leave empty to skip. |

## 2. Start

```bash
docker compose up -d
```

Default listen port is **8787**. To use another host port, change the left side of the mapping in `docker-compose.yml`:

```yaml
ports:
  - "9090:8787"   # host 9090 → container 8787
```

Then open `http://YOUR-HOST:9090/`.

## 3. First login

Sign in with the username and password from `.env`.  
If you leave auth mode as `forms` (default), the UI requires login. API key still works for Torznab either way.

## 4. Indexers

Fresh installs have **no indexers**.

1. Go to **Indexers**.
2. Click **+ Add Indexer**.
3. Pick from the catalog (KayoAnime, AnimeKaizen, Generic HTML, …).
4. Adjust settings and save.

The catalog is loaded from `config/indexers-catalog.json` when that file exists. You can edit or replace it to share an updated list with other users. The UI lets each user choose which entries to actually enable.

User-selected indexers are stored in `config/sources.json` (starts as `[]`).

## 5. FlareSolverr (optional)

If a site is behind Cloudflare, set:

```env
DIRECTARR_FLARESOLVERR_URL=http://YOUR-FLARESOLVERR:8191/v1
```

The Directarr container must be able to reach that address. Leave the variable empty to fetch URLs directly. You can also toggle it in **Settings → General** after startup.

## 6. Browser extension

The extension quietly captures stream URLs and shows a badge on the icon.

1. Load the `extension/` folder as an unpacked extension in Chrome/Edge.
2. Open the popup → **Settings**.
3. Set **Directarr URL** to your instance, e.g. `http://localhost:8787` or `http://YOUR-HOST:8787`.
4. Play a video on a supported page; the badge appears when streams are found.
5. Open the popup and click **Send** on the quality you want.

There is no hardcoded server address. Each user sets their own URL and port.

## 7. Userscript (optional)

`directarr-capture.user.js` is an editable Tampermonkey script. Default URL is `http://localhost:8787`. Right-click the script → **Set Directarr URL** to change it. It does not auto-send; use it together with the extension badge or adjust the script if you prefer auto-queue.

## 8. Torznab

```
http://YOUR-HOST:8787/api?apikey=YOUR_API_KEY
```

Add that as a custom Torznab indexer in Sonarr. See the README note about download-client limitations.
