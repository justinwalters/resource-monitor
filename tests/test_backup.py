import importlib.util
import os
import plistlib
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "backup_db.py"
INSTALLER = ROOT / "deploy" / "install-backup.sh"
PLIST = ROOT / "deploy" / "com.resource-monitor.backup.plist"
SPEC = importlib.util.spec_from_file_location("rm_backup_db", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BACKUP_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKUP_MODULE)
create_backup = BACKUP_MODULE.create_backup
prune_backups = BACKUP_MODULE.prune_backups
restore_backup = BACKUP_MODULE.restore_backup
verify_database = BACKUP_MODULE.verify_database


def make_database(path: Path, rows: int = 10) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY, value TEXT)")
    connection.executemany(
        "INSERT INTO observations(value) VALUES (?)", ((f"row-{index}",) for index in range(rows))
    )
    connection.commit()
    connection.close()


def row_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT count(*) FROM observations").fetchone()[0])
    finally:
        connection.close()


def test_online_backup_includes_committed_wal_data_and_is_verified(tmp_path):
    database = tmp_path / "live.sqlite3"
    destination = tmp_path / "backups"
    make_database(database, rows=50)

    result = create_backup(database, destination)
    backup = Path(result["backup"])

    assert result["status"] == "created"
    assert result["verification"]["quick_check"] == "ok"
    assert verify_database(backup)["quick_check"] == "ok"
    assert row_count(backup) == 50
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert not list(destination.glob(".resource-monitor-*"))


def test_corrupt_source_is_never_published(tmp_path):
    database = tmp_path / "corrupt.sqlite3"
    database.write_bytes(b"not a sqlite database")
    destination = tmp_path / "backups"

    with pytest.raises(sqlite3.DatabaseError):
        create_backup(database, destination)

    assert not list(destination.glob("resource-monitor-*.sqlite3"))
    assert not list(destination.glob(".resource-monitor-*"))


def test_restore_is_verified_and_refuses_overwrite(tmp_path):
    database = tmp_path / "live.sqlite3"
    make_database(database, rows=7)
    created = create_backup(database, tmp_path / "backups")
    backup = Path(created["backup"])
    restored = tmp_path / "restore" / "resource-monitor.sqlite3"

    result = restore_backup(backup, restored)
    assert result["status"] == "restored"
    assert row_count(restored) == 7
    assert stat.S_IMODE(restored.stat().st_mode) == 0o600
    with pytest.raises(ValueError, match="already exists"):
        restore_backup(backup, restored)


def test_verify_rejects_corruption_and_symlinks(tmp_path):
    corrupt = tmp_path / "bad.sqlite3"
    corrupt.write_bytes(b"bad")
    with pytest.raises(sqlite3.DatabaseError):
        verify_database(corrupt)
    link = tmp_path / "link.sqlite3"
    link.symlink_to(corrupt)
    with pytest.raises(ValueError, match="regular file"):
        verify_database(link)


def test_pruning_keeps_recent_plus_one_per_utc_day_and_unrelated_files(tmp_path):
    destination = tmp_path / "backups"
    destination.mkdir()
    now = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    paths = []
    for hours in (0, 1, 2, 25, 26, 49, 73):
        stamp = now - timedelta(hours=hours)
        path = destination / f"resource-monitor-{stamp.strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
        path.write_bytes(b"x")
        paths.append(path)
    unrelated = destination / "do-not-delete.txt"
    unrelated.write_text("keep")
    symlink = destination / "resource-monitor-20200101T000000000000Z.sqlite3"
    symlink.symlink_to(unrelated)

    removed = prune_backups(destination, retain_recent=2, retain_daily=2)

    assert len(removed) == 3
    assert paths[0].exists() and paths[1].exists()
    assert paths[3].exists() and paths[5].exists()
    assert unrelated.exists() and symlink.is_symlink()


def test_cli_verify_returns_nonzero_for_corrupt_file(tmp_path):
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"bad")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--verify", str(corrupt)], text=True, capture_output=True
    )
    assert result.returncode == 1
    assert "backup failed" in result.stderr


def test_backup_plist_is_bounded_and_has_no_credentials():
    content = PLIST.read_text()
    assert "StartInterval" in content
    assert "__RM_BACKUP_INTERVAL__" in content
    assert "--retain-recent" in content and "--retain-daily" in content
    assert "TOKEN" not in content and "PASSWORD" not in content and "SECRET" not in content
    assert "KeepAlive" not in content


def test_installer_dry_run_is_side_effect_free_and_validates_interval(tmp_path):
    database = tmp_path / "live.sqlite3"
    make_database(database)
    home = tmp_path / "home"
    env = {**os.environ, "HOME": str(home)}
    result = subprocess.run(
        [
            str(INSTALLER), "--python", sys.executable, "--db", str(database),
            "--interval", "900", "--dry-run",
        ],
        cwd=ROOT, text=True, capture_output=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "<integer>900</integer>" in result.stdout
    assert "api-token" not in result.stdout
    assert not home.exists()
    rendered = plistlib.loads(result.stdout.encode())
    assert rendered["StartInterval"] == 900
    assert rendered["ProgramArguments"][-4:] == ["--retain-recent", "96", "--retain-daily", "30"]

    refused = subprocess.run(
        [
            str(INSTALLER), "--python", sys.executable, "--db", str(database),
            "--interval", "59", "--dry-run",
        ],
        cwd=ROOT, text=True, capture_output=True, env=env,
    )
    assert refused.returncode != 0
    assert "at least 60" in refused.stderr
