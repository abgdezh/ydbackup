import io
import hashlib
import threading
from pathlib import PurePosixPath

import pytest
from rich.console import Console

from ydbackup.google_drive import GoogleDriveClient
from ydbackup.models import YandexFile
from ydbackup.parallel_cli import build_parser
from ydbackup import parallel_cli
from ydbackup.parallel_engine import run_parallel_backup
from ydbackup.parallel_progress import ProgressEvent, TransferDisplay
from ydbackup.source_tree import enumerate_tree, limit_tree, render_tree, reverse_folders


def source(path):
    return YandexFile("disk:/" + path, 3, modified="now")


def tree(*paths):
    return enumerate_tree(map(source, paths))


class DestinationDrive:
    is_unchanged = staticmethod(GoogleDriveClient.is_unchanged)

    def __init__(self, events, existing=None):
        self.events = events
        self.folders = {}
        self.existing = existing or {}
        self.creations = []

    def load_inventory(self):
        self.events.append(("inventory",))

    def ensure_folder(self, parent, name, *, create=True):
        key = (parent, name)
        if key in self.folders:
            return self.folders[key]
        if not create:
            return None
        self.events.append(("mkdir", name))
        self.creations.append(key)
        self.folders[key] = parent + "/" + name
        return self.folders[key]

    def find_file(self, parent, name):
        return self.existing.get((parent, name))


class Downloader:
    def __init__(self, events, barrier=None, fail=False):
        self.events, self.barrier, self.fail = events, barrier, fail

    def download(self, source, target):
        self.events.append(("download", source.path))
        if self.barrier:
            self.barrier.wait(timeout=5)
        if self.fail:
            raise RuntimeError("download unavailable")
        target.write_bytes(b"abc")


class Uploader:
    def __init__(self, events, fail=False):
        self.events, self.fail = events, fail

    def upload(self, target, source, parent, name, existing_file_id=None):
        assert target.read_bytes() == b"abc"
        self.events.append(("upload", source.path, parent, existing_file_id))
        if self.fail:
            raise RuntimeError("upload unavailable")
        return {"id": "uploaded"}


def test_reverse_order_and_parent_files_are_not_lost(tmp_path):
    inventory = tree("A/first.txt", "A/sub/child.txt", "B/one.txt", "B/two.txt", "root.txt")
    events = []
    summary = run_parallel_backup(inventory, DestinationDrive(events),
        lambda: (Downloader(events), Uploader(events)), workers=1, temp_dir=tmp_path,
        on_event=lambda _: None)
    uploads = [event[1] for event in events if event[0] == "upload"]
    assert uploads == ["disk:/B/two.txt", "disk:/B/one.txt", "disk:/A/sub/child.txt",
                       "disk:/A/first.txt", "disk:/root.txt"]
    assert summary.uploaded == 5 and summary.failed == 0
    assert summary.bytes_uploaded == 15
    assert list(tmp_path.iterdir()) == []


def test_lazy_creation_and_no_duplicate_shared_parents(tmp_path):
    events, clients = [], []
    coordinator = DestinationDrive(events)
    barrier = threading.Barrier(3)
    def factory():
        clients.append(threading.current_thread().name)
        return Downloader(events, barrier), Uploader(events)
    summary = run_parallel_backup(tree("P/a/x", "P/b/y", "P/c/z"), coordinator, factory,
                                  workers=3, temp_dir=tmp_path, on_event=lambda _: None)
    assert summary.uploaded == 3 and summary.failed == 0
    assert len(set(clients)) == 3  # Three real simultaneous downloads, separate worker clients.
    assert len(coordinator.creations) == len(set(coordinator.creations)) == 5
    first_creation = next(i for i, e in enumerate(events) if e[0] == "mkdir")
    assert sum(e[0] == "download" for e in events[:first_creation]) == 3


def test_failed_download_does_not_create_destination(tmp_path):
    events = []
    coordinator = DestinationDrive(events)
    summary = run_parallel_backup(tree("A/file.txt"), coordinator,
        lambda: (Downloader(events, fail=True), Uploader(events)),
        temp_dir=tmp_path, on_event=lambda _: None)
    assert summary.failed == 1 and summary.uploaded == 0
    assert coordinator.creations == []


