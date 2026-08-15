# Resource Monitor database backup and restore

The deployment host is the sole owner of the live Resource Monitor database. Git protects
the code, not this operational history. `com.resource-monitor.backup` therefore creates a consistent
SQLite online backup every 15 minutes while the service remains live.

## Safety and retention

- Backups use SQLite's online backup API, so committed WAL data is included and the result is
  transactionally consistent. The live database is never copied as an ordinary file.
- A backup remains hidden as `.partial` until `PRAGMA quick_check` succeeds, the file is
  fsynced, and it is atomically renamed.
- The backup directory is mode 0700 and completed databases are mode 0600.
- An advisory lock prevents overlapping launchd/manual runs.
- Retention keeps the newest 96 backups (24 hours at 15-minute intervals) and one additional
  recovery point per UTC day for 30 days. Only files matching RM's exact backup filename
  pattern can be pruned; symlinks and unrelated files are never touched.

Default location:

```text
~/Library/Application Support/Resource Monitor/backups/
```

## Install or refresh the LaunchAgent

Run from the deployment checkout:

```bash
deploy/install-backup.sh \
  --python .venv/bin/python \
  --db data/resource-monitor.db \
  --load
```

The installed plist contains no credentials. Inspect it and the latest run with:

```bash
plutil -lint ~/Library/LaunchAgents/com.resource-monitor.backup.plist
launchctl print gui/$(id -u)/com.resource-monitor.backup
tail -1 ~/Library/Application\ Support/Resource\ Monitor/backup.stdout.log
```

## Verify and drill a restore

Verification and restore always operate on a completed backup; restore refuses to overwrite an
existing target.

```bash
latest=$(find ~/Library/Application\ Support/Resource\ Monitor/backups \
  -name 'resource-monitor-*.sqlite3' -type f | sort | tail -1)
python scripts/backup_db.py --verify "$latest"
scratch=$(mktemp -d)
python scripts/backup_db.py --restore "$latest" --to "$scratch/restored.sqlite3"
python scripts/backup_db.py --verify "$scratch/restored.sqlite3"
```

For disaster recovery, first verify the selected backup and restore it to a new path as above.
Then stop `com.resource-monitor.service`, move the damaged database aside (do not delete it),
atomically move the verified restored file to the configured database path, and restart the
service. Never restore over a running database or discard the old file until authenticated RM
health and snapshot queries have succeeded.
