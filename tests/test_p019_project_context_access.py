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
import re
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
import rosc.config as pc_config
import rosc.project_context as pc
from rosc.agent_registry import AgentRegistryService
from rosc.app import create_app
from rosc.architect_rules import ArchitectRulesLoader
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
    """Three registered Projects; P0001 has an active, activated Architect on the real adapter + fake server.

    P027: the default composition has no RulesSnapshot pin; legacy P021 tests opt in with ``legacy_rules = True``."""

    legacy_rules = False

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
        # P021: activation verifies the Owner-approved ruleset, so the synthetic library carries a copy of the real one.
        real = pc_config.global_library_dir()
        for top in ("principles", "agents"):
            shutil.copytree(real / top, self.lib / top)
        for name, body in (("agents/architect/rules/architect.md", "# Global rule\nBe brief.\n"),
                           ("skills/review.md", "skill text\n"), ("tools/notes.yaml", "name: demo\n"),
                           ("agents/architect/rules/.env", MARK_SECRET)):
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

    def listed_definitions(self) -> list[str]:
        """Definition files the library tools are expected to serve: approved suffixes under the four namespaces, no
        secret-named entries (the synthetic library = copied approved ruleset + the fixture's own files)."""
        found = []
        for top in pc.GLOBAL_LIBRARY_DIRS:
            for dirpath, _dirs, names in os.walk(self.lib / top):
                found += [str((Path(dirpath) / n).relative_to(self.lib)) for n in names
                          if n.lower().endswith(pc.GLOBAL_SUFFIXES) and not n.startswith(".env")]
        return sorted(found)

    def git(self, cwd: Path, *args: str) -> str:
        env = {"PATH": os.environ["PATH"], "HOME": str(self.base), "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run(["git", "-C", str(cwd), *args], env=env, check=True, capture_output=True, text=True).stdout

    def restart(self, first: bool = False) -> None:
        if not first:
            self.client.close()
        self.runtime = OpenCodeRuntime(self.data, self.env_map)
        self.client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace",
                                            architect_runtime=self.runtime, global_library_root=self.lib,
                                            architect_rules=ArchitectRulesLoader(self.lib) if self.legacy_rules else None),
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
        self.settle_onboarding()
        return response.json()["session"]["session_id"]

    def settle_onboarding(self) -> None:
        """P029: a NEW session's one-time onboarding runs in the background.  Finish it and forget its trace so the
        test's own turns are observed from a clean baseline."""
        self.architect.drain()
        for name in ("prompts.log", "last_prompt.txt"):
            (self.bin / name).unlink(missing_ok=True)

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
                                 "source_read", "source_search", "source_git", "task_list", "task_read", "run_list", "run_read"})
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

    def test_text_plain_documents_update_losslessly_through_the_tools(self) -> None:
        self.activate()
        created = self.one("docs_create", doc_id="notes-txt", content="plain v1 \u2014 \u043f\u0440\u0438\u0432\u0435\u0442\n",
                           media_type="text/plain", logical_path="notes/plain")
        self.assertTrue(created["ok"], created)
        self.assertEqual((created["result"]["media_type"], created["result"]["revision"]), ("text/plain", 1))
        self.assertEqual(self.one("docs_read", doc_id="notes-txt")["result"]["content"], "plain v1 \u2014 \u043f\u0440\u0438\u0432\u0435\u0442\n")
        # an update that says nothing about the format keeps the document's own media type
        updated = self.one("docs_update", doc_id="notes-txt", content="plain v2\n\n  keep   spacing\n", expected_revision=1)
        self.assertTrue(updated["ok"], updated)
        self.assertEqual((updated["result"]["media_type"], updated["result"]["revision"]), ("text/plain", 2))
        self.assertEqual(self.one("docs_read", doc_id="notes-txt")["result"]["content"], "plain v2\n\n  keep   spacing\n")
        history = self.one("docs_read", doc_id="notes-txt", revision=1)["result"]
        self.assertEqual((history["content"], history["revision"], history["current_revision"]),
                         ("plain v1 \u2014 \u043f\u0440\u0438\u0432\u0435\u0442\n", 1, 2))
        # the update tool offers no format change at all, and the document keeps its type and content
        changed = self.one("docs_update", doc_id="notes-txt", content="# md", expected_revision=2, media_type="text/markdown")
        self.assertEqual((changed["ok"], changed["error"]["code"]), (False, "invalid_argument"))
        info = self.store().read(1, "notes-txt").info
        self.assertEqual((info.revision, info.media_type), (2, "text/plain"))
        self.assertEqual(self.store().read(1, "notes-txt").text, "plain v2\n\n  keep   spacing\n")
        # a second plain update, then a stale revision is still denied with the current revision reported
        third = self.one("docs_update", doc_id="notes-txt", content="plain v3", expected_revision=2)["result"]
        self.assertEqual((third["revision"], third["media_type"]), (3, "text/plain"))
        stale = self.one("docs_update", doc_id="notes-txt", content="stale", expected_revision=2)
        self.assertEqual((stale["error"]["code"], stale["error"]["current_revision"]), ("revision_conflict", 3))
        self.assertEqual(self.store().read(1, "notes-txt").text, "plain v3")
        # Markdown documents are unaffected
        self.one("docs_create", doc_id="md-doc", content="# one")
        md = self.one("docs_update", doc_id="md-doc", content="# two", expected_revision=1)["result"]
        self.assertEqual((md["media_type"], md["revision"]), ("text/markdown", 2))
        self.assertEqual(self.one("docs_update", doc_id="absent", content="x", expected_revision=1)["error"]["code"], "not_found")

    def test_documents_live_outside_every_checkout_and_other_dirs_are_untouched(self) -> None:
        self.activate()
        before = {label: sorted(p.name for p in root.iterdir()) for label, root in self.roots.items()}
        self.one("docs_create", doc_id="private", content=MARK_A)
        self.assertEqual(before, {label: sorted(p.name for p in root.iterdir()) for label, root in self.roots.items()})
        stored = self.data / "workspace/projects/P0001/architect/documents/private"
        self.assertTrue(stored.is_dir())
        self.assertFalse(stored.resolve().is_relative_to(pc.__file__ and Path(pc.__file__).resolve().parents[2]))

    def test_trash_restore_refuses_when_the_id_was_reused(self) -> None:
        self.activate()
        self.one("docs_create", doc_id="n", content="one")
        trash_id = self.one("docs_delete", doc_id="n", expected_revision=1)["result"]["trash_id"]
        self.one("docs_create", doc_id="n", content="two")
        self.assertEqual(self.one("docs_restore", trash_id=trash_id)["error"]["code"], "already_exists")
        self.assertEqual(self.store().list_trash(1)[0].trash_id, trash_id)  # nothing was lost

    def test_invalid_ids_and_content_are_reported_not_applied(self) -> None:
        self.activate()
        for doc_id in ("../x", "A/B", "", "a" * 80):
            self.assertFalse(self.one("docs_create", doc_id=doc_id, content="x")["ok"], doc_id)
        self.assertFalse(self.one("docs_create", doc_id="ok-id", content="")["ok"])
        self.assertFalse(self.one("docs_create", doc_id="ok-id", content="x", logical_path="../escape")["ok"])
        self.assertEqual(self.store().list(1), [])

    def test_search_finds_own_content_with_provenance(self) -> None:
        self.activate()
        self.one("docs_create", doc_id="alpha", content=f"intro {MARK_A} tail")
        hit = self.one("docs_search", query=MARK_A.lower())["result"]
        self.assertEqual([m["doc_id"] for m in hit["matches"]], ["alpha"])
        self.assertTrue(hit["complete"])
        self.assertIn(MARK_A, hit["matches"][0]["snippet"])


