from __future__ import annotations

import argparse
import getpass
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

from .config import (
    default_google_token_file,
    default_yandex_token_file,
    read_yandex_token,
    save_secret,
    state_dir,
)
from .engine import run_backup
from .google_drive import GoogleDriveClient, authenticate_google, build_drive
from .yandex import YandexDiskClient


DEFAULT_YANDEX_CLIENT_ID = "56ab24676d874e5491d9663bc0350e39"


def yandex_authorization_url(client_id: str = DEFAULT_YANDEX_CLIENT_ID) -> str:
    return "https://oauth.yandex.ru/authorize?" + urlencode(
        {"response_type": "token", "client_id": client_id}
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ydbackup", description="Back up Yandex Disk files to Google Drive without deleting destination files."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    yandex_auth = subparsers.add_parser("yandex-auth", help="Authorize Yandex Disk and save its token")
    yandex_auth.add_argument("--client-id", default=DEFAULT_YANDEX_CLIENT_ID)
    yandex_auth.add_argument("--token-file", type=Path, default=default_yandex_token_file())
    yandex_auth.add_argument("--no-browser", action="store_true")

    google_auth = subparsers.add_parser("google-auth", help="Authorize Google Drive")
    google_auth.add_argument("--client-secret", type=Path, default=Path("client_secret.json"))
    google_auth.add_argument("--token-file", type=Path, default=default_google_token_file())

    backup = subparsers.add_parser("backup", help="Run an incremental backup")
    backup.add_argument("--source", default="disk:", help="Yandex path prefix, for example disk:/Photos")
    backup.add_argument("--destination", default="Yandex Disk", help="Managed folder name in Google Drive")
    backup.add_argument("--client-secret", type=Path, default=Path("client_secret.json"))
    backup.add_argument("--google-token-file", type=Path, default=default_google_token_file())
    backup.add_argument("--yandex-token-file", type=Path, default=default_yandex_token_file())
    backup.add_argument("--temp-dir", type=Path, default=state_dir() / "downloads")
    backup.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    backup.add_argument("--max-files", type=int)
    backup.add_argument("--dry-run", action="store_true")
    return parser


def _google_credentials(args: argparse.Namespace):
    return authenticate_google(args.client_secret, args.token_file if args.command == "google-auth" else args.google_token_file)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "yandex-auth":
            url = yandex_authorization_url(args.client_id)
            print("Open this URL and approve access to Yandex Disk:\n")
            print(url)
            print("\nAfter approval, copy the access_token value from the redirected URL.")
            if not args.no_browser:
                webbrowser.open(url)
            token = getpass.getpass("Yandex access token: ").strip()
            if not token:
                raise RuntimeError("No token entered")
            save_secret(args.token_file, token)
            print(f"Saved Yandex token to {args.token_file}")
            return 0

        if args.command == "google-auth":
            _google_credentials(args)
            print(f"Saved Google authorization to {args.token_file}")
            return 0

        credentials = _google_credentials(args)
        yandex = YandexDiskClient(read_yandex_token(args.yandex_token_file))
        drive = GoogleDriveClient(build_drive(credentials))
        summary = run_backup(
            yandex,
            drive,
            source_prefix=args.source,
            destination_name=args.destination,
            temp_dir=args.temp_dir.expanduser(),
            dry_run=args.dry_run,
            excludes=args.exclude,
            max_files=args.max_files,
        )
        action = "planned" if args.dry_run else "uploaded"
        print(
            f"\nExamined {summary.examined}; {action} {summary.uploaded}; "
            f"skipped {summary.skipped}; failed {summary.failed}; "
            f"bytes {summary.bytes_uploaded}."
        )
        return 1 if summary.failed else 0
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
