from pathlib import PurePosixPath

from ydbackup.engine import relative_source_path, run_backup
from ydbackup.models import YandexFile


def test_relative_source_path_for_root() -> None:
    assert relative_source_path("disk:/Photos/a.jpg", "disk:") == PurePosixPath("Photos/a.jpg")


def test_relative_source_path_for_subfolder() -> None:
    assert relative_source_path("disk:/Photos/2026/a.jpg", "disk:/Photos") == PurePosixPath("2026/a.jpg")
    assert relative_source_path("disk:/Other/a.jpg", "disk:/Photos") is None


def test_prefix_requires_path_boundary() -> None:
    assert relative_source_path("disk:/Photostuff/a.jpg", "disk:/Photos") is None


class FakeYandex:
    def __init__(self) -> None:
        self.files = [YandexFile("disk:/Photos/a.jpg", 3, modified="now")]

    def iter_files(self):
        return iter(self.files)

    def download(self, source, target) -> None:
        target.write_bytes(b"abc")


class FakeDrive:
    def __init__(self) -> None:
        self.uploads = []
        self.inventory_loads = 0

    def load_inventory(self):
        self.inventory_loads += 1

    def ensure_folder(self, parent_id, name, *, create=True):
        return f"{parent_id}/{name}" if create else None

    def find_file(self, parent_id, name):
        return None

    def is_unchanged(self, remote, source):
        return False

    def upload(self, local_path, source, parent_id, name, existing_file_id=None):
        self.uploads.append((local_path.read_bytes(), source.path, parent_id, name))


def test_run_backup_downloads_uploads_and_cleans_temp_file(tmp_path) -> None:
    drive = FakeDrive()
    summary = run_backup(FakeYandex(), drive, temp_dir=tmp_path, on_event=lambda _: None)

    assert summary.uploaded == 1
    assert summary.failed == 0
    assert drive.inventory_loads == 1
    assert drive.uploads == [
        (b"abc", "disk:/Photos/a.jpg", "root/Yandex Disk/Photos", "a.jpg")
    ]
    assert list(tmp_path.iterdir()) == []