class NoConsentGateTests(ContextCase):
    """The Owner's provider/model choice plus explicit activation is the only gate; the four-tier matrix does the rest."""

    def test_authorized_reads_work_immediately_without_any_disclosure_step(self) -> None:
        session_id = self.activate()
        self.store().write(1, "seed", MARK_A, actor_id="x")
        self.store().write(2, "seed", MARK_B, actor_id="x")
        own = self.direct(session_id, "docs_read", doc_id="seed")
        other = self.direct(session_id, "docs_read", doc_id="seed", project="P0002")
        self.assertIn(MARK_A, own["result"]["content"])
        self.assertEqual(other["error"]["code"], "cross_project_not_approved")
        self.assertNotIn(MARK_B, json.dumps(other))
        self.assertIn(MARK_SRC, self.direct(session_id, "source_read", path="README.md")["result"]["content"])
        self.assertTrue(self.direct(session_id, "docs_search", query="SYNTH")["result"]["matches"])
        briefing = self.direct(session_id, "briefing")["result"]
        self.assertEqual(briefing["architect_documents"]["status"], "listed")
        for tool, args in (("docs_list", {}), ("source_list", {}), ("source_search", {"query": "entry"})):
            self.assertTrue(self.direct(session_id, tool, **args)["ok"], tool)
        self.assertNotIn("consent_required", json.dumps(self.audit_outcomes()))

    def audit_outcomes(self) -> list[str]:
        return [row.outcome for row in self.audit()]

    def test_matrix_still_denies_what_it_never_allowed_and_the_disclosure_surface_is_gone(self) -> None:
        session_id = self.activate()
        self.assertEqual(self.direct(session_id, "docs_create", doc_id="x", content="c", project="P0002")["error"]["code"],
                         "write_other_project_denied")
        self.assertEqual(self.direct(session_id, "source_read", path="README.md", project="P0002")["error"]["code"],
                         "source_other_project_denied")
        self.assertEqual(self.client.put("/api/projects/P0001/architect/context-access/disclosure",
                                         json={"category": "project_source", "covered": "all", "enabled": True}).status_code, 404)
        self.assertFalse(hasattr(self.architect, "set_disclosure"))
        import rosc.models as models
        self.assertFalse(hasattr(models, "ProviderDisclosureScope"))

    def test_access_summary_is_an_owner_readable_description_not_a_set_of_switches(self) -> None:
        self.activate()
        view = self.client.get("/api/projects/P0001/architect/context-access").json()
        self.assertEqual(view["tools"]["transport"], "active")
        self.assertNotIn("disclosure", view)
        self.assertEqual((view["provider"]["provider_id"], view["provider"]["model_id"]), ("prov", "model-a"))
        self.assertIn("Owner-selected provider/model", view["provider"]["note"])
        self.assertEqual(view["source"], {"bound": True, "binding_version": 1, "docs_entrypoint": "README.md",
                                          "access": "read_only"})
        self.assertEqual([m["mode"] for m in view["matrix"]], ["read_only", "read_write", "read_only", "denied", "read_only", "denied"])
        self.assertFalse(view["publication"]["automatic_sync"])

    def test_agents_have_no_authority_over_the_summary(self) -> None:
        agent = ActorContext(ACTOR_AGENT, self.agent["agent_id"], "adapter_verified")
        self.assertFalse(evaluate(LocalOwnerPolicy(), agent, "architect.context.read", ResourceScope(project_id=1)).allowed)
        with self.assertRaises(Exception):
            self.architect.context_access(agent, 1)

    def test_opening_a_project_sends_nothing_and_starts_nothing(self) -> None:
        self.client.get("/api/projects")
        self.client.get("/api/projects/P0001/architect/context-access")
        self.assertEqual(self.client.get("/api/projects/P0001/architect").json()["state"], "stopped")
        self.assertIsNone(self.host._server)  # no listener until an explicit activation