def test_failed_upload_retains_download_for_resume(tmp_path):
    events = []
    summary = run_parallel_backup(tree("A/file.txt"), DestinationDrive(events),
        lambda: (Downloader(events), Uploader(events, fail=True)),
        temp_dir=tmp_path, on_event=lambda _: None)
    assert summary.failed == 1 and summary.uploaded == 0
    assert len(list(tmp_path.glob("*.part"))) == 1


def test_dry_run_does_not_create_or_download(tmp_path):
    events = []
    coordinator = DestinationDrive(events)
    def forbidden():
        pytest.fail("dry run created transfer clients")
    summary = run_parallel_backup(tree("A/a", "B/b"), coordinator, forbidden,
        dry_run=True, temp_dir=tmp_path, workers=2, on_event=lambda _: None)
    assert summary.uploaded == 2 and summary.failed == 0
    assert coordinator.creations == []
    assert events == [("inventory",)]
    assert list(tmp_path.iterdir()) == []


def test_existing_backup_is_skipped_without_transfer_clients(tmp_path):
    events = []
    root, folder = "root/Yandex Disk", "root/Yandex Disk/A"
    coordinator = DestinationDrive(events, {(folder, "a"): {
        "id": "existing", "size": "3", "appProperties": {
            "yd_size": "3", "yd_modified": "now",
            "yd_path_hash": hashlib.sha256(b"disk:/A/a").hexdigest()}}})
    coordinator.folders = {("root", "Yandex Disk"): root, (root, "A"): folder}
    def forbidden():
        pytest.fail("unchanged file created transfer clients")
    summary = run_parallel_backup(tree("A/a"), coordinator, forbidden,
                                  temp_dir=tmp_path, on_event=lambda _: None)
    assert summary.skipped == 1 and summary.uploaded == 0 and summary.failed == 0
    assert coordinator.creations == []


def test_worker_limit_and_single_folder_are_sequential(tmp_path):
    events, clients = [], []
    def factory():
        clients.append(threading.current_thread().name)
        return Downloader(events), Uploader(events)
    run_parallel_backup(tree("A/a", "A/b", "A/c"), DestinationDrive(events), factory,
                        temp_dir=tmp_path, workers=8, on_event=lambda _: None)
    assert len(clients) == 1
    assert [e[1] for e in events if e[0] == "upload"] == ["disk:/A/c", "disk:/A/b", "disk:/A/a"]


def test_max_files_applies_after_complete_scan_in_reverse_order():
    visited = []
    def listing():
        for path in ("A/a", "B/b", "C/c"):
            visited.append(path)
            yield source(path)
    inventory = enumerate_tree(listing())
    limited = limit_tree(inventory, 1)
    assert visited == ["A/a", "B/b", "C/c"]
    assert [entry.relative for folder in reverse_folders(limited) for entry in folder.files] == [PurePosixPath("C/c")]
    assert not any(folder.files for folder in reverse_folders(limit_tree(inventory, 0)))


def test_filter_tree_and_duplicate_deduplication():
    inventory = enumerate_tree(map(source, ("Photos/a", "Photostuff/b", "Photos/c.tmp")),
                               source_prefix="disk:/Photos", excludes=["*.tmp"])
    assert [f.relative for f in inventory.files] == [PurePosixPath("a")]
    assert "a (3 bytes)" in "\n".join(render_tree(inventory))
    repeated = tree("A/a", "A/a")
    assert sum(len(folder.files) for folder in reverse_folders(repeated)) == 1


def test_overlapping_pages_keep_latest_metadata_and_first_position():
    original, later = source("A/a"), source("B/b")
    updated = YandexFile("disk:/A/a", 5, modified="later")
    inventory = enumerate_tree([original, later, updated])
    entries = [entry for folder in reverse_folders(inventory) for entry in folder.files]
    assert [str(entry.relative) for entry in entries] == ["B/b", "A/a"]
    assert entries[1].source == updated and entries[1].ordinal == 0


def test_limit_matches_leaf_priority_when_parent_file_appears_last():
    inventory = tree("A/sub/early", "B/later", "A/last")
    plan = [str(entry.relative) for folder in reverse_folders(inventory) for entry in reversed(folder.files)]
    assert plan == ["B/later", "A/sub/early", "A/last"]
    limited = limit_tree(inventory, 1)
    assert [str(entry.relative) for folder in reverse_folders(limited) for entry in folder.files] == ["B/later"]


