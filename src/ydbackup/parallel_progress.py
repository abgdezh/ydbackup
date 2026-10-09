from __future__ import annotations

import threading
from dataclasses import dataclass

from rich.console import Console, Group
from rich.live import Live
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text


@dataclass(frozen=True)
class ProgressEvent:
    kind: str
    worker: str
    folder: str
    path: str = ""
    file_index: int = 0
    file_total: int = 0
    size: int = 0


class TransferDisplay:
    def __init__(self, folders: int, files: int, *, console: Console, log_path: str) -> None:
        self.console = console
        self.log_path = log_path
        self.lock = threading.RLock()
        self.workers: dict[str, ProgressEvent] = {}
        self.counts = {"file_uploaded": 0, "file_skipped": 0, "file_failed": 0, "file_planned": 0}
        self.files = files
        self.progress = Progress(TextColumn("[bold]Folders"), BarColumn(), MofNCompleteColumn(),
                                 TimeElapsedColumn(), console=console, auto_refresh=False)
        self.task = self.progress.add_task("Folders", total=folders)
        self.live = Live(get_renderable=self.render, console=console, refresh_per_second=4)

    def update(self, event: ProgressEvent) -> None:
        with self.lock:
            self.workers[event.worker] = event
            if event.kind in self.counts:
                self.counts[event.kind] += 1
            if event.kind == "folder_done":
                self.progress.advance(self.task)

    def render(self) -> Group:
        with self.lock:
            done = sum(self.counts.values())
            totals = Text(f"Files {done}/{self.files}  •  Uploaded {self.counts['file_uploaded']}  •  "
                          f"Skipped {self.counts['file_skipped']}  •  Failed {self.counts['file_failed']}")
            if self.counts["file_planned"]:
                totals.append(f"  •  Planned {self.counts['file_planned']}")
            table = Table(expand=True)
            table.add_column("Worker", width=12)
            table.add_column("Folder", ratio=2, overflow="ellipsis", no_wrap=True)
            table.add_column("Current file", ratio=3, overflow="ellipsis", no_wrap=True)
            table.add_column("Stage", width=13)
            table.add_column("File", justify="right", width=9)
            labels = {"folder_start": "Starting", "download": "Downloading", "prepare": "Preparing",
                      "upload": "Uploading",
                      "file_uploaded": "Verified", "file_skipped": "Skipped", "file_failed": "Failed",
                      "file_planned": "Planned", "folder_done": "Idle"}
            for worker, event in sorted(self.workers.items()):
                idle = event.kind == "folder_done"
                folder_width = max(8, int((self.console.width - 50) * 0.4))
                folder = event.folder
                if len(folder) > folder_width:
                    folder = "…" + folder[-(folder_width - 1):]
                table.add_row(Text(worker), Text("—" if idle else folder),
                              Text("—" if idle else event.path.rsplit("/", 1)[-1]),
                              Text(labels.get(event.kind, event.kind)),
                              Text("—" if idle else f"{event.file_index}/{event.file_total}"))
            if not self.workers:
                table.add_row("—", "Waiting for folder workers", "—", "Starting", "—")
            return Group(Text("Yandex Disk → Google Drive", style="bold"), self.progress, totals,
                         table, Text(f"Log: {self.log_path}", style="dim"))

    def __enter__(self):
        self.live.start(refresh=True)
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.live.stop()
