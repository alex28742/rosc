#!/usr/bin/env python3
"""Offline ROSC SQLite migration: verified backup, Alembic upgrade, and snapshot restoration.

Run from the ROSC checkout while `make local-down` has stopped the application.
Uses the same ROSC_DATA_DIR rule as src/rosc/config.py and scripts/local_app.py.
No backend, frontend, Worker, or extra ROSC instance is started by this command.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class UpgradeError(Exception):
    pass


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")


def resolve_data_dir(root: Path, path: str | None = None) -> Path:
    explicit = path or (os.environ.get("ROSC_DATA_DIR") or "").strip()
    return (Path(explicit).expanduser() if explicit else root / "data").resolve()


def inspect_db(path: Path) -> str:
    """No mutation, no silent SQLite creation. Verify integrity and the Alembic revision."""
    if not path.is_file() or path.is_symlink():
        raise UpgradeError(f"Not a regular SQLite database: {path}")
    try:
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise UpgradeError(f"SQLite integrity_check failed for {path}")
            violations = db.execute("PRAGMA foreign_key_check").fetchmany(1)
            if violations:
                raise UpgradeError(f"SQLite foreign_key_check failed for {path}: {violations[0]}")
            rows = db.execute("SELECT version_num FROM alembic_version").fetchall()
            if len(rows) != 1:
                raise UpgradeError(f"Expected one Alembic revision, got {len(rows)} in {path}")
            return str(rows[0][0])
    except sqlite3.DatabaseError as exc:
        raise UpgradeError(f"Cannot verify SQLite database {path}: {exc}") from exc


def migration_head(root: Path) -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    if len(heads) != 1:
        raise UpgradeError(f"Expected one Alembic head, got {heads}")
    return heads[0]


def ensure_app_stopped(root: Path, data_dir: Path) -> None:
    """Fail closed unless ROSC's existing local launcher reports STOPPED.

    A manually launched backend is not managed by local-app; also refuse when an
    unexpected listener uses a standard dev port or a process holds this DB open.
    """
    script = root / "scripts" / "local_app.py"
    if not script.is_file():
        raise UpgradeError(f"Cannot find the ROSC launcher: {script}")
    env = {**os.environ, "ROSC_DATA_DIR": str(data_dir)}
    try:
        result = subprocess.run([sys.executable, str(script), "status"], cwd=root, env=env,
                                capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpgradeError(f"Cannot verify ROSC stopped state: {exc}") from exc
    if (result.returncode != 3 or not result.stdout.startswith("STOPPED\n")
            or "port held by another process" in result.stdout):
        raise UpgradeError("ROSC is not demonstrably stopped. Run `make local-down`, inspect "
                           f"`make local-status`, then retry. Status: {result.stdout[:700].strip()} "
                           f"{result.stderr[:300].strip()}")
    # An explicitly launched, unmanaged app may use a ROSC port; do not proceed.
    import socket
    for port in (8010, 5173, 4173):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                raise UpgradeError(f"Port {port} still has a listener. Stop the app before migration.")
        except (ConnectionRefusedError, TimeoutError, OSError):
            pass
    # A DB user with its fd open is a stronger indication than port liveness.
    targets = {str(data_dir / f"rosc.db{s}") for s in ("", "-wal", "-shm")}
    proc = Path("/proc")
    if proc.is_dir():
        for pid in proc.iterdir():
            if not pid.name.isdecimal() or int(pid.name) == os.getpid():
                continue
            with contextlib.suppress(OSError):
                for fd in (pid / "fd").iterdir():
                    with contextlib.suppress(OSError):
                        pointed = os.readlink(fd)
                        if pointed in targets or pointed.removesuffix(" (deleted)") in targets:
                            raise UpgradeError(f"Process {pid.name} still holds the ROSC database open; stop it first.")


def backup_db(db_path: Path, backups: Path, revision: str) -> Path:
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = f"rosc-before-{revision.split('_', 1)[0]}-{stamp()}.db"
    dest = backups / filename
    fd, tmp_name = tempfile.mkstemp(prefix=".rosc-backup-", suffix=".tmp", dir=backups)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        os.chmod(tmp, 0o600)
        with contextlib.closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5)) as src:
            with contextlib.closing(sqlite3.connect(tmp)) as out:
                src.backup(out)  # consistent snapshot, including uncheckpointed WAL pages
        if inspect_db(tmp) != revision:
            raise UpgradeError("The backup revision differs from the source revision")
        with tmp.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(tmp, dest)
        return dest
    finally:
        tmp.unlink(missing_ok=True)


def restore_backup(db_path: Path, backup: Path, backups: Path) -> str:
    """Restore exact verified snapshot with an atomic DB-file replacement.

    Preconditions: app is stopped and no process holds DB/WAL/SHM files.
    Quarantine any old WAL/SHM alongside a best-effort failed DB copy.
    """
    backup = backup.resolve()
    old_version = inspect_db(backup)
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".rosc-restore-", suffix=".tmp", dir=db_path.parent)
    tmp = Path(name)
    os.close(fd)
    moved = []
    try:
        os.chmod(tmp, 0o600)
        shutil.copyfile(backup, tmp)
        if inspect_db(tmp) != old_version:
            raise UpgradeError("The restored candidate does not match the verified backup revision")
        with tmp.open("rb") as stream:
            os.fsync(stream.fileno())
        failed = f"rosc-failed-{stamp()}"
        with contextlib.suppress(OSError):
            shutil.copy2(db_path, backups / f"{failed}.db")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                quarantine = backups / f"{failed}{suffix}"
                os.replace(sidecar, quarantine)
                moved.append((quarantine, sidecar))
        try:
            os.replace(tmp, db_path)
        except OSError:
            for quarantine, sidecar in reversed(moved):
                os.replace(quarantine, sidecar)
            raise
        version = inspect_db(db_path)
        if version != old_version:
            raise UpgradeError(f"Post-restore revision mismatch: {version} vs {old_version}")
        return version
    finally:
        tmp.unlink(missing_ok=True)


def upgrade(root: Path, data_dir: Path) -> None:
    db_path = data_dir / "rosc.db"
    ensure_app_stopped(root, data_dir)
    current = inspect_db(db_path)
    head = migration_head(root)
    if current == head:
        print(f"ALREADY_AT_HEAD | revision={head} | database={db_path}")
        return
    backups = data_dir / "backups"
    snapshot = backup_db(db_path, backups, current)
    print(f"BACKUP VERIFIED | revision={current} | file={snapshot}", flush=True)
    env = {**os.environ, "ROSC_DATA_DIR": str(data_dir)}
    try:
        result = subprocess.run([sys.executable, "-m", "alembic", "-c", str(root / "alembic.ini"),
                                 "upgrade", "head"], cwd=root, env=env, check=False)
        if result.returncode != 0:
            raise UpgradeError(f"Alembic exited with code {result.returncode}")
        after = inspect_db(db_path)
        if after != head:
            raise UpgradeError(f"Expected Alembic revision {head}, got {after}")
    except (UpgradeError, OSError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        print(f"MIGRATION FAILED: {exc}. Restoring verified backup…", file=sys.stderr, flush=True)
        try:
            ensure_app_stopped(root, data_dir)
            restored = restore_backup(db_path, snapshot, backups)
        except Exception as restore_exc:
            raise UpgradeError(f"Automatic restore FAILED: {restore_exc}; keep ROSC stopped and restore manually "
                               f"from {snapshot} (original migration error: {exc})") from restore_exc
        raise UpgradeError(f"Upgrade failed but DB restored to {restored}. Snapshot retained: {snapshot}") from exc
    print(f"UPGRADE OK | {current} → {head} | backup={snapshot} | database={db_path}")


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description="ROSC offline SQLite backup + migration + restore")
    parser.add_argument("--data-dir", metavar="PATH", help="override ROSC_DATA_DIR / repo data root")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("upgrade", help="verified backup, Alembic upgrade head, automatic restore on failure")
    restore = sub.add_parser("restore", help="explicit restore from a verified snapshot; ROSC must be stopped")
    restore.add_argument("--backup", type=Path, required=True, help="path printed by previous upgrade")
    restore.add_argument("--yes", action="store_true", help="confirm overwriting the current SQLite database")
    args = parser.parse_args(argv)
    data_dir = resolve_data_dir(root, args.data_dir)
    db_path = data_dir / "rosc.db"
    try:
        if not data_dir.is_dir() or data_dir.is_symlink():
            raise UpgradeError(f"Data root is not an existing normal directory: {data_dir}")
        lock_path = data_dir / ".db-upgrade.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command == "upgrade":
                upgrade(root, data_dir)
            else:
                if not args.yes:
                    raise UpgradeError("Restore overwrites the current database: pass --yes to confirm")
                ensure_app_stopped(root, data_dir)
                if not db_path.is_file() or db_path.is_symlink():
                    raise UpgradeError(f"Target ROSC database is missing or not regular: {db_path}")
                # Do not require it to be intact: this is exactly when manual restore is needed.
                restored = restore_backup(db_path, args.backup, data_dir / "backups")
                print(f"RESTORED OK | revision={restored} | database={db_path}")
    except (UpgradeError, OSError, sqlite3.Error, ImportError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
