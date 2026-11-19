"""P058: stable Project-local Task ordinal ("№N") beside the unchanged global Task identity (T%04d).

Synthetic only: temporary SQLite migrated with Alembic, in-process FastAPI TestClient and the existing P040/P056
fixtures.  No model call, no real CLI/tmux, no Owner data and never the live database.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from rosc import authorization as authz
from rosc.app import create_app
from rosc.task_authoring import ArchitectAuthor, TaskAuthoringService
from test_p040_task_authoring import TASK, AuthoringCase
from test_p056_task_lifecycle_owner_decision import LifecycleCase

ROOT = Path(__file__).resolve().parents[1]


def numbers(client: TestClient, project: str) -> list[tuple[str, int | None]]:
    response = client.get(f"/api/projects/{project}/tasks")
    assert response.status_code == 200, response.text
    return [(t["id"], t["project_number"]) for t in response.json()]


class NumberingTests(AuthoringCase):
    """Setup: P0001 owns T0001, T0002; P0002 owns T0003 (all created through the Owner API)."""

    def test_each_project_counts_from_one_and_global_ids_are_untouched(self) -> None:
        self.assertEqual(numbers(self.client, "P0001"), [("T0001", 1), ("T0002", 2)])
        self.assertEqual(numbers(self.client, "P0002"), [("T0003", 1)])
        created = self.client.post("/api/projects/P0002/tasks", json={**TASK, "title": "Second of P0002"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual((created.json()["id"], created.json()["project_number"]), ("T0004", 2))
        created = self.client.post("/api/projects/P0001/tasks", json={**TASK, "title": "Third of P0001"})
        self.assertEqual((created.json()["id"], created.json()["project_number"]), ("T0005", 3))
        self.assertEqual(numbers(self.client, "P0001"), [("T0001", 1), ("T0002", 2), ("T0005", 3)])
        self.assertEqual(numbers(self.client, "P0002"), [("T0003", 1), ("T0004", 2)])
        self.assertEqual(self.rows("select id, last_task_number from projects order by id")[:2], [(1, 3), (2, 2)])

    def test_routes_stay_on_the_global_id_and_the_ordinal_is_no_alias(self) -> None:
        self.assertEqual(self.client.get("/api/projects/P0002/tasks/T0003/authoring").status_code, 200)
        # №1 of P0002 is NOT reachable as T0001 there (that global Task belongs to P0001)
        self.assertEqual(self.client.get("/api/projects/P0002/tasks/T0001/authoring").status_code, 404)
        for alias in ("1", "%E2%84%961", "P0002-1"):
            self.assertEqual(self.client.get(f"/api/projects/P0002/tasks/{alias}/authoring").status_code, 404)

    def test_architect_creation_paths_allocate_the_next_number_once(self) -> None:
        sid = self.activate()  # real broker / MCP Tool path of the Project Architect
        result = self.direct(sid, "task_create", title="From architect", description="d", acceptance_criteria="c")
        self.assertTrue(result["ok"], result)
        service = TaskAuthoringService(self.sessions, authz.LocalOwnerPolicy(), self.data / "workspace")
        direct = service.create_task(ArchitectAuthor(1, str(uuid.uuid4()), str(uuid.uuid4())), 1, title="Service",
                                     description="<p>d</p>", acceptance_criteria="<p>c</p>")
        self.assertEqual(direct["task"].project_number, 4)
        self.assertEqual([n for _, n in numbers(self.client, "P0001")], [1, 2, 3, 4])
        self.assertEqual(numbers(self.client, "P0002"), [("T0003", 1)])

    def test_edits_and_reload_never_shift_a_number(self) -> None:
        before = numbers(self.client, "P0001")
        saved = self.client.post("/api/projects/P0001/tasks/T0001/spec/revisions", json={
            "expected_revision": 1, "title": "Renamed", "description": "<p>x</p>", "acceptance_criteria": "<p>y</p>"})
        self.assertEqual(saved.status_code, 201, saved.text)
        self.client.close()
        self.client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace"), base_url="http://localhost")
        self.assertEqual(numbers(self.client, "P0001"), before)

    def test_database_guards_refuse_duplicates_reuse_and_rewrites(self) -> None:
        with sqlite3.connect(self.data / "rosc.db") as db:
            for statement in (
                    "UPDATE tasks SET project_number = 9 WHERE id = 1",
                    "UPDATE tasks SET project_id = 2 WHERE id = 1",
                    "UPDATE projects SET last_task_number = 1 WHERE id = 1",  # would recycle №2
                    "INSERT INTO tasks (project_id, project_number, title, description, acceptance_criteria, status,"
                    " workspace_state, created_at) VALUES (1, 2, 't', 'd', 'a', 'Draft', 'ready', '2026-01-01')",
                    "INSERT INTO tasks (project_id, project_number, title, description, acceptance_criteria, status,"
                    " workspace_state, created_at) VALUES (1, NULL, 't', 'd', 'a', 'Draft', 'ready', '2026-01-01')"):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                    db.execute(statement)
            insert = ("INSERT INTO tasks (project_id, project_number, title, description, acceptance_criteria, status,"
                      " workspace_state, created_at) VALUES (1, 3, 't', 'd', 'a', 'Draft', 'ready', '2026-01-01')")
            db.execute("UPDATE projects SET last_task_number = 3 WHERE id = 1")  # allocate №3 by hand ...
            db.execute(insert)
            with self.assertRaises(sqlite3.IntegrityError):  # ... a second №3 passes the trigger but not the index
                db.execute(insert)
            db.rollback()
        self.assertEqual(numbers(self.client, "P0001"), [("T0001", 1), ("T0002", 2)])


class LifecycleMigrationTests(LifecycleCase):
    def test_reupgrade_keeps_numbers_and_the_accepted_run_and_decision(self) -> None:
        rid, review = self.reviewed()
        self.assertEqual(self.decide(rid, review).status_code, 201)
        self.assertEqual(self.task_status(), "Completed")
        before = {name: self.rows(f"select * from {name} order by 1") for name in
                  ("managed_run_states", "managed_run_reviews", "task_owner_decisions", "task_spec_revisions")}
        listed = numbers(self.client, "P0001"), numbers(self.client, "P0002")
        self.client.close()
        command.downgrade(self.alembic, "0022_task_lifecycle_owner_decision")
        command.upgrade(self.alembic, "head")
        self.build()
        self.assertEqual((numbers(self.client, "P0001"), numbers(self.client, "P0002")), listed)
        self.assertEqual(listed, ([("T0001", 1), ("T0002", 2)], [("T0003", 1)]))
        for name, rows in before.items():
            self.assertEqual(self.rows(f"select * from {name} order by 1"), rows, name)
        self.assertEqual(self.task_status(), "Completed")
        self.assertEqual(self.next_view()["task_status"], "Completed")


class BackfillTests(unittest.TestCase):
    """The Owner scenario on a bare 0022 database: P0002's first Task is global T0007 and must become №1."""

    def test_backfill_is_deterministic_per_project_and_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ROSC_DATA_DIR": tmp}):
            db = Path(tmp) / "rosc.db"
            cfg = Config(str(ROOT / "alembic.ini"))
            cfg.set_main_option("script_location", str(ROOT / "migrations"))
            command.upgrade(cfg, "0022_task_lifecycle_owner_decision")
            with sqlite3.connect(db) as conn:
                for pid in (1, 2, 3):
                    conn.execute("insert into projects (id,name,description,root_path,mode,workspace_state,created_at,"
                                 "project_uuid,registration_root_path) values (?,?,'d',?,'new','ready','2026-01-01',?,?)",
                                 (pid, f"p{pid}", f"/x{pid}", str(uuid.uuid4()), f"/x{pid}"))
                # P0001: T0001..T0006 but inserted out of order; P0002: T0007, T0009; P0003: no Task
                for tid, pid in ((3, 1), (1, 1), (2, 1), (7, 2), (4, 1), (6, 1), (5, 1), (9, 2)):
                    conn.execute("insert into tasks (id,project_id,title,description,acceptance_criteria,status,"
                                 "workspace_state,created_at) values (?,?,'t','d','a','Active','ready','2026-01-01')",
                                 (tid, pid))
            command.upgrade(cfg, "head")
            with sqlite3.connect(db) as conn:
                rows = conn.execute("select id, project_id, project_number from tasks order by id").fetchall()
                counters = conn.execute("select id, last_task_number from projects order by id").fetchall()
            self.assertEqual(rows, [(1, 1, 1), (2, 1, 2), (3, 1, 3), (4, 1, 4), (5, 1, 5), (6, 1, 6),
                                    (7, 2, 1), (9, 2, 2)])
            self.assertEqual(counters, [(1, 6), (2, 2), (3, 0)])
            command.downgrade(cfg, "0022_task_lifecycle_owner_decision")  # clean, reversible
            with sqlite3.connect(db) as conn:
                columns = [r[1] for r in conn.execute("pragma table_info(tasks)")]
            self.assertNotIn("project_number", columns)
            command.upgrade(cfg, "head")
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("select id, project_id, project_number from tasks order by id").fetchall(),
                                 rows)


