"""P041: internal Execution Package builder/view.  No HTTP route, no Run, no Worker, no Task status change."""
from __future__ import annotations

import sqlite3
import tempfile
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


from rosc import authorization as authz
from rosc.task_authoring import AuthoringConflict, AuthoringNotFound, TaskAuthoringService
from test_p040_task_authoring import AuthoringCase


class PackageCase(AuthoringCase):
    def setUp(self) -> None:
        super().setUp()
        self.svc = TaskAuthoringService(self.sessions, authz.LocalOwnerPolicy(), self.data / "workspace")

    def approved(self, rev=None, src=1, body="Do the work") -> dict:
        row = self.prompt(rev, src, body=body).json()["prompt"]
        r = self.client.post(f"{self.t}/prompt/revisions/{row['revision']}/approve", json={"digest": row["digest"]})
        self.assertEqual(r.status_code, 200, r.text)
        return row

    def count(self, table: str = "execution_packages") -> int:
        return self.rows(f"select count(*) from {table}")[0][0]


class BuilderTests(PackageCase):
    def test_build_view_idempotent_and_minimal(self) -> None:
        self.approved()
        a = self.svc.build_execution_package(1, 1)
        self.assertTrue(a["created"])
        b = self.svc.build_execution_package(1, 1)
        self.assertEqual((b["created"], b["package_id"]), (False, a["package_id"]))
        self.assertEqual(self.count(), 1)
        cols = [c[1] for c in self.rows("pragma table_info(execution_packages)")]
        self.assertEqual(cols, ["id", "project_id", "task_id", "prompt_approval_id", "root_binding_version", "created_at"])
        v = self.svc.execution_package_view(1, 1, a["package_id"])
        self.assertEqual((v["task_id"], v["root_binding_version"], v["spec"]["revision"], v["prompt"]["revision"]),
                         ("T0001", 1, 1, 1))
        self.assertEqual(v["prompt"]["body"], "Do the work")
        self.assertEqual(v["spec"]["acceptance_criteria_text"], "Criterion A")
        self.assertEqual(v["prompt"]["approval"]["prompt_revision"], 1)
        self.assertEqual((self.count("run_records"), self.rows("select status from tasks where id=1")), (0, [("Draft",)]))

    def test_new_approved_revision_gets_new_package_and_history_is_stable(self) -> None:
        self.approved()
        first = self.svc.build_execution_package(1, 1)["package_id"]
        self.approved(rev=1, body="Second body")
        second = self.svc.build_execution_package(1, 1)["package_id"]
        self.assertNotEqual(first, second)
        old = self.svc.execution_package_view(1, 1, first)
        self.assertEqual(old["prompt"]["body"], "Do the work")
        self.assertEqual(self.svc.execution_package_view(1, 1, second)["prompt"]["body"], "Second body")
        self.assertEqual(self.client.post(f"{self.t}/spec/revisions", json={
            "expected_revision": 1, "title": "Spec task", "description": "<p>New</p>", "acceptance_criteria": "<p>N</p>"}).status_code, 201)
        self.assertEqual(self.svc.execution_package_view(1, 1, first)["spec"]["description_text"], "Line one\n\nLine two")

    def test_rejections_write_nothing(self) -> None:
        with self.assertRaises(AuthoringNotFound):
            self.svc.build_execution_package(1, 1)  # no prompt
        self.prompt(None, 1)
        with self.assertRaises(AuthoringConflict) as c:
            self.svc.build_execution_package(1, 1)
        self.assertEqual(c.exception.code, "not_approved")
        self.approved(rev=1, body="Approved")
        self.client.post(f"{self.t}/spec/revisions", json={
            "expected_revision": 1, "title": "Spec task", "description": "<p>New</p>", "acceptance_criteria": "<p>N</p>"})
        with self.assertRaises(AuthoringConflict) as c:
            self.svc.build_execution_package(1, 1)
        self.assertEqual(c.exception.code, "prompt_stale")
        with self.assertRaises(AuthoringNotFound):
            self.svc.build_execution_package(1, 3)  # Task of another Project
        with self.assertRaises(AuthoringNotFound):
            self.svc.build_execution_package(99, 1)
        self.assertEqual(self.count(), 0)

    def test_superseded_unapproved_head_and_tampering_rejected(self) -> None:
        self.approved()
        self.prompt(1, 1, body="Unapproved newer")  # head is now unapproved; the approved vN is superseded
        with self.assertRaises(AuthoringConflict) as c:
            self.svc.build_execution_package(1, 1)
        self.assertEqual(c.exception.code, "not_approved")
        self.assertEqual(self.count(), 0)

    def test_digest_mismatch_and_binding_failure(self) -> None:
        self.approved()
        with sqlite3.connect(self.data / "rosc.db") as db:
            db.execute("update prompt_revisions set body='tampered' where revision=1")
        with self.assertRaises(AuthoringConflict) as c:
            self.svc.build_execution_package(1, 1)
        self.assertEqual(c.exception.code, "digest_mismatch")
        with sqlite3.connect(self.data / "rosc.db") as db:
            db.execute("update prompt_revisions set body='Do the work' where revision=1")
            db.execute("update project_root_bindings set state='superseded' where project_id=1")
        with self.assertRaises(Exception):
            self.svc.build_execution_package(1, 1)
        self.assertEqual(self.count(), 0)

    def test_concurrent_builds_create_one_row(self) -> None:
        self.approved()
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self._try(), range(4)))
        self.assertEqual(self.count(), 1)
        self.assertTrue(any(r == "ok" for r in results))

    def _try(self) -> str:
        try:
            self.svc.build_execution_package(1, 1)
            return "ok"
        except AuthoringConflict:
            return "conflict"

    def test_view_is_scoped(self) -> None:
        self.approved()
        pid = self.svc.build_execution_package(1, 1)["package_id"]
        with self.assertRaises(AuthoringNotFound):
            self.svc.execution_package_view(1, 2, pid)
        with self.assertRaises(AuthoringNotFound):
            self.svc.execution_package_view(2, 1, pid)


