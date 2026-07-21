from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import YandexFile


API_ROOT = "https://cloud-api.yandex.net/v1/disk"


class YandexAPIError(RuntimeError):
    pass


class IntegrityError(RuntimeError):
    pass


def _retrying_session() -> requests.Session:
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class YandexDiskClient:
    def __init__(
        self,
        token: str,
        *,
        api_session: requests.Session | None = None,
        download_session: requests.Session | None = None,
        timeout: tuple[float, float] = (15.0, 300.0),
    ) -> None:
        self.api_session = api_session or _retrying_session()
        self.download_session = download_session or _retrying_session()
        self.api_session.headers.update({"Authorization": f"OAuth {token}"})
        self.timeout = timeout

    def _get_json(self, endpoint: str, *, params: dict[str, Any]) -> dict[str, Any]:
        response = self.api_session.get(
            f"{API_ROOT}/{endpoint}", params=params, timeout=self.timeout
        )
        if not response.ok:
            raise YandexAPIError(
                f"Yandex API request failed ({response.status_code}): {response.text[:500]}"
            )
        return response.json()

    def iter_files(self, *, page_size: int = 1000) -> Iterator[YandexFile]:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        offset = 0
        fields = "items.path,items.size,items.sha256,items.md5,items.modified,items.mime_type"
        while True:
            payload = self._get_json(
                "resources/files",
                params={"fields": fields, "limit": page_size, "offset": offset},
            )
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise YandexAPIError("Yandex returned an invalid file listing")
            for item in items:
                yield YandexFile(
                    path=item["path"],
                    size=int(item.get("size", 0)),
                    sha256=item.get("sha256"),
                    md5=item.get("md5"),
                    modified=item.get("modified"),
                    mime_type=item.get("mime_type"),
                )
            offset += len(items)
            if len(items) < page_size:
                break

    def get_download_url(self, path: str) -> str:
        payload = self._get_json("resources/download", params={"path": path})
        href = payload.get("href")
        if not href:
            raise YandexAPIError(f"Yandex did not return a download URL for {path}")
        return str(href)

    def download(self, source: YandexFile, target: Path, *, chunk_size: int = 8 * 1024 * 1024) -> None:
        """Download to target, resuming a partial file when the server supports ranges."""
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.stat().st_size if target.exists() else 0
        if existing > source.size:
            target.unlink()
            existing = 0

        headers = {"Range": f"bytes={existing}-"} if existing else {}
        response = self.download_session.get(
            self.get_download_url(source.path),
            headers=headers,
            stream=True,
            timeout=self.timeout,
        )
        if response.status_code == 416 and existing == source.size:
            pass
        else:
            if not response.ok:
                # Do not include the signed download URL in an exception message.
                raise YandexAPIError(
                    f"Yandex download failed for {source.path} ({response.status_code})"
                )
            resumed = existing > 0 and response.status_code == 206
            mode = "ab" if resumed else "wb"
            with target.open(mode) as output:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        output.write(chunk)

        actual_size = target.stat().st_size
        if actual_size != source.size:
            raise IntegrityError(
                f"Size mismatch for {source.path}: expected {source.size}, downloaded {actual_size}"
            )
        if source.sha256:
            digest = hashlib.sha256()
            with target.open("rb") as downloaded:
                for block in iter(lambda: downloaded.read(chunk_size), b""):
                    digest.update(block)
            if digest.hexdigest().lower() != source.sha256.lower():
                target.unlink(missing_ok=True)
                raise IntegrityError(f"SHA-256 mismatch for {source.path}")
