from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

from alembic import command
from fastapi.testclient import TestClient

import test_slice01 as base
from rosc import authorization as authz
from rosc.agent_registry import (CAPABILITY_ARCHITECT_DOCUMENT_READ, CAPABILITY_ARCHITECT_DOCUMENT_WRITE,
                                 CAPABILITY_PROJECT_CONTEXT_READ, CAPABILITY_TASK_CONTEXT_READ, CAPABILITY_TASK_ARTIFACT_PUBLISH,
                                 AgentRegistryService, InvalidRegistryInput, RegistryDenied)
from rosc.app import create_app
from rosc.architect_store import ArchitectDocumentService, AuthorizationDenied
from rosc.authorization import (ActorContext, AuthorizationDecision, LocalOwnerPolicy, LocalOwnerProvider, ResourceScope,
                                evaluate)
from rosc.db import make_session_factory

OWNER = LocalOwnerProvider().actor()
TASK = {"title": "T", "description": "D", "acceptance_criteria": "A"}
CAPS_ARCHITECT = sorted({CAPABILITY_PROJECT_CONTEXT_READ, CAPABILITY_ARCHITECT_DOCUMENT_READ,
                         CAPABILITY_ARCHITECT_DOCUMENT_WRITE})
CAPS_WORKER = sorted({CAPABILITY_PROJECT_CONTEXT_READ, CAPABILITY_TASK_CONTEXT_READ})
M2B_ACTIONS = frozenset({"project.list", "project.register", "project.reconcile", "project.repair", "task.list",
                         "task.create", "task.reconcile", "task.repair", "architect_document.list",
                         "architect_document.read", "architect_document.write", "architect_document.reconcile"})


def agent_body(**overrides) -> dict:
    body = {"display_name": "Architect One", "icon_label": None, "runtime_adapter": "manual",
            "provider_id": "local", "model_id": "model-a", "capabilities": CAPS_ARCHITECT}
    body.update(overrides)
    return body


def update_body(display_name: str, version: int, **overrides) -> dict:
    body = {"expected_version": version, **agent_body(display_name=display_name)}
    body.update(overrides)
    return body


def m2b_scope(action: str) -> ResourceScope:
    required, _ = authz._SHAPES[action]
    return ResourceScope(project_id=1 if "project_id" in required else None,
                         task_id=1 if "task_id" in required else None,
                         doc_id="notes" if "doc_id" in required else None)


class AgentProvider:
    """An agent-kind actor claiming registered-agent authority without any trusted runtime."""

    def __init__(self, agent_id: str = "agent:claim") -> None:
        self._agent_id = agent_id

    def actor(self) -> ActorContext:
        return ActorContext(authz.ACTOR_AGENT, self._agent_id, authz.ASSURANCE_LOCAL_UNVERIFIED)


class DenyAll:
    def decide(self, actor, action, resource):
        return AuthorizationDecision(False, "test_denied")


ASSIGNMENT_RACE = """
import json, sys, time
from pathlib import Path
from rosc.agent_registry import AgentRegistryService, RegistryConflict, RegistryStoreError
from rosc.authorization import LocalOwnerPolicy, LocalOwnerProvider
from rosc.db import make_session_factory
db, start, agent_id, role, caps_json, task = sys.argv[1:7]
service = AgentRegistryService(make_session_factory(Path(db), busy_timeout_ms=5000), LocalOwnerPolicy())
time.sleep(max(0.0, float(start) - time.time()))
try:
    created = service.create_assignment(LocalOwnerProvider().actor(), 1, agent_id=agent_id, role=role,
                                        capabilities=json.loads(caps_json),
                                        task_id=None if task == '-' else int(task))
    print('created', created['assignment_id'])
except RegistryConflict:
    print('conflict')
except RegistryStoreError:
    print('busy')
"""


