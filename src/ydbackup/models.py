from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class YandexFile:
    path: str
    size: int
    sha256: str | None = None
    md5: str | None = None
    modified: str | None = None
    mime_type: str | None = None


@dataclass(slots=True)
class BackupSummary:
    examined: int = 0
    uploaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_uploaded: int = 0

