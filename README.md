# Yandex Disk → Google Drive backup

`ydbackup` copies files from Yandex Disk into a `Yandex Disk/` folder in Google Drive.

- Incremental: completed files are skipped on later runs.
- Efficient: Google Drive metadata and checksums are loaded in bulk.
- Restartable: partial Yandex downloads resume automatically.
- Verified: checksums are compared when available.
- Non-deleting: removing a file from Yandex does not remove its Google Drive backup.

## Quick start

### 1. Install

Python 3.10 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

### 2. Authorize Yandex Disk

```bash
ydbackup yandex-auth
```

The command opens Yandex authorization in your browser. Approve access, copy the `access_token` value from the redirected URL, and paste it into the hidden terminal prompt.

The token is saved with owner-only permissions at:

```text
~/.config/ydbackup/yandex-token.txt
```

If the browser should not open automatically, use `ydbackup yandex-auth --no-browser`.

### 3. Authorize Google Drive

You need a Google Cloud **Desktop app** OAuth client:

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create or select a project.
2. Enable the [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com).
3. Open [Google Auth Platform](https://console.cloud.google.com/auth/overview) and complete the requested Branding and Audience settings.
4. If the app is External and in testing, add your Google account as a test user.
5. Open [Google Auth Platform → Clients](https://console.cloud.google.com/auth/clients), create a client, and choose **Desktop app**.
6. Download the JSON file and save it in this folder as `client_secret.json`.
7. Authorize the tool:

```bash
chmod 600 client_secret.json
ydbackup google-auth
```

Google opens a consent page and saves the resulting authorization at:

```text
~/.config/ydbackup/google-token.json
```

The tool requests the limited `drive.file` scope, allowing it to manage the backup files and folders it creates. Access tokens are refreshed automatically.

### 4. Preview and run

Preview one file without uploading anything:

```bash
ydbackup backup --dry-run --max-files 1
```

Back up everything:

```bash
ydbackup backup
```

A source file such as `disk:/Photos/2026/a.jpg` is stored as `Yandex Disk/Photos/2026/a.jpg`.

## Useful options

Back up one Yandex folder:

```bash
ydbackup backup --source 'disk:/Photos'
```

Choose another Google Drive folder:

```bash
ydbackup backup --destination 'My Yandex Backup'
```

Exclude files or folders with shell-style patterns:

```bash
ydbackup backup --exclude '*.tmp' --exclude 'Cache/*'
```

Limit a test run:

```bash
ydbackup backup --max-files 10
```

## Restarting a backup

Run the same command again after an interruption. Successfully uploaded files are detected through private Google Drive metadata and checksums, so they are skipped.

Incomplete Yandex downloads are retained in:

```text
~/.local/state/ydbackup/downloads/
```

An interrupted Google upload restarts only the current file; previously completed files are not uploaded again.

## Troubleshooting

- **No Yandex token found:** run `ydbackup yandex-auth` again.
- **Google client file not found:** make sure the downloaded file is named `client_secret.json` and is in the project folder.
- **Google `redirect_uri_mismatch`:** recreate the OAuth client as a **Desktop app**, not a Web application.
- **Google blocks a test user:** add that account under Google Auth Platform → Audience → Test users.
- **Google token expired or revoked:** move the saved token aside and authorize again:

  ```bash
  mv ~/.config/ydbackup/google-token.json ~/.config/ydbackup/google-token.json.old
  ydbackup google-auth
  ```

## Keep credentials private

Never publish or commit:

- `client_secret.json`
- `~/.config/ydbackup/yandex-token.txt`
- `~/.config/ydbackup/google-token.json`
- `.env` files or service-account keys

These files are covered by the repository's `.gitignore`. If a credential is exposed, revoke it at the provider and create a replacement; deleting the local file alone does not revoke access.

Official references: [Yandex OAuth](https://yandex.com/dev/id/doc/en/concepts/ya-oauth-intro), [Yandex Disk API](https://yandex.com/dev/disk/rest/), [Google desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app), and [Google Drive uploads](https://developers.google.com/workspace/drive/api/guides/manage-uploads).
