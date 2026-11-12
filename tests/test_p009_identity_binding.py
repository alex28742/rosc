from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from unittest.mock import patch

import yaml
from alembic import command
from fastapi.testclient import TestClient

import test_slice01 as base
from rosc.app import create_app
from rosc.architect_store import ArchitectDocumentService, UnsafeStore
from rosc.authorization import LocalOwnerPolicy, LocalOwnerProvider
from rosc.db import make_session_factory
from rosc.models import (BindingStateError, Project, ProjectRootBinding, ProjectUUIDError,
                         validate_project_uuid)
from rosc.workspace import provision_project

ACTOR = LocalOwnerProvider().actor()


AllowAll = LocalOwnerPolicy  # explicit, narrow default-deny policy for the local Owner actor


class IdentityCase(base.StageCase):
    """Migration-managed cases: the base class already migrates a fresh database to head."""

    def sql(self, statement: str, *args) -> None:
        with sqlite3.connect(self.data / "rosc.db") as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(statement, args)

    def fetch(self, statement: str, *args) -> list[tuple]:
        with sqlite3.connect(self.data / "rosc.db") as connection:
            return connection.execute(statement, args).fetchall()

    def api(self) -> TestClient:
        return TestClient(create_app(self.data / "rosc.db", self.data / "workspace"), base_url="http://localhost")

    def make_root(self, name: str, *files: str) -> Path:
        path = self.base / name
        path.mkdir()
        for file_name in files:
            (path / file_name).write_text("content\n")
        return path

    def project_view(self, project_id: str = "P0001") -> dict:
        return self.api().get("/api/projects").json()[int(project_id[1:]) - 1]

    def manifest(self, project_id: str = "P0001") -> Path:
        return self.data / "workspace" / "projects" / project_id / "project.yaml"

    def binding_rows(self, project_id: int = 1) -> list[tuple]:
        return self.fetch("SELECT id, project_id, version, root_path, state FROM project_root_bindings "
                          "WHERE project_id = ? ORDER BY version", project_id)


class FreshRegistrationTests(IdentityCase):
    def test_registration_creates_uuid_binding_and_v3_manifest_once(self) -> None:
        root = self.new_root()
        self.assertEqual(self.register(root).status_code, 201)
        created = self.project_view()
        self.assertEqual(uuid.UUID(created["project_uuid"]).version, 4)
        self.assertEqual(created["root_path"], str(root.resolve()))
        self.assertEqual(created["registration_root_path"], str(root.resolve()))
        self.assertEqual(set(created["root_binding"]),
                         {"binding_id", "version", "root_path", "docs_entrypoint", "mode", "state", "created_at"})
        self.assertEqual((created["root_binding"]["version"], created["root_binding"]["state"]), (1, "current"))
        self.assertEqual(created["root_binding"]["root_path"], str(root.resolve()))
        payload = yaml.safe_load(self.manifest().read_text())
        self.assertEqual(payload, {"format": "rosc.project.v3", "project_id": "P0001",
                                   "project_uuid": created["project_uuid"]})
        self.assertEqual(self.binding_rows(), [(1, 1, 1, str(root.resolve()), "current")])
        self.assertEqual(list(root.iterdir()), [], "the project source folder is never written")

        # Restart and a re-run of the migration keep every identity value.
        self.assertEqual(self.api().get("/api/projects").json()[0]["project_uuid"], created["project_uuid"])
        command.upgrade(self.alembic, "head")
        again = self.api().get("/api/projects").json()[0]
        self.assertEqual((again["project_uuid"], again["root_binding"]["binding_id"], again["root_binding"]["version"]),
                         (created["project_uuid"], created["root_binding"]["binding_id"], 1))

    def test_single_migration_head(self) -> None:
        from alembic.script import ScriptDirectory
        self.assertEqual(ScriptDirectory.from_config(self.alembic).get_heads(), ["0013_execution_package_core"])

    def test_read_model_adds_identity_and_keeps_legacy_root_path(self) -> None:
        root = self.new_root()
        created = self.register(root).json()
        self.assertEqual(created["root_path"], created["registration_root_path"],
                         "the legacy field keeps its previous meaning")
        listed = self.api().get("/api/projects").json()[0]
        self.assertEqual(listed, created)
        self.assertEqual(listed["architect"], "not_active")
        self.assertEqual(listed["active_workers"], 0)
        self.assertEqual(self.api().get("/api/health").json()["agent_runtime"], "not_started")

    def test_no_binding_mutation_route_or_agent_launch_is_added(self) -> None:
        app = create_app(self.data / "rosc.db", self.data / "workspace")
        paths = {route.path for route in app.routes if hasattr(route, "path")}
        # P011 adds Owner-only Agent registry/assignment administration; it must still add no activation, binding or
        # execution route (no RuntimeBinding, session, Run or Worker launch surface).
        for forbidden in ("binding", "rebind", "unlink", "archive", "run", "worker", "session", "runtime"):
            self.assertFalse([path for path in paths if forbidden in path], (forbidden, sorted(paths)))
        self.assertIn("/api/projects/{project_id}/tasks", paths)
        self.assertIn("/api/agents", paths)

    def test_second_current_binding_is_rejected_by_the_database(self) -> None:
        self.assertFalse(self.register(self.new_root()).status_code >= 400)
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO project_root_bindings (project_id, version, root_path, docs_entrypoint, mode, state, created_at)"
                     " VALUES (1, 2, ?, NULL, 'new', 'current', '2026-01-01 00:00:00')", str(self.base / "other"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO project_root_bindings (project_id, version, root_path, docs_entrypoint, mode, state, created_at)"
                     " VALUES (999, 1, ?, NULL, 'new', 'current', '2026-01-01 00:00:00')", str(self.base / "orphan"))

    def test_in_memory_duplicate_current_bindings_fail_closed(self) -> None:
        project = Project(id=1, name="n", description="d", root_path="/x", registration_root_path="/x",
                          project_uuid=str(uuid.uuid4()), mode="new", workspace_state="ready")
        project.bindings = [ProjectRootBinding(version=1, root_path="/x", mode="new", state="current"),
                            ProjectRootBinding(version=2, root_path="/x", mode="new", state="current")]
        with self.assertRaises(BindingStateError):
            project.current_binding