class RegistryCase(base.StageCase):
    TABLES = ("projects", "project_root_bindings", "tasks", "agent_definitions", "agent_capabilities",
              "role_assignments", "assignment_capabilities")

    def api(self, **kwargs) -> TestClient:
        return TestClient(create_app(self.data / "rosc.db", self.data / "workspace", **kwargs), base_url="http://localhost")

    def service(self, policy=None) -> AgentRegistryService:
        return AgentRegistryService(make_session_factory(self.data / "rosc.db"), policy or LocalOwnerPolicy())

    def sql(self, statement: str, *args) -> None:
        with sqlite3.connect(self.data / "rosc.db") as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(statement, args)

    def fetch(self, statement: str) -> list[tuple]:
        with sqlite3.connect(self.data / "rosc.db") as connection:
            return connection.execute(statement).fetchall()

    def table_names(self) -> set[str]:
        return {row[0] for row in self.fetch("SELECT name FROM sqlite_master WHERE type='table'")}

    def state(self) -> dict:
        """Every P011-owned and pre-existing row plus the exact Workspace bytes."""
        workspace = self.data / "workspace"
        tree = ({str(path.relative_to(workspace)): (path.read_bytes() if path.is_file() else None)
                 for path in sorted(workspace.rglob("*"))} if workspace.exists() else {})
        with sqlite3.connect(self.data / "rosc.db") as connection:
            rows = {table: connection.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid").fetchall()
                    for table in self.TABLES}
        return {"tree": tree, "rows": rows}

    def project(self, name: str = "empty-project") -> Path:
        path = self.base / name
        path.mkdir(exist_ok=True)
        self.assertEqual(self.register(path).status_code, 201)
        return path

    def task(self, project: str = "P0001") -> dict:
        response = self.client.post(f"/api/projects/{project}/tasks", json=TASK)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def register_agent(self, **overrides) -> dict:
        response = self.client.post("/api/agents", json=agent_body(**overrides))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def create_assignment(self, agent_id: str, role: str, capabilities, *, task_id: str | None = None,
                          project: str = "P0001") -> dict:
        body = {"agent_id": agent_id, "role": role, "capabilities": list(capabilities)}
        if task_id is not None:
            body["task_id"] = task_id
        response = self.client.post(f"/api/projects/{project}/assignments", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def patch_agent(self, agent_id: str, version: int, **overrides):
        return self.client.patch(f"/api/agents/{agent_id}", json=update_body("Edited", version, **overrides))


class AgentIdentityTests(RegistryCase):
    def test_ids_are_unique_canonical_and_survive_restart_and_edits(self) -> None:
        first = self.register_agent()
        second = self.register_agent(display_name="Worker One", capabilities=CAPS_WORKER)
        ids = [first["agent_id"], second["agent_id"]]
        self.assertNotEqual(*ids)
        for value in ids:
            self.assertEqual(uuid.UUID(value).version, 4)
            self.assertEqual(str(uuid.UUID(value)), value, "the stored id must be canonical, not normalized on read")
        restarted = self.api()
        listed = restarted.get("/api/agents").json()
        self.assertEqual({agent["agent_id"] for agent in listed}, set(ids))
        self.assertEqual(restarted.get(f"/api/agents/{ids[0]}").json()["display_name"], "Architect One")
        self.assertEqual([(c["capability"], c["status"]) for c in first["capabilities"]],
                         [(name, "declared") for name in CAPS_ARCHITECT])
        self.assertEqual(first["runtime_state"], "unverified")
        self.assertNotIn(first["runtime_state"], {"verified", "ready", "active", "granted"})

    def test_metadata_edit_preserves_identity_and_relationships(self) -> None:
        self.project("p1")
        agent = self.register_agent()
        assignment = self.create_assignment(agent["agent_id"], "architect", CAPS_ARCHITECT)
        before = self.client.get(f"/api/agents/{agent['agent_id']}").json()
        response = self.client.patch(f"/api/agents/{agent['agent_id']}", json={
            "expected_version": 1, "display_name": "Renamed Agent", "icon_label": "a1",
            "runtime_adapter": "manual", "provider_id": "local", "model_id": "model-b",
            "capabilities": CAPS_ARCHITECT})
        self.assertEqual(response.status_code, 200, response.text)
        updated = response.json()
        self.assertEqual(updated["agent_id"], agent["agent_id"])
        self.assertEqual((updated["display_name"], updated["model_id"], updated["version"]),
                         ("Renamed Agent", "model-b", 2))
        self.assertEqual(updated["created_at"], before["created_at"])
        self.assertNotEqual(updated["updated_at"], before["updated_at"])
        self.assertEqual([c["capability"] for c in updated["capabilities"]], CAPS_ARCHITECT)
        current = self.client.get("/api/projects/P0001/assignments").json()
        self.assertEqual([(row["assignment_id"], row["agent_id"]) for row in current],
                         [(assignment["assignment_id"], agent["agent_id"])])
        self.assertEqual(self.fetch("SELECT count(*) FROM agent_definitions"), [(1,)])
        self.assertEqual(agent["agent_id"], updated["agent_id"], "identity is never re-identified by an edit")

    def test_invalid_or_secret_bearing_metadata_is_rejected_without_rows(self) -> None:
        service = self.service()
        before = self.state()
        invalid = [
            {"display_name": ""}, {"display_name": "x" * 81}, {"display_name": 7},
            {"display_name": "token=abc123"}, {"display_name": "https://example.test/agent"},
            {"model_id": "sk-live-abcdef123456"}, {"model_id": "a/b"}, {"model_id": "C:\\models\\a"},
            {"model_id": "ghp_abcdefghijklmnop"}, {"provider_id": "Bearer abc"},
            {"runtime_adapter": "rm -rf /"}, {"runtime_adapter": "x" * 65},
            {"capabilities": ["tool.shell.exec"]}, {"capabilities": ["*"]},
            {"capabilities": ["project.context.read", "project.context.read"]},
            {"capabilities": [CAPABILITY_PROJECT_CONTEXT_READ] * 9},
            {"capabilities": "project.context.read"},
            {"icon_label": "../../etc/passwd"}, {"icon_label": "x" * 33},
        ]
        for override in invalid:
            with self.subTest(override=override):
                with self.assertRaises(InvalidRegistryInput):
                    service.register_agent(OWNER, **agent_body(**override))
        for agent_key in ("not-a-uuid", 7, None, str(uuid.uuid4()).upper(), uuid.uuid4().hex):
            with self.subTest(agent_id=agent_key):
                with self.assertRaises(RegistryDenied):
                    service.get_agent(OWNER, agent_key)
        self.assertEqual(self.state(), before)

    def test_stale_edit_is_a_conflict_and_never_overwrites_later_changes(self) -> None:
        agent = self.register_agent()
        first = self.client.patch(f"/api/agents/{agent['agent_id']}", json=update_body("First", 1))
        self.assertEqual(first.status_code, 200, first.text)
        stale = self.client.patch(f"/api/agents/{agent['agent_id']}", json=update_body("Second", 1))
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self.client.get(f"/api/agents/{agent['agent_id']}").json()["display_name"], "First")
        self.assertEqual(self.client.patch(f"/api/agents/{agent['agent_id']}",
                                           json=update_body("Third", 0)).status_code, 422)