def test_parent_files_wait_for_all_parallel_children(tmp_path):
    events = []
    barrier = threading.Barrier(3)
    class ParentAwareDownloader(Downloader):
        def download(self, source, target):
            if source.path == "disk:/P/main":
                assert sum(e[0] == "upload" for e in events) == 3
                target.write_bytes(b"abc")
            else:
                super().download(source, target)
    summary = run_parallel_backup(tree("P/a/x", "P/b/y", "P/c/z", "P/main"),
        DestinationDrive(events), lambda: (ParentAwareDownloader(events, barrier), Uploader(events)),
        workers=3, temp_dir=tmp_path, on_event=lambda _: None)
    assert summary.uploaded == 4 and summary.failed == 0


def test_progress_reports_each_folder_and_failure(tmp_path):
    events, progress = [], []
    run_parallel_backup(tree("A/a", "B/b"), DestinationDrive(events),
        lambda: (Downloader(events, fail=True), Uploader(events)), temp_dir=tmp_path,
        workers=2, on_event=lambda _: None, on_progress=progress.append)
    assert sum(e.kind == "folder_start" for e in progress) == 2
    assert sum(e.kind == "folder_done" for e in progress) == 2
    assert sum(e.kind == "file_failed" for e in progress) == 2
    assert all(e.file_total == 1 for e in progress)


def test_tui_shows_folder_progress_and_current_work():
    output = io.StringIO()
    console = Console(file=output, width=120)
    display = TransferDisplay(3, 7, console=console, log_path="backup.log")
    display.update(ProgressEvent("download", "ydbackup_0", "Photos/2026", "Photos/2026/a.jpg", 2, 4))
    console.print(display.render())
    assert "Photos/2026" in output.getvalue() and "a.jpg" in output.getvalue()
    assert "Downloading" in output.getvalue() and "0/3" in output.getvalue()
    display.update(ProgressEvent("file_uploaded", "ydbackup_0", "Photos/2026"))
    display.update(ProgressEvent("folder_done", "ydbackup_0", "Photos/2026"))
    assert display.progress.tasks[0].completed == 1
    assert display.counts["file_uploaded"] == 1


def test_tree_cli_never_authenticates_google(monkeypatch, capsys):
    class Session:
        def close(self):
            pass
    class Scanner:
        api_session = download_session = Session()
        def __init__(self, token):
            pass
        def iter_files(self):
            return iter([source("A/a"), source("B/b")])
    monkeypatch.setattr(parallel_cli, "YandexDiskClient", Scanner)
    monkeypatch.setattr(parallel_cli, "read_yandex_token", lambda _: "test")
    def forbidden(*args):
        pytest.fail("tree command accessed Google Drive")
    monkeypatch.setattr(parallel_cli, "authenticate_google", forbidden)
    assert parallel_cli.main(["tree"]) == 0
    assert "B/" in capsys.readouterr().out


def test_live_tui_keeps_long_paths_to_one_worker_row():
    output = io.StringIO()
    console = Console(file=output, width=80, force_terminal=True, color_system=None)
    display = TransferDisplay(4, 8, console=console, log_path="backup.log")
    with display:
        for i in range(4):
            display.update(ProgressEvent("upload", f"ydbackup_{i}", "a/" * 80 + "leaf",
                                         "path/" + "x" * 150, 1, 2))
    final_output = output.getvalue().split("Yandex Disk → Google Drive")[-1]
    assert len(final_output.splitlines()) < 15
    assert "leaf" in final_output


def test_incomplete_source_scan_cannot_create_destination(monkeypatch, tmp_path):
    class Session:
        def close(self):
            pass
    class Scanner:
        api_session = download_session = Session()
        def __init__(self, token):
            pass
        def iter_files(self):
            yield source("A/a")
            raise RuntimeError("listing unavailable")
    monkeypatch.setattr(parallel_cli, "YandexDiskClient", Scanner)
    monkeypatch.setattr(parallel_cli, "read_yandex_token", lambda _: "test")
    def forbidden(*args):
        pytest.fail("Google authorization started before enumeration completed")
    monkeypatch.setattr(parallel_cli, "authenticate_google", forbidden)
    log = tmp_path / "backup.log"
    assert parallel_cli.main(["backup", "--log-file", str(log)]) == 2
    assert "listing unavailable" in log.read_text()


@pytest.mark.parametrize("options", [["--workers", "0"], ["--workers", "-1"], ["--max-files", "-1"]])
def test_invalid_limits_are_rejected(options):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["backup", *options])