class RegistrationAtomicityTests(IdentityCase):
    def test_failed_commit_leaves_no_half_registered_project(self) -> None:
        self.assertEqual(self.register(self.make_root("atomic-one")).status_code, 201)
        first = self.project_view()
        with patch("rosc.app.uuid.uuid4", return_value=uuid.UUID(first["project_uuid"])):
            response = self.register(self.make_root("atomic-two"))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(len(self.api().get("/api/projects").json()), 1)
        self.assertEqual(self.binding_rows(2), [])
        self.assertFalse((self.data / "workspace/projects/P0002").exists(), "no Workspace is published for a failed insert")


class RootAvailabilityTests(IdentityCase):
    def test_duplicate_and_nested_roots_still_conflict(self) -> None:
        root = self.make_root("root", "a.txt")
        self.assertEqual(self.register(root, "existing").status_code, 201)
        nested = root / "child"
        nested.mkdir()
        (nested / "b.txt").write_text("x")
        self.assertEqual(self.register(nested, "existing").status_code, 409)
        parent = self.base / "parent-root"
        (parent / "sub").mkdir(parents=True)
        (parent / "c.txt").write_text("x")
        self.assertEqual(self.register(parent, "existing").status_code, 201)
        (parent / "sub" / "d.txt").write_text("x")
        self.assertEqual(self.register(parent / "sub", "existing").status_code, 409)

    def test_historical_binding_does_not_reserve_a_root(self) -> None:
        root = self.make_root("root", "a.txt")
        self.assertEqual(self.register(root, "existing").status_code, 201)
        released = self.make_root("released-root", "b.txt")
        self.sql("UPDATE project_root_bindings SET state = 'superseded' WHERE project_id = 1")
        self.sql("INSERT INTO project_root_bindings (project_id, version, root_path, docs_entrypoint, mode, state, created_at)"
                 " VALUES (1, 2, ?, NULL, 'existing', 'current', '2026-01-01 00:00:00')", str(released))
        # A child of the former root is free again, because the historical binding no longer reserves it.
        child = root / "child"
        child.mkdir()
        (child / "c.txt").write_text("x")
        self.assertEqual(self.register(child, "existing").status_code, 201)
        # Reusing the exact old root is still blocked by the documented transitional `projects.root_path` UNIQUE.
        self.assertEqual(self.register(root, "existing").status_code, 409)

    def test_missing_or_contradictory_binding_fails_closed(self) -> None:
        self.assertEqual(self.register(self.make_root("bound", "a.txt"), "existing").status_code, 201)
        self.sql("DELETE FROM project_root_bindings WHERE project_id = 1")
        response = self.register(self.make_root("unbound", "a.txt"), "existing")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.binding_rows(1), [])

    def test_binding_that_disagrees_with_registration_root_fails_closed(self) -> None:
        self.assertEqual(self.register(self.make_root("bound", "a.txt"), "existing").status_code, 201)
        self.sql("UPDATE project_root_bindings SET root_path = ? WHERE project_id = 1", str(self.base / "tampered"))
        self.assertEqual(self.register(self.make_root("unbound", "a.txt"), "existing").status_code, 503)