class AssignmentSemanticsTests(RegistryCase):
    def setUp(self) -> None:
        super().setUp()
        self.project("p1")
        self.project("p2")

    def test_architect_assignment_is_scoped_intent_not_activation(self) -> None:
        source_root = self.base / "p1"
        agent = self.register_agent()
        created = self.create_assignment(agent["agent_id"], "architect", CAPS_ARCHITECT)
        self.assertEqual((created["role"], created["scope"], created["task_id"]), ("architect", "project", None))
        self.assertEqual((created["state"], created["readiness"], created["runtime_effect"]),
                         ("active", "unverified", "none"))
        self.assertEqual((created["created_by"], created["created_by_assurance"]), ("local-owner", "local_unverified"))
        projects = {row["id"]: row for row in self.client.get("/api/projects").json()}
        self.assertEqual((projects["P0001"]["architect"], projects["P0001"]["active_workers"]), ("not_active", 0))
        self.assertEqual(projects["P0002"]["architect"], "not_active")
        self.assertEqual(self.client.get("/api/projects/P0002/assignments").json(), [])
        self.assertEqual(self.client.get("/api/health").json()["agent_runtime"], "not_started")
        self.assertEqual(self.fetch("SELECT count(*) FROM role_assignments"), [(1,)])
        self.assertFalse((self.data / "workspace/projects/P0001/architect/documents").exists())
        self.assertEqual(list(source_root.iterdir()), [])

    def test_worker_task_scope_must_belong_to_the_same_project(self) -> None:
        worker = self.register_agent(display_name="Worker", capabilities=CAPS_WORKER)
        self.task("P0001")
        created = self.create_assignment(worker["agent_id"], "worker", CAPS_WORKER, task_id="T0001")
        self.assertEqual((created["scope"], created["task_id"], created["role"]), ("task", "T0001", "worker"))
        body = {"agent_id": worker["agent_id"], "role": "worker", "task_id": "T0001", "capabilities": CAPS_WORKER}
        self.assertEqual(self.client.post("/api/projects/P0002/assignments", json=body).status_code, 422)
        self.assertEqual(self.client.post("/api/projects/P0001/assignments",
                                          json={**body, "task_id": "T0009"}).status_code, 422)
        self.assertEqual(self.client.post("/api/projects/P0001/assignments",
                                          json={**body, "task_id": "0001"}).status_code, 422)
        self.assertEqual(self.fetch("SELECT count(*) FROM role_assignments WHERE state = 'active'"), [(1,)])

    def test_one_architect_per_project_and_no_duplicate_equivalent_assignment(self) -> None:
        first = self.register_agent()
        second = self.register_agent(display_name="Second")
        self.create_assignment(first["agent_id"], "architect", CAPS_ARCHITECT)
        body = {"agent_id": second["agent_id"], "role": "architect", "capabilities": CAPS_ARCHITECT}
        self.assertEqual(self.client.post("/api/projects/P0001/assignments", json=body).status_code, 409)
        # The same Agent may hold an architect role on another Project only through its own assignment.
        self.create_assignment(second["agent_id"], "architect", CAPS_ARCHITECT, project="P0002")
        worker = self.register_agent(display_name="Worker", capabilities=CAPS_WORKER)
        self.create_assignment(worker["agent_id"], "worker", CAPS_WORKER)
        duplicate = {"agent_id": worker["agent_id"], "role": "worker", "capabilities": CAPS_WORKER}
        self.assertEqual(self.client.post("/api/projects/P0001/assignments", json=duplicate).status_code, 409)
        other_worker = self.register_agent(display_name="Other Worker", capabilities=CAPS_WORKER)
        self.create_assignment(other_worker["agent_id"], "worker", CAPS_WORKER)
        architect_with_task = {"agent_id": first["agent_id"], "role": "architect", "task_id": "T0001",
                               "capabilities": CAPS_ARCHITECT}
        self.assertEqual(self.client.post("/api/projects/P0002/assignments", json=architect_with_task).status_code, 422)
        self.assertEqual(self.fetch("SELECT count(*) FROM role_assignments WHERE state = 'active'"), [(4,)])

    def test_revocation_keeps_history_and_reassignment_is_explicit(self) -> None:
        agent = self.register_agent()
        created = self.create_assignment(agent["agent_id"], "architect", CAPS_ARCHITECT)
        url = f"/api/projects/P0001/assignments/{created['assignment_id']}/revoke"
        self.assertEqual(self.client.post(url, json={"expected_version": 9}).status_code, 409)
        revoked = self.client.post(url, json={"expected_version": 1})
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual((revoked.json()["state"], revoked.json()["version"]), ("revoked", 2))
        self.assertIsNotNone(revoked.json()["revoked_at"])
        self.assertEqual((revoked.json()["revoked_by"], revoked.json()["revoked_by_assurance"]),
                         ("local-owner", "local_unverified"))
        self.assertEqual(self.client.get("/api/projects/P0001/assignments").json(), [])
        history = self.client.get("/api/projects/P0001/assignments?include_revoked=true").json()
        self.assertEqual([(row["assignment_id"], row["state"]) for row in history],
                         [(created["assignment_id"], "revoked")])
        self.assertEqual(self.client.post(url, json={"expected_version": 2}).status_code, 409, "already revoked")
        reassigned = self.create_assignment(agent["agent_id"], "architect", CAPS_ARCHITECT)
        self.assertNotEqual(reassigned["assignment_id"], created["assignment_id"])
        self.assertEqual(self.fetch("SELECT count(*) FROM role_assignments"), [(2,)])

    def test_role_and_capability_bounds_are_enforced(self) -> None:
        architect = self.register_agent()
        unproven = self.register_agent(display_name="Unproven", capabilities=[])
        url = "/api/projects/P0001/assignments"
        cases = [
            {"agent_id": architect["agent_id"], "role": "owner", "capabilities": CAPS_ARCHITECT},
            {"agent_id": architect["agent_id"], "role": "architect",
             "capabilities": sorted({CAPABILITY_PROJECT_CONTEXT_READ, CAPABILITY_ARCHITECT_DOCUMENT_READ})},
            {"agent_id": architect["agent_id"], "role": "architect",
             "capabilities": CAPS_ARCHITECT + [CAPABILITY_TASK_CONTEXT_READ]},
            {"agent_id": architect["agent_id"], "role": "architect", "capabilities": CAPS_ARCHITECT + ["unknown.action"]},
            {"agent_id": architect["agent_id"], "role": "architect", "capabilities": CAPS_ARCHITECT + CAPS_ARCHITECT[:1]},
            {"agent_id": architect["agent_id"], "role": "architect", "capabilities": "project.context.read"},
            {"agent_id": "not-a-uuid", "role": "architect", "capabilities": CAPS_ARCHITECT},
            {"agent_id": architect["agent_id"], "role": "worker", "capabilities": CAPS_WORKER},
            {"agent_id": unproven["agent_id"], "role": "architect", "capabilities": CAPS_ARCHITECT},
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.client.post(url, json=body).status_code, 422, body)
        self.assertEqual(self.fetch("SELECT count(*) FROM role_assignments"), [(0,)])

    def test_capability_edit_blocks_but_never_verifies(self) -> None:
        agent = self.register_agent()
        created = self.create_assignment(agent["agent_id"], "architect", CAPS_ARCHITECT)
        self.assertEqual(created["readiness"], "unverified")
        reduced = [name for name in CAPS_ARCHITECT if name != CAPABILITY_ARCHITECT_DOCUMENT_WRITE]
        self.assertEqual(self.patch_agent(agent["agent_id"], 1, capabilities=reduced).status_code, 200)
        blocked = self.client.get("/api/projects/P0001/assignments").json()[0]
        self.assertEqual(blocked["readiness"], "blocked")
        self.assertNotEqual(blocked["version"], created["version"])
        self.assertEqual(self.client.get("/api/health").json()["agent_runtime"], "not_started")
        # A descriptor-only edit never clears the block.
        model_only = self.patch_agent(agent["agent_id"], 2, capabilities=reduced, model_id="model-c",
                                      provider_id="local-2")
        self.assertEqual(model_only.status_code, 200, model_only.text)
        self.assertEqual(self.client.get("/api/projects/P0001/assignments").json()[0]["readiness"], "blocked")
        # Re-declaring the capability returns to unverified, never to a verified/effective state.
        self.assertEqual(self.patch_agent(agent["agent_id"], 3, capabilities=CAPS_ARCHITECT).status_code, 200)
        restored = self.client.get("/api/projects/P0001/assignments").json()[0]
        self.assertEqual(restored["readiness"], "unverified")
        self.assertNotIn(restored["readiness"], {"verified", "ready", "active", "granted"})
        self.assertEqual(restored["runtime_effect"], "none")
        # The assignment, not the Agent, is what a future Run slice must re-check: it still exists unchanged.
        self.assertEqual(restored["assignment_id"], created["assignment_id"])

    def test_global_agent_reads_survive_a_damaged_project(self) -> None:
        agent = self.register_agent()
        self.sql("UPDATE projects SET project_uuid = 'not-a-uuid' WHERE id = 1")
        self.assertEqual(self.client.get("/api/projects").status_code, 503)
        self.assertEqual(self.client.get("/api/agents").status_code, 200)
        self.assertEqual(self.client.get(f"/api/agents/{agent['agent_id']}").status_code, 200)
        before = self.state()
        response = self.client.post("/api/projects/P0001/assignments",
                                    json={"agent_id": agent["agent_id"], "role": "architect",
                                          "capabilities": CAPS_ARCHITECT})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"],
                         "Project identity records are inconsistent; manual reconciliation is required")
        self.assertEqual(self.state(), before)
        self.assertEqual(list((self.data / "workspace/projects/P0001/architect").iterdir()), [])
        self.assertEqual(self.fetch("SELECT count(*) FROM project_root_bindings WHERE state = 'current'"), [(2,)])


