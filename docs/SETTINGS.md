# Environment variables

Only two values are required to start. Everything else has a default in the app or is saved in the UI (`config/settings.json`).

The `DIRECTARR_` prefix is how the app names its own settings. You do not need to put optional ones in `docker-compose.yml` — set them in `.env` only if you want to override the default.

## Required

| Variable | Purpose |
|----------|---------|
| `DIRECTARR_API_KEY` | Long random string for Torznab and API clients. Generate with `openssl rand -hex 32`. |
| `DIRECTARR_AUTH_PASSWORD` | Web UI password. |

## Optional — authentication

| Variable | Default | Purpose |
|----------|---------|---------|
| `DIRECTARR_AUTH_MODE` | `forms` | `forms` (login required) or `none` (open UI). API key still works for Torznab either way. |
| `DIRECTARR_AUTH_USERNAME` | `admin` | Web UI username. |

After first boot you can also change username, mode, and password in **Settings → General**. Those values are stored in `config/settings.json`.

## Optional — FlareSolverr

| Variable | Default | Purpose |
|----------|---------|---------|
| `DIRECTARR_FLARESOLVERR_URL` | empty | Initial FlareSolverr `/v1` endpoint, e.g. `http://flaresolverr:8191/v1`. |

You normally set this in the UI (**Settings → General → FlareSolverr**). The URL and on/off toggle are saved in `config/settings.json` inside the container’s config volume. You only need the env var if you want a value present before the first UI save. Leave it unset to fetch pages directly.

## Optional — downloads

| Variable | Default | Purpose |
|----------|---------|---------|
| `DIRECTARR_MAX_DOWNLOAD_MB` | `20000` | Max size for a single download. |
| `DIRECTARR_DOWNLOAD_CONNECTIONS` | `8` | Parallel connections for HTTPS direct downloads (not used for Drive). |
| `DIRECTARR_DOWNLOAD_MULTI_MIN_MB` | `8` | Only split into multi-connection parts when the file is at least this large. |
| `DIRECTARR_ALLOWED_HOSTS` | empty | Comma-separated hostnames allowed for outbound downloads. Empty = no extra allow-list. |

## Optional — paths (Docker)

These are set in the compose file to match the volume mounts. Change them only if you also change the mounts.

| Variable | Compose value | Purpose |
|----------|---------------|---------|
| `DIRECTARR_CONFIG_DIR` | `/config` | Sources, SQLite, settings, OAuth files. |
| `DIRECTARR_DOWNLOAD_DIR` | `/downloads` | Completed files. |
| `DIRECTARR_STAGING_DIR` | `/staging` | Partial downloads. |

## Optional — Google Drive

| Variable | Default | Purpose |
|----------|---------|---------|
| `DIRECTARR_GDRIVE_CREDENTIALS` | `/config/credentials.json` | Path to the OAuth client secrets JSON. |
| `DIRECTARR_GDRIVE_TOKEN` | `/config/token.json` | Path to the stored OAuth token. |
| `DIRECTARR_GDRIVE_SCOPES` | `https://www.googleapis.com/auth/drive.readonly` | Drive API scopes. |

See [GOOGLE_DRIVE.md](GOOGLE_DRIVE.md) for the full OAuth setup.

## Port

The compose file maps host `8787` to container `8787`. To use a different host port, change only the left side:

```yaml
ports:
  - "9090:8787"
```

Then open `http://YOUR-HOST:9090/`. The container still listens on 8787 unless you also change the app’s listen port (requires a restart).