class NoSideEffectTests(PackageCase):
    def test_product_actions_never_create_a_package(self) -> None:
        self.approved()
        self.client.get(self.t)
        self.client.get(f"{self.t}/authoring")
        self.assertEqual((self.count(), self.count("run_records")), (0, 0))
        routes = {getattr(r, "path", "") for r in self.client.app.routes}
        self.assertFalse([p for p in routes if "package" in p.lower()])
        self.assertEqual(self.client.post(f"{self.t}/execution-package").status_code in (404, 405), True)


class MigrationTests(unittest.TestCase):
    def test_upgrade_from_0012_keeps_data(self) -> None:
        from alembic import command
        from alembic.config import Config
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ROSC_DATA_DIR": tmp}):
            db = Path(tmp) / "rosc.db"
            cfg = Config(str(root / "alembic.ini"))
            cfg.set_main_option("script_location", str(root / "migrations"))
            command.upgrade(cfg, "0011_task_native_sessions")
            with sqlite3.connect(db) as conn:
                conn.execute("insert into projects (id,name,description,root_path,mode,workspace_state,created_at,project_uuid,registration_root_path) "
                             "values (1,'p','d','/x','new','ready','2026-01-01','11111111-1111-4111-8111-111111111111','/x')")
                conn.execute("insert into tasks (id,project_id,title,description,acceptance_criteria,status,workspace_state,created_at) "
                             "values (1,1,'T','a','c','Draft','ready','2026-01-01')")
            command.upgrade(cfg, "0012_task_spec_prompt_authoring")
            command.upgrade(cfg, "0013_execution_package_core")
            command.upgrade(cfg, "head")
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("select count(*) from task_spec_revisions").fetchone(), (1,))
                self.assertEqual(conn.execute("select count(*) from tasks").fetchone(), (1,))
                self.assertEqual(conn.execute("select count(*) from execution_packages").fetchone(), (0,))
            command.downgrade(cfg, "0012_task_spec_prompt_authoring")


if __name__ == "__main__":
    unittest.main()
