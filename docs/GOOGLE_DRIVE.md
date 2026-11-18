# Google Drive setup

Directarr uses the official Google Drive API with **your** Google account. It does not bypass permissions. Drive links still require that the account you authorize can access the files.

## 1. Create OAuth credentials

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project (or pick an existing one).
3. Enable the **Google Drive API**.
4. Configure the OAuth consent screen (External is fine for personal use; add yourself as a test user if the app is in testing).
5. Create an OAuth client ID of type **Desktop app**.
6. Download the JSON.

Authorized redirect URI must include `http://localhost` (Google’s loopback requirement).

## 2. Place the secrets file

Save the downloaded JSON as:

```
config/credentials.json
```

(or set `DIRECTARR_GDRIVE_CREDENTIALS` to another path).

Restart Directarr if it was already running so it sees the file.

## 3. Authorize in the UI

1. Open Directarr → **Google Drive**.
2. Click the button that starts the OAuth flow (or call `GET /api/v1/gdrive/auth-url`).
3. Open the returned URL in a browser signed into the Google account that can access the shared files.
4. After you click Allow, the browser will try to load `http://localhost/?code=...` and fail (connection refused). That is expected.
5. Copy the **entire address-bar URL** (or just the `code` value) and paste it into the Directarr UI.

The backend writes `config/token.json`. Scope default is `drive.readonly`.

## 4. Resolve and download

- Use **Google Drive** in the UI or `POST /api/v1/gdrive/resolve` with a share URL to list folder/file metadata.
- Queue a download with `POST /api/v1/downloads` (or the UI) supplying the file id when you want a single file from a folder.

## Notes

- Tokens stay in `config/token.json`. Back that file up if you want to avoid re-auth.
- Only automate sources and files you are authorized to access.
- KayoAnime (and similar) return Drive share links; resolution and download require this OAuth setup.
