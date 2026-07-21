from __future__ import annotations

import os
import tempfile
from pathlib import Path


APP_NAME = "ydbackup"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base).expanduser() / APP_NAME if base else Path.home() / ".config" / APP_NAME


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    return Path(base).expanduser() / APP_NAME if base else Path.home() / ".local" / "state" / APP_NAME


def default_yandex_token_file() -> Path:
    return config_dir() / "yandex-token.txt"


def default_google_token_file() -> Path:
    return config_dir() / "google-token.json"


def read_yandex_token(path: Path | None = None) -> str:
    env_token = os.environ.get("YANDEX_TOKEN", "").strip()
    if env_token:
        return env_token
    token_path = path or default_yandex_token_file()
    try:
        token = token_path.expanduser().read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(
            "No Yandex token found. Run 'ydbackup yandex-auth' or set YANDEX_TOKEN."
        ) from exc
    if not token:
        raise RuntimeError(f"Yandex token file is empty: {token_path}")
    return token


def save_secret(path: Path, value: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            os.chmod(temporary_name, 0o600)
            temporary.write(value)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
