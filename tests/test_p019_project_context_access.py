"""P019 / P017-B Project Context Access: the ROSC Tools access matrix, end to end and offline.

The runtime under test is the REAL ``OpenCodeRuntime`` talking to the test-only fake ``opencode serve``; the fake does
what the installed 2.2.5 does that matters here: it registers the remote MCP server (initialize + tools/list with the
configured bearer header against the REAL in-process ROSC ToolHost), evaluates session permission rules last-match-wins,
and runs scripted "model" tool calls through the same permission check and the same HTTP transport.  No model, provider or
network is involved, and every private marker below is synthetic.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import test_p015_architect_activation as p15
import test_slice01 as base
import rosc.project_context as pc
from rosc.agent_registry import AgentRegistryService
from rosc.app import create_app
from rosc.architect_runtime import ENV_APPROVED_MODEL, ENV_BINARY, OpenCodeRuntime
from rosc.architect_store import (ArchitectDocumentStore, DocumentExists, DocumentNotFound, InvalidDocument,
                                  RevisionConflict)
from rosc.architect_tools import TOOL_NAMES, TOOLS
from rosc.authorization import ACTOR_AGENT, ActorContext, LocalOwnerPolicy, ResourceScope, evaluate
from rosc.db import make_session_factory
from rosc.models import ArchitectToolAudit, ProjectRootBinding

TESTS = Path(__file__).resolve().parent
OWNER = p15.OWNER
MARK_A = "SYNTH-OWN-DOC-MARKER-7f3a"
MARK_B = "SYNTH-OTHER-PROJECT-DOC-MARKER-91c2"
MARK_SRC = "SYNTH-SOURCE-MARKER-55de"
MARK_SECRET = "SYNTH-SECRET-VALUE-0b1c"
HAVE_GIT = shutil.which("git") is not None


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ContextCase(base.StageCase):
    """Three registered Projects; P0001 has an active, activated Architect on the real adapter + fake server."""

    def setUp(self) -> None:
        super().setUp()
        self.script_count = 0
        self.bin = self.base / "bin"
        self.bin.mkdir()
        launcher = self.bin / "opencode"
        launcher.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(TESTS)!r})\n"
                            "import fake_opencode\nfake_opencode.main()\n")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
        (self.bin / "mode").write_text("ok")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.base), ENV_BINARY: str(launcher),
               ENV_APPROVED_MODEL: "prov/model-a"}
        self.env_map = env
        self.lib = self.base / "library"
        for name, body in (("rules/architect.md", "# Global rule\nBe brief.\n"), ("skills/review.md", "skill text\n"),
                           ("tools/notes.yaml", "name: demo\n"), ("rules/.env", MARK_SECRET)):
            path = self.lib / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        self.roots = {}
        for label in ("a", "b", "c"):
            root = self.base / f"src-{label}"
            root.mkdir()
            self.roots[label] = root
        a = self.roots["a"]
        (a / "src").mkdir()
        (a / "README.md").write_text(f"# Project A\n{MARK_SRC} entry\nline three\n")
        (a / "src/app.py").write_text(f"def main():\n    return '{MARK_SRC}-code'\n")
        (a / ".env").write_text(f"TOKEN={MARK_SECRET}\n")
        (a / "notes.pem").write_text(MARK_SECRET)
        (self.roots["b"] / "README.md").write_text("B source SYNTH-B-SOURCE-MARKER\n")
        (self.roots["c"] / "README.md").write_text("C source\n")
        if HAVE_GIT:
            self.git(a, "init", "-q", "-b", "main")
            self.git(a, "add", "README.md", "src/app.py")
            self.git(a, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "first")
        self.restart(first=True)

    def git(self, cwd: Path, *args: str) -> str:
        env = {"PATH": os.environ["PATH"], "HOME": str(self.base), "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run(["git", "-C", str(cwd), *args], env=env, check=True, capture_output=True, text=True).stdout

    def restart(self, first: bool = False) -> None:
        if not first:
            self.client.close()
        self.runtime = OpenCodeRuntime(self.data, self.env_map)
        self.client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace",
                                            architect_runtime=self.runtime, global_library_root=self.lib),
                                 base_url="http://localhost")
        self.architect = self.client.app.state.architect
        self.broker = self.architect._context
        self.host = self.architect._tools
        self.sessions = make_session_factory(self.data / "rosc.db")
        if first:
            self.addCleanup(self.cleanup_runtime)
            for label, name in (("a", "Alpha"), ("b", "Beta"), ("c", "Gamma")):
                created = self.register(self.roots[label], mode="existing", name=name,
                                        **({"docs_entrypoint": "README.md"} if label == "a" else {}))
                self.assertEqual(created.status_code, 201, created.text)
            registry = AgentRegistryService(self.sessions, LocalOwnerPolicy())
            self.agent = registry.register_agent(OWNER, display_name="Architect", runtime_adapter="opencode",
                                                 provider_id="prov", model_id="model-a", capabilities=p15.CAPS)
            registry.create_assignment(OWNER, 1, agent_id=self.agent["agent_id"], role="architect",
                                       capabilities=p15.CAPS)
            self.registry = registry

    def cleanup_runtime(self) -> None:
        try:
            self.client.post("/api/projects/P0001/architect/deactivate")
        finally:
            self.host.shutdown()

    # -- helpers ------------------------------------------------------------------------------------------------------
    def activate(self) -> str:
        response = self.client.post("/api/projects/P0001/architect/activate")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "ready", response.json())
        return response.json()["session"]["session_id"]

    def consent(self, category: str, covered: str = "all", enabled: bool = True):
        return self.client.put("/api/projects/P0001/architect/context-access/disclosure",
                               json={"category": category, "covered": covered, "enabled": enabled})

    def consent_all(self) -> None:
        for category in ("project_docs_own", "project_docs_other", "project_source"):
            self.assertEqual(self.consent(category).status_code, 200)

    def model_calls(self, calls: list[dict]) -> list[dict]:
        """Scripted model: the fake performs these through permission check + the real MCP transport."""
        log = self.bin / "tool_log.jsonl"
        log.unlink(missing_ok=True)
        (self.bin / "tool_calls.json").write_text(json.dumps(calls))
        self.script_count += 1
        sent = self.client.post("/api/projects/P0001/conversation/messages", json={
            "client_key": f"tool-script-{self.script_count:04d}-xx", "body": "go"})
        self.assertEqual(sent.status_code, 201, sent.text)
        self.architect.drain()
        (self.bin / "tool_calls.json").unlink()
        return [json.loads(line) for line in log.read_text().splitlines()]

    def one(self, tool: str, **args) -> dict:
        """One scripted tool call; returns the decoded {"ok":..., ...} outcome (asserting it was permitted)."""
        entry = self.model_calls([{"tool": tool, "args": args}])[0]
        self.assertTrue(entry["allowed"], entry)
        return entry["result"]

    def direct(self, session_id: str, tool: str, **args) -> dict:
        return self.broker.call(session_id, tool, args)

    def store(self) -> ArchitectDocumentStore:
        return ArchitectDocumentStore(self.data / "workspace")

    def audit(self) -> list[ArchitectToolAudit]:
        with self.sessions() as session:
            return list(session.query(ArchitectToolAudit).order_by(ArchitectToolAudit.audit_id))


class TransportTests(ContextCase):
    """Gate 0: scoped ROSC tools work while the generic native tools stay denied."""

    def test_rosc_tools_work_and_native_tools_stay_denied(self) -> None:
        self.activate()
        self.consent_all()
        entries = self.model_calls([{"native": name} for name in ("read", "shell", "edit", "webfetch", "subagent")]
                                   + [{"tool": "briefing"}])
        self.assertEqual([e["allowed"] for e in entries], [False] * 5 + [True])
        self.assertTrue(entries[-1]["result"]["ok"])
        self.assertEqual(entries[-1]["result"]["result"]["project"]["project_id"], "P0001")

    def test_session_permissions_are_deny_all_plus_only_the_rosc_tools(self) -> None:
        from rosc.architect_runtime import DENY_ALL, TOOLS_PERMISSIONS
        self.activate()  # activation itself verifies the rule set and every restricted action against the server
        self.assertEqual(TOOLS_PERMISSIONS, DENY_ALL + [{"action": "rosc_*", "resource": "*", "effect": "allow"}])
        self.assertEqual(self.client.get("/api/projects/P0001/architect").json()["state"], "ready")

    def test_activation_fails_honestly_when_the_tool_server_cannot_connect(self) -> None:
        (self.bin / "mode").write_text("mcp_fail")
        with patch("rosc.architect_runtime.MCP_CONNECT_TIMEOUT", 2.0):
            view = self.client.post("/api/projects/P0001/architect/activate").json()
        self.assertNotEqual(view["state"], "ready")
        self.assertEqual(view["detail"], "tools_unavailable")
        self.assertFalse(self.host.active(view["session"]["session_id"]))

    def test_tool_surface_has_no_write_for_global_source_or_other_projects(self) -> None:
        names = set(TOOL_NAMES)
        self.assertEqual(names, {"briefing", "global_list", "global_read", "docs_list", "docs_search", "docs_read",
                                 "docs_create", "docs_update", "docs_delete", "docs_restore", "source_list",
                                 "source_read", "source_search", "source_git"})
        for tool in TOOLS:  # writes exist only for ROSC documents and name no target Project
            if tool["name"] in ("docs_create", "docs_update", "docs_delete", "docs_restore"):
                self.assertNotIn("project", tool["inputSchema"]["properties"])
        self.assertFalse([n for n in names if n.startswith(("source_w", "global_w", "shell", "exec", "git_"))])

    def test_endpoint_rejects_wrong_token_origin_host_unknown_tool_and_bad_arguments(self) -> None:
        session_id = self.activate()
        endpoint = self.host.open(session_id)  # a second incarnation token replaces the first
        port = int(endpoint.url.rsplit(":", 1)[1].split("/")[0])

        def post(payload, token=endpoint.token, **headers):
            request = urllib.request.Request(endpoint.url, data=json.dumps(payload).encode(), method="POST", headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {token}", **headers})
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read() or b"null")
            except urllib.error.HTTPError as exc:
                return exc.code, None

        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "briefing", "arguments": {}}}
        self.assertEqual(post(call)[0], 200)
        self.assertEqual(post(call, token="not-the-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")[0], 401)
        self.assertEqual(post(call, Origin="http://evil.example")[0], 401)
        self.assertEqual(post(call, Host="evil.example")[0], 401)
        unknown = {**call, "params": {"name": "shell", "arguments": {}}}
        self.assertEqual(post(unknown)[1]["error"]["code"], -32602)
        extra = {**call, "params": {"name": "docs_list", "arguments": {"path": "/etc"}}}
        outcome = json.loads(post(extra)[1]["result"]["content"][0]["text"])
        self.assertEqual(outcome["error"]["code"], "invalid_argument")
        request = urllib.request.Request(endpoint.url, method="GET", headers={"Authorization": f"Bearer {endpoint.token}"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 405)
        self.assertEqual(urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp", data=b"{}", method="POST",
            headers={"Authorization": f"Bearer {endpoint.token}"}), timeout=5).status, 200)

    def test_token_is_revoked_on_deactivation_and_session_is_refused(self) -> None:
        session_id = self.activate()
        self.assertTrue(self.host.active(session_id))
        self.client.post("/api/projects/P0001/architect/deactivate")
        self.assertFalse(self.host.active(session_id))
        self.assertEqual(self.direct(session_id, "briefing")["error"]["code"], "session_not_active")


class OwnDocumentTests(ContextCase):
    def test_crud_with_revisions_conflict_recoverable_delete_and_restart(self) -> None:
        session_id = self.activate()
        self.consent_all()
        created = self.one("docs_create", doc_id="plan", content=f"# Plan\n{MARK_A}\n", logical_path="plans/main")
        self.assertTrue(created["ok"], created)
        self.assertEqual((created["result"]["revision"], created["result"]["project_id"]), (1, "P0001"))
        read = self.one("docs_read", doc_id="plan")["result"]
        self.assertIn(MARK_A, read["content"])
        self.assertEqual(read["scope"], "own")
        self.assertEqual(read["sha256"], created["result"]["sha256"])
        updated = self.one("docs_update", doc_id="plan", content="# Plan v2\n", expected_revision=1)["result"]
        self.assertEqual(updated["revision"], 2)
        stale = self.one("docs_update", doc_id="plan", content="stale", expected_revision=1)
        self.assertEqual((stale["ok"], stale["error"]["code"], stale["error"]["current_revision"]),
                         (False, "revision_conflict", 2))
        self.assertEqual(self.one("docs_create", doc_id="plan", content="x")["error"]["code"], "already_exists")
        # an old revision is still readable (history), and the Architect's attribution is recorded
        old = self.one("docs_read", doc_id="plan", revision=1)["result"]
        self.assertIn(MARK_A, old["content"])
        info = self.store().read(1, "plan").info
        self.assertEqual(info.revisions[-1].actor_id, f"architect:{self.agent['agent_id']}")
        # recoverable delete: needs the current revision, moves history to trash, id is free again
        self.assertEqual(self.one("docs_delete", doc_id="plan", expected_revision=1)["error"]["code"], "revision_conflict")
        deleted = self.one("docs_delete", doc_id="plan", expected_revision=2)["result"]
        self.assertTrue(deleted["recoverable"])
        self.assertEqual(self.one("docs_list")["result"]["documents"], [])
        self.assertEqual(self.one("docs_read", doc_id="plan")["error"]["code"], "not_found")
        trashed = self.one("docs_list", include_trash=True)["result"]["trashed"]
        self.assertEqual([t["doc_id"] for t in trashed], ["plan"])
        restored = self.one("docs_restore", trash_id=deleted["trash_id"])["result"]
        self.assertEqual(restored["revision"], 2)
        self.assertIn("Plan v2", self.one("docs_read", doc_id="plan")["result"]["content"])
        self.assertIn(MARK_A, self.one("docs_read", doc_id="plan", revision=1)["result"]["content"])
        # restart: documents persist, and the tool credential does not survive the process
        self.restart()
        self.assertEqual(self.store().read(1, "plan").info.revision, 2)
        self.assertFalse(self.client.app.state.architect._tools.active(session_id))
        self.assertEqual(self.client.get("/api/projects/P0001/architect/context-access").json()["tools"]["transport"],
                         "inactive")

    def test_documents_live_outside_every_checkout_and_other_dirs_are_untouched(self) -> None:
        self.activate()
        self.consent_all()
        before = {label: sorted(p.name for p in root.iterdir()) for label, root in self.roots.items()}
        self.one("docs_create", doc_id="private", content=MARK_A)
        self.assertEqual(before, {label: sorted(p.name for p in root.iterdir()) for label, root in self.roots.items()})
        stored = self.data / "workspace/projects/P0001/architect/documents/private"
        self.assertTrue(stored.is_dir())
        self.assertFalse(stored.resolve().is_relative_to(pc.__file__ and Path(pc.__file__).resolve().parents[2]))

    def test_trash_restore_refuses_when_the_id_was_reused(self) -> None:
        self.activate()
        self.consent_all()
        self.one("docs_create", doc_id="n", content="one")
        trash_id = self.one("docs_delete", doc_id="n", expected_revision=1)["result"]["trash_id"]
        self.one("docs_create", doc_id="n", content="two")
        self.assertEqual(self.one("docs_restore", trash_id=trash_id)["error"]["code"], "already_exists")
        self.assertEqual(self.store().list_trash(1)[0].trash_id, trash_id)  # nothing was lost

    def test_invalid_ids_and_content_are_reported_not_applied(self) -> None:
        self.activate()
        self.consent_all()
        for doc_id in ("../x", "A/B", "", "a" * 80):
            self.assertFalse(self.one("docs_create", doc_id=doc_id, content="x")["ok"], doc_id)
        self.assertFalse(self.one("docs_create", doc_id="ok-id", content="")["ok"])
        self.assertFalse(self.one("docs_create", doc_id="ok-id", content="x", logical_path="../escape")["ok"])
        self.assertEqual(self.store().list(1), [])

    def test_search_finds_own_content_with_provenance(self) -> None:
        self.activate()
        self.consent_all()
        self.one("docs_create", doc_id="alpha", content=f"intro {MARK_A} tail")
        hit = self.one("docs_search", query=MARK_A.lower())["result"]
        self.assertEqual([m["doc_id"] for m in hit["matches"]], ["alpha"])
        self.assertTrue(hit["complete"])
        self.assertIn(MARK_A, hit["matches"][0]["snippet"])


class DisclosureTests(ContextCase):
    def test_nothing_private_is_returned_without_an_enabled_scope(self) -> None:
        session_id = self.activate()
        self.store().write(1, "seed", f"{MARK_A}", actor_id="x")
        self.store().write(2, "seed", f"{MARK_B}", actor_id="x")
        calls = [("docs_list", {}), ("docs_read", {"doc_id": "seed"}), ("docs_search", {"query": "SYNTH"}),
                 ("docs_list", {"project": "P0002"}), ("docs_read", {"doc_id": "seed", "project": "P0002"}),
                 ("source_list", {}), ("source_read", {"path": "README.md"}), ("source_search", {"query": "entry"}),
                 ("source_git", {})]
        for tool, args in calls:
            outcome = self.direct(session_id, tool, **args)
            self.assertEqual(outcome["error"]["code"], "consent_required", (tool, outcome))
            blob = json.dumps(outcome)
            for marker in (MARK_A, MARK_B, MARK_SRC, "seed", "README"):
                self.assertNotIn(marker, blob)
        briefing = self.direct(session_id, "briefing")["result"]
        self.assertEqual(briefing["architect_documents"]["status"], "consent_required")
        self.assertNotIn("seed", json.dumps(briefing))

    def test_scope_is_per_provider_model_category_and_project_and_revocable(self) -> None:
        session_id = self.activate()
        self.store().write(2, "seed", MARK_B, actor_id="x")
        self.store().write(3, "seed", "gamma-text", actor_id="x")
        self.assertEqual(self.consent("project_docs_other", "P0003").status_code, 200)
        self.assertEqual(self.direct(session_id, "docs_read", doc_id="seed", project="P0002")["error"]["code"],
                         "consent_required")
        self.assertIn("gamma-text", self.direct(session_id, "docs_read", doc_id="seed", project="P0003")["result"]["content"])
        # another category is a different decision
        self.assertEqual(self.direct(session_id, "docs_list")["error"]["code"], "consent_required")
        self.consent("project_docs_other", "P0003", enabled=False)
        self.assertEqual(self.direct(session_id, "docs_read", doc_id="seed", project="P0003")["error"]["code"],
                         "consent_required")
        # a scope for a DIFFERENT model never applies
        with self.sessions() as session:
            from rosc.models import ProviderDisclosureScope
            session.add(ProviderDisclosureScope(scope_id="11111111-1111-4111-8111-111111111111", provider_id="prov",
                                                model_id="other-model", category="project_docs_other", covered="all",
                                                enabled=1, updated_by="x"))
            session.commit()
        self.assertEqual(self.direct(session_id, "docs_read", doc_id="seed", project="P0002")["error"]["code"],
                         "consent_required")

    def test_owner_api_validates_input_and_status_reports_defaults(self) -> None:
        self.activate()
        view = self.client.get("/api/projects/P0001/architect/context-access").json()
        self.assertEqual(view["tools"]["transport"], "active")
        self.assertEqual(view["disclosure"]["default"], "none")
        self.assertEqual(view["disclosure"]["scopes"], [])
        self.assertEqual(view["source"], {"bound": True, "binding_version": 1, "docs_entrypoint": "README.md",
                                          "access": "read_only"})
        self.assertEqual([m["mode"] for m in view["matrix"]], ["read_only", "read_write", "read_only", "read_only", "denied"])
        self.assertFalse(view["publication"]["automatic_sync"])
        self.assertEqual(self.consent("everything").status_code, 409)
        self.assertEqual(self.consent("project_source", "P0099").status_code, 409)
        self.assertEqual(self.consent("project_source", "nonsense").status_code, 409)
        self.assertEqual(self.client.put("/api/projects/P0001/architect/context-access/disclosure",
                                         json={"category": "project_source"}).status_code, 422)
        self.assertEqual(self.consent("project_source").status_code, 200)
        scopes = self.client.get("/api/projects/P0001/architect/context-access").json()["disclosure"]["scopes"]
        self.assertEqual([(s["category"], s["covered"], s["enabled"]) for s in scopes], [("project_source", "all", True)])

    def test_agents_have_no_authority_over_the_scope(self) -> None:
        agent = ActorContext(ACTOR_AGENT, self.agent["agent_id"], "adapter_verified")
        for action in ("architect.context.read", "architect.context.configure"):
            self.assertFalse(evaluate(LocalOwnerPolicy(), agent, action, ResourceScope(project_id=1)).allowed)
        with self.assertRaises(Exception):
            self.architect.set_disclosure(agent, 1, category="project_source", covered="all", enabled=True)

    def test_opening_a_project_discloses_nothing_and_starts_nothing(self) -> None:
        self.client.get("/api/projects")
        self.client.get("/api/projects/P0001/architect/context-access")
        self.assertEqual(self.client.get("/api/projects/P0001/architect").json()["state"], "stopped")
        self.assertIsNone(self.host._server)  # no listener until an explicit activation


class OtherProjectDocumentTests(ContextCase):
    def test_other_project_documents_are_readable_without_per_document_approval_and_never_writable(self) -> None:
        self.activate()
        self.consent_all()
        self.store().write(2, "beta-notes", MARK_B, actor_id="owner", logical_path="notes/beta")
        listed = self.one("docs_list", project="P0002")["result"]
        self.assertEqual((listed["project_id"], listed["scope"]), ("P0002", "other_project_read_only"))
        self.assertEqual([d["doc_id"] for d in listed["documents"]], ["beta-notes"])
        read = self.one("docs_read", doc_id="beta-notes", project="P0002")["result"]
        self.assertIn(MARK_B, read["content"])
        self.assertEqual((read["project_id"], read["scope"]), ("P0002", "other_project_read_only"))
        self.assertEqual(len(read["sha256"]), 64)
        everywhere = self.one("docs_search", query=MARK_B[:20].lower(), project="all")["result"]
        self.assertEqual([(m["project_id"], m["scope"]) for m in everywhere["matches"]],
                         [("P0002", "other_project_read_only")])
        before = digest(self.data / "workspace/projects/P0002/architect/documents/beta-notes/document.json")
        for tool, extra in (("docs_create", {"content": "x"}), ("docs_update", {"content": "x", "expected_revision": 1}),
                            ("docs_delete", {"expected_revision": 1})):
            denied = self.one(tool, doc_id="beta-notes", project="P0002", **extra)
            self.assertEqual(denied["error"]["code"], "write_other_project_denied", tool)
        self.assertEqual(before, digest(self.data / "workspace/projects/P0002/architect/documents/beta-notes/document.json"))
        self.assertEqual(self.store().list_trash(2), [])
        self.assertEqual(self.store().read(2, "beta-notes").info.revision, 1)
        self.assertEqual(self.store().list(1), [])  # nothing was created in the caller's own Project either

    def test_unknown_project_and_malformed_references_are_refused(self) -> None:
        session_id = self.activate()
        self.consent_all()
        self.assertEqual(self.direct(session_id, "docs_list", project="P0099")["error"]["code"], "project_not_found")
        for bad in ("2", "../P0002", "P0002/../P0001", "all", True, "p0002 "):
            self.assertFalse(self.direct(session_id, "docs_list", project=bad)["ok"], bad)

    def test_other_project_source_checkout_is_inaccessible(self) -> None:
        session_id = self.activate()
        self.consent_all()
        for tool, args in (("source_read", {"path": "README.md"}), ("source_list", {}), ("source_search", {"query": "SOURCE"}),
                           ("source_git", {})):
            outcome = self.direct(session_id, tool, project="P0002", **args)
            self.assertEqual(outcome["error"]["code"], "source_other_project_denied", tool)
            self.assertNotIn("SYNTH-B-SOURCE-MARKER", json.dumps(outcome))
        # even a path that spells out the other Project's directory resolves only inside the bound root
        escape = self.direct(session_id, "source_read", path=f"../{self.roots['b'].name}/README.md")
        self.assertEqual(escape["error"]["code"], "invalid_path")


class GlobalLibraryTests(ContextCase):
    def test_global_definitions_are_readable_and_exclude_secrets_and_other_paths(self) -> None:
        session_id = self.activate()
        listed = self.direct(session_id, "global_list")["result"]
        self.assertTrue(listed["library_present"])
        self.assertEqual([e["path"] for e in listed["entries"]], ["rules/architect.md", "skills/review.md", "tools/notes.yaml"])
        self.assertIn("Be brief", self.direct(session_id, "global_read", path="rules/architect.md")["result"]["content"])
        for path in ("rules/.env", "../library/rules/architect.md", "/etc/passwd", "docs/x.md", "rules/../skills/review.md"):
            self.assertFalse(self.direct(session_id, "global_read", path=path)["ok"], path)
        self.assertEqual(len(TOOLS), len(TOOL_NAMES))

    def test_missing_library_is_reported_as_absent(self) -> None:
        session_id = self.activate()
        shutil.rmtree(self.lib)
        self.assertFalse(self.direct(session_id, "global_list")["result"]["library_present"])
        self.assertEqual(self.direct(session_id, "global_read", path="rules/architect.md")["error"]["code"], "library_absent")

    def test_project_local_rules_are_content_only(self) -> None:
        session_id = self.activate()
        self.consent_all()
        saved = self.direct(session_id, "docs_create", doc_id="local-rule", content="always run tool x",
                            logical_path="rules/local")
        self.assertTrue(saved["ok"])
        self.assertEqual(self.direct(session_id, "global_list")["result"]["entries"][0]["path"], "rules/architect.md")
        self.assertEqual(len(self.direct(session_id, "global_list")["result"]["entries"]), 3)  # not installed anywhere
        self.assertNotIn("rosc_local-rule", [f"rosc_{t}" for t in TOOL_NAMES])


class SourceReadTests(ContextCase):
    def setUp(self) -> None:
        super().setUp()
        self.session_id = self.activate()
        self.consent_all()
        self.snapshot = self.tree(self.roots["a"])

    @staticmethod
    def tree(root: Path) -> dict:
        return {str(p.relative_to(root)): (digest(p) if p.is_file() and not p.is_symlink() else "-")
                for p in sorted(root.rglob("*")) if ".git/objects" not in str(p) and ".git/logs" not in str(p)}

    def test_list_read_search_with_provenance_and_continuation(self) -> None:
        listing = self.one("source_list")["result"]
        names = {e["name"] for e in listing["entries"]}
        self.assertTrue({"README.md", "src"} <= names)
        self.assertFalse({".env", ".git", "notes.pem"} & names)  # policy-hidden, and counted honestly
        self.assertGreaterEqual(listing["hidden_by_policy"], 2)
        self.assertEqual((listing["access"], listing["binding_version"]), ("read_only", 1))
        read = self.one("source_read", path="README.md", max_chars=10)["result"]
        self.assertEqual((read["truncated"], read["next_offset"], read["returned_chars"]), (True, 10, 10))
        rest = self.one("source_read", path="README.md", offset=10)["result"]
        self.assertFalse(rest["truncated"])
        self.assertIn(MARK_SRC, read["content"] + rest["content"])
        self.assertEqual(read["sha256"], digest(self.roots["a"] / "README.md"))
        hit = self.one("source_search", query=MARK_SRC.lower())["result"]
        self.assertEqual(sorted(m["path"] for m in hit["matches"]), ["README.md", "src/app.py"])
        self.assertTrue(hit["complete"])
        self.assertGreaterEqual(hit["coverage"]["files_scanned"], 2)
        sub = self.one("source_list", path="src")["result"]
        self.assertEqual([e["name"] for e in sub["entries"]], ["app.py"])

    def test_the_source_checkout_is_never_modified_by_any_tool(self) -> None:
        for call in ({"tool": "source_list"}, {"tool": "source_read", "args": {"path": "README.md"}},
                     {"tool": "source_search", "args": {"query": "main"}}, {"tool": "source_git", "args": {"operation": "status"}},
                     {"tool": "source_git", "args": {"operation": "log"}}, {"tool": "source_git", "args": {"operation": "head"}},
                     {"tool": "docs_create", "args": {"doc_id": "x", "content": "y", "logical_path": "README.md"}},
                     {"tool": "docs_update", "args": {"doc_id": "README.md", "content": "y", "expected_revision": 1}},
                     {"tool": "source_write", "args": {"path": "README.md", "content": "pwn"}}):
            self.model_calls([call])
        self.assertEqual(self.snapshot, self.tree(self.roots["a"]))
        invented = self.model_calls([{"tool": "source_write", "args": {"path": "README.md", "content": "pwn"}}])[0]
        self.assertEqual(invented["rpc_error"], -32602, "there is no such operation to request")
        self.assertEqual(self.snapshot, self.tree(self.roots["a"]))

    def test_secret_vcs_traversal_and_special_paths_are_refused(self) -> None:
        a = self.roots["a"]
        outside = self.base / "outside-data.txt"
        outside.write_text(MARK_SECRET)
        (a / "link-file").symlink_to(outside)
        (a / "link-dir").symlink_to(self.base)
        os.link(outside, a / "hardlinked.txt")
        os.mkfifo(a / "pipe")
        (a / "blob.bin").write_bytes(b"\x00\x01binary")
        (a / "huge.txt").write_text("x" * (pc.MAX_SOURCE_FILE_BYTES + 1))
        cases = {".env": "path_excluded", "notes.pem": "path_excluded", ".git/config": "path_excluded",
                 ".git/HEAD": "path_excluded", "../outside-data.txt": "invalid_path", "/etc/passwd": "invalid_path",
                 "src/../README.md": "invalid_path", "src\\app.py": "invalid_path", "link-file": "unsafe_path",
                 "link-dir/outside-data.txt": "unsafe_path", "hardlinked.txt": "unsafe_path",
                 "pipe": "not_regular_file", "blob.bin": "not_text", "huge.txt": "file_too_large", "src": "not_regular_file",
                 "missing.txt": "not_found", "": "invalid_path"}
        for path, code in cases.items():
            outcome = self.direct(self.session_id, "source_read", path=path)
            self.assertEqual(outcome["error"]["code"], code, (path, outcome))
            self.assertNotIn(MARK_SECRET, json.dumps(outcome))
        listing = self.direct(self.session_id, "source_list")["result"]
        self.assertEqual({e["name"]: e["type"] for e in listing["entries"]}["link-file"], "symlink")
        found = self.direct(self.session_id, "source_search", query=MARK_SECRET.lower())["result"]
        self.assertEqual(found["matches"], [], "secret stores, symlinks and hard links are never searched")
        self.assertEqual(self.direct(self.session_id, "source_list", path="link-dir")["error"]["code"], "unsafe_path")

    def test_concurrent_worker_change_is_reported_honestly(self) -> None:
        target = self.roots["a"] / "src/app.py"
        real_read = os.read

        seen = {"n": 0}

        def racing(fd, n):
            data = real_read(fd, n)
            if data:
                seen["n"] += 1  # a Worker touches the file while every attempt is reading it
                os.utime(target, ns=(10 ** 18 + seen["n"], 10 ** 18 + seen["n"]))
            return data

        with patch.object(pc.os, "read", racing):
            outcome = self.direct(self.session_id, "source_read", path="src/app.py")
        self.assertEqual(outcome["error"]["code"], "changed_during_read")
        calls = {"n": 0}

        def once(fd, n):
            data = real_read(fd, n)
            calls["n"] += 1
            if data and calls["n"] == 1:
                with open(target, "a") as handle:
                    handle.write("# one more\n")
            return data

        with patch.object(pc.os, "read", once):
            outcome = self.direct(self.session_id, "source_read", path="src/app.py")
        self.assertTrue(outcome["ok"])
        self.assertIn("one more", outcome["result"]["content"])
        self.assertEqual(outcome["result"]["sha256"], digest(target))

    def test_search_is_bounded_and_says_so(self) -> None:
        for index in range(12):
            (self.roots["a"] / f"f{index}.txt").write_text("needle here\n")
        with patch.object(pc, "MAX_SEARCH_RESULTS", 5):
            result = self.direct(self.session_id, "source_search", query="needle")["result"]
        self.assertEqual((len(result["matches"]), result["complete"], result["stopped_reason"]), (5, False, "result_limit"))
        with patch.object(pc, "MAX_SEARCH_FILES", 2):
            result = self.direct(self.session_id, "source_search", query="needle")["result"]
        self.assertEqual((result["complete"], result["stopped_reason"]), (False, "scan_limit"))

    def test_rebound_root_and_revoked_assignment_are_refused(self) -> None:
        with self.sessions() as session:
            old = session.query(ProjectRootBinding).filter_by(project_id=1).one()
            old.state = "superseded"
            session.flush()
            moved = self.base / "moved-root"
            moved.mkdir()
            (moved / "README.md").write_text("moved SYNTH-B-SOURCE-MARKER\n")
            session.add(ProjectRootBinding(project_id=1, version=2, root_path=str(moved), mode="existing",
                                           state="current"))
            session.commit()
        outcome = self.direct(self.session_id, "source_read", path="README.md")
        self.assertEqual(outcome["error"]["code"], "root_binding_changed")
        self.assertNotIn("SYNTH-B-SOURCE-MARKER", json.dumps(outcome))
        assignment = self.registry.list_assignments(OWNER, 1)[0]
        self.registry.revoke_assignment(OWNER, 1, assignment["assignment_id"], expected_version=assignment["version"])
        self.assertEqual(self.direct(self.session_id, "docs_list")["error"]["code"], "assignment_changed")

    def test_missing_capability_is_refused(self) -> None:
        with self.sessions() as session:
            session.connection().exec_driver_sql("DELETE FROM assignment_capabilities WHERE capability = 'architect.document.write'")
            session.commit()
        self.assertEqual(self.direct(self.session_id, "docs_create", doc_id="x", content="y")["error"]["code"],
                         "capability_not_declared")
        self.assertTrue(self.direct(self.session_id, "docs_list")["ok"])


class GitFactsTests(ContextCase):
    def setUp(self) -> None:
        super().setUp()
        if not HAVE_GIT:
            self.skipTest("git is not installed")
        self.session_id = self.activate()
        self.consent_all()

    def test_head_status_and_log_are_read_only_facts(self) -> None:
        index = self.roots["a"] / ".git/index"
        before = digest(index)
        head = self.direct(self.session_id, "source_git", operation="head")["result"]
        self.assertEqual((head["status"], head["branch"]), ("ok", "main"))
        self.assertEqual(head["head"], self.git(self.roots["a"], "rev-parse", "HEAD").strip())
        clean = self.direct(self.session_id, "source_git", operation="status")["result"]
        self.assertFalse(clean["dirty"])
        (self.roots["a"] / "README.md").write_text("changed by a Worker\n")
        dirty = self.direct(self.session_id, "source_git", operation="status")["result"]
        self.assertEqual((dirty["dirty"], dirty["changed_files"]), (True, ["README.md"]))
        log = self.direct(self.session_id, "source_git", operation="log", limit=5)["result"]
        self.assertEqual([c["subject"] for c in log["commits"]], ["first"])
        self.assertEqual(before, digest(index), "Git inspection must not rewrite the index")
        self.assertEqual(self.git(self.roots["a"], "log", "--oneline").count("\n"), 1)  # nothing was committed

    def test_unsupported_operations_and_executable_config_are_reported_not_run(self) -> None:
        for operation in ("diff", "commit", "push", "add -A", "--help"):
            result = self.direct(self.session_id, "source_git", operation=operation)["result"]
            self.assertEqual(result["status"], "unsupported", operation)
        marker = self.base / "filter-ran"
        config = self.roots["a"] / ".git/config"
        config.write_text(config.read_text() + f'[filter "x"]\n\tclean = touch {marker}\n')
        (self.roots["a"] / ".gitattributes").write_text("*.md filter=x\n")
        result = self.direct(self.session_id, "source_git", operation="status")["result"]
        self.assertEqual((result["status"], result["reason"]), ("unsupported", "repository_config_may_execute_helpers"))
        self.assertFalse(marker.exists())

    def test_non_git_checkout_reports_unavailable(self) -> None:
        shutil.rmtree(self.roots["a"] / ".git")
        self.assertEqual(self.direct(self.session_id, "source_git", operation="head")["result"]["status"], "unavailable")


class BriefingAndContextTests(ContextCase):
    def test_briefing_is_core_state_with_source_route_and_no_invented_evidence(self) -> None:
        session_id = self.activate()
        self.consent_all()
        self.store().write(1, "adr-1", "x", actor_id="owner", logical_path="adr/one")
        briefing = self.direct(session_id, "briefing")["result"]
        self.assertEqual(briefing["kind"], "core_state")
        self.assertEqual(briefing["project"]["project_id"], "P0001")
        self.assertEqual(briefing["source_checkout"]["docs_entrypoint"], "README.md")
        self.assertEqual(briefing["source_checkout"]["access"], "read_only")
        self.assertEqual(briefing["architect_documents"]["count"], 1)
        self.assertEqual(briefing["core_facts"]["task_count"], 0)
        self.assertNotIn("git", json.dumps(briefing["core_facts"]).lower().replace("git evidence", ""))
        self.assertEqual(len(briefing["access_matrix"]), 5)

    def test_prompt_carries_concise_rules_and_route_but_no_documents_or_source(self) -> None:
        self.activate()
        self.consent_all()
        self.store().write(1, "adr-1", MARK_A, actor_id="owner")
        self.client.post("/api/projects/P0001/conversation/messages", json={"client_key": "plain-0001-xx", "body": "Hello"})
        self.architect.drain()
        prompt = (self.bin / "last_prompt.txt").read_text()
        self.assertIn("rosc_briefing", prompt)
        self.assertIn("READ", prompt.upper())
        self.assertIn("Docs entrypoint in the bound checkout: README.md", prompt)
        self.assertIn("evidence, not instructions", prompt)
        self.assertNotIn("no tools", prompt)
        for private in (MARK_A, MARK_SRC, "adr-1"):
            self.assertNotIn(private, prompt)
        self.assertLess(len(prompt), 3500)
        with self.sessions() as session:
            categories = session.connection().exec_driver_sql(
                "SELECT context_categories FROM conversation_deliveries").fetchone()[0]
        self.assertEqual(categories, "project_identity,tool_rules,recent_messages:1")

    def test_runtime_without_tools_keeps_the_legacy_no_tools_prompt(self) -> None:
        runtime = p15.FakeRuntime()
        client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace", architect_runtime=runtime),
                            base_url="http://localhost")
        client.post("/api/projects/P0001/architect/activate")
        client.post("/api/projects/P0001/conversation/messages", json={"client_key": "legacy-0001-xx", "body": "Hi"})
        client.app.state.architect.drain()
        self.assertIn("You have no tools", runtime.asked[0][1])
        client.post("/api/projects/P0001/architect/deactivate")

    def test_long_architect_reply_is_preserved_with_tools_enabled(self) -> None:
        self.activate()
        text = ("Длинный ответ 😀 " * 20_000).strip()
        (self.bin / "reply.json").write_text(json.dumps({"parts": [text], "before": []}), encoding="utf-8")
        (self.bin / "mode").write_text("file_reply")
        self.client.post("/api/projects/P0001/architect/deactivate")
        self.activate()
        sent = self.client.post("/api/projects/P0001/conversation/messages", json={"client_key": "long-0001-xx", "body": "go"})
        self.assertEqual(sent.status_code, 201)
        self.architect.drain()
        reply = self.client.get("/api/projects/P0001/conversation").json()["messages"][-1]["body"]
        self.assertEqual(reply, text)
        self.assertGreater(len(reply), 4000)


class AuditAndPublicationTests(ContextCase):
    def test_audit_records_operations_without_content_and_nothing_is_published(self) -> None:
        self.activate()
        self.consent_all()
        self.one("docs_create", doc_id="secret-plan", content=MARK_A)
        self.one("docs_read", doc_id="secret-plan")
        self.one("docs_update", doc_id="secret-plan", content="x", expected_revision=9)
        self.one("source_read", path=".env")
        rows = self.audit()
        self.assertEqual([(r.tool, r.outcome) for r in rows if r.tool != "briefing"],
                         [("docs_create", "ok"), ("docs_read", "ok"), ("docs_update", "revision_conflict"),
                          ("source_read", "path_excluded")])
        blob = json.dumps([[r.tool, r.target, r.outcome, r.session_id, r.project_id] for r in rows])
        for private in (MARK_A, MARK_SECRET):
            self.assertNotIn(private, blob)
        self.assertTrue(all(r.project_id == 1 for r in rows))

    def test_tool_failures_never_break_the_transport_or_leak_exceptions(self) -> None:
        session_id = self.activate()
        self.consent_all()
        with patch.object(self.broker._store, "list", side_effect=RuntimeError("boom /private/path")):
            outcome = self.direct(session_id, "docs_list")
        self.assertEqual(outcome, {"ok": False, "error": {"code": "internal_error", "message": "The tool failed"}})
        self.assertEqual(self.direct(session_id, "no_such_tool")["error"]["code"], "unknown_tool")
        self.assertEqual(self.direct("no-such-session", "briefing")["error"]["code"], "session_not_active")
        self.assertFalse(self.broker.call(session_id, "docs_list", "not-an-object")["ok"])


class MigrationTests(base.StageCase):
    """0008 is additive and reversible; it never runs at application start (the Owner applies it deliberately)."""

    def tables(self) -> set[str]:
        from sqlalchemy import create_engine, inspect
        return set(inspect(create_engine(f"sqlite:///{self.data / 'rosc.db'}")).get_table_names())

    def test_additive_upgrade_and_clean_downgrade_keep_existing_rows(self) -> None:
        from alembic import command
        self.client.close()
        command.downgrade(self.alembic, "0007_architect_reply_unbounded_body")
        self.assertFalse({"provider_disclosure_scopes", "architect_tool_audit"} & self.tables())
        client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace"), base_url="http://localhost")
        self.assertEqual(client.post("/api/projects", json={
            "name": "Kept", "description": "d", "root_path": str(self.new_root()), "mode": "new"}).status_code, 201)
        client.close()
        command.upgrade(self.alembic, "head")
        self.assertTrue({"provider_disclosure_scopes", "architect_tool_audit"} <= self.tables())
        client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace"), base_url="http://localhost")
        self.assertEqual([p["name"] for p in client.get("/api/projects").json()], ["Kept"])
        client.close()


class StoreDeleteRestoreTests(base.StageCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = ArchitectDocumentStore(self.data / "workspace")
        self.assertEqual(self.register(self.new_root()).status_code, 201)

    def test_delete_requires_current_revision_and_restore_keeps_history(self) -> None:
        self.store.write(1, "n", "one", actor_id="a")
        self.store.write(1, "n", "two", actor_id="a", expected_revision=1)
        with self.assertRaises(RevisionConflict):
            self.store.delete(1, "n", actor_id="a", expected_revision=1)
        with self.assertRaises(DocumentNotFound):
            self.store.delete(1, "absent", actor_id="a", expected_revision=1)
        with self.assertRaises(InvalidDocument):
            self.store.delete(1, "n", actor_id="a", expected_revision=0)
        trashed = self.store.delete(1, "n", actor_id="who", expected_revision=2)
        self.assertEqual((trashed.revision, trashed.deleted_by), (2, "who"))
        self.assertEqual(self.store.list(1), [])
        self.assertEqual([t.trash_id for t in self.store.list_trash(1)], [trashed.trash_id])
        with self.assertRaises(InvalidDocument):
            self.store.restore(1, "../../x", actor_id="a")
        info = self.store.restore(1, trashed.trash_id, actor_id="a")
        self.assertEqual(info.revision, 2)
        self.assertEqual([self.store.read(1, "n", r).text for r in (1, 2)], ["one", "two"])
        self.assertEqual(self.store.list_trash(1), [])
        self.store.write(1, "n", "three", actor_id="a", expected_revision=2)

    def test_restore_refuses_a_reused_logical_path(self) -> None:
        self.store.write(1, "a", "x", actor_id="a", logical_path="notes/a")
        trashed = self.store.delete(1, "a", actor_id="a", expected_revision=1)
        self.store.write(1, "b", "y", actor_id="a", logical_path="notes/a")
        with self.assertRaises(DocumentExists):
            self.store.restore(1, trashed.trash_id, actor_id="a")
        self.assertEqual(len(self.store.list_trash(1)), 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
