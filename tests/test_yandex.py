from __future__ import annotations

import hashlib
from typing import Any

from ydbackup.models import YandexFile
from ydbackup.yandex import YandexDiskClient


class FakeResponse:
    ok = True
    status_code = 200
    text = ""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeSession:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = iter(pages)
        self.headers: dict[str, str] = {}
        self.offsets: list[int] = []

    def get(self, url: str, *, params: dict[str, Any], timeout: Any) -> FakeResponse:
        self.offsets.append(params["offset"])
        return FakeResponse(next(self.pages))


def test_iter_files_paginates() -> None:
    session = FakeSession(
        [
            {"items": [{"path": "disk:/a", "size": 1}, {"path": "disk:/b", "size": 2}]},
            {"items": [{"path": "disk:/c", "size": 3}]},
        ]
    )
    client = YandexDiskClient("secret", api_session=session)  # type: ignore[arg-type]
    files = list(client.iter_files(page_size=2))
    assert [item.path for item in files] == ["disk:/a", "disk:/b", "disk:/c"]
    assert session.offsets == [0, 2]


class DownloadResponse:
    ok = True
    status_code = 206

    def __init__(self, content: bytes) -> None:
        self.content = content

    def iter_content(self, *, chunk_size: int):
        yield self.content


class DownloadSession:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.headers_seen: dict[str, str] = {}

    def get(self, url: str, *, headers: dict[str, str], stream: bool, timeout: Any):
        self.headers_seen = headers
        return DownloadResponse(self.content)


class DownloadClient(YandexDiskClient):
    def get_download_url(self, path: str) -> str:
        return "https://download.example/signed"


def test_download_resumes_and_verifies_sha256(tmp_path) -> None:
    destination = tmp_path / "file.part"
    destination.write_bytes(b"abc")
    session = DownloadSession(b"def")
    source = YandexFile(
        path="disk:/file",
        size=6,
        sha256=hashlib.sha256(b"abcdef").hexdigest(),
    )
    client = DownloadClient("secret", download_session=session)  # type: ignore[arg-type]

    client.download(source, destination)

    assert destination.read_bytes() == b"abcdef"
    assert session.headers_seen == {"Range": "bytes=3-"}
