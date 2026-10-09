from __future__ import annotations

import fnmatch
import heapq
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .engine import relative_source_path
from .models import YandexFile


@dataclass(frozen=True)
class TreeFile:
    source: YandexFile
    relative: PurePosixPath
    ordinal: int


@dataclass
class Folder:
    path: PurePosixPath
    files: list[TreeFile] = field(default_factory=list)
    children: dict[str, Folder] = field(default_factory=dict)
    last_ordinal: int = -1


def build_tree(entries: Iterable[TreeFile]) -> Folder:
    root = Folder(PurePosixPath("."))
    for entry in entries:
        node = root
        node.last_ordinal = max(node.last_ordinal, entry.ordinal)
        for part in entry.relative.parts[:-1]:
            if part not in node.children:
                node.children[part] = Folder(node.path / part)
            node = node.children[part]
            node.last_ordinal = max(node.last_ordinal, entry.ordinal)
        node.files.append(entry)
    return root


def enumerate_tree(
    files: Iterable[YandexFile],
    *,
    source_prefix: str = "disk:",
    excludes: Iterable[str] = (),
    on_progress: Callable[[str], None] = lambda _: None,
) -> Folder:
    entries: dict[str, TreeFile] = {}
    duplicates = 0
    patterns = tuple(excludes)
    for ordinal, source in enumerate(files):
        if (ordinal + 1) % 1000 == 0:
            on_progress(f"SCAN   {ordinal + 1} source files enumerated")
        relative = relative_source_path(source.path, source_prefix)
        if relative is None or any(fnmatch.fnmatch(str(relative), p) for p in patterns):
            continue
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError(f"Invalid Yandex file path: {source.path}")
        previous = entries.get(source.path)
        if previous is not None:
            duplicates += 1
            # Offset pages can overlap. Keep one job at its first position;
            # refresh its metadata if the source changed during enumeration.
            entries[source.path] = TreeFile(source, relative, previous.ordinal)
        else:
            entries[source.path] = TreeFile(source, relative, ordinal)
    on_progress(f"SCAN   complete: {len(entries)} matching files, "
                f"{sum(e.source.size for e in entries.values())} bytes; "
                f"{duplicates} repeated records deduplicated")
    return build_tree(entries.values())


def reverse_folders(root: Folder) -> Iterator[Folder]:
    """Highest original ordinal first among folders whose children have finished."""
    nodes = {}
    stack = [root]
    while stack:
        node = stack.pop()
        nodes[node.path] = node
        stack.extend(node.children.values())
    remaining = {path: len(node.children) for path, node in nodes.items()}
    ready = [(-node.last_ordinal, str(path), path) for path, node in nodes.items()
             if remaining[path] == 0]
    heapq.heapify(ready)
    while ready:
        _, _, path = heapq.heappop(ready)
        yield nodes[path]
        if path != root.path:
            parent = path.parent
            remaining[parent] -= 1
            if remaining[parent] == 0:
                heapq.heappush(ready, (-nodes[parent].last_ordinal, str(parent), parent))


def limit_tree(root: Folder, max_files: int | None) -> Folder:
    if max_files is None:
        return root
    if max_files < 0:
        raise ValueError("max_files must not be negative")
    selected = []
    for folder in reverse_folders(root):
        for entry in reversed(folder.files):
            if len(selected) == max_files:
                return build_tree(sorted(selected, key=lambda e: e.ordinal))
            selected.append(entry)
    return build_tree(sorted(selected, key=lambda e: e.ordinal))


def render_tree(root: Folder, *, label: str = "disk:", include_files: bool = True) -> Iterator[str]:
    yield label

    def lines(node: Folder, prefix: str) -> Iterator[str]:
        items: list[Folder | TreeFile] = sorted(
            node.children.values(), key=lambda f: f.last_ordinal, reverse=True
        )
        if include_files:
            items.extend(reversed(node.files))
        for index, item in enumerate(items):
            last = index == len(items) - 1
            branch = "└── " if last else "├── "
            if isinstance(item, Folder):
                yield f"{prefix}{branch}{item.path.name}/ ({len(item.files)} direct files)"
                yield from lines(item, prefix + ("    " if last else "│   "))
            else:
                yield f"{prefix}{branch}{item.relative.name} ({item.source.size} bytes)"

    if not include_files and root.files:
        yield f"    ({len(root.files)} files directly in this folder)"
    yield from lines(root, "")