class OptionalCapabilityGuardTests(RegistryCase):
    """P011.1: a requested optional capability must already be declared by the Agent, not merely role-allowed.

    The role allowlist says what a role *may* request; the AgentDefinition says what that Agent has actually declared.
    An assignment may only request the intersection, so an undeclared optional capability is a 422 at creation
    instead of a silently `unverified` durable row.
    """

    OPTIONAL = CAPABILITY_TASK_ARTIFACT_PUBLISH

    def setUp(self) -> None:
        super().setUp()
        self.project("p1")
        self.task("P0001")

    def worker_body(self, agent_id: str, capabilities) -> dict:
        return {"agent_id": agent_id, "role": "worker", "task_id": "T0001", "capabilities": list(capabilities)}

    def test_undeclared_optional_capability_is_refused_without_mutation(self) -> None:
        agent = self.register_agent(display_name="Worker", capabilities=CAPS_WORKER)
        self.assertNotIn(self.OPTIONAL, [item["capability"] for item in agent["capabilities"]],
                         "the fixture Agent declares only the role-required Worker capabilities")
        before = self.state()
        response = self.client.post("/api/projects/P0001/assignments",
                                    json=self.worker_body(agent["agent_id"], CAPS_WORKER + [self.OPTIONAL]))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn(self.OPTIONAL, response.text, "the refusal message stays sanitized")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.fetch("SELECT count(*) FROM role_assignments"), [(0,)])
        self.assertEqual(self.fetch("SELECT count(*) FROM assignment_capabilities"), [(0,)])

    def test_direct_service_applies_the_same_optional_capability_guard(self) -> None:
        agent = self.register_agent(display_name="Worker", capabilities=CAPS_WORKER)
        service = self.service()
        before = self.state()
        with self.assertRaises(InvalidRegistryInput):
            service.create_assignment(OWNER, 1, agent_id=agent["agent_id"], role="worker", task_id=1,
                                      capabilities=CAPS_WORKER + [self.OPTIONAL])
        self.assertEqual(self.state(), before)
        self.assertEqual(self.fetch("SELECT count(*) FROM role_assignments"), [(0,)])

    def test_declared_optional_capability_is_accepted_then_withdrawal_blocks_the_assignment(self) -> None:
        declared = CAPS_WORKER + [self.OPTIONAL]
        agent = self.register_agent(display_name="Worker", capabilities=declared)
        tree_before = self.state()["tree"]
        created = self.create_assignment(agent["agent_id"], "worker", declared, task_id="T0001")
        self.assertEqual((created["state"], created["readiness"], created["runtime_effect"]),
                         ("active", "unverified", "none"))
        self.assertNotIn(created["readiness"], {"verified", "ready", "granted", "authorized"})
        self.assertEqual(self.state()["tree"], tree_before, "an assignment writes no Workspace byte or directory")
        # Withdrawing the optional declaration downgrades the existing assignment rather than fabricating a grant.
        self.assertEqual(self.patch_agent(agent["agent_id"], 1, capabilities=CAPS_WORKER).status_code, 200)
        blocked = self.client.get("/api/projects/P0001/assignments").json()[0]
        self.assertEqual((blocked["readiness"], blocked["runtime_effect"]), ("blocked", "none"))
        self.assertNotEqual(blocked["version"], created["version"])
        # Restoring the declaration returns at most `unverified`, never a verified/effective state.
        self.assertEqual(self.patch_agent(agent["agent_id"], 2, capabilities=declared).status_code, 200)
        restored = self.client.get("/api/projects/P0001/assignments").json()[0]
        self.assertEqual(restored["readiness"], "unverified")
        self.assertNotIn(restored["readiness"], {"verified", "ready", "granted", "authorized"})


