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

## Parallel folder CLI

The separate `ydbackup-parallel` command uses the same authorization and backup
metadata as `ydbackup`. Reinstall with `python -m pip install -e .` after updating
the repository to register the new command.

List Yandex files as a tree without accessing Google Drive:

```bash
ydbackup-parallel tree
```

Enumerate the entire source first, then upload with four folder workers:

```bash
ydbackup-parallel backup --workers 4
```

Each worker processes one folder's files sequentially. Leaf folders are scheduled
first, reversing the order returned by the Yandex listing used by the original
CLI. Siblings are prioritized by their last occurrence in that listing, and files
within a folder are reversed too. Files directly in parent folders are processed
after their child folders finish, so they are not left out. Parallel jobs can
finish in a different order than they start.

The source tree is reconstructed from the complete file listing; empty source
folders are omitted. Repeated paths across listing pages become one upload job,
retaining their first position and the latest listed metadata.
No Google folders are created during enumeration, inventory
loading, dry runs, or skipped files. When a downloaded file is ready to upload,
its destination folder and required ancestors are created immediately before the
upload. If that upload fails, the created folder may remain empty until a retry.

The live terminal view shows completed folder batches out of the total, file
totals, and each worker's current folder, file, and download/upload stage.
Folders containing direct files count as batches; ancestor folders with no
direct files do not count as transfer work. Detailed timestamped events are
appended to `~/.local/state/ydbackup/parallel-backup.log` with owner-only permissions.

```bash
ydbackup-parallel backup --workers 6 --log-file ~/ydbackup.log
ydbackup-parallel backup --no-tui --quiet-tree
ydbackup-parallel backup --workers 3 --dry-run --max-files 10
```

`--max-files` limits the reverse upload plan **after** complete enumeration.
`--source`, `--destination`, and `--exclude` work as in the original CLI.
Redirected output automatically uses plain output instead of the TUI.

Folder creation is synchronized among workers, and each worker has its own API
connections. Run only one uploading CLI against a destination at a time: separate
processes keep separate Drive inventories and can otherwise create duplicate
folders. Stop the original run with Ctrl+C before switching to the new command,
or use a different `--destination` when running both.

Rerun the same command after an interruption to skip completed uploads and resume
partial downloads from `~/.local/state/ydbackup/parallel-downloads/`. Ctrl+C stops
scheduling new work and waits for active transfer requests to return before
exiting. Uploads already in progress may finish during shutdown.

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
