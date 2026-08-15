#!/usr/bin/env python3
"""Create, verify, and restore transactionally consistent RM SQLite backups."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

BACKUP_PATTERN = re.compile(
    r"^resource-monitor-(?P<stamp>\d{8}T\d{12}Z)\.sqlite3$"
)


def _readonly_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"


def verify_database(path: Path) -> dict[str, int | str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"database is not a regular file: {path}")
    connection = sqlite3.connect(_readonly_uri(path), uri=True, timeout=30)
    try:
        rows = [row[0] for row in connection.execute("PRAGMA quick_check")]
        if rows != ["ok"]:
            raise RuntimeError(f"SQLite quick_check failed: {rows!r}")
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    finally:
        connection.close()
    return {"quick_check": "ok", "page_count": page_count, "bytes": path.stat().st_size}


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_database(source: Path, target: Path) -> None:
    source_connection = sqlite3.connect(_readonly_uri(source), uri=True, timeout=30)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.execute("PRAGMA busy_timeout=30000")
        source_connection.backup(target_connection, pages=256, sleep=0.05)
        target_connection.commit()
        journal_mode = target_connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(journal_mode).lower() != "delete":
            raise RuntimeError(f"could not make backup portable: journal_mode={journal_mode!r}")
        rows = [row[0] for row in target_connection.execute("PRAGMA quick_check")]
        if rows != ["ok"]:
            raise RuntimeError(f"SQLite quick_check failed before publish: {rows!r}")
    finally:
        target_connection.close()
        source_connection.close()


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _secure_backup_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"backup destination may not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError(f"backup destination is not a directory: {path}")
    os.chmod(path, 0o700)


def _backup_stamp(path: Path) -> datetime | None:
    match = BACKUP_PATTERN.fullmatch(path.name)
    if not match or path.is_symlink() or not path.is_file():
        return None
    return datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%S%fZ").replace(
        tzinfo=timezone.utc
    )


def prune_backups(destination: Path, retain_recent: int, retain_daily: int) -> list[str]:
    candidates = []
    for path in destination.iterdir():
        stamp = _backup_stamp(path)
        if stamp is not None:
            candidates.append((stamp, path))
    candidates.sort(reverse=True)

    keep = {path for _, path in candidates[:retain_recent]}
    represented_days = {stamp.date() for stamp, _ in candidates[:retain_recent]}
    retained_daily = 0
    for stamp, path in candidates[retain_recent:]:
        if retained_daily >= retain_daily:
            break
        if stamp.date() in represented_days:
            continue
        represented_days.add(stamp.date())
        retained_daily += 1
        keep.add(path)

    removed = []
    for _, path in candidates:
        if path not in keep:
            path.unlink()
            removed.append(path.name)
    if removed:
        _fsync_directory(destination)
    return removed


@contextmanager
def backup_lock(destination: Path):
    lock_path = destination / ".backup.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(lock_path, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)


def create_backup(
    database: Path,
    destination: Path,
    *,
    retain_recent: int = 96,
    retain_daily: int = 30,
) -> dict[str, object]:
    if database.is_symlink() or not database.is_file():
        raise ValueError(f"source database is not a regular file: {database}")
    _secure_backup_directory(destination)

    with backup_lock(destination) as acquired:
        if not acquired:
            return {"status": "skipped", "reason": "backup already running"}

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        final_path = destination / f"resource-monitor-{stamp}.sqlite3"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".resource-monitor-", suffix=".partial", dir=destination
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            os.chmod(temporary_path, 0o600)
            _copy_database(database, temporary_path)
            _remove_sqlite_sidecars(temporary_path)
            verification = verify_database(temporary_path)
            _fsync_file(temporary_path)
            os.replace(temporary_path, final_path)
            os.chmod(final_path, 0o600)
            _fsync_directory(destination)
            removed = prune_backups(destination, retain_recent, retain_daily)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            _remove_sqlite_sidecars(temporary_path)
            raise

    return {
        "status": "created",
        "backup": str(final_path),
        "verification": verification,
        "pruned": removed,
    }


def restore_backup(backup: Path, target: Path) -> dict[str, object]:
    verification = verify_database(backup)
    if target.exists() or target.is_symlink():
        raise ValueError(f"restore target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".restore-partial", dir=target.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        os.chmod(temporary_path, 0o600)
        _copy_database(backup, temporary_path)
        _remove_sqlite_sidecars(temporary_path)
        restored_verification = verify_database(temporary_path)
        _fsync_file(temporary_path)
        os.replace(temporary_path, target)
        os.chmod(target, 0o600)
        _fsync_directory(target.parent)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        _remove_sqlite_sidecars(temporary_path)
        raise
    return {
        "status": "restored",
        "backup": str(backup),
        "target": str(target),
        "source_verification": verification,
        "verification": restored_verification,
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--db", type=Path, help="live SQLite database to back up")
    modes.add_argument("--verify", type=Path, help="verify one completed backup")
    modes.add_argument("--restore", type=Path, help="completed backup to restore")
    parser.add_argument("--destination", type=Path, help="backup directory")
    parser.add_argument("--to", type=Path, help="new path for --restore; must not exist")
    parser.add_argument("--retain-recent", type=_positive_int, default=96)
    parser.add_argument("--retain-daily", type=_positive_int, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.db:
            if args.destination is None:
                parser.error("--destination is required with --db")
            result = create_backup(
                args.db,
                args.destination,
                retain_recent=args.retain_recent,
                retain_daily=args.retain_daily,
            )
        elif args.verify:
            result = {"status": "verified", "backup": str(args.verify), **verify_database(args.verify)}
        else:
            if args.to is None:
                parser.error("--to is required with --restore")
            result = restore_backup(args.restore, args.to)
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
        print(f"backup failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
