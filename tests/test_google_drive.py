import hashlib

from ydbackup.google_drive import GoogleDriveClient, escape_query_value
from ydbackup.models import YandexFile


def test_escape_query_value() -> None:
    assert escape_query_value("a'b\\c") == "a\\'b\\\\c"


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
