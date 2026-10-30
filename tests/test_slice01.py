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


class FoundationSliceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
