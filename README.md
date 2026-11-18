# Directarr

Anime-focused direct-download indexer for self-hosting. Fresh installs start with **no indexers** — add the ones you want from the built-in catalog.

## Quick start

1. Copy `.env.example` to `.env`.
2. Set a long random `DIRECTARR_API_KEY` and a `DIRECTARR_AUTH_PASSWORD`.
3. `docker compose up -d`
4. Open `http://YOUR-HOST:8787/` (default port **8787**).
5. Sign in, then **Indexers → + Add Indexer** and pick from the catalog.

See [docs/SETUP.md](docs/SETUP.md) for the full guide and [docs/GOOGLE_DRIVE.md](docs/GOOGLE_DRIVE.md) for Google Drive OAuth.

## Fresh install defaults

- No indexers are enabled. `config/sources.json` starts empty.
- The shareable catalog lives in `config/indexers-catalog.json`. Update that file to add or change indexer definitions for all users; the UI reads it when present.
- FlareSolverr is off unless you set `DIRECTARR_FLARESOLVERR_URL`.
- API key and web password must be set in `.env` — there is no insecure default.

## Architecture

- FastAPI: REST API plus Torznab-shaped `caps`, `search`, and `tvsearch`.
- Adapters: pluggable site parsers. Generic CSS-selector adapter included.
- Normalizer: season, episode, resolution, codec.
- SQLite: releases and download jobs.
- Internal downloader: queues HTTPS URLs into the downloads volume.
- Optional browser extension / userscript for stream capture.

## *Arr integration note

You can add Directarr to Sonarr as a custom Torznab indexer. Sonarr does not have a general download-client plugin API, so a Torznab result that contains a direct/Drive URL will not automatically download. Directarr provides its own download queue for that.

## Volumes

- `./config` — sources, SQLite, optional `credentials.json` + `token.json`, `indexers-catalog.json`
- `./downloads` — completed files
- `./staging` — partial downloads

## Docs

- [Setup](docs/SETUP.md) — install, first login, adding indexers, FlareSolverr, extension
- [Google Drive](docs/GOOGLE_DRIVE.md) — OAuth client, auth flow, resolving share links