class ExistingProjectIntegrityTests(IdentityCase):
    task = {"title": "Must not land", "description": "d", "acceptance_criteria": "a"}

    def project_files(self) -> dict[str, bytes]:
        directory = self.data / "workspace/projects/P0001"
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def service(self) -> ArchitectDocumentService:
        return ArchitectDocumentService(make_session_factory(self.data / "rosc.db"),
                                         self.data / "workspace", AllowAll())

    def assert_identity_error(self, response) -> None:
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"],
                         "Project identity records are inconsistent; manual reconciliation is required")

    def assert_mutations_rejected_without_side_effects(self, before: dict[str, bytes]) -> None:
        api = self.api()
        self.assertEqual(api.post("/api/projects/P0001/tasks", json=self.task).status_code, 503)
        self.assertEqual(api.post("/api/projects/P0001/reconcile-workspace").status_code, 503)
        self.assertEqual(api.post("/api/projects/P0001/repair-workspace",
                                  json={"confirm_repair": True}).status_code, 503)
        with self.assertRaises(UnsafeStore):
            self.service().write_document(ACTOR, 1, "blocked", "must not land")
        self.assertEqual(self.fetch("SELECT id FROM tasks"), [])
        self.assertEqual(self.project_files(), before)
        self.assertFalse((self.data / "workspace/projects/P0001/architect/documents").exists())
        self.assertFalse((self.data / "workspace/quarantine").exists())

    def test_missing_current_binding_fails_closed_everywhere(self) -> None:
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        before = self.project_files()
        self.sql("DELETE FROM project_root_bindings WHERE project_id = 1")
        response = self.api().get("/api/projects")
        self.assert_identity_error(response)
        self.assert_mutations_rejected_without_side_effects(before)
        self.assertEqual(self.binding_rows(), [], "Workspace recovery must not manufacture a binding")

    def test_contradictory_initial_binding_fails_closed_everywhere(self) -> None:
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        before = self.project_files()
        original_manifest = self.manifest().read_bytes()
        self.sql("UPDATE project_root_bindings SET root_path = ? WHERE project_id = 1",
                 str(self.base / "contradictory-root"))
        response = self.api().get("/api/projects")
        self.assert_identity_error(response)
        self.assert_mutations_rejected_without_side_effects(before)
        self.assertEqual(self.manifest().read_bytes(), original_manifest)

    def test_invalid_db_uuid_is_never_an_operational_identity(self) -> None:
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        before = self.project_files()
        invalid_values = ("not-a-uuid", str(uuid.uuid1()), str(uuid.uuid4()).upper(), uuid.uuid4().hex)
        for value in invalid_values:
            with self.subTest(value=value):
                self.sql("UPDATE projects SET project_uuid = ? WHERE id = 1", value)
                self.assert_identity_error(self.api().get("/api/projects"))
                self.assert_mutations_rejected_without_side_effects(before)

    def test_invalid_uuid_cannot_rebuild_a_missing_manifest(self) -> None:
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        self.sql("UPDATE projects SET project_uuid = 'not-a-uuid' WHERE id = 1")
        self.manifest().unlink()
        before = self.project_files()
        api = self.api()
        self.assertEqual(api.post("/api/projects/P0001/reconcile-workspace").status_code, 503)
        self.assertEqual(api.post("/api/projects/P0001/repair-workspace",
                                  json={"confirm_repair": True}).status_code, 503)
        self.assertFalse(self.manifest().exists())
        self.assertEqual(self.project_files(), before)
        self.assertFalse((self.data / "workspace/quarantine").exists())

    def test_invalid_uuid_guard_precedes_workspace_directory_publication(self) -> None:
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        self.sql("UPDATE projects SET project_uuid = 'not-a-uuid' WHERE id = 1")
        isolated = self.data / "isolated-workspace"
        sessions = make_session_factory(self.data / "rosc.db")
        with sessions() as session:
            with self.assertRaises(ProjectUUIDError):
                provision_project(isolated, session.get(Project, 1))
        self.assertFalse(isolated.exists())

    def test_matching_malformed_v3_manifest_does_not_validate_corrupt_db_identity(self) -> None:
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        self.sql("UPDATE projects SET project_uuid = 'not-a-uuid' WHERE id = 1")
        malformed = yaml.safe_dump({"format": "rosc.project.v3", "project_id": "P0001",
                                    "project_uuid": "not-a-uuid"}).encode()
        self.manifest().write_bytes(malformed)
        self.assert_identity_error(self.api().get("/api/projects"))
        self.assert_mutations_rejected_without_side_effects(self.project_files())
        self.assertEqual(self.manifest().read_bytes(), malformed)

    def test_invalid_uuid_preserves_legacy_manifests_until_db_identity_is_restored(self) -> None:
        root_path = self.new_root()
        self.assertEqual(self.register(root_path).status_code, 201)
        original_uuid = self.fetch("SELECT project_uuid FROM projects WHERE id = 1")[0][0]
        root = str(root_path.resolve())
        candidates = (
            {"format": "rosc.project.v1", "project_id": "P0001", "name": "Legacy", "root_path": root,
             "mode": "new", "docs_entrypoint": None},
            {"format": "rosc.project.v2", "project_id": "P0001", "root_path": root},
        )
        for payload in candidates:
            with self.subTest(format=payload["format"]):
                raw = yaml.safe_dump(payload).encode()
                self.manifest().write_bytes(raw)
                self.sql("UPDATE projects SET project_uuid = 'not-a-uuid' WHERE id = 1")
                self.assert_identity_error(self.api().get("/api/projects"))
                self.assertEqual(self.api().post("/api/projects/P0001/reconcile-workspace").status_code, 503)
                self.assertEqual(self.api().post("/api/projects/P0001/repair-workspace",
                                                 json={"confirm_repair": True}).status_code, 503)
                self.assertEqual(self.manifest().read_bytes(), raw)
                self.sql("UPDATE projects SET project_uuid = ? WHERE id = 1", original_uuid)
                self.assertEqual(self.api().get("/api/projects").json()[0]["workspace_state"], "ready")
                self.assertEqual(self.manifest().read_bytes(), raw)

    def test_task_workspace_mutations_require_valid_parent_identity(self) -> None:
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        created = self.api().post("/api/projects/P0001/tasks", json=self.task)
        self.assertEqual(created.status_code, 201, created.text)
        task_manifest = self.data / "workspace/projects/P0001/tasks/T0001/task.yaml"
        before = task_manifest.read_bytes()
        self.sql("UPDATE projects SET project_uuid = 'not-a-uuid' WHERE id = 1")
        self.assertEqual(self.api().post("/api/projects/P0001/tasks/T0001/reconcile-workspace").status_code, 503)
        self.assertEqual(self.api().post("/api/projects/P0001/tasks/T0001/repair-workspace",
                                         json={"confirm_repair": True}).status_code, 503)
        self.assertEqual(task_manifest.read_bytes(), before)

    def test_uuid_validator_accepts_only_canonical_rfc4122_v4(self) -> None:
        valid = str(uuid.uuid4())
        self.assertEqual(validate_project_uuid(valid), valid)
        for value in (None, 1, "", "not-a-uuid", str(uuid.uuid1()), str(uuid.uuid3(uuid.NAMESPACE_DNS, "x")),
                      str(uuid.uuid5(uuid.NAMESPACE_DNS, "x")), valid.upper(), uuid.UUID(valid).hex):
            with self.subTest(value=value), self.assertRaises(ProjectUUIDError):
                validate_project_uuid(value)