class OtherProjectDocumentTests(ContextCase):
    """ROSC 1.0: an Architect reads ROSC-private documents of its own Project only."""

    def read_spy(self):
        """Record every DocumentStore read/list/search target, so a refusal is shown to precede any store access."""
        touched: list[tuple[str, int]] = []
        for name in ("list", "read", "list_trash"):
            original = getattr(ArchitectDocumentStore, name)

            def spy(store, project_id, *a, _n=name, _o=original, **k):
                touched.append((_n, project_id))
                return _o(store, project_id, *a, **k)
            patcher = patch.object(ArchitectDocumentStore, name, spy)
            patcher.start()
            self.addCleanup(patcher.stop)
        return touched

    def test_own_project_documents_work_with_absent_or_explicit_own_project(self) -> None:
        self.activate()
        self.store().write(1, "alpha", MARK_A, actor_id="owner", logical_path="notes/alpha")
        for extra in ({}, {"project": "P0001"}, {"project": ""}, {"project": 1}):
            listed = self.one("docs_list", **extra)["result"]
            self.assertEqual((listed["project_id"], listed["scope"], listed["total"]), ("P0001", "own", 1), extra)
            read = self.one("docs_read", doc_id="alpha", **extra)["result"]
            self.assertIn(MARK_A, read["content"])
            self.assertEqual((read["project_id"], read["scope"]), ("P0001", "own"))
            found = self.one("docs_search", query=MARK_A[:20].lower(), **extra)["result"]
            self.assertEqual([(m["project_id"], m["scope"]) for m in found["matches"]], [("P0001", "own")])
        paged = self.one("docs_list", limit=1, offset=0)["result"]
        self.assertEqual((len(paged["documents"]), paged["next_offset"]), (1, None))

    def test_other_project_documents_are_refused_before_any_store_access(self) -> None:
        self.activate()
        self.store().write(2, "beta-notes", MARK_B, actor_id="owner", logical_path="notes/beta")
        touched = self.read_spy()
        cases = (("docs_list", {"project": "P0002"}), ("docs_read", {"doc_id": "beta-notes", "project": "P0002"}),
                 ("docs_search", {"query": MARK_B[:20].lower(), "project": "P0002"}),
                 ("docs_search", {"query": MARK_B[:20].lower(), "project": "all"}),
                 ("docs_list", {"project": "all"}), ("docs_read", {"doc_id": "beta-notes", "project": "all"}),
                 ("docs_list", {"project": 2}), ("docs_list", {"project": "P0099"}))
        for tool, args in cases:
            outcome = self.one(tool, **args)
            self.assertEqual(outcome["error"]["code"], "cross_project_not_approved", (tool, args))
            self.assertNotIn(MARK_B, json.dumps(outcome), (tool, args))
        self.assertEqual(touched, [], "a refused call must not touch any DocumentStore")

    def test_malformed_project_arguments_are_invalid_and_never_normalised_to_the_own_project(self) -> None:
        session_id = self.activate()
        self.store().write(2, "beta-notes", MARK_B, actor_id="owner")
        touched = self.read_spy()
        for bad in ("2", "../P0002", "P0002/../P0001", "p0002", "p0001", " P0001", "P0001 ", " P0002 ", "P0002\n",
                    "P00010000000", "P0001/../P0002", True, False, 1.0, ["P0001"], ["P0002"], {"id": "P0001"}):
            for tool, extra in (("docs_list", {}), ("docs_read", {"doc_id": "beta-notes"}), ("docs_search", {"query": "synthetic"})):
                outcome = self.direct(session_id, tool, project=bad, **extra)
                self.assertFalse(outcome["ok"], (tool, bad))
                self.assertIn(outcome["error"]["code"], ("invalid_argument", "cross_project_not_approved"), (tool, bad))
                self.assertNotIn(MARK_B, json.dumps(outcome))
        self.assertEqual(self.direct(session_id, "docs_list", project=" P0001")["error"]["code"], "invalid_argument")
        self.assertEqual(self.direct(session_id, "docs_list", project=["P0001"])["error"]["code"], "invalid_argument")
        self.assertEqual(self.direct(session_id, "docs_list", project=True)["error"]["code"], "invalid_argument")
        self.assertEqual(touched, [])

    def test_other_project_documents_are_never_writable_and_unchanged(self) -> None:
        self.activate()
        self.store().write(2, "beta-notes", MARK_B, actor_id="owner", logical_path="notes/beta")
        before = digest(self.data / "workspace/projects/P0002/architect/documents/beta-notes/document.json")
        for tool, extra in (("docs_create", {"content": "x"}), ("docs_update", {"content": "x", "expected_revision": 1}),
                            ("docs_delete", {"expected_revision": 1}), ("docs_restore", {"trash_id": "x"})):
            args = {"doc_id": "beta-notes", **extra} if tool != "docs_restore" else dict(extra)
            denied = self.one(tool, project="P0002", **args)
            self.assertEqual(denied["error"]["code"], "write_other_project_denied", tool)
        self.assertEqual(before, digest(self.data / "workspace/projects/P0002/architect/documents/beta-notes/document.json"))
        self.assertEqual(self.store().list_trash(2), [])
        self.assertEqual(self.store().read(2, "beta-notes").info.revision, 1)
        self.assertEqual(self.store().list(1), [])  # nothing was created in the caller's own Project either

    def test_tool_descriptions_and_matrix_no_longer_promise_cross_project_reads(self) -> None:
        docs = {t["name"]: t for t in TOOLS if t["name"].startswith("docs_")}
        for name in ("docs_list", "docs_search", "docs_read"):
            text = json.dumps(docs[name]).lower()
            self.assertNotIn("'all'", text, name)
            self.assertNotIn("read-only)", text, name)
            self.assertIn("current project", text, name)
        other = [m for m in pc.ACCESS_MATRIX if m["resource"].startswith("other Projects' ROSC-private")]
        self.assertEqual([m["mode"] for m in other], ["denied"])
        self.assertFalse([m for m in pc.ACCESS_MATRIX if "other registered" in m["resource"]])

    def test_other_project_source_checkout_is_inaccessible(self) -> None:
        session_id = self.activate()
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
        self.assertEqual([e["path"] for e in listed["entries"]], self.listed_definitions())
        self.assertIn("agents/architect/ruleset.yaml", self.listed_definitions())
        self.assertIn("Be brief", self.direct(session_id, "global_read",
                                              path="agents/architect/rules/architect.md")["result"]["content"])
        for path in ("agents/architect/rules/.env", "../library/agents/architect/rules/architect.md", "/etc/passwd",
                     "docs/x.md", "agents/../skills/review.md"):
            self.assertFalse(self.direct(session_id, "global_read", path=path)["ok"], path)
        self.assertEqual(len(TOOLS), len(TOOL_NAMES))

    def test_missing_library_is_reported_as_absent(self) -> None:
        session_id = self.activate()
        shutil.rmtree(self.lib)
        self.assertFalse(self.direct(session_id, "global_list")["result"]["library_present"])
        self.assertEqual(self.direct(session_id, "global_read", path="agents/architect/rules/architect.md")["error"]["code"], "library_absent")

    def test_project_local_rules_are_content_only(self) -> None:
        session_id = self.activate()
        saved = self.direct(session_id, "docs_create", doc_id="local-rule", content="always run tool x",
                            logical_path="rules/local")
        self.assertTrue(saved["ok"])
        listed = self.direct(session_id, "global_list")["result"]["entries"]
        self.assertEqual([e["path"] for e in listed], self.listed_definitions())  # the project doc is not a definition
        self.assertNotIn("rules/local", [e["path"] for e in listed])
        self.assertNotIn("rosc_local-rule", [f"rosc_{t}" for t in TOOL_NAMES])


