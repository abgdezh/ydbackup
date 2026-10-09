from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from contextlib import nullcontext
from pathlib import Path

from google.oauth2.credentials import Credentials
from rich.console import Console

from .config import default_google_token_file, default_yandex_token_file, read_yandex_token, state_dir
from .google_drive import DRIVE_FILE_SCOPE, GoogleDriveClient, authenticate_google, build_drive
from .parallel_engine import run_parallel_backup
from .parallel_progress import TransferDisplay
from .source_tree import enumerate_tree, limit_tree, render_tree, reverse_folders
from .yandex import YandexDiskClient


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ydbackup-parallel", description=
        "Enumerate Yandex Disk, then back up folders in parallel in reverse source order.")
    commands = parser.add_subparsers(dest="command", required=True)
    tree = commands.add_parser("tree", help="List the complete file tree without accessing Google Drive")
    backup = commands.add_parser("backup", help="Enumerate first, then upload leaf folders in parallel")
    for command in (tree, backup):
        command.add_argument("--source", default="disk:")
        command.add_argument("--exclude", action="append", default=[], metavar="GLOB")
        command.add_argument("--yandex-token-file", type=Path, default=default_yandex_token_file())
    backup.add_argument("--workers", type=positive_int, default=4, help="Concurrent folder workers (default: 4)")
    backup.add_argument("--destination", default="Yandex Disk")
    backup.add_argument("--client-secret", type=Path, default=Path("client_secret.json"))
    backup.add_argument("--google-token-file", type=Path, default=default_google_token_file())
    backup.add_argument("--temp-dir", type=Path, default=state_dir() / "parallel-downloads")
    backup.add_argument("--max-files", type=nonnegative_int, help="Limit files after full enumeration, in reverse order")
    backup.add_argument("--dry-run", action="store_true")
    backup.add_argument("--quiet-tree", action="store_true", help="Print scan totals but omit the folder tree")
    backup.add_argument("--no-tui", action="store_true", help="Print transfer events instead of a live terminal display")
    backup.add_argument("--log-file", type=Path, default=state_dir() / "parallel-backup.log")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scanner = None
    coordinator = None
    log_handler = None
    logger = logging.getLogger("ydbackup.parallel")
    try:
        if args.command == "backup":
            log_path = args.log_file.expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.fchmod(fd, 0o600)
            os.close(fd)
            log_handler = logging.FileHandler(log_path, encoding="utf-8")
            log_handler.setFormatter(logging.Formatter("%(asctime)s %(threadName)s %(message)s"))
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger.addHandler(log_handler)
            logger.info("START source=%s destination=%s workers=%s dry_run=%s", args.source,
                        args.destination, args.workers, args.dry_run)
        def scan_progress(message: str) -> None:
            logger.info(message)
            print(message, file=sys.stderr, flush=True)
        token = read_yandex_token(args.yandex_token_file)
        scanner = YandexDiskClient(token)
        tree = enumerate_tree(scanner.iter_files(), source_prefix=args.source, excludes=args.exclude,
                              on_progress=scan_progress)
        if args.command == "tree" or not args.quiet_tree:
            for line in render_tree(tree, label=args.source, include_files=args.command == "tree"):
                print(line, flush=True)
        if args.command == "tree":
            return 0
        tree = limit_tree(tree, args.max_files)
        credentials = authenticate_google(args.client_secret, args.google_token_file)
        coordinator = GoogleDriveClient(build_drive(credentials))
        # Credentials and HTTP transports are private to each worker thread.
        credential_info = json.loads(credentials.to_json())

        def client_factory():
            worker_credentials = Credentials.from_authorized_user_info(credential_info, [DRIVE_FILE_SCOPE])
            return YandexDiskClient(token), GoogleDriveClient(build_drive(worker_credentials))

        console = Console()
        use_tui = not args.no_tui and console.is_terminal
        nodes = list(reverse_folders(tree))
        display = TransferDisplay(sum(bool(node.files) for node in nodes),
            sum(len(node.files) for node in nodes), console=console, log_path=str(log_path)) if use_tui else None
        def transfer_event(message: str) -> None:
            logger.info(message)
            if not use_tui:
                print(message, flush=True)
        with display if display else nullcontext():
            summary = run_parallel_backup(tree, coordinator, client_factory,
                workers=args.workers, destination_name=args.destination, temp_dir=args.temp_dir.expanduser(),
                dry_run=args.dry_run, on_event=transfer_event,
                on_progress=display.update if display else lambda _: None)
        action = "planned" if args.dry_run else "uploaded"
        message = (f"Examined {summary.examined}; {action} {summary.uploaded}; skipped {summary.skipped}; "
                   f"failed {summary.failed}; bytes {summary.bytes_uploaded}.")
        logger.info(message)
        print("\n" + message)
        return 1 if summary.failed else 0
    except KeyboardInterrupt:
        logger.info("INTERRUPTED")
        print("Interrupted. Rerun the same command to resume.", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.error("ERROR %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        if log_handler is not None:
            logger.removeHandler(log_handler)
            log_handler.close()
        if scanner is not None:
            scanner.api_session.close()
            scanner.download_session.close()
        if coordinator is not None:
            coordinator.service.close()


if __name__ == "__main__":
    raise SystemExit(main())
