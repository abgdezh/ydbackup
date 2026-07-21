from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from .google_drive import GoogleDriveClient
from .models import BackupSummary, YandexFile
from .yandex import YandexDiskClient


EventHandler = Callable[[str], None]


def relative_source_path(path: str, source_prefix: str) -> PurePosixPath | None:
    prefix = source_prefix.rstrip("/")
    if path == prefix:
        return PurePosixPath(PurePosixPath(path).name)
    boundary = f"{prefix}/"
    if not path.startswith(boundary):
        return None
    relative = path[len(boundary) :]
    return PurePosixPath(relative)


def _is_excluded(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def run_backup(
    yandex: YandexDiskClient,
    drive: GoogleDriveClient,
    *,
    source_prefix: str = "disk:",
    destination_name: str = "Yandex Disk",
    temp_dir: Path,
    dry_run: bool = False,
    excludes: Iterable[str] = (),
    max_files: int | None = None,
    on_event: EventHandler = print,
) -> BackupSummary:
    summary = BackupSummary()
    root_id = drive.ensure_folder("root", destination_name, create=not dry_run)

    for source in yandex.iter_files():
        relative = relative_source_path(source.path, source_prefix)
        if relative is None or _is_excluded(relative.as_posix(), excludes):
            continue
        if max_files is not None and summary.examined >= max_files:
            break
        summary.examined += 1
        try:
            parent_id = root_id
            for part in relative.parts[:-1]:
                if parent_id is None:
                    break
                parent_id = drive.ensure_folder(parent_id, part, create=not dry_run)

            existing = drive.find_file(parent_id, relative.name) if parent_id else None
            if drive.is_unchanged(existing, source):
                summary.skipped += 1
                on_event(f"SKIP   {relative.as_posix()}")
                continue
            if dry_run:
                summary.uploaded += 1
                summary.bytes_uploaded += source.size
                on_event(f"PLAN   {relative.as_posix()} ({source.size} bytes)")
                continue
            if parent_id is None:
                raise RuntimeError(f"Could not create destination folder for {relative}")

            temp_dir.mkdir(parents=True, exist_ok=True)
            version_key = "\0".join(
                [source.path, str(source.size), source.sha256 or "", source.modified or ""]
            )
            temp_name = hashlib.sha256(version_key.encode("utf-8")).hexdigest() + ".part"
            local_path = temp_dir / temp_name
            on_event(f"GET    {relative.as_posix()} ({source.size} bytes)")
            yandex.download(source, local_path)
            on_event(f"PUT    {relative.as_posix()}")
            drive.upload(
                local_path,
                source,
                parent_id,
                relative.name,
                existing_file_id=existing["id"] if existing else None,
            )
            local_path.unlink(missing_ok=True)
            summary.uploaded += 1
            summary.bytes_uploaded += source.size
            on_event(f"OK     {relative.as_posix()}")
        except Exception as exc:  # Keep backing up independent files after one failure.
            summary.failed += 1
            on_event(f"ERROR  {relative.as_posix()}: {exc}")
    return summary
