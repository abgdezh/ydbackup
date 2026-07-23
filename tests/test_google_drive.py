import hashlib

import pytest

from ydbackup.google_drive import (
    ALL_FILES_QUERY,
    FOLDER_MIME_TYPE,
    ROOT_CHILDREN_QUERY,
    DuplicateDestinationError,
    GoogleDriveClient,
)
from ydbackup.models import YandexFile


class FakeExecuteRequest:
    def __init__(self, response):
        self.response = response

    def execute(self, num_retries=0):
        return self.response


class FakeUploadRequest:
    def __init__(self, response):
        self.response = response

    def next_chunk(self, num_retries=0):
        return None, self.response


class FakeFilesResource:
    def __init__(self, pages):
        self.pages = pages
        self.list_calls = []
        self.create_calls = []

    def list(self, **arguments):
        self.list_calls.append(arguments)
        key = (arguments["q"], arguments.get("pageToken"))
        return FakeExecuteRequest(self.pages[key])

    def create(self, *, body, fields, media_body=None):
        self.create_calls.append(body)
        if media_body is not None:
            return FakeUploadRequest(
                {
                    "id": "uploaded-file",
                    "name": body["name"],
                    "parents": body["parents"],
                    "mimeType": "text/plain",
                    "size": body["appProperties"]["yd_size"],
                    "sha256Checksum": body["appProperties"]["yd_sha256"],
                    "appProperties": body["appProperties"],
                }
            )
        return FakeExecuteRequest(
            {
                "id": "created-folder",
                "name": body["name"],
                "parents": body["parents"],
                "mimeType": FOLDER_MIME_TYPE,
            }
        )


class FakeService:
    def __init__(self, pages):
        self.files_resource = FakeFilesResource(pages)

    def files(self):
        return self.files_resource


def test_bulk_inventory_paginates_and_indexes_files() -> None:
    service = FakeService(
        {
            (ROOT_CHILDREN_QUERY, None): {
                "files": [
                    {
                        "id": "backup-root",
                        "name": "Yandex Disk",
                        "parents": ["root-id"],
                        "mimeType": FOLDER_MIME_TYPE,
                    }
                ]
            },
            (ALL_FILES_QUERY, None): {
                "files": [
                    {
                        "id": "backup-root",
                        "name": "Yandex Disk",
                        "parents": ["root-id"],
                        "mimeType": FOLDER_MIME_TYPE,
                    },
                    {
                        "id": "photos",
                        "name": "Photos",
                        "parents": ["backup-root"],
                        "mimeType": FOLDER_MIME_TYPE,
                    },
                ],
                "nextPageToken": "page-2",
            },
            (ALL_FILES_QUERY, "page-2"): {
                "files": [
                    {
                        "id": "photo",
                        "name": "a.jpg",
                        "parents": ["photos"],
                        "mimeType": "image/jpeg",
                        "size": "3",
                        "sha256Checksum": "abc",
                    }
                ]
            },
        }
    )
    client = GoogleDriveClient(service)  # type: ignore[arg-type]

    client.load_inventory()

    assert client.find_folder("root", "Yandex Disk") == "backup-root"
    assert client.find_folder("backup-root", "Photos") == "photos"
    assert client.find_file("photos", "a.jpg")["sha256Checksum"] == "abc"
    assert len(service.files_resource.list_calls) == 3
    assert service.files_resource.list_calls[0]["pageSize"] == 1000
    assert "pageToken" not in service.files_resource.list_calls[0]
    assert service.files_resource.list_calls[0]["q"] == ROOT_CHILDREN_QUERY
    assert service.files_resource.list_calls[1]["q"] == ALL_FILES_QUERY
    assert service.files_resource.list_calls[2]["pageToken"] == "page-2"
    assert "sha256Checksum" in service.files_resource.list_calls[0]["fields"]


def test_inventory_duplicate_detection_is_preserved() -> None:
    service = FakeService(
        {
            (ROOT_CHILDREN_QUERY, None): {"files": []},
            (ALL_FILES_QUERY, None): {
                "files": [
                    {
                        "id": "one",
                        "name": "duplicate.txt",
                        "parents": ["folder"],
                        "mimeType": "text/plain",
                    },
                    {
                        "id": "two",
                        "name": "duplicate.txt",
                        "parents": ["folder"],
                        "mimeType": "text/plain",
                    },
                ]
            }
        }
    )
    client = GoogleDriveClient(service)  # type: ignore[arg-type]

    with pytest.raises(DuplicateDestinationError):
        client.find_file("folder", "duplicate.txt")


def test_incomplete_inventory_is_rejected() -> None:
    service = FakeService(
        {
            (ROOT_CHILDREN_QUERY, None): {"files": []},
            (ALL_FILES_QUERY, None): {
                "files": [],
                "incompleteSearch": True,
            },
        }
    )
    client = GoogleDriveClient(service)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="incomplete bulk file listing"):
        client.load_inventory()


def test_new_folder_is_added_to_loaded_inventory() -> None:
    service = FakeService(
        {
            (ROOT_CHILDREN_QUERY, None): {"files": []},
            (ALL_FILES_QUERY, None): {"files": []},
        }
    )
    client = GoogleDriveClient(service)  # type: ignore[arg-type]
    client.load_inventory()

    assert client.ensure_folder("root", "New", create=True) == "created-folder"
    assert client.find_folder("root", "New") == "created-folder"
    assert len(service.files_resource.list_calls) == 2


def test_uploaded_file_is_added_to_loaded_inventory(tmp_path) -> None:
    service = FakeService(
        {
            (ROOT_CHILDREN_QUERY, None): {"files": []},
            (ALL_FILES_QUERY, None): {"files": []},
        }
    )
    client = GoogleDriveClient(service)  # type: ignore[arg-type]
    client.load_inventory()
    source = YandexFile(path="disk:/a.txt", size=3, sha256="abc", mime_type="text/plain")
    local_file = tmp_path / "a.txt"
    local_file.write_bytes(b"abc")

    client.upload(local_file, source, "parent", "a.txt")

    assert client.find_file("parent", "a.txt")["id"] == "uploaded-file"
    assert len(service.files_resource.list_calls) == 2


def test_checksum_marks_remote_unchanged() -> None:
    source = YandexFile(path="disk:/a.txt", size=3, sha256="abc")
    remote = {
        "size": "3",
        "sha256Checksum": "abc",
        "appProperties": {"yd_sha256": "abc", "yd_size": "3"},
    }
    assert GoogleDriveClient.is_unchanged(remote, source)


def test_drive_checksum_disagreement_forces_upload() -> None:
    source = YandexFile(path="disk:/a.txt", size=3, sha256="abc")
    remote = {
        "size": "3",
        "sha256Checksum": "different",
        "appProperties": {"yd_sha256": "abc", "yd_size": "3"},
    }
    assert not GoogleDriveClient.is_unchanged(remote, source)


def test_fallback_metadata_marks_remote_unchanged() -> None:
    source = YandexFile(path="disk:/a.txt", size=3, modified="2026-07-21T10:00:00Z")
    remote = {
        "appProperties": {
            "yd_path_hash": hashlib.sha256(b"disk:/a.txt").hexdigest(),
            "yd_size": "3",
            "yd_modified": "2026-07-21T10:00:00Z",
        }
    }
    assert GoogleDriveClient.is_unchanged(remote, source)