class LegacyUpgradeTests(IdentityCase):
    def seed_0001(self) -> tuple[Path, Path, bytes, bytes]:
        """A real 0001 database with two Projects, a Task and v1/v2 Project manifests."""
        command.downgrade(self.alembic, "0001_project_task")
        one, two = self.base / "legacy-one", self.base / "legacy-two"
        one.mkdir()
        two.mkdir()
        (one / "README.md").write_text("existing documentation\n")
        self.sql("INSERT INTO projects (id, name, description, root_path, mode, docs_entrypoint, workspace_state, created_at)"
                 " VALUES (1, 'Legacy One', 'd', ?, 'existing', 'README.md', 'ready', '2026-01-01 00:00:00')", str(one))
        self.sql("INSERT INTO projects (id, name, description, root_path, mode, docs_entrypoint, workspace_state, created_at)"
                 " VALUES (2, 'Legacy Two', 'd', ?, 'new', NULL, 'needs_reconciliation', '2026-01-02 00:00:00')", str(two))
        self.sql("INSERT INTO tasks (id, project_id, title, description, acceptance_criteria, status, workspace_state, created_at)"
                 " VALUES (1, 1, 'Legacy task', 'd', 'a', 'Draft', 'ready', '2026-01-03 00:00:00')")
        for project_id, payload in (
                ("P0001", {"format": "rosc.project.v1", "project_id": "P0001", "name": "Legacy One",
                           "root_path": str(one), "mode": "existing", "docs_entrypoint": "README.md"}),
                ("P0002", {"format": "rosc.project.v2", "project_id": "P0002", "root_path": str(two)})):
            path = self.data / "workspace/projects" / project_id / "project.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(payload))
        task_path = self.data / "workspace/projects/P0001/tasks/T0001/task.yaml"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text(yaml.safe_dump({"format": "rosc.task.v2", "task_id": "T0001", "project_id": "P0001"}))
        return one, two, self.manifest("P0001").read_bytes(), self.manifest("P0002").read_bytes()

    def test_upgrade_backfills_identity_and_preserves_legacy_manifests(self) -> None:
        one, two, v1_bytes, v2_bytes = self.seed_0001()
        command.upgrade(self.alembic, "head")
        projects = self.api().get("/api/projects").json()
        self.assertEqual([p["id"] for p in projects], ["P0001", "P0002"])
        self.assertEqual(len({p["project_uuid"] for p in projects}), 2, "each Project gets its own UUID")
        for project in projects:
            self.assertEqual(uuid.UUID(project["project_uuid"]).version, 4)
        # v1 and v2 readers accept the untouched legacy files.
        self.assertEqual((projects[0]["workspace_state"], projects[0]["workspace_problem"]), ("ready", None))
        self.assertEqual((projects[1]["workspace_state"], projects[1]["workspace_problem"]), ("needs_reconciliation", "pending"))
        self.assertEqual(self.manifest("P0001").read_bytes(), v1_bytes, "a valid v1 manifest is never upgraded")
        self.assertEqual(self.manifest("P0002").read_bytes(), v2_bytes, "a valid v2 manifest is never upgraded")
        self.assertEqual([p["registration_root_path"] for p in projects], [str(one), str(two)])
        for project_id in (1, 2):
            rows = self.binding_rows(project_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0][2], rows[0][4]), (1, "current"))
            self.assertEqual(rows[0][3], projects[project_id - 1]["registration_root_path"])
        self.assertEqual([t["id"] for t in self.api().get("/api/projects/P0001/tasks").json()], ["T0001"])
        self.assertEqual((one / "README.md").read_text(), "existing documentation\n")

        # Backfill is stable: a repeated upgrade and a restart never regenerate identity.
        uuids = [p["project_uuid"] for p in projects]
        command.upgrade(self.alembic, "head")
        self.assertEqual([p["project_uuid"] for p in self.api().get("/api/projects").json()], uuids)

        # A v3 manifest carrying another Project's UUID is foreign and is left untouched.
        foreign = yaml.safe_dump({"format": "rosc.project.v3", "project_id": "P0002",
                                  "project_uuid": str(uuid.uuid4())}).encode()
        self.manifest("P0002").write_bytes(foreign)
        self.assertEqual(self.project_view("P0002")["workspace_problem"], "foreign")
        self.assertEqual(self.manifest("P0002").read_bytes(), foreign)
        self.assertEqual(self.api().post("/api/projects/P0002/reconcile-workspace").status_code, 409)
        self.assertEqual(self.manifest("P0002").read_bytes(), foreign)

    def test_downgrade_removes_identity_without_deleting_rows(self) -> None:
        self.seed_0001()
        command.upgrade(self.alembic, "head")
        command.downgrade(self.alembic, "0001_project_task")
        tables = {row[0] for row in self.fetch("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("project_root_bindings", tables)
        self.assertEqual([row[0] for row in self.fetch("SELECT id FROM projects ORDER BY id")], [1, 2])
        self.assertEqual([row[0] for row in self.fetch("SELECT id FROM tasks")], [1])
        command.upgrade(self.alembic, "head")
        self.assertEqual([row[2] for row in self.fetch("SELECT project_id, version, state FROM project_root_bindings "
                                                       "ORDER BY project_id")], ["current", "current"])


class ManifestCompatibilityTests(IdentityCase):
    def test_missing_manifest_is_recreated_as_v3_with_the_same_identity(self) -> None:
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        created = self.project_view()
        self.manifest().unlink()
        self.assertEqual(self.project_view()["workspace_problem"], "missing")
        response = self.api().post("/api/projects/P0001/reconcile-workspace")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(yaml.safe_load(self.manifest().read_text()),
                         {"format": "rosc.project.v3", "project_id": "P0001", "project_uuid": created["project_uuid"]})
        self.assertEqual(response.json()["workspace_state"], "ready")

    def test_legacy_manifests_stay_untouched_and_are_never_adopted(self) -> None:
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        root = self.project_view()["registration_root_path"]
        legacy = {"format": "rosc.project.v1", "project_id": "P0001", "name": "Old", "root_path": root,
                  "mode": "new", "docs_entrypoint": None}
        self.manifest().write_text(yaml.safe_dump(legacy))
        before = self.manifest().read_bytes()
        self.assertEqual(self.api().post("/api/projects/P0001/reconcile-workspace").status_code, 200)
        self.assertEqual(self.manifest().read_bytes(), before)
        # A legacy file naming another root is foreign, and explicit repair never rewrites it.
        self.manifest().write_text(yaml.safe_dump({**legacy, "root_path": str(self.base / "elsewhere")}))
        foreign = self.manifest().read_bytes()
        self.assertEqual(self.project_view()["workspace_problem"], "foreign")
        self.assertEqual(self.api().post("/api/projects/P0001/repair-workspace", json={"confirm_repair": True}).status_code, 409)
        self.assertEqual(self.manifest().read_bytes(), foreign)

    def test_v3_drift_repair_rewrites_the_current_format_with_a_backup(self) -> None:
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        created = self.project_view()
        drifted = {**yaml.safe_load(self.manifest().read_text()), "root_path": created["root_path"]}
        self.manifest().write_text(yaml.safe_dump(drifted))
        self.assertEqual(self.project_view()["workspace_problem"], "drift")
        response = self.api().post("/api/projects/P0001/repair-workspace", json={"confirm_repair": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(yaml.safe_load(self.manifest().read_text())), {"format", "project_id", "project_uuid"})
        backup = self.data / "workspace" / response.json()["repair"]["backup_path"]
        self.assertEqual(yaml.safe_load(backup.read_text()), drifted)

    def test_tampered_v3_uuid_and_copied_manifest_are_rejected(self) -> None:
        self.assertEqual(self.register(self.make_root("first", "a.txt"), "existing").status_code, 201)
        self.assertEqual(self.register(self.make_root("second", "b.txt"), "existing").status_code, 201)
        original = self.manifest().read_bytes()
        for label, payload in {
            "tampered uuid": {**yaml.safe_load(original), "project_uuid": str(uuid.uuid4())},
            "copied from another project": yaml.safe_load(self.manifest("P0002").read_text()),
            "malformed uuid type": {**yaml.safe_load(original), "project_uuid": 1},
        }.items():
            with self.subTest(label):
                candidate = yaml.safe_dump(payload).encode()
                self.manifest().write_bytes(candidate)
                self.assertIn(self.project_view()["workspace_problem"], ("foreign", "drift"))
                self.assertEqual(self.api().post("/api/projects/P0001/reconcile-workspace").status_code, 409)
                self.assertEqual(self.manifest().read_bytes(), candidate)
        self.manifest().write_bytes(original)
        self.assertEqual(self.project_view()["workspace_state"], "ready")

    def test_task_creation_and_architect_roundtrip_work_with_v3(self) -> None:
        root = self.new_root()
        self.assertEqual(self.register(root).status_code, 201)
        task = self.api().post("/api/projects/P0001/tasks", json={
            "title": "Check v3", "description": "d", "acceptance_criteria": "a"})
        self.assertEqual(task.status_code, 201, task.text)
        service = ArchitectDocumentService(make_session_factory(self.data / "rosc.db"), self.data / "workspace", AllowAll())
        written = service.write_document(ACTOR, 1, "identity-notes", "# Notes\n")
        self.assertEqual(written.revision, 1)
        self.assertEqual(service.read_document(ACTOR, 1, "identity-notes").content, b"# Notes\n")
        self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
