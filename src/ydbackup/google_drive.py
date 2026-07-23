from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.http import MediaFileUpload

from .config import default_google_token_file, save_secret
from .models import YandexFile


DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
ALL_FILES_QUERY = "trashed = false"
ROOT_CHILDREN_QUERY = "'root' in parents and trashed = false"
INVENTORY_FIELDS = (
    "nextPageToken,incompleteSearch,"
    "files(id,name,parents,mimeType,size,sha256Checksum,md5Checksum,"
    "appProperties,modifiedTime)"
)


class DuplicateDestinationError(RuntimeError):
    pass


def authenticate_google(
    client_secret_file: Path,
    token_file: Path | None = None,
) -> Credentials:
    token_path = (token_file or default_google_token_file()).expanduser()
    credentials: Credentials | None = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(
            str(token_path), [DRIVE_FILE_SCOPE]
        )
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError:
            credentials = None
    if not credentials or not credentials.valid:
        if not client_secret_file.expanduser().exists():
            raise RuntimeError(f"Google OAuth client file not found: {client_secret_file}")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret_file.expanduser()), [DRIVE_FILE_SCOPE]
        )
        credentials = flow.run_local_server(port=0)
    save_secret(token_path, credentials.to_json())
    return credentials


def build_drive(credentials: Credentials) -> Resource:
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