class AuthorizationBoundaryTests(RegistryCase):
    def test_agent_context_and_denying_policy_are_refused_on_every_new_route(self) -> None:
        self.project("p1")
        agent = self.register_agent()
        unknown = str(uuid.uuid4())
        routes = [("get", "/api/agents", None), ("post", "/api/agents", agent_body(display_name="Injected")),
                  ("get", f"/api/agents/{agent['agent_id']}", None),
                  ("patch", f"/api/agents/{agent['agent_id']}", update_body("Injected", 1)),
                  ("get", "/api/projects/P0001/assignments", None),
                  ("post", "/api/projects/P0001/assignments",
                   {"agent_id": agent["agent_id"], "role": "architect", "capabilities": CAPS_ARCHITECT}),
                  ("post", f"/api/projects/P0001/assignments/{unknown}/revoke", {"expected_version": 1})]
        for client in (self.api(auth_provider=AgentProvider()), self.api(authorization_policy=DenyAll())):
            before = self.state()
            for method, url, payload in routes:
                response = getattr(client, method)(url, **({"json": payload} if payload is not None else {}))
                self.assertEqual(response.status_code, 403, (url, response.text))
                self.assertEqual(response.json(), {"detail": "Not authorized for this operation"})
            self.assertEqual(self.state(), before)

    def test_forged_identity_claims_do_not_select_the_actor(self) -> None:
        self.project("p1")
        agent = self.register_agent()
        headers = {"X-Actor-Role": "owner", "X-Actor-Id": agent["agent_id"], "X-Actor-Kind": "owner",
                   "Authorization": f"Bearer {agent['agent_id']}", "Cookie": "actor_kind=owner; role=architect"}
        body = {**agent_body(display_name="Header Claimant"), "actor_kind": "owner", "actor_id": "local-owner",
                "role": "architect"}
        created = self.client.post("/api/agents", json=body, headers=headers)
        self.assertEqual(created.status_code, 201, created.text)
        assignment = self.client.post("/api/projects/P0001/assignments",
                                      json={"agent_id": created.json()["agent_id"], "role": "architect",
                                            "capabilities": CAPS_ARCHITECT, "actor_kind": "owner"},
                                      headers=headers)
        self.assertEqual(assignment.status_code, 201, assignment.text)
        self.assertEqual(assignment.json()["created_by"], "local-owner")
        self.assertEqual(self.client.get("/api/agents", headers=headers,
                                         params={"actor_id": "local-owner"}).status_code, 200)

    def test_direct_service_calls_are_guarded_before_any_lookup(self) -> None:
        service = self.service()
        before = self.state()
        forged = [AgentProvider().actor(), None, "local-owner",
                  ActorContext("owner", "local-owner", "authenticated"), ActorContext("owner", "x", "local_unverified")]
        for actor in forged:
            with self.assertRaises(RegistryDenied):
                service.register_agent(actor, **agent_body())
            with self.assertRaises(RegistryDenied):
                service.list_agents(actor)
            with self.assertRaises(RegistryDenied):
                service.create_assignment(actor, 1, agent_id=str(uuid.uuid4()), role="architect",
                                          capabilities=CAPS_ARCHITECT)
        self.assertEqual(self.state(), before)
        guarded = AgentRegistryService(make_session_factory(self.data / "rosc.db"), DenyAll())
        with self.assertRaises(RegistryDenied):
            guarded.get_agent(OWNER, str(uuid.uuid4()))  # denial, not a 404 existence probe
        with self.assertRaises(RegistryDenied):
            guarded.list_assignments(OWNER, 99)

    def test_a_valid_assignment_does_not_unlock_m2b_agent_authority(self) -> None:
        self.project("p1")
        agent = self.register_agent()
        self.create_assignment(agent["agent_id"], "architect", CAPS_ARCHITECT)
        agent_actor = ActorContext(authz.ACTOR_AGENT, agent["agent_id"], authz.ASSURANCE_LOCAL_UNVERIFIED)
        self.assertEqual(authz.IMPLEMENTED_ACTIONS & M2B_ACTIONS, M2B_ACTIONS)
        for action in sorted(M2B_ACTIONS):
            decision = evaluate(LocalOwnerPolicy(), agent_actor, action, m2b_scope(action))
            self.assertFalse(decision.allowed, action)
            self.assertEqual(decision.reason, "agent_authority_not_implemented")
        documents = ArchitectDocumentService(make_session_factory(self.data / "rosc.db"), self.data / "workspace",
                                             LocalOwnerPolicy())
        with self.assertRaises(AuthorizationDenied):
            documents.list_documents(agent_actor, 1)
        with self.assertRaises(AuthorizationDenied):
            documents.write_document(agent_actor, 1, "notes", "# must not land")
        self.assertFalse((self.data / "workspace/projects/P0001/architect/documents").exists())

    def test_new_routes_are_inside_the_local_request_guard(self) -> None:
        self.project("p1")
        agent = self.register_agent()
        before = self.state()
        for host in ("evil.example", "localhost.evil.example", "0.0.0.0", "127.0.0.1.evil.example"):
            for url in ("/api/agents", "/api/projects/P0001/assignments"):
                self.assertEqual(self.client.get(url, headers={"Host": host}).status_code, 403, (host, url))
        for origin in ("null", "http://evil.example", "https://localhost:5173"):
            self.assertEqual(self.client.post("/api/agents", json=agent_body(display_name="Origin"),
                                              headers={"Origin": origin}).status_code, 403, origin)
            self.assertEqual(self.client.post("/api/projects/P0001/assignments",
                                              json={"agent_id": agent["agent_id"], "role": "architect",
                                                    "capabilities": CAPS_ARCHITECT},
                                              headers={"Origin": origin}).status_code, 403, origin)
        accepted = self.client.get("/api/agents", headers={"Origin": "http://localhost:5173"})
        self.assertEqual(accepted.status_code, 200)
        self.assertFalse([header for header in accepted.headers if header.lower().startswith("access-control-")])
        self.assertEqual(self.state(), before)

    def test_errors_are_truthful_and_leak_nothing(self) -> None:
        self.project("p1")
        agent = self.register_agent()
        secret = "sk-live-SECRET-abc123"
        response = self.client.post("/api/agents", json=agent_body(model_id=secret))
        self.assertEqual(response.status_code, 422)
        for leaked in (secret, "SECRET", "/home/", "token", "Bearer"):
            self.assertNotIn(leaked, response.text)
        self.assertEqual(self.client.get(f"/api/agents/{uuid.uuid4()}").status_code, 404)
        self.assertEqual(self.client.get("/api/agents/not-a-uuid").status_code, 404)
        self.assertEqual(self.client.get("/api/projects/P0009/assignments").status_code, 404)
        self.assertEqual(self.client.post("/api/projects/P0001/assignments",
                                          json={"agent_id": str(uuid.uuid4()), "role": "architect",
                                                "capabilities": CAPS_ARCHITECT}).status_code, 404)
        conflict = self.client.post("/api/projects/P0001/assignments",
                                    json={"agent_id": agent["agent_id"], "role": "architect",
                                          "capabilities": CAPS_ARCHITECT})
        self.assertEqual(conflict.status_code, 201, conflict.text)
        duplicate = self.client.post("/api/projects/P0001/assignments",
                                     json={"agent_id": agent["agent_id"], "role": "architect",
                                           "capabilities": CAPS_ARCHITECT})
        self.assertEqual(duplicate.status_code, 409)
        for leaked in ("/home/", "SECRET", "Traceback", "sqlite"):
            self.assertNotIn(leaked, duplicate.text)