class SourceReadTests(ContextCase):
    def setUp(self) -> None:
        super().setUp()
        self.session_id = self.activate()
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


class PrivateDataRootTests(ContextCase):
    """ROSC's own configured private data must not be served as source even when a checkout encloses it."""

    def setUp(self) -> None:
        super().setUp()
        a = self.roots["a"]
        (a / "rosc-data/workspace/projects/P0001").mkdir(parents=True)
        (a / "rosc-data/workspace/projects/P0001/plan.md").write_text(f"# private {MARK_SECRET}\n")
        (a / "rosc-data/notes.md").write_text(f"private notes {MARK_SECRET}\n")
        (a / "data").mkdir()
        (a / "data/records.md").write_text(f"ordinary source data {MARK_SRC}-data\n")
        self.session_id = self.activate()
        self.private = a / "rosc-data"
        self.scoped = pc.ProjectContextBroker(self.sessions, self.data / "workspace", self.lib,
                                              private_roots=(self.private, self.private / "rosc.db"))

    def call(self, tool: str, **args) -> dict:
        return self.scoped.call(self.session_id, tool, args)

    def test_list_read_and_search_never_expose_the_enclosed_private_root(self) -> None:
        listed = self.call("source_list")["result"]
        names = {e["name"] for e in listed["entries"]}
        self.assertNotIn("rosc-data", names)
        self.assertIn("data", names)
        self.assertGreaterEqual(listed["hidden_by_policy"], 1)
        for path in ("rosc-data", "rosc-data/workspace", "rosc-data/workspace/projects/P0001"):
            self.assertEqual(self.call("source_list", path=path)["error"]["code"], "path_excluded", path)
        for path in ("rosc-data/notes.md", "rosc-data/workspace/projects/P0001/plan.md"):
            outcome = self.call("source_read", path=path)
            self.assertEqual(outcome["error"]["code"], "path_excluded", path)
            self.assertNotIn(MARK_SECRET, json.dumps(outcome))
        for kwargs in ({}, {"path": "rosc-data"}):
            found = self.call("source_search", query=MARK_SECRET, **kwargs)
            if "path" in kwargs:
                self.assertEqual(found["error"]["code"], "path_excluded")
            else:
                self.assertEqual(found["result"]["matches"], [])  # the root's private files were never scanned
                self.assertNotIn("rosc-data", json.dumps(found["result"]["matches"]))
        # the same Tool surface without the configured root WOULD have served it (the exclusion is what protects it)
        unscoped = self.broker.call(self.session_id, "source_read", {"path": "rosc-data/notes.md"})
        self.assertIn(MARK_SECRET, unscoped["result"]["content"])

    def test_an_ordinary_data_directory_and_normal_source_stay_readable(self) -> None:
        self.assertIn(MARK_SRC, self.call("source_read", path="data/records.md")["result"]["content"])
        self.assertEqual(self.call("source_list", path="data")["result"]["entries"][0]["name"], "records.md")
        hit = self.call("source_search", query=f"{MARK_SRC}-data")["result"]
        self.assertEqual([m["path"] for m in hit["matches"]], ["data/records.md"])
        self.assertIn(MARK_SRC, self.call("source_read", path="README.md")["result"]["content"])

    def test_cross_project_rosc_documents_are_refused(self) -> None:
        self.store().write(2, "seed", MARK_B, actor_id="x")
        outcome = self.call("docs_read", doc_id="seed", project="P0002")
        self.assertEqual(outcome["error"]["code"], "cross_project_not_approved")
        self.assertNotIn(MARK_B, json.dumps(outcome))

    def test_a_checkout_that_is_the_private_root_itself_is_refused(self) -> None:
        whole = pc.ProjectContextBroker(self.sessions, self.data / "workspace", self.lib,
                                        private_roots=(self.roots["a"],))
        outcome = whole.call(self.session_id, "source_read", {"path": "README.md"})
        self.assertEqual(outcome["error"]["code"], "root_unavailable")
        self.assertNotIn(MARK_SRC, json.dumps(outcome))

    def test_a_checkout_inside_the_private_root_is_refused_but_siblings_and_prefix_lookalikes_are_not(self) -> None:
        a = self.roots["a"]
        inside = pc.ProjectContextBroker(self.sessions, self.data / "workspace", self.lib, private_roots=(a.parent,))
        outcome = inside.call(self.session_id, "source_read", {"path": "README.md"})
        self.assertEqual(outcome["error"]["code"], "root_unavailable")
        self.assertNotIn(MARK_SRC, json.dumps(outcome))
        for tool, args in (("source_list", {}), ("source_search", {"query": "entry"}), ("source_git", {})):
            self.assertEqual(inside.call(self.session_id, tool, args)["error"]["code"], "root_unavailable", tool)
        # an unrelated private root, and one whose NAME merely starts with the checkout's name, do not block it
        lookalike = a.parent / (a.name + "-private")
        for private in (self.base / "elsewhere", lookalike):
            other = pc.ProjectContextBroker(self.sessions, self.data / "workspace", self.lib, private_roots=(private,))
            self.assertIn(MARK_SRC, other.call(self.session_id, "source_read", {"path": "README.md"})["result"]["content"])

    def test_registering_the_rosc_repository_itself_stays_possible_and_readable(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        outside = pc.ProjectContextBroker(self.sessions, self.data / "workspace", self.lib, private_roots=(self.data,))
        # a source root that is a checkout whose private data lives elsewhere (the usual ROSC layout) is unaffected
        self.assertTrue(outside.call(self.session_id, "source_list", {})["ok"])
        created = self.register(repo, mode="existing", name="ROSC itself")
        self.assertEqual(created.status_code, 201, created.text)

    def test_git_facts_do_not_name_private_paths(self) -> None:
        if not HAVE_GIT:
            self.skipTest("git unavailable")
        a = self.roots["a"]
        self.git(a, "add", "-f", "rosc-data/notes.md")
        self.git(a, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "tracked private")
        (a / "rosc-data/notes.md").write_text("changed\n")
        (a / "src/app.py").write_text("changed = 1\n")
        status = self.call("source_git", operation="status")["result"]
        self.assertEqual(status["changed_files"], ["src/app.py"])
        self.assertNotIn("rosc-data", json.dumps(status))

    def test_the_application_wires_the_actual_data_and_workspace_roots(self) -> None:
        wired = self.architect._context._private_roots
        for expected in (self.data, self.data / "rosc.db", self.data / "workspace"):
            self.assertIn(os.path.realpath(expected), wired)


class GitFactsTests(ContextCase):
    def setUp(self) -> None:
        super().setUp()
        if not HAVE_GIT:
            self.skipTest("git is not installed")
        self.session_id = self.activate()

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
        self.store().write(1, "adr-1", "x", actor_id="owner", logical_path="adr/one")
        briefing = self.direct(session_id, "briefing")["result"]
        self.assertEqual(briefing["kind"], "core_state")
        self.assertEqual(briefing["project"]["project_id"], "P0001")
        self.assertEqual(briefing["source_checkout"]["docs_entrypoint"], "README.md")
        self.assertEqual(briefing["source_checkout"]["access"], "read_only")
        self.assertEqual(briefing["architect_documents"]["count"], 1)
        self.assertEqual(briefing["core_facts"]["task_count"], 0)
        self.assertNotIn("git", json.dumps(briefing["core_facts"]).lower().replace("git evidence", ""))
        self.assertEqual(len(briefing["access_matrix"]), 6)

    def test_warm_turn_carries_only_the_owner_message_and_no_documents_or_source(self) -> None:
        self.activate()
        self.store().write(1, "adr-1", MARK_A, actor_id="owner")
        self.client.post("/api/projects/P0001/conversation/messages", json={"client_key": "plain-0001-xx", "body": "Hello"})
        self.architect.drain()
        self.assertEqual((self.bin / "last_prompt.txt").read_text(), "Hello")  # P029: exactly the new body
        self.assertEqual(self.prompts_log(), 1)
        with self.sessions() as session:
            categories = session.connection().exec_driver_sql(
                "SELECT context_categories FROM conversation_deliveries").fetchone()[0]
        self.assertEqual(categories, "owner_message")
        for second in ("two-0002-xxxx", "three-0003-xx"):  # no recap of earlier messages on later turns either
            self.client.post("/api/projects/P0001/conversation/messages", json={"client_key": second, "body": second})
            self.architect.drain()
            self.assertEqual((self.bin / "last_prompt.txt").read_text(), second)

    def prompts_log(self) -> int:
        path = self.bin / "prompts.log"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def test_runtime_without_tools_sends_only_the_owner_message_and_no_onboarding(self) -> None:
        runtime = p15.FakeRuntime()
        client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace", architect_runtime=runtime),
                            base_url="http://localhost")
        view = client.post("/api/projects/P0001/architect/activate").json()
        client.app.state.architect.drain()
        self.assertEqual(runtime.asked, [])  # nothing is sent at activation without a Tools transport
        shown = client.get("/api/projects/P0001/architect").json()
        self.assertEqual((view["state"], shown["onboarding"]),
                         ("ready", {"state": "blocked", "detail": "tools_not_supported"}))
        client.post("/api/projects/P0001/conversation/messages", json={"client_key": "legacy-0001-xx", "body": "Hi"})
        client.app.state.architect.drain()
        self.assertEqual(runtime.asked[0][1], "Hi")
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
        self.assertFalse({"architect_tool_audit"} & self.tables())
        client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace"), base_url="http://localhost")
        self.assertEqual(client.post("/api/projects", json={
            "name": "Kept", "description": "d", "root_path": str(self.new_root()), "mode": "new"}).status_code, 201)
        client.close()
        command.upgrade(self.alembic, "head")
        self.assertTrue({"architect_tool_audit"} <= self.tables())
        self.assertNotIn("provider_disclosure_scopes", self.tables())
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