class LegacySchemaTests(unittest.TestCase):
    def test_task_list_on_a_database_not_yet_upgraded_reports_no_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data, root = Path(tmp) / "data", Path(tmp) / "root"
            data.mkdir()
            root.mkdir()
            with patch.dict(os.environ, {"ROSC_DATA_DIR": str(data)}):
                cfg = Config(str(ROOT / "alembic.ini"))
                cfg.set_main_option("script_location", str(ROOT / "migrations"))
                command.upgrade(cfg, "head")
                client = TestClient(create_app(data / "rosc.db", data / "workspace"), base_url="http://localhost")
                self.assertEqual(client.post("/api/projects", json={
                    "name": "P", "description": "d", "root_path": str(root), "mode": "new"}).status_code, 201)
                self.assertEqual(client.post("/api/projects/P0001/tasks", json=TASK).status_code, 201)
                client.close()
                command.downgrade(cfg, "0022_task_lifecycle_owner_decision")
                client = TestClient(create_app(data / "rosc.db", data / "workspace"), base_url="http://localhost")
                self.assertEqual(numbers(client, "P0001"), [("T0001", None)])  # neutral, never fabricated
                self.assertEqual(client.get("/api/projects/P0001/tasks/T0001/authoring").status_code, 200)
                client.close()


if __name__ == "__main__":
    unittest.main()