class ConcurrencyTests(RegistryCase):
    def race(self, claimants: list[tuple[str, str, list[str]]], task: str = "-") -> tuple[list[list[str]], list[str]]:
        """Start independent OS processes that attempt one assignment at the same instant."""
        start = str(time.time() + 1.5)
        processes = [subprocess.Popen([sys.executable, "-c", ASSIGNMENT_RACE, str(self.data / "rosc.db"), start,
                                       agent_id, role, json.dumps(capabilities), task],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for agent_id, role, capabilities in claimants]
        outputs = [process.communicate(timeout=60) for process in processes]
        return [output[0].split() for output in outputs], [output[1] for output in outputs]

    def test_two_processes_cannot_install_two_current_architects(self) -> None:
        self.project("p1")
        first = self.register_agent()
        second = self.register_agent(display_name="Second")
        results, errors = self.race([(first["agent_id"], "architect", CAPS_ARCHITECT),
                                     (second["agent_id"], "architect", CAPS_ARCHITECT)])
        self.assertEqual(sorted(item[0] for item in results), ["conflict", "created"], (results, errors))
        self.assertEqual(self.fetch("SELECT count(*) FROM role_assignments"), [(1,)])
        self.assertEqual(len(self.fetch("SELECT assignment_id FROM role_assignments WHERE state = 'active'")), 1)

    def test_two_processes_cannot_duplicate_the_same_worker_assignment(self) -> None:
        self.project("p1")
        worker = self.register_agent(display_name="Worker", capabilities=CAPS_WORKER)
        results, errors = self.race([(worker["agent_id"], "worker", CAPS_WORKER),
                                     (worker["agent_id"], "worker", CAPS_WORKER)])
        self.assertEqual(sorted(item[0] for item in results), ["conflict", "created"], (results, errors))
        self.assertEqual(self.fetch("SELECT count(*) FROM role_assignments WHERE state = 'active'"), [(1,)])

    def test_lock_contention_is_a_controlled_503_without_rows(self) -> None:
        self.project("p1")
        worker = self.register_agent(display_name="Worker", capabilities=CAPS_WORKER)
        client = self.api(busy_timeout_ms=50)
        before = self.state()
        blocker = sqlite3.connect(self.data / "rosc.db", isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            response = client.post("/api/agents", json=agent_body())
            assignment = client.post("/api/projects/P0001/assignments",
                                     json={"agent_id": worker["agent_id"], "role": "worker",
                                           "capabilities": CAPS_WORKER})
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(assignment.status_code, 503, assignment.text)
        self.assertEqual(self.fetch("SELECT count(*) FROM agent_definitions WHERE display_name = 'Architect One'"), [(0,)])
        after = self.state()
        self.assertEqual(after["rows"]["role_assignments"], [])
        self.assertEqual(after["tree"], before["tree"])


class MigrationTests(RegistryCase):
    def workspace_bytes(self) -> dict[str, bytes]:
        workspace = self.data / "workspace"
        return {str(path.relative_to(workspace)): path.read_bytes()
                for path in sorted(workspace.rglob("*")) if path.is_file()}

    def test_single_head_and_fresh_schema(self) -> None:
        from alembic.script import ScriptDirectory
        self.assertEqual(ScriptDirectory.from_config(self.alembic).get_heads(), ["0006_architect_runtime_sessions"])
        self.assertTrue({"agent_definitions", "agent_capabilities", "role_assignments", "assignment_capabilities"}
                        <= self.table_names())

    def test_upgrade_from_populated_0002_preserves_rows_bindings_and_manifests(self) -> None:
        command.downgrade(self.alembic, "0002_project_identity_binding")
        self.assertNotIn("role_assignments", self.table_names())
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=TASK).status_code, 201)
        documents = ArchitectDocumentService(make_session_factory(self.data / "rosc.db"), self.data / "workspace",
                                             LocalOwnerPolicy())
        documents.write_document(OWNER, 1, "notes", "# kept bytes\n")
        before_rows = {table: self.fetch(f"SELECT rowid, * FROM {table} ORDER BY rowid")
                       for table in ("projects", "project_root_bindings", "tasks")}
        before_files = self.workspace_bytes()
        command.upgrade(self.alembic, "head")
        self.assertEqual(self.table_names() & {"agent_definitions", "role_assignments"}, {"agent_definitions",
                                                                                         "role_assignments"})
        self.assertEqual({table: self.fetch(f"SELECT rowid, * FROM {table} ORDER BY rowid")
                          for table in ("projects", "project_root_bindings", "tasks")}, before_rows)
        self.assertEqual(self.workspace_bytes(), before_files)
        self.assertEqual(self.client.get("/api/projects").json()[0]["workspace_state"], "ready")
        self.assertEqual(documents.read_document(OWNER, 1, "notes").text, "# kept bytes\n")
        # The registry is usable on the migrated database without any rewrite of the accepted rows.
        agent = self.register_agent()
        self.create_assignment(agent["agent_id"], "architect", CAPS_ARCHITECT)
        self.assertEqual(self.fetch("SELECT count(*) FROM role_assignments"), [(1,)])

    def test_downgrade_removes_only_the_p011_schema(self) -> None:
        self.project("p1")
        agent = self.register_agent()
        self.create_assignment(agent["agent_id"], "architect", CAPS_ARCHITECT)
        project_rows = self.fetch("SELECT id, project_uuid FROM projects ORDER BY id")
        command.downgrade(self.alembic, "0002_project_identity_binding")
        for table in ("agent_definitions", "agent_capabilities", "role_assignments", "assignment_capabilities"):
            self.assertNotIn(table, self.table_names())
        self.assertEqual(self.fetch("SELECT id, project_uuid FROM projects ORDER BY id"), project_rows)
        self.assertEqual(self.fetch("SELECT count(*) FROM project_root_bindings"), [(1,)])
        command.upgrade(self.alembic, "head")
        self.assertEqual(self.fetch("SELECT count(*) FROM role_assignments"), [(0,)])
        self.assertEqual(self.fetch("SELECT count(*) FROM agent_definitions"), [(0,)])

    def test_sqlite_enforces_foreign_keys_and_partial_unique_indexes(self) -> None:
        self.project("p1")
        self.project("p2")
        self.task("P0001")
        first = self.register_agent()
        second = self.register_agent(display_name="Second")
        self.create_assignment(first["agent_id"], "architect", CAPS_ARCHITECT)
        columns = ("assignment_id, agent_id, project_id, task_id, role, state, readiness, version, created_at, "
                   "updated_at, revoked_at, created_by, created_by_assurance, revoked_by, revoked_by_assurance")
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql(f"INSERT INTO role_assignments ({columns}) VALUES (?, 'no-such-agent', 1, NULL, 'worker', "
                     f"'active', 'unverified', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00', NULL, 'local-owner', "
                     f"'local_unverified', NULL, NULL)", str(uuid.uuid4()))
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql(f"INSERT INTO role_assignments ({columns}) VALUES (?, ?, 1, NULL, 'architect', 'active', "
                     f"'unverified', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00', NULL, 'local-owner', "
                     f"'local_unverified', NULL, NULL)", str(uuid.uuid4()), second["agent_id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO agent_capabilities (agent_id, capability, status, declared_at) "
                     "VALUES (?, 'project.context.read', 'verified', '2026-01-01 00:00:00')", first["agent_id"])
        with self.assertRaises(sqlite3.IntegrityError):
            # An architect assignment is Project-scoped: naming a Task violates the table CHECK.
            self.sql(f"INSERT INTO role_assignments ({columns}) VALUES (?, ?, 2, 1, 'architect', 'active', "
                     f"'unverified', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00', NULL, 'local-owner', "
                     f"'local_unverified', NULL, NULL)", str(uuid.uuid4()), second["agent_id"])
        with self.assertRaises(sqlite3.IntegrityError):
            # Readiness has no ``verified`` value at the database level either.
            self.sql(f"INSERT INTO role_assignments ({columns}) VALUES (?, ?, 2, NULL, 'worker', 'active', "
                     f"'verified', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00', NULL, 'local-owner', "
                     f"'local_unverified', NULL, NULL)", str(uuid.uuid4()), second["agent_id"])

    def test_restart_readback_keeps_identity_versions_and_states(self) -> None:
        self.project("p1")
        agent = self.register_agent()
        assignment = self.create_assignment(agent["agent_id"], "architect", CAPS_ARCHITECT)
        revoked = self.client.post(f"/api/projects/P0001/assignments/{assignment['assignment_id']}/revoke",
                                   json={"expected_version": 1})
        self.assertEqual(revoked.status_code, 200, revoked.text)
        restarted = self.api()
        self.assertEqual(restarted.get(f"/api/agents/{agent['agent_id']}").json()["version"], 1)
        history = restarted.get("/api/projects/P0001/assignments?include_revoked=true").json()
        self.assertEqual([(row["assignment_id"], row["state"], row["version"]) for row in history],
                         [(assignment["assignment_id"], "revoked", 2)])
        self.assertEqual(restarted.get("/api/agents").json()[0]["agent_id"], agent["agent_id"])


if __name__ == "__main__":
    unittest.main()
