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


def escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveClient:
    def __init__(self, service: Resource) -> None:
        self.service = service
        self._folder_cache: dict[tuple[str, str], str] = {}

    def _find_children(self, parent_id: str, name: str, mime_type: str | None = None) -> list[dict[str, Any]]:
        clauses = [
            f"'{escape_query_value(parent_id)}' in parents",
            f"name = '{escape_query_value(name)}'",
            "trashed = false",
        ]
        if mime_type:
            clauses.append(f"mimeType = '{escape_query_value(mime_type)}'")
        result = (
            self.service.files()
            .list(
                q=" and ".join(clauses),
                spaces="drive",
                pageSize=10,
                fields="files(id,name,mimeType,size,sha256Checksum,md5Checksum,appProperties,modifiedTime)",
            )
            .execute(num_retries=5)
        )
        return list(result.get("files", []))

    def find_folder(self, parent_id: str, name: str) -> str | None:
        cache_key = (parent_id, name)
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]
        matches = self._find_children(parent_id, name, FOLDER_MIME_TYPE)
        if len(matches) > 1:
            raise DuplicateDestinationError(
                f"Multiple Google Drive folders named {name!r} exist under the same parent"
            )
        if not matches:
            return None
        folder_id = matches[0]["id"]
        self._folder_cache[cache_key] = folder_id
        return folder_id

    def ensure_folder(self, parent_id: str, name: str, *, create: bool = True) -> str | None:
        existing = self.find_folder(parent_id, name)
        if existing or not create:
            return existing
        folder = (
            self.service.files()
            .create(
                body={"name": name, "mimeType": FOLDER_MIME_TYPE, "parents": [parent_id]},
                fields="id",
            )
            .execute(num_retries=5)
        )
        folder_id = folder["id"]
        self._folder_cache[(parent_id, name)] = folder_id
        return folder_id

    def find_file(self, parent_id: str, name: str) -> dict[str, Any] | None:
        matches = [
            item
            for item in self._find_children(parent_id, name)
            if item.get("mimeType") != FOLDER_MIME_TYPE
        ]
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
                fields="id,name,size,sha256Checksum,md5Checksum,appProperties",
            )
        else:
            request = self.service.files().create(
                body=body,
                media_body=media,
                fields="id,name,size,sha256Checksum,md5Checksum,appProperties",
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
        return response
