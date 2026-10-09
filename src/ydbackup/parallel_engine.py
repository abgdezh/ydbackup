from __future__ import annotations

import hashlib
import heapq
import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path, PurePosixPath

from .google_drive import GoogleDriveClient
from .models import BackupSummary
from .parallel_progress import ProgressEvent
from .source_tree import Folder, reverse_folders
from .yandex import YandexDiskClient


ClientFactory = Callable[[], tuple[YandexDiskClient, GoogleDriveClient]]


class Destination:
    """Serialize metadata requests and folder creation on one Drive connection."""

    def __init__(self, drive: GoogleDriveClient, name: str) -> None:
        self.drive = drive
        self.name = name
        self.lock = threading.RLock()

    def folder(self, path: PurePosixPath, *, create: bool = False) -> str | None:
        with self.lock:
            parent = "root"
            for part in (self.name, *path.parts):
                parent = self.drive.ensure_folder(parent, part, create=create)
                if parent is None:
                    return None
            return parent

    def lookup(self, path: PurePosixPath):
        with self.lock:
            parent = self.folder(path.parent)
            return self.drive.find_file(parent, path.name) if parent else None


def run_parallel_backup(
    tree: Folder,
    drive: GoogleDriveClient,
    client_factory: ClientFactory,
    *,
    temp_dir: Path,
    workers: int = 4,
    destination_name: str = "Yandex Disk",
    dry_run: bool = False,
    on_event: Callable[[str], None] = print,
    on_progress: Callable[[ProgressEvent], None] = lambda _: None,
) -> BackupSummary:
    if workers < 1:
        raise ValueError("workers must be positive")
    summary = BackupSummary()
    nodes = {node.path: node for node in reverse_folders(tree)}
    if not any(node.files for node in nodes.values()):
        return summary
    drive.load_inventory()
    destination = Destination(drive, destination_name)
    # Detect ambiguous roots before starting any download; never create here.
    destination.folder(PurePosixPath("."))
    output_lock = threading.Lock()
    clients_lock = threading.Lock()
    local = threading.local()
    clients: list[tuple[YandexDiskClient, GoogleDriveClient]] = []
    stopped = threading.Event()

    def emit(message: str) -> None:
        with output_lock:
            on_event(message)

    def worker_clients():
        if not hasattr(local, "clients"):
            local.clients = client_factory()
            with clients_lock:
                clients.append(local.clients)
        return local.clients

    def transfer(folder: Folder) -> BackupSummary:
        result = BackupSummary()
        def notify(kind: str, entry=None, index: int = 0) -> None:
            with output_lock:
                on_progress(ProgressEvent(kind, threading.current_thread().name, str(folder.path),
                    str(entry.relative) if entry else "", index, len(folder.files),
                    entry.source.size if entry else 0))
        notify("folder_start")
        emit(f"FOLDER {folder.path} ({len(folder.files)} files)")
        for index, entry in enumerate(reversed(folder.files), 1):
            if stopped.is_set():
                break
            source, relative = entry.source, entry.relative
            result.examined += 1
            try:
                existing = destination.lookup(relative)
                if drive.is_unchanged(existing, source):
                    result.skipped += 1
                    emit(f"SKIP   {relative}")
                    notify("file_skipped", entry, index)
                    continue
                if dry_run:
                    result.uploaded += 1
                    result.bytes_uploaded += source.size
                    emit(f"PLAN   {relative} ({source.size} bytes)")
                    notify("file_planned", entry, index)
                    continue
                yandex, uploader = worker_clients()
                temp_dir.mkdir(parents=True, exist_ok=True)
                version = "\0".join([source.path, str(source.size), source.sha256 or "", source.modified or ""])
                target = temp_dir / (hashlib.sha256(version.encode()).hexdigest() + ".part")
                emit(f"GET    {relative} ({source.size} bytes)")
                notify("download", entry, index)
                yandex.download(source, target)
                if stopped.is_set():
                    break
                # The file is ready: create its folder chain just before PUT.
                notify("prepare", entry, index)
                parent = destination.folder(relative.parent, create=True)
                if parent is None:
                    raise RuntimeError(f"Could not create destination folder for {relative}")
                emit(f"PUT    {relative}")
                notify("upload", entry, index)
                uploader.upload(target, source, parent, relative.name,
                                existing_file_id=existing["id"] if existing else None)
                target.unlink(missing_ok=True)
                result.uploaded += 1
                result.bytes_uploaded += source.size
                emit(f"OK     {relative}")
                notify("file_uploaded", entry, index)
            except Exception as exc:
                result.failed += 1
                emit(f"ERROR  {relative}: {exc}")
                notify("file_failed", entry, index)
        emit(f"DONE   {folder.path}: uploaded {result.uploaded}; skipped {result.skipped}; failed {result.failed}")
        notify("folder_done")
        return result

    remaining = {path: len(node.children) for path, node in nodes.items()}
    ready = []

    def enqueue(path: PurePosixPath) -> None:
        node = nodes[path]
        heapq.heappush(ready, (-node.last_ordinal, str(path), path))

    for path, count in remaining.items():
        if count == 0:
            enqueue(path)

    def completed(path: PurePosixPath) -> None:
        if path == tree.path:
            return
        parent = path.parent
        remaining[parent] -= 1
        if remaining[parent] == 0:
            enqueue(parent)

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ydbackup")
    pending = {}
    try:
        while ready or pending:
            while ready and len(pending) < workers:
                _, _, path = heapq.heappop(ready)
                if not nodes[path].files:
                    completed(path)
                    continue
                pending[executor.submit(transfer, nodes[path])] = path
            if not pending:
                continue
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                path = pending.pop(future)
                result = future.result()
                for field in ("examined", "uploaded", "skipped", "failed", "bytes_uploaded"):
                    setattr(summary, field, getattr(summary, field) + getattr(result, field))
                completed(path)
    except BaseException:
        stopped.set()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        for yandex, uploader in clients:
            for resource in (getattr(yandex, "api_session", None),
                             getattr(yandex, "download_session", None),
                             getattr(uploader, "service", None)):
                if resource is not None and hasattr(resource, "close"):
                    resource.close()
    return summary
