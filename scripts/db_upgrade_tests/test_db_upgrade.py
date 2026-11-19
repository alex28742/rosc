"""Offline unit tests for the migration safety wrapper, on temporary SQLite only."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
import textwrap
from types import SimpleNamespace
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db_upgrade as d

CURRENT = "0021_managed_run_reviews"
HEAD = "0022_task_lifecycle_owner_decision"


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.db = self.data / "rosc.db"
        with sqlite3.connect(self.db) as cn:
            cn.executescript(f"""CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL);
                                INSERT INTO alembic_version VALUES ('{CURRENT}');
                                CREATE TABLE tasks (id INTEGER PRIMARY KEY, status TEXT);
                                INSERT INTO tasks VALUES (7, 'Draft');""")

    def test_snapshot_is_verified_and_does_not_change_source(self):
        saved = d.backup_db(self.db, self.data / "backups", CURRENT)
        self.assertEqual(d.inspect_db(saved), CURRENT)
        self.assertEqual(d.inspect_db(self.db), CURRENT)
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(saved) as conn:
            self.assertEqual(conn.execute("SELECT status FROM tasks").fetchone(), ("Draft",))

    def test_success_migrates_with_retained_backup(self):
        def fake_run(argv, **kwargs):
            with sqlite3.connect(self.db) as cn:
                cn.execute("UPDATE tasks SET status='Active' WHERE id=7")
                cn.execute("UPDATE alembic_version SET version_num=?", (HEAD,))
            return SimpleNamespace(returncode=0)
        with patch.object(d, "ensure_app_stopped"), patch.object(d, "migration_head", return_value=HEAD), \
                patch.object(d.subprocess, "run", side_effect=fake_run):
            d.upgrade(self.root, self.data)
        self.assertEqual(d.inspect_db(self.db), HEAD)
        saved = list((self.data / "backups").glob("rosc-before-*.db"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(d.inspect_db(saved[0]), CURRENT)

    def test_failure_auto_restores_unmodified_snapshot(self):
        def fake_run(argv, **kwargs):
            # Simulate a partially applied, non-transactional SQLite DDL migration.
            with sqlite3.connect(self.db) as cn:
                cn.execute("ALTER TABLE tasks ADD COLUMN added_by_failed_migration TEXT")
                cn.execute("UPDATE tasks SET status='Active' WHERE id=7")
            return SimpleNamespace(returncode=1)
        with patch.object(d, "ensure_app_stopped"), patch.object(d, "migration_head", return_value=HEAD), \
                patch.object(d.subprocess, "run", side_effect=fake_run):
            with self.assertRaisesRegex(d.UpgradeError, "restored to"):
                d.upgrade(self.root, self.data)
        self.assertEqual(d.inspect_db(self.db), CURRENT)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT status FROM tasks").fetchone(), ("Draft",))
            self.assertNotIn("added_by_failed_migration", [x[1] for x in conn.execute("PRAGMA table_info(tasks)")])
        self.assertEqual(len(list((self.data / "backups").glob("rosc-before-*.db"))), 1)

    def test_success_with_wrong_revision_rolls_back(self):
        def fake_run(argv, **kwargs):
            with sqlite3.connect(self.db) as cn:
                cn.execute("UPDATE alembic_version SET version_num='unexpected'")
            return SimpleNamespace(returncode=0)
        with patch.object(d, "ensure_app_stopped"), patch.object(d, "migration_head", return_value=HEAD), \
                patch.object(d.subprocess, "run", side_effect=fake_run):
            with self.assertRaisesRegex(d.UpgradeError, "restored to"):
                d.upgrade(self.root, self.data)
        self.assertEqual(d.inspect_db(self.db), CURRENT)

    def test_manual_restore_explicit_snapshot(self):
        snapshot = d.backup_db(self.db, self.data / "backups", CURRENT)
        with sqlite3.connect(self.db) as cn:
            cn.execute("UPDATE alembic_version SET version_num=?", (HEAD,))
            cn.execute("UPDATE tasks SET status='Active'")
        restored = d.restore_backup(self.db, snapshot, self.data / "backups")
        self.assertEqual(restored, CURRENT)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT status FROM tasks").fetchone(), ("Draft",))

    def test_running_app_refuses_before_backup(self):
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "local_app.py").touch()
        with patch.object(d.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="RUNNING\n", stderr="")):
            with self.assertRaisesRegex(d.UpgradeError, "not demonstrably stopped"):
                d.ensure_app_stopped(self.root, self.data)
        self.assertFalse((self.data / "backups").exists())

    def test_stopped_launcher_allows_offline_preflight(self):
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "local_app.py").write_text(
            "import sys\nprint('STOPPED')\nprint('database : test')\nsys.exit(3)\n")
        d.ensure_app_stopped(self.root, self.data)  # real child process; no local ROSC app started

    def test_missing_db_never_created(self):
        self.db.unlink()
        with self.assertRaisesRegex(d.UpgradeError, "Not a regular SQLite"):
            d.inspect_db(self.db)
        self.assertFalse(self.db.exists())

    def test_real_alembic_cli_on_disposable_db(self):
        # A minimal Alembic fixture exercises the actual CLI, not a mocked subprocess.
        migration_dir = self.root / "migrations"
        version_dir = migration_dir / "versions"
        version_dir.mkdir(parents=True)
        (self.root / "alembic.ini").write_text("[alembic]\nscript_location = migrations\n")
        (migration_dir / "env.py").write_text(textwrap.dedent("""\
            from alembic import context
            from sqlalchemy import create_engine
            import os
            from pathlib import Path
            engine = create_engine('sqlite:///' + str(Path(os.environ['ROSC_DATA_DIR']) / 'rosc.db'))
            with engine.connect() as connection:
                context.configure(connection=connection, render_as_batch=True)
                with context.begin_transaction():
                    context.run_migrations()
        """))
        (version_dir / "0022_dummy.py").write_text(textwrap.dedent(f"""\
            from alembic import op
            revision = '{HEAD}'
            down_revision = '{CURRENT}'
            branch_labels = None
            depends_on = None
            def upgrade():
                op.execute("UPDATE tasks SET status = 'Active' WHERE id = 7")
            def downgrade():
                op.execute("UPDATE tasks SET status = 'Draft' WHERE id = 7")
        """))
        (version_dir / "0021_dummy.py").write_text(textwrap.dedent(f"""\
            revision = '{CURRENT}'
            down_revision = None
            branch_labels = None
            depends_on = None
            def upgrade():
                pass
            def downgrade():
                pass
        """))
        with patch.object(d, "ensure_app_stopped"):
            d.upgrade(self.root, self.data)
        self.assertEqual(d.inspect_db(self.db), HEAD)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT status FROM tasks WHERE id=7").fetchone(), ("Active",))
        self.assertEqual(d.inspect_db(next((self.data / 'backups').glob('rosc-before-*.db'))), CURRENT)

    def test_backup_contains_live_wal_pages(self):
        with sqlite3.connect(self.db) as cn:
            cn.execute("PRAGMA journal_mode=WAL")
            cn.execute("INSERT INTO tasks VALUES(8, 'Active')")
            cn.commit()  # committed in WAL, without requiring a checkpoint
            snapshot = d.backup_db(self.db, self.data / "backups", CURRENT)
            with sqlite3.connect(snapshot) as b:
                self.assertEqual(b.execute("SELECT status FROM tasks WHERE id=8").fetchone(), ("Active",))


if __name__ == "__main__":
    unittest.main()