class GoogleDriveClient:
    def __init__(self, service: Resource) -> None:
        self.service = service
        self._inventory_loaded = False
        self._folder_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._file_index: dict[tuple[str, str], list[dict[str, Any]]] = {}

    @staticmethod
    def _add_index_item(
        index: dict[tuple[str, str], list[dict[str, Any]]],
        key: tuple[str, str],
        item: dict[str, Any],
    ) -> None:
        matches = index.setdefault(key, [])
        if not any(existing.get("id") == item.get("id") for existing in matches):
            matches.append(item)

    def _index_item(self, item: dict[str, Any]) -> None:
        name = item.get("name")
        if not name:
            return
        index = (
            self._folder_index
            if item.get("mimeType") == FOLDER_MIME_TYPE
            else self._file_index
        )
        for parent_id in item.get("parents") or []:
            self._add_index_item(index, (parent_id, name), item)

    def _index_root_item(self, item: dict[str, Any]) -> None:
        name = item.get("name")
        if not name:
            return
        index = (
            self._folder_index
            if item.get("mimeType") == FOLDER_MIME_TYPE
            else self._file_index
        )
        self._add_index_item(index, ("root", name), item)

    def _load_inventory_pages(self, query: str, *, root_children: bool = False) -> None:
        page_token: str | None = None
        while True:
            arguments: dict[str, Any] = {
                "q": query,
                "spaces": "drive",
                "pageSize": 1000,
                "fields": INVENTORY_FIELDS,
            }
            if page_token:
                arguments["pageToken"] = page_token
            result = (
                self.service.files()
                .list(**arguments)
                .execute(num_retries=5)
            )
            if result.get("incompleteSearch"):
                raise RuntimeError(
                    "Google Drive returned an incomplete bulk file listing"
                )
            for item in result.get("files", []):
                if root_children:
                    self._index_root_item(item)
                else:
                    self._index_item(item)
            page_token = result.get("nextPageToken")
            if not page_token:
                break

    def load_inventory(self) -> None:
        """Load all app-visible Drive metadata in pages of up to 1,000 items."""
        if self._inventory_loaded:
            return
        self._folder_index.clear()
        self._file_index.clear()
        try:
            # With drive.file scope, files.get("root") can return 404. Listing
            # app-visible root children resolves the "root" alias safely.
            self._load_inventory_pages(
                ROOT_CHILDREN_QUERY,
                root_children=True,
            )
            self._load_inventory_pages(ALL_FILES_QUERY)
        except Exception:
            self._folder_index.clear()
            self._file_index.clear()
            raise
        self._inventory_loaded = True

    def find_folder(self, parent_id: str, name: str) -> str | None:
        self.load_inventory()
        matches = self._folder_index.get((parent_id, name), [])
        if len(matches) > 1:
            raise DuplicateDestinationError(
                f"Multiple Google Drive folders named {name!r} exist under the same parent"
            )
        if not matches:
            return None
        return matches[0]["id"]

    def ensure_folder(self, parent_id: str, name: str, *, create: bool = True) -> str | None:
        existing = self.find_folder(parent_id, name)
        if existing or not create:
            return existing
        folder = (
            self.service.files()
            .create(
                body={"name": name, "mimeType": FOLDER_MIME_TYPE, "parents": [parent_id]},
                fields="id,name,parents,mimeType",
            )
            .execute(num_retries=5)
        )
        folder.setdefault("name", name)
        folder.setdefault("parents", [parent_id])
        folder.setdefault("mimeType", FOLDER_MIME_TYPE)
        self._index_item(folder)
        if parent_id == "root":
            self._index_root_item(folder)
        return folder["id"]

    def find_file(self, parent_id: str, name: str) -> dict[str, Any] | None:
        self.load_inventory()
        matches = self._file_index.get((parent_id, name), [])
        if len(matches) > 1:
            raise DuplicateDestinationError(
                f"Multiple Google Drive files named {name!r} exist under the same parent"
            )
        return matches[0] if matches else None

    @staticmethod
    def is_unchanged(remote: dict[str, Any] | None, source: YandexFile) -> bool:
        if not remote:
            return False
        properties = remote.get("appProperties") or {}
        size_matches = properties.get("yd_size") == str(source.size) and (
            remote.get("size") is None or str(remote.get("size")) == str(source.size)
        )
        if source.sha256:
            remote_sha256 = remote.get("sha256Checksum")
            if remote_sha256:
                return remote_sha256.lower() == source.sha256.lower() and size_matches
            return properties.get("yd_sha256") == source.sha256 and size_matches
        return (
            properties.get("yd_path_hash")
            == hashlib.sha256(source.path.encode("utf-8")).hexdigest()
            and size_matches
            and properties.get("yd_modified") == (source.modified or "")
        )

    def upload(
        self,
        local_path: Path,
        source: YandexFile,
        parent_id: str,
        name: str,
        existing_file_id: str | None = None,
        *,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> dict[str, Any]:
        properties = {
            # Drive limits each custom property key + value to 124 UTF-8 bytes.
            "yd_path_hash": hashlib.sha256(source.path.encode("utf-8")).hexdigest(),
            "yd_size": str(source.size),
            "yd_sha256": source.sha256 or "",
            "yd_modified": source.modified or "",
        }
        body: dict[str, Any] = {"name": name, "appProperties": properties}
        if not existing_file_id:
            body["parents"] = [parent_id]
        media = MediaFileUpload(
            str(local_path),
            mimetype=source.mime_type or "application/octet-stream",
            chunksize=chunk_size,
            resumable=True,
        )
        if existing_file_id:
            request = self.service.files().update(
                fileId=existing_file_id,
                body=body,
                media_body=media,
                fields=(
                    "id,name,parents,mimeType,size,sha256Checksum,"
                    "md5Checksum,appProperties"
                ),
            )
        else:
            request = self.service.files().create(
                body=body,
                media_body=media,
                fields=(
                    "id,name,parents,mimeType,size,sha256Checksum,"
                    "md5Checksum,appProperties"
                ),
            )
        response = None
        while response is None:
            _, response = request.next_chunk(num_retries=5)
        remote_size = response.get("size")
        if remote_size is not None and int(remote_size) != source.size:
            raise RuntimeError(
                f"Google Drive size mismatch for {source.path}: expected {source.size}, got {remote_size}"
            )
        remote_sha256 = response.get("sha256Checksum")
        if source.sha256 and remote_sha256 and remote_sha256.lower() != source.sha256.lower():
            raise RuntimeError(f"Google Drive SHA-256 mismatch for {source.path}")
        response.setdefault("name", name)
        response.setdefault("parents", [parent_id])
        response.setdefault("mimeType", source.mime_type or "application/octet-stream")
        key = (parent_id, name)
        current = self._file_index.setdefault(key, [])
        if existing_file_id:
            replaced = False
            for index, item in enumerate(current):
                if item.get("id") == existing_file_id:
                    current[index] = response
                    replaced = True
                    break
            if not replaced:
                current.append(response)
        else:
            current.append(response)
        return response
