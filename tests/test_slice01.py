from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import yaml

from rosc.app import create_app

ROOT = Path(__file__).resolve().parents[1]


class StageCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.data = self.base / "data"
        self.data.mkdir()
        self.env = patch.dict(os.environ, {"ROSC_DATA_DIR": str(self.data)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.alembic = Config(str(ROOT / "alembic.ini"))
        self.alembic.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(self.alembic, "head")
        self.client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace"))

    def new_root(self) -> Path:
        path = self.base / "empty-project"
        path.mkdir()
        return path

    def register(self, path: Path, mode: str = "new", **overrides) -> dict:
        body = {"name": "Fixture", "description": "Isolated project", "root_path": str(path),
                "mode": mode, "no_external_agent_confirmed": mode == "existing", **overrides}
        return self.client.post("/api/projects", json=body)


class FoundationSliceTests(StageCase):
    def test_project_and_task_survive_api_restart_without_agent_launch(self) -> None:
        root = self.new_root()
        created = self.register(root)
        self.assertEqual(created.status_code, 201, created.text)
        project = created.json()
        self.assertEqual(project["id"], "P0001")
        self.assertEqual(project["workspace_state"], "ready")
        self.assertEqual(project["architect"], "not_active")
        self.assertEqual(project["active_workers"], 0)
        manifest = self.data / "workspace/projects/P0001/project.yaml"
        self.assertEqual(yaml.safe_load(manifest.read_text())["root_path"], str(root.resolve()))
        self.assertEqual(list(root.iterdir()), [], "ROSC must not write into application source root")

        response = self.client.post("/api/projects/P0001/tasks", json={
            "title": "Check API", "description": "Inspect behavior", "acceptance_criteria": "Evidence recorded"
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "Draft")
        self.assertEqual(response.json()["worker"], "not_assigned")
        self.assertTrue((self.data / "workspace/projects/P0001/tasks/T0001/task.yaml").is_file())
        task_manifest = self.data / "workspace/projects/P0001/tasks/T0001/task.yaml"
        task_manifest.unlink()
        self.assertEqual(self.client.get("/api/projects/P0001/tasks").json()[0]["workspace_state"], "needs_reconciliation")
        repaired = self.client.post("/api/projects/P0001/tasks/T0001/reconcile-workspace")
        self.assertEqual(repaired.status_code, 200)
        self.assertEqual(repaired.json()["workspace_state"], "ready")

        restarted = TestClient(create_app(self.data / "rosc.db", self.data / "workspace"))
        self.assertEqual(restarted.get("/api/projects").json()[0]["id"], "P0001")
        self.assertEqual(restarted.get("/api/projects/P0001/tasks").json()[0]["id"], "T0001")
        self.assertEqual(restarted.get("/api/health").json()["agent_runtime"], "not_started")

    def test_root_validation_and_no_external_agent_boundary(self) -> None:
        root = self.new_root()
        (root / "README.md").write_text("existing source")
        self.assertEqual(self.register(root, "new").status_code, 422)
        self.assertEqual(self.register(root, "existing", no_external_agent_confirmed=False).status_code, 422)
        self.assertEqual(self.register(root, "existing", docs_entrypoint="../wrong.md").status_code, 422)
        self.assertEqual(self.register(root, "existing", docs_entrypoint="README.md").status_code, 201)
        self.assertEqual(self.register(root, "existing", docs_entrypoint="README.md").status_code, 409)
        nested = root / "subfolder"
        nested.mkdir()
        (nested / "file.txt").write_text("data")
        self.assertEqual(self.register(nested, "existing").status_code, 409)
        self.assertEqual((root / "README.md").read_text(), "existing source")

    def test_missing_manifest_is_visible_and_repairable_from_db(self) -> None:
        root = self.new_root()
        self.assertEqual(self.register(root).status_code, 201)
        manifest = self.data / "workspace/projects/P0001/project.yaml"
        manifest.unlink()
        self.assertEqual(self.client.get("/api/projects").json()[0]["workspace_state"], "needs_reconciliation")
        response = self.client.post("/api/projects/P0001/reconcile-workspace")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["workspace_state"], "ready")
        manifest.write_text('project_id: P9999\n')
        self.assertEqual(self.client.post("/api/projects/P0001/reconcile-workspace").status_code, 409)
        self.assertEqual(yaml.safe_load(manifest.read_text())["project_id"], "P9999")

    def test_migration_round_trip_on_empty_database(self) -> None:
        command.downgrade(self.alembic, "base")
        command.upgrade(self.alembic, "head")
        self.assertEqual(self.client.get("/api/projects").json(), [])


FOREIGN_PROJECT = b"format: rosc.project.v1\nproject_id: P9999\nname: ORPHAN\nroot_path: /elsewhere\n"
FOREIGN_TASK = b"format: rosc.task.v1\ntask_id: T9999\nproject_id: P9999\nstatus: Draft\n"
TASK_BODY = {"title": "Check", "description": "Inspect", "acceptance_criteria": "Done"}

RACE_WORKER = """
import sys, time
from fastapi.testclient import TestClient
from rosc.app import create_app
db, ws, root, start = sys.argv[1:5]
client = TestClient(create_app(__import__('pathlib').Path(db), __import__('pathlib').Path(ws)))
time.sleep(max(0.0, float(start) - time.time()))
r = client.post('/api/projects', json={'name': 'n', 'description': 'd', 'root_path': root, 'mode': 'existing', 'no_external_agent_confirmed': True})
print(r.status_code)
"""


class HardeningTests(StageCase):
    def project_dir(self) -> Path:
        return self.data / "workspace/projects/P0001"

    def make_project(self) -> Path:
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        return self.project_dir() / "project.yaml"

    def make_task(self) -> Path:
        self.make_project()
        self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=TASK_BODY).status_code, 201)
        return self.project_dir() / "tasks/T0001/task.yaml"

    def repair(self, url: str, confirm: bool = True, **extra):
        return self.client.post(url, json={"confirm_repair": confirm, **extra})

    # F1 --------------------------------------------------------------
    def test_orphan_and_malformed_project_manifests_are_never_replaced(self) -> None:
        for index, original in enumerate((FOREIGN_PROJECT, b": : [broken", b"")):
            with self.subTest(index=index):
                directory = self.data / f"workspace/projects/P{index + 1:04d}"
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "project.yaml").write_bytes(original)
                root = self.base / f"root{index}"
                root.mkdir()
                response = self.register(root)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn("collides", response.json()["detail"])
                self.assertEqual((directory / "project.yaml").read_bytes(), original)
                listed = self.client.get("/api/projects").json()[index]
                self.assertEqual(listed["workspace_state"], "needs_reconciliation")
                self.assertEqual(listed["workspace_problem"], "foreign" if index == 0 else "corrupt")

    def test_foreign_and_malformed_task_manifests_are_never_replaced(self) -> None:
        self.make_project()
        for index, original in enumerate((FOREIGN_TASK, b"- not a mapping\n")):
            with self.subTest(index=index):
                path = self.project_dir() / f"tasks/T{index + 1:04d}/task.yaml"
                path.parent.mkdir(parents=True)
                path.write_bytes(original)
                response = self.client.post("/api/projects/P0001/tasks", json=TASK_BODY)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(path.read_bytes(), original)

    def test_symlink_and_nonregular_manifest_paths_fail_closed(self) -> None:
        outside = self.base / "outside.yaml"
        outside.write_bytes(b"keep: me\n")
        directory = self.data / "workspace/projects/P0001"
        directory.mkdir(parents=True)
        (directory / "project.yaml").symlink_to(outside)
        self.assertEqual(self.register(self.new_root()).status_code, 409)
        self.assertEqual(outside.read_bytes(), b"keep: me\n")
        self.assertEqual(self.client.get("/api/projects").json()[0]["workspace_problem"], "unsafe")
        self.assertEqual(self.repair("/api/projects/P0001/repair-workspace").status_code, 409)
        self.assertEqual(outside.read_bytes(), b"keep: me\n")
        (directory / "project.yaml").unlink()
        (directory / "project.yaml").mkdir()
        self.assertEqual(self.client.post("/api/projects/P0001/reconcile-workspace").status_code, 409)
        self.assertTrue((directory / "project.yaml").is_dir())

    def test_symlinked_task_directory_is_not_followed(self) -> None:
        self.make_project()
        victim = self.base / "victim"
        victim.mkdir()
        (self.project_dir() / "tasks/T0001").symlink_to(victim)
        response = self.client.post("/api/projects/P0001/tasks", json=TASK_BODY)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(list(victim.iterdir()), [])

    def test_manifest_that_appears_after_the_check_is_not_clobbered(self) -> None:
        from rosc import workspace
        self.make_project()
        manifest = self.project_dir() / "project.yaml"
        manifest.unlink()
        manifest.write_bytes(FOREIGN_PROJECT)  # appears between "missing" check and publish
        real = workspace.inspect_manifest
        calls = []

        def stale_first(*args, **kwargs):
            calls.append(1)
            return workspace.Inspection(workspace.MISSING) if len(calls) == 1 else real(*args, **kwargs)

        with patch.object(workspace, "inspect_manifest", stale_first):
            response = self.client.post("/api/projects/P0001/reconcile-workspace")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(manifest.read_bytes(), FOREIGN_PROJECT)

    # F2 --------------------------------------------------------------
    def test_ordinary_reconcile_only_rebuilds_missing_manifests(self) -> None:
        manifest = self.make_project()
        manifest.unlink()
        self.assertEqual(self.client.get("/api/projects").json()[0]["workspace_problem"], "missing")
        self.assertEqual(self.client.post("/api/projects/P0001/reconcile-workspace").status_code, 200)
        manifest.write_bytes(b": : [broken")
        response = self.client.post("/api/projects/P0001/reconcile-workspace")
        self.assertEqual(response.status_code, 409)
        self.assertIn("repair", response.json()["detail"])
        self.assertEqual(manifest.read_bytes(), b": : [broken")

    def test_corrupt_project_manifest_repair_needs_confirmation_and_keeps_backup(self) -> None:
        manifest = self.make_project()
        corrupt = b"\xff\xfe not yaml: [\n"
        manifest.write_bytes(corrupt)
        self.assertEqual(self.client.get("/api/projects").json()[0]["workspace_problem"], "corrupt")
        self.assertEqual(self.repair("/api/projects/P0001/repair-workspace", confirm=False).status_code, 422)
        self.assertEqual(manifest.read_bytes(), corrupt)
        response = self.repair("/api/projects/P0001/repair-workspace")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["workspace_state"], "ready")
        record = body["repair"]
        backup = self.data / "workspace" / record["backup_path"]
        self.assertEqual(backup.read_bytes(), corrupt)
        self.assertEqual(record["sha256"], __import__("hashlib").sha256(corrupt).hexdigest())
        self.assertTrue(backup.with_suffix(".json").is_file())
        self.assertEqual(yaml.safe_load(manifest.read_text())["project_id"], "P0001")
        again = self.repair("/api/projects/P0001/repair-workspace")
        self.assertEqual(again.status_code, 200)
        self.assertIsNone(again.json()["repair"])
        self.assertEqual(len(list(backup.parent.glob("*.bak"))), 1)

    def test_same_identity_drift_repair_and_unique_backups(self) -> None:
        manifest = self.make_project()
        for attempt in range(2):
            drifted = yaml.safe_load(manifest.read_text())
            drifted[f"unexpected{attempt}"] = "x"  # same identity but outside the supported shape
            manifest.write_text(yaml.safe_dump(drifted))
            self.assertEqual(self.client.get("/api/projects").json()[0]["workspace_problem"], "drift")
            self.assertEqual(self.repair("/api/projects/P0001/repair-workspace").status_code, 200)
            self.assertEqual(set(yaml.safe_load(manifest.read_text())), {"format", "project_id", "root_path"})
        backups = list((self.data / "workspace/quarantine/projects/P0001").glob("*.bak"))
        self.assertEqual(len(backups), 2)

    def test_task_manifest_repair_for_corrupt_and_shape_drift(self) -> None:
        manifest = self.make_task()
        manifest.write_bytes(b"garbage: [")
        self.assertEqual(self.repair("/api/projects/P0001/tasks/T0001/repair-workspace").status_code, 200)
        drifted = yaml.safe_load(manifest.read_text())
        drifted["unexpected"] = "x"
        manifest.write_text(yaml.safe_dump(drifted))
        self.assertEqual(self.client.get("/api/projects/P0001/tasks").json()[0]["workspace_problem"], "drift")
        self.assertEqual(self.repair("/api/projects/P0001/tasks/T0001/repair-workspace").status_code, 200)
        self.assertEqual(set(yaml.safe_load(manifest.read_text())), {"format", "task_id", "project_id"})
        self.assertEqual(len(list((self.data / "workspace/quarantine/tasks/T0001").glob("*.bak"))), 2)
        self.assertNotIn("title", yaml.safe_load(manifest.read_text()))

    def test_foreign_identity_is_protected_even_with_force(self) -> None:
        manifest = self.make_project()
        manifest.write_bytes(FOREIGN_PROJECT)
        response = self.repair("/api/projects/P0001/repair-workspace", force=True)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(manifest.read_bytes(), FOREIGN_PROJECT)
        self.assertFalse((self.data / "workspace/quarantine").exists())
        # same project_id but another root is ambiguous ownership, also foreign
        manifest.write_text(yaml.safe_dump({"format": "rosc.project.v1", "project_id": "P0001", "name": "Fixture",
                                            "root_path": "/another/root", "mode": "new", "docs_entrypoint": None}))
        before = manifest.read_bytes()
        self.assertEqual(self.repair("/api/projects/P0001/repair-workspace", force=True).status_code, 409)
        self.assertEqual(manifest.read_bytes(), before)

    def test_repair_failures_never_lose_the_original(self) -> None:
        from rosc import workspace
        manifest = self.make_project()
        original = b"broken: ["
        manifest.write_bytes(original)
        with patch.object(workspace, "_write_backup", side_effect=OSError("disk full")):
            self.assertEqual(self.repair("/api/projects/P0001/repair-workspace").status_code, 503)
        self.assertEqual(manifest.read_bytes(), original)
        with patch.object(workspace, "_atomic_replace", side_effect=OSError("io")):
            self.assertEqual(self.repair("/api/projects/P0001/repair-workspace").status_code, 503)
        self.assertEqual(manifest.read_bytes(), original)
        self.assertEqual(list(manifest.parent.glob(".rosc-*")), [], "temporary files must be cleaned up")
        with patch.object(workspace, "_dump", return_value=b"format: wrong\n"):
            self.assertEqual(self.repair("/api/projects/P0001/repair-workspace").status_code, 503)
        self.assertEqual(manifest.read_bytes(), original, "failed verification restores the original bytes")
        self.assertEqual(self.repair("/api/projects/P0001/repair-workspace").status_code, 200)

    def test_oversized_manifest_is_never_backed_up_partially_or_replaced(self) -> None:
        from rosc import workspace
        manifest = self.make_project()
        tail = b"\n# DISTINCTIVE-TAIL-BEYOND-READ-BOUNDARY\n"
        original = b"x: 1\n" + b"#" * (workspace.MAX_MANIFEST_BYTES + 10) + tail
        manifest.write_bytes(original)
        view = self.client.get("/api/projects").json()[0]
        self.assertEqual((view["workspace_state"], view["workspace_problem"]), ("needs_reconciliation", "oversized"))
        for url in ("/api/projects/P0001/repair-workspace", "/api/projects/P0001/reconcile-workspace"):
            response = self.repair(url) if url.endswith("repair-workspace") else self.client.post(url)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("manual resolution", response.json()["detail"])
            self.assertEqual(manifest.read_bytes(), original)
        self.assertFalse((self.data / "workspace/quarantine").exists(), "no truncated backup may be advertised")
        self.assertEqual(self.repair("/api/projects/P0001/repair-workspace", force=True).status_code, 409)
        self.assertEqual(manifest.read_bytes(), original)

    def test_post_replace_fsync_failure_is_reported_as_uncertain_not_intact(self) -> None:
        from rosc import workspace
        manifest = self.make_project()
        original = b"broken: ["
        manifest.write_bytes(original)
        real_replace, real_sync, replaced = os.replace, workspace._fsync_directory, []

        def replace(source, target):
            real_replace(source, target)
            replaced.append(target)

        def sync(directory):
            if replaced:  # fail only once the rename has actually happened
                raise OSError("sync failed")
            real_sync(directory)
        with patch.object(workspace.os, "replace", side_effect=replace), patch.object(workspace, "_fsync_directory", side_effect=sync):
            response = self.client.post("/api/projects/P0001/repair-workspace", json={"confirm_repair": True})
        self.assertEqual(response.status_code, 503, response.text)
        detail = response.json()["detail"]
        self.assertIn("uncertain", detail)
        self.assertNotIn("intact", detail)
        self.assertNotIn("original manifest is preserved", detail)
        self.assertEqual(yaml.safe_load(manifest.read_text())["project_id"], "P0001", "the rename really happened")
        backups = list((self.data / "workspace/quarantine/projects/P0001").glob("*.bak"))
        self.assertEqual([b.read_bytes() for b in backups], [original])
        self.assertIn(backups[0].stem, detail)
        self.assertEqual(self.client.get("/api/projects").json()[0]["workspace_state"], "ready")
        self.assertEqual(list(manifest.parent.glob(".rosc-*")), [])

    def test_failed_restore_after_verification_failure_is_reported(self) -> None:
        from rosc import workspace
        manifest = self.make_project()
        original = b"broken: ["
        manifest.write_bytes(original)
        real = workspace._atomic_replace
        calls = []

        def flaky(source, target):
            calls.append(source)
            if len(calls) == 2:
                raise OSError("restore failed")
            real(source, target)
        with patch.object(workspace, "_dump", return_value=b"format: wrong\n"), patch.object(workspace, "_atomic_replace", side_effect=flaky):
            response = self.repair("/api/projects/P0001/repair-workspace")
        self.assertEqual(response.status_code, 503)
        self.assertIn("uncertain", response.json()["detail"])
        self.assertIn("restoring the original failed", response.json()["detail"])
        self.assertEqual(manifest.read_bytes(), b"format: wrong\n")
        self.assertEqual([b.read_bytes() for b in (self.data / "workspace/quarantine/projects/P0001").glob("*.bak")], [original])

    def test_restore_does_not_overwrite_a_concurrently_changed_target(self) -> None:
        from rosc import workspace
        manifest = self.make_project()
        manifest.write_bytes(b"broken: [")
        real = workspace._atomic_replace

        def replace_then_interfere(source, target):
            real(source, target)
            target.write_bytes(b"someone: else\n")
        with patch.object(workspace, "_dump", return_value=b"format: wrong\n"), patch.object(workspace, "_atomic_replace", side_effect=replace_then_interfere):
            response = self.repair("/api/projects/P0001/repair-workspace")
        self.assertEqual(response.status_code, 503)
        self.assertIn("changed since", response.json()["detail"])
        self.assertEqual(manifest.read_bytes(), b"someone: else\n")

    # Manifest shape and identity (P006) --------------------------------
    # Replaces the former "every projected field is compared": name/docs_entrypoint/mode/status are mutable
    # SQLite-authoritative data, so comparing them produced false drift. The invariant that replaces it:
    # identity is compared with SQLite; format/keys/types are validated strictly.
    def sql(self, statement: str, *args) -> None:
        import sqlite3
        with sqlite3.connect(self.data / "rosc.db") as connection:
            connection.execute(statement, args)

    def project_view(self) -> dict:
        return self.client.get("/api/projects").json()[0]

    def task_view(self) -> dict:
        return self.client.get("/api/projects/P0001/tasks").json()[0]

    def state(self, view: dict) -> tuple:
        return view["workspace_state"], view["workspace_problem"]

    def test_new_manifests_are_identity_only_v2_and_idempotent(self) -> None:
        manifest = self.make_task()
        root = str(Path(self.base / "empty-project").resolve())
        self.assertEqual(yaml.safe_load((self.project_dir() / "project.yaml").read_text()),
                         {"format": "rosc.project.v2", "project_id": "P0001", "root_path": root})
        self.assertEqual(yaml.safe_load(manifest.read_text()), {"format": "rosc.task.v2", "task_id": "T0001", "project_id": "P0001"})
        before = (self.project_dir() / "project.yaml").read_bytes(), manifest.read_bytes()
        self.assertEqual(self.client.post("/api/projects/P0001/reconcile-workspace").status_code, 200)
        self.assertEqual(self.client.post("/api/projects/P0001/tasks/T0001/reconcile-workspace").status_code, 200)
        restarted = TestClient(create_app(self.data / "rosc.db", self.data / "workspace"))
        self.assertEqual(restarted.get("/api/projects").json()[0]["workspace_state"], "ready")
        self.assertEqual(((self.project_dir() / "project.yaml").read_bytes(), manifest.read_bytes()), before)

    def test_mutable_project_metadata_never_changes_identity_or_workspace_state(self) -> None:
        self.make_task()
        self.sql("UPDATE projects SET name = ?, docs_entrypoint = ? WHERE id = 1", "Fixtrue corrected", "docs/NEW.md")
        view = self.project_view()
        self.assertEqual((view["id"], view["name"], view["docs_entrypoint"]), ("P0001", "Fixtrue corrected", "docs/NEW.md"))
        self.assertEqual(self.state(view), ("ready", None))
        self.assertEqual(self.state(self.task_view()), ("ready", None))
        self.assertEqual(self.task_view()["id"], "T0001")
        self.assertTrue((self.project_dir() / "tasks/T0001/task.yaml").is_file())

    def test_task_title_and_status_changes_keep_parent_path_and_state(self) -> None:
        self.make_task()
        self.sql("UPDATE tasks SET title = ?, status = ? WHERE id = 1", "Renamed task", "Running")
        view = self.task_view()
        self.assertEqual((view["id"], view["title"], view["status"]), ("T0001", "Renamed task", "Running"))
        self.assertEqual(self.state(view), ("ready", None))
        self.assertEqual(self.client.get("/api/projects").json()[0]["id"], "P0001")

    def legacy_project(self, **overrides) -> dict:
        root = str(Path(self.base / "empty-project").resolve())
        return {"format": "rosc.project.v1", "project_id": "P0001", "name": "Fixture", "root_path": root,
                "mode": "new", "docs_entrypoint": None, **overrides}

    def test_legacy_v1_project_manifest_is_ready_with_stale_mutable_values(self) -> None:
        manifest = self.make_project()
        for overrides in ({}, {"name": "Old typo"}, {"docs_entrypoint": "README.md"}, {"mode": "existing"},
                          {"name": "Stale", "docs_entrypoint": "x.md", "mode": "existing"}):
            with self.subTest(overrides=overrides):
                manifest.write_text(yaml.safe_dump(self.legacy_project(**overrides)))
                before = manifest.read_bytes()
                self.assertEqual(self.state(self.project_view()), ("ready", None))
                self.assertEqual(self.client.post("/api/projects/P0001/reconcile-workspace").status_code, 200)
                self.assertEqual(manifest.read_bytes(), before, "a valid legacy manifest is never rewritten")

    def test_legacy_v1_task_manifest_is_ready_with_stale_status(self) -> None:
        manifest = self.make_task()
        self.sql("UPDATE tasks SET status = ? WHERE id = 1", "Done")
        for status in ("Draft", "Running", "anything"):
            with self.subTest(status=status):
                manifest.write_text(yaml.safe_dump({"format": "rosc.task.v1", "task_id": "T0001", "project_id": "P0001", "status": status}))
                self.assertEqual(self.state(self.task_view()), ("ready", None))

    def test_unexpected_project_shapes_are_never_ready(self) -> None:
        manifest = self.make_project()
        good = self.legacy_project()
        v2 = {"format": "rosc.project.v2", "project_id": "P0001", "root_path": good["root_path"]}
        missing = {k: v for k, v in good.items() if k != "name"}
        cases = {
            "unsupported version": ({**good, "format": "rosc.project.v0"}, "drift"),
            "future version": ({**v2, "format": "rosc.project.v3"}, "drift"),
            "non-string format": ({**good, "format": 1}, "drift"),
            "missing legacy key": (missing, "drift"),
            "extra key on v1": ({**good, "extra": 1}, "drift"),
            "extra key on v2": ({**v2, "name": "Fixture"}, "drift"),
            "bad name type": ({**good, "name": 5}, "drift"),
            "bad docs type": ({**good, "docs_entrypoint": ["a"]}, "drift"),
            "bad mode value": ({**good, "mode": "other"}, "drift"),
            "missing identity": ({k: v for k, v in v2.items() if k != "root_path"}, "corrupt"),
            "not a mapping": (["a"], "corrupt"),
        }
        for label, (content, problem) in cases.items():
            with self.subTest(label):
                manifest.write_text(yaml.safe_dump(content))
                self.assertEqual(self.state(self.project_view()), ("needs_reconciliation", problem))
        manifest.write_bytes(b"a: [unterminated")
        self.assertEqual(self.state(self.project_view()), ("needs_reconciliation", "corrupt"))

    def test_unexpected_task_shapes_are_never_ready(self) -> None:
        manifest = self.make_task()
        v1 = {"format": "rosc.task.v1", "task_id": "T0001", "project_id": "P0001", "status": "Draft"}
        v2 = {"format": "rosc.task.v2", "task_id": "T0001", "project_id": "P0001"}
        for label, content in {"unsupported": {**v2, "format": "rosc.task.v0"}, "extra v2": {**v2, "status": "Draft"},
                               "extra v1": {**v1, "title": "t"}, "missing status": {k: v for k, v in v1.items() if k != "status"},
                               "bad status type": {**v1, "status": 1}}.items():
            with self.subTest(label):
                manifest.write_text(yaml.safe_dump(content))
                self.assertEqual(self.state(self.task_view()), ("needs_reconciliation", "drift"))

    def test_identity_tampering_is_foreign_and_never_auto_replaced(self) -> None:
        manifest = self.make_project()
        good = yaml.safe_load(manifest.read_text())
        for field, value in (("project_id", "P0002"), ("root_path", str(self.base / "elsewhere"))):
            with self.subTest(field=field):
                tampered = yaml.safe_dump({**good, field: value}).encode()
                manifest.write_bytes(tampered)
                self.assertEqual(self.state(self.project_view()), ("needs_reconciliation", "foreign"))
                self.assertNotEqual(self.client.post("/api/projects/P0001/reconcile-workspace").status_code, 200)
                self.assertNotEqual(self.repair("/api/projects/P0001/repair-workspace").status_code, 200)
                self.assertEqual(manifest.read_bytes(), tampered)
        manifest.write_text(yaml.safe_dump(good))
        task_manifest = self.make_task_manifest_path()
        task_good = yaml.safe_load(task_manifest.read_text())
        for field, value in (("task_id", "T0009"), ("project_id", "P0002")):
            with self.subTest(field=field):
                tampered = yaml.safe_dump({**task_good, field: value}).encode()
                task_manifest.write_bytes(tampered)
                self.assertEqual(self.state(self.task_view()), ("needs_reconciliation", "foreign"))
                self.assertEqual(task_manifest.read_bytes(), tampered)

    def make_task_manifest_path(self) -> Path:
        self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=TASK_BODY).status_code, 201)
        return self.project_dir() / "tasks/T0001/task.yaml"

    def test_legacy_v1_with_foreign_root_or_id_stays_foreign(self) -> None:
        manifest = self.make_project()
        manifest.write_text(yaml.safe_dump(self.legacy_project(root_path="/somewhere/else")))
        self.assertEqual(self.state(self.project_view()), ("needs_reconciliation", "foreign"))

    # F3 ----------------------------------------------------------------
    def test_independent_processes_cannot_register_overlapping_roots(self) -> None:
        import subprocess
        import sys
        import time
        for round_index in range(6):
            parent = self.base / f"race{round_index}"
            child = parent / "child"
            child.mkdir(parents=True)
            (child / "f").write_text("x")
            start = str(time.time() + 1.5)
            procs = [subprocess.Popen([sys.executable, "-c", RACE_WORKER, str(self.data / "rosc.db"), str(self.data / "workspace"), str(root), start],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy())
                     for root in (parent, child)]
            outputs = [p.communicate(timeout=60) for p in procs]
            codes = [out[0].strip() for out in outputs]
            self.assertTrue(all(code in {"201", "409", "503"} for code in codes), (codes, outputs))
            self.assertLessEqual(codes.count("201"), 1, codes)
        roots = [Path(p["root_path"]) for p in self.client.get("/api/projects").json()]
        for one in roots:
            for other in roots:
                if one != other:
                    self.assertFalse(one in other.parents, (one, other))

    def test_lock_contention_returns_controlled_503_and_registers_nothing(self) -> None:
        import sqlite3
        client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace", busy_timeout_ms=50))
        blocker = sqlite3.connect(self.data / "rosc.db", isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            response = client.post("/api/projects", json={"name": "n", "description": "d", "root_path": str(self.new_root()), "mode": "new"})
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(client.get("/api/projects").json(), [])

    def test_siblings_and_symlinked_overlaps_keep_working(self) -> None:
        one, two = self.base / "app", self.base / "app-two"
        one.mkdir()
        two.mkdir()
        self.assertEqual(self.register(one).status_code, 201)
        self.assertEqual(self.register(two).status_code, 201)
        link = self.base / "link-to-app"
        link.symlink_to(one)
        self.assertEqual(self.register(link).status_code, 409)

    # F5 / F6 ------------------------------------------------------------
    def test_only_canonical_external_ids_are_accepted(self) -> None:
        from rosc.app import numeric_id
        from fastapi import HTTPException
        self.make_task()
        for bad in ("P1", "P00001", "P0001 ", " P0001", "p0001", "P\u0661\u0662\u0663\u0664", "P０００１", "T0001", "P-001", "P0001/", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(HTTPException):
                    numeric_id(bad, "P")
        for bad in ("T1", "T00001", "T\u0661\u0662\u0663\u0664", "P0001"):
            with self.subTest(task=bad):
                self.assertEqual(self.client.post(f"/api/projects/P0001/tasks/{bad}/reconcile-workspace").status_code, 404)
        self.assertEqual(self.client.get("/api/projects/P1/tasks").status_code, 404)
        self.assertEqual(self.client.get("/api/projects/P00001/tasks").status_code, 404)
        self.assertEqual(self.client.get("/api/projects/P0001/tasks").status_code, 200)
        self.assertEqual(numeric_id("P0001", "P"), 1)
        self.assertEqual(numeric_id("P9999", "P"), 9999)
        self.assertEqual(numeric_id("P10000", "P"), 10000)
        self.assertEqual(numeric_id("T123456", "T"), 123456)
        self.assertEqual(self.client.get("/api/projects/P10000/tasks").status_code, 404)

    def test_ids_above_9999_round_trip_as_emitted(self) -> None:
        from rosc.models import Project
        from rosc.app import numeric_id, project_view
        project = Project(id=12345, name="n", description="d", root_path="/x", mode="new", workspace_state="ready",
                          created_at=__import__("datetime").datetime(2026, 1, 1))
        emitted = project_view(project, self.data / "workspace")["id"]
        self.assertEqual(emitted, "P12345")
        self.assertEqual(numeric_id(emitted, "P"), 12345)

    def test_created_at_is_explicit_utc_everywhere(self) -> None:
        self.make_task()
        created_project = self.client.get("/api/projects").json()[0]["created_at"]
        task = self.client.get("/api/projects/P0001/tasks").json()[0]
        for value in (created_project, task["created_at"]):
            self.assertRegex(value, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
        restarted = TestClient(create_app(self.data / "rosc.db", self.data / "workspace"))
        self.assertEqual(restarted.get("/api/projects").json()[0]["created_at"], created_project)
        self.assertEqual(restarted.get("/api/projects/P0001/tasks").json()[0]["created_at"], task["created_at"])
        second = self.base / "second"
        second.mkdir()
        response = self.register(second)
        self.assertRegex(response.json()["created_at"], r"Z$")
        listed = {p["id"]: p["created_at"] for p in self.client.get("/api/projects").json()}
        self.assertEqual(response.json()["created_at"], listed["P0002"])

    def test_registration_and_task_creation_start_no_agent_and_touch_no_source(self) -> None:
        root = self.new_root()
        self.assertEqual(self.register(root).status_code, 201)
        self.client.post("/api/projects/P0001/tasks", json=TASK_BODY)
        self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(list((self.data / "workspace/projects/P0001/architect").iterdir()), [])
        from sqlalchemy import create_engine, inspect
        tables = set(inspect(create_engine(f"sqlite:///{self.data / 'rosc.db'}")).get_table_names())
        self.assertEqual(tables, {"projects", "tasks", "alembic_version"})


if __name__ == "__main__":
    unittest.main()
