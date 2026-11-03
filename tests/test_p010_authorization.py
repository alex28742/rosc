from __future__ import annotations

import asyncio
import sqlite3
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from rosc import authorization as authz
from rosc.app import create_app
from rosc.architect_store import ArchitectDocumentService, AuthorizationDenied, DocumentInfo
from rosc.authorization import (ActorContext, AuthorizationDecision, LocalOwnerPolicy, LocalOwnerProvider, ResourceScope,
                                evaluate)
from rosc.db import make_session_factory

import test_slice01 as base

OWNER = LocalOwnerProvider().actor()
TASK = {"title": "T", "description": "D", "acceptance_criteria": "A"}
# Canonical UUIDv4 fixtures for the P011 registry scope; they are identifiers, never actor credentials.
AGENT_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
ASSIGNMENT_ID = "9b2d1f0e-6a3c-4c5b-8e7d-1f2a3b4c5d6e"


class DenyAll:
    def decide(self, actor, action, resource):
        return AuthorizationDecision(False, "test_denied")


class AgentProvider:
    def actor(self) -> ActorContext:
        return ActorContext(authz.ACTOR_AGENT, "agent:claim", authz.ASSURANCE_LOCAL_UNVERIFIED)


def scope_for(action: str) -> ResourceScope:
    required, _ = authz._SHAPES[action]
    return ResourceScope(project_id=1 if "project_id" in required else None, task_id=1 if "task_id" in required else None,
                         doc_id="notes" if "doc_id" in required else None,
                         agent_id=AGENT_ID if "agent_id" in required else None,
                         assignment_id=ASSIGNMENT_ID if "assignment_id" in required else None,
                         run_id=ASSIGNMENT_ID if "run_id" in required else None)


class ActorAndPolicyTests(unittest.TestCase):
    def test_local_owner_provider_is_stable_and_never_claims_authentication(self) -> None:
        first, second = LocalOwnerProvider().actor(), LocalOwnerProvider().actor()
        self.assertEqual(first, second)
        self.assertEqual((first.actor_kind, first.actor_id, first.assurance), ("owner", "local-owner", "local_unverified"))
        self.assertNotIn(first.assurance, ("authenticated", "verified"))
        with self.assertRaises(Exception):
            first.assurance = "authenticated"  # type: ignore[misc]

    def test_second_provider_yields_a_different_actor_through_the_same_interface(self) -> None:
        self.assertEqual(AgentProvider().actor().actor_kind, "agent")
        self.assertNotEqual(AgentProvider().actor(), OWNER)

    def test_owner_is_allowed_exactly_the_implemented_actions(self) -> None:
        # The 12 M2-B actions are frozen here; P011 adds Owner-only registry administration, P013 external Run
        # provenance and P014 the Owner-only Project Conversation to the same default-deny policy without changing
        # how those 12 behave.
        m2b_actions = frozenset({"project.list", "project.register", "project.reconcile", "project.repair", "task.list",
                                 "task.create", "task.reconcile", "task.repair", "architect_document.list",
                                 "architect_document.read", "architect_document.write", "architect_document.reconcile"})
        self.assertTrue(m2b_actions <= authz.IMPLEMENTED_ACTIONS)
        self.assertEqual(len(authz.IMPLEMENTED_ACTIONS), 27)
        policy = LocalOwnerPolicy()
        for action in sorted(authz.IMPLEMENTED_ACTIONS):
            self.assertTrue(evaluate(policy, OWNER, action, scope_for(action)).allowed, action)

    def test_unknown_and_reserved_actions_are_denied(self) -> None:
        for action in sorted(authz.RESERVED_OWNER_ACTIONS) + ["", "project.delete", "*", "PROJECT.LIST", None]:
            decision = evaluate(LocalOwnerPolicy(), OWNER, action, ResourceScope(project_id=1))  # type: ignore[arg-type]
            self.assertFalse(decision.allowed, action)
            self.assertEqual(decision.reason, "unknown_action")

    def test_agents_and_unprivileged_or_forged_actors_are_denied_everything(self) -> None:
        forged = [AgentProvider().actor(),
                  ActorContext("agent", "local-owner", "local_unverified"),
                  ActorContext("owner", "someone-else", "local_unverified"),
                  ActorContext("owner", "local-owner", "authenticated"),
                  ActorContext("owner", "local-owner", ""),
                  ActorContext("admin", "local-owner", "local_unverified"),
                  ActorContext("", "", ""), ActorContext(None, None, None)]  # type: ignore[arg-type]
        for actor in forged:
            for action in sorted(authz.IMPLEMENTED_ACTIONS):
                self.assertFalse(evaluate(LocalOwnerPolicy(), actor, action, scope_for(action)).allowed, (actor, action))
        for bogus in ("local-owner", {"actor_kind": "owner"}, None, 7):
            self.assertEqual(evaluate(LocalOwnerPolicy(), bogus, authz.PROJECT_LIST, ResourceScope()).reason, "untrusted_actor")

    def test_missing_contradictory_or_malformed_scope_is_denied(self) -> None:
        policy = LocalOwnerPolicy()
        bad = [(authz.PROJECT_RECONCILE, ResourceScope()), (authz.PROJECT_RECONCILE, ResourceScope(project_id=0)),
               (authz.PROJECT_RECONCILE, ResourceScope(project_id=True)),  # bool is not an id
               (authz.PROJECT_RECONCILE, ResourceScope(project_id="1")),  # type: ignore[arg-type]
               (authz.PROJECT_RECONCILE, ResourceScope(project_id=1, task_id=1)),
               (authz.PROJECT_LIST, ResourceScope(project_id=1)), (authz.PROJECT_REGISTER, ResourceScope(doc_id="x")),
               (authz.TASK_RECONCILE, ResourceScope(project_id=1)), (authz.TASK_REPAIR, ResourceScope(task_id=1)),
               (authz.TASK_CREATE, ResourceScope(project_id=1, task_id=2)),
               (authz.DOCUMENT_READ, ResourceScope(project_id=1)), (authz.DOCUMENT_WRITE, ResourceScope(project_id=1, doc_id=5)),  # type: ignore[arg-type]
               (authz.DOCUMENT_LIST, ResourceScope(project_id=1, doc_id="x")), (authz.DOCUMENT_RECONCILE, ResourceScope()),
               (authz.PROJECT_LIST, None), (authz.PROJECT_LIST, {"project_id": 1}),
               # P011 registry scope: a canonical agent/assignment id is required, and nothing else may be added.
               (authz.AGENT_READ, ResourceScope()), (authz.AGENT_READ, ResourceScope(agent_id="not-a-uuid")),
               (authz.AGENT_READ, ResourceScope(agent_id=AGENT_ID.upper())),
               (authz.AGENT_READ, ResourceScope(agent_id=AGENT_ID, project_id=1)),
               (authz.AGENT_UPDATE, ResourceScope(agent_id=AGENT_ID, task_id=1)),
               (authz.ASSIGNMENT_LIST, ResourceScope(agent_id=AGENT_ID)),
               (authz.ASSIGNMENT_REVOKE, ResourceScope(project_id=1)),
               (authz.ASSIGNMENT_REVOKE, ResourceScope(project_id=1, assignment_id=ASSIGNMENT_ID, doc_id="x")),
               (authz.ASSIGNMENT_CREATE, ResourceScope(project_id=1, assignment_id=ASSIGNMENT_ID))]
        for action, resource in bad:
            decision = evaluate(policy, OWNER, action, resource)
            self.assertFalse(decision.allowed, (action, resource))
            self.assertEqual(decision.reason, "invalid_resource")

    def test_a_permissive_or_broken_policy_cannot_bypass_the_canonical_checks(self) -> None:
        class AllowEverything:
            def decide(self, actor, action, resource):
                return AuthorizationDecision(True, "yes")

        class Boom:
            def decide(self, actor, action, resource):
                raise RuntimeError("path /home/secret")

        class Truthy:
            def decide(self, actor, action, resource):
                return True

        self.assertFalse(evaluate(AllowEverything(), OWNER, "project.unlink", ResourceScope(project_id=1)).allowed)  # reserved/unknown stays denied
        self.assertFalse(evaluate(AllowEverything(), OWNER, authz.PROJECT_RECONCILE, ResourceScope()).allowed)
        self.assertFalse(evaluate(AllowEverything(), "owner", authz.PROJECT_LIST, ResourceScope()).allowed)
        self.assertEqual(evaluate(Boom(), OWNER, authz.PROJECT_LIST, ResourceScope()).reason, "policy_error")
        self.assertEqual(evaluate(Truthy(), OWNER, authz.PROJECT_LIST, ResourceScope()).reason, "invalid_decision")


class ApiCase(base.StageCase):
    def app(self, **kwargs) -> TestClient:
        return TestClient(create_app(self.data / "rosc.db", self.data / "workspace", **kwargs), base_url="http://localhost")

    def state(self) -> dict:
        workspace = self.data / "workspace"
        tree = {str(p.relative_to(workspace)): (p.read_bytes() if p.is_file() else None) for p in sorted(workspace.rglob("*"))} if workspace.exists() else {}
        with sqlite3.connect(self.data / "rosc.db") as connection:
            rows = {t: connection.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                    for t in ("projects", "project_root_bindings", "tasks")}
        return {"tree": tree, "rows": rows}

    def seed(self) -> None:
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=TASK).status_code, 201)


class LocalOwnerApiTests(ApiCase):
    def test_existing_flows_work_for_the_controlled_local_owner(self) -> None:
        self.seed()
        self.assertEqual(self.client.get("/api/health").json()["agent_runtime"], "not_started")
        self.assertEqual(len(self.client.get("/api/projects").json()), 1)
        self.assertEqual(len(self.client.get("/api/projects/P0001/tasks").json()), 1)
        self.assertEqual(self.client.post("/api/projects/P0001/reconcile-workspace").status_code, 200)
        self.assertEqual(self.client.post("/api/projects/P0001/repair-workspace", json={"confirm_repair": True}).status_code, 200)
        self.assertEqual(self.client.post("/api/projects/P0001/tasks/T0001/reconcile-workspace").status_code, 200)
        self.assertEqual(self.client.post("/api/projects/P0001/tasks/T0001/repair-workspace", json={"confirm_repair": True}).status_code, 200)

    def test_confirmation_is_still_required_and_missing_things_are_404(self) -> None:
        self.seed()
        self.assertEqual(self.client.post("/api/projects/P0001/repair-workspace", json={"confirm_repair": False}).status_code, 422)
        self.assertEqual(self.client.post("/api/projects/P0009/reconcile-workspace").status_code, 404)
        self.assertEqual(self.client.get("/api/projects/P0009/tasks").status_code, 404)
        self.assertEqual(self.client.post("/api/projects/P0001/tasks/T0009/reconcile-workspace").status_code, 404)

    def test_cross_project_task_confusion_is_404_under_an_authorized_owner(self) -> None:
        self.seed()
        other = self.base / "other-project"
        other.mkdir()
        self.assertEqual(self.register(other).status_code, 201)
        before = self.state()
        self.assertEqual(self.client.post("/api/projects/P0002/tasks/T0001/reconcile-workspace").status_code, 404)
        self.assertEqual(self.client.post("/api/projects/P0002/tasks/T0001/repair-workspace", json={"confirm_repair": True}).status_code, 404)
        self.assertEqual(self.state(), before)

    def test_corrupt_identity_stays_503_and_authorization_does_not_repair_it(self) -> None:
        self.seed()
        with sqlite3.connect(self.data / "rosc.db") as connection:
            connection.execute("UPDATE projects SET project_uuid = 'not-a-uuid' WHERE id = 1")
        before = self.state()
        for url, body in (("/api/projects/P0001/reconcile-workspace", None),
                          ("/api/projects/P0001/repair-workspace", {"confirm_repair": True}),
                          ("/api/projects/P0001/tasks/T0001/reconcile-workspace", None),
                          ("/api/projects/P0001/tasks/T0001/repair-workspace", {"confirm_repair": True}),
                          ("/api/projects/P0001/tasks", TASK)):
            self.assertEqual(self.client.post(url, json=body).status_code, 503, url)
        self.assertEqual(self.client.get("/api/projects").status_code, 503)
        self.assertEqual(self.state(), before)


class DeniedApiTests(ApiCase):
    ROUTES = (("get", "/api/projects", None), ("post", "/api/projects/P0001/reconcile-workspace", None),
              ("post", "/api/projects/P0001/repair-workspace", {"confirm_repair": True}),
              ("get", "/api/projects/P0001/tasks", None), ("post", "/api/projects/P0001/tasks", TASK),
              ("post", "/api/projects/P0001/tasks/T0001/reconcile-workspace", None),
              ("post", "/api/projects/P0001/tasks/T0001/repair-workspace", {"confirm_repair": True}),
              # missing resources: authorization is decided before existence is revealed
              ("get", "/api/projects/P0009/tasks", None), ("post", "/api/projects/P0009/reconcile-workspace", None))

    def assert_everything_denied_without_side_effects(self, client: TestClient) -> None:
        before = self.state()
        new_root = self.base / "never-registered"
        new_root.mkdir()
        body = {"name": "x", "description": "x", "root_path": str(new_root), "mode": "new"}
        for method, url, payload in self.ROUTES + (("post", "/api/projects", body),):
            response = getattr(client, method)(url, **({"json": payload} if method == "post" else {}))
            self.assertEqual(response.status_code, 403, (url, response.text))
            self.assertEqual(response.json(), {"detail": "Not authorized for this operation"})
        self.assertEqual(self.state(), before)
        self.assertEqual(list(new_root.iterdir()), [])

    def test_injected_negative_policy_blocks_every_route_before_any_mutation(self) -> None:
        self.seed()
        self.assert_everything_denied_without_side_effects(self.app(authorization_policy=DenyAll()))

    def test_an_agent_context_is_unprivileged_on_every_route(self) -> None:
        self.seed()
        self.assert_everything_denied_without_side_effects(self.app(auth_provider=AgentProvider()))

    def test_request_supplied_actor_claims_cannot_raise_privileges(self) -> None:
        self.seed()
        denied = self.app(authorization_policy=DenyAll())
        before = self.state()
        headers = {"X-Actor-Role": "owner", "X-Actor-Id": "local-owner", "X-Actor-Kind": "owner",
                   "Authorization": "Bearer owner", "Cookie": "actor_kind=owner; role=owner",
                   "Forwarded": "for=127.0.0.1;host=localhost", "X-Forwarded-For": "127.0.0.1", "X-Forwarded-User": "owner"}
        for url in ("/api/projects/P0001/tasks", "/api/projects"):
            self.assertEqual(denied.get(url, headers=headers, params={"actor_id": "local-owner", "actor_kind": "owner"}).status_code, 403)
        spoofed = {**TASK, "actor_kind": "owner", "actor_id": "local-owner", "role": "owner"}
        self.assertEqual(denied.post("/api/projects/P0001/tasks", json=spoofed, headers=headers).status_code, 403)
        agent = self.app(auth_provider=AgentProvider())
        self.assertEqual(agent.post("/api/projects/P0001/tasks", json={**TASK, "actor_kind": "owner"}, headers=headers).status_code, 403)
        self.assertEqual(self.state(), before)

    def test_provider_returning_a_non_actor_fails_closed(self) -> None:
        class Broken:
            def actor(self):
                return "local-owner"

        self.assertEqual(self.app(auth_provider=Broken()).get("/api/projects").status_code, 403)


class RaisingProvider:
    """Realistic custom failure carrying a distinctive secret-like string."""
    SECRET = "SECRET-token-9f8e7d /home/als/private/path"

    def __init__(self) -> None:
        self.calls = 0

    def actor(self):
        self.calls += 1
        raise RuntimeError(self.SECRET)


class FlakyProvider:
    """Owner on the first call, then failing: a privileged actor must never be cached."""
    def __init__(self) -> None:
        self.calls = 0

    def actor(self):
        self.calls += 1
        if self.calls == 1:
            return OWNER
        raise ConnectionError("backend gone")


def raw_get(app, path: str, host_headers: list[bytes]) -> tuple[int, bytes]:
    """Drive the ASGI app directly so that repeated Host headers are represented exactly."""
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "server": ("127.0.0.1", 8000),
             "client": ("127.0.0.1", 50000), "root_path": "",
             "headers": [(b"host", h) for h in host_headers]}
    out: dict = {"status": None, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], out["body"]


class ProviderFailureTests(ApiCase):
    def test_raising_provider_is_a_safe_503_on_every_route_without_side_effects(self) -> None:
        self.seed()
        provider = RaisingProvider()
        client = self.app(auth_provider=provider)
        before = self.state()
        new_root = self.base / "never-registered-503"
        new_root.mkdir()
        body = {"name": "x", "description": "x", "root_path": str(new_root), "mode": "new"}
        for method, url, payload in DeniedApiTests.ROUTES + (("post", "/api/projects", body),):
            response = getattr(client, method)(url, **({"json": payload} if method == "post" else {}))
            self.assertEqual(response.status_code, 503, (url, response.text))
            self.assertEqual(response.json(), {"detail": "Actor context unavailable"})
            for leaked in ("SECRET", "token", "/home/", "RuntimeError", "Traceback"):
                self.assertNotIn(leaked, response.text)
        self.assertGreater(provider.calls, 0)
        self.assertEqual(self.state(), before)
        self.assertEqual(list(new_root.iterdir()), [])

    def test_health_does_not_consult_the_provider(self) -> None:
        provider = RaisingProvider()
        self.assertEqual(self.app(auth_provider=provider).get("/api/health").status_code, 200)
        self.assertEqual(provider.calls, 0)

    def test_no_privileged_actor_is_cached_between_requests(self) -> None:
        self.seed()
        provider = FlakyProvider()
        client = self.app(auth_provider=provider)
        self.assertEqual(client.get("/api/projects").status_code, 200)
        before = self.state()
        self.assertEqual(client.get("/api/projects").status_code, 503)
        self.assertEqual(client.post("/api/projects/P0001/tasks", json=TASK).status_code, 503)
        self.assertEqual(self.state(), before)

    def test_malformed_provider_returns_stay_a_403_without_side_effects(self) -> None:
        self.seed()
        before = self.state()
        for value in ("local-owner", None, {"actor_kind": "owner"}, 7, object()):
            class Odd:
                def actor(self, value=value):
                    return value
            client = self.app(auth_provider=Odd())
            self.assertEqual(client.get("/api/projects").status_code, 403, value)
            self.assertEqual(client.post("/api/projects/P0001/tasks", json=TASK).status_code, 403, value)
        self.assertEqual(self.state(), before)


class LocalRequestGuardTests(ApiCase):
    VALID_HOSTS = ("localhost", "127.0.0.1", "[::1]", "LOCALHOST", "LocalHost:5173", "localhost:5173", "localhost:4173",
                   "127.0.0.1:8000", "[::1]:8000", "[::1]:1", "localhost:65535")
    MALFORMED_HOSTS = ("[::1]evil", "[::1]:8000evil", "[::1", "::1", "::1:8000", "[::1]:", "[::1]:abc", "localhost:", "localhost:abc",
                       "localhost:65536", "localhost:0", "localhost:-1", "localhost:+80", "localhost:8000:evil",
                       "user@localhost", "user:pw@localhost:8000", "localhost/path", "localhost?x", "localhost#f", "localhost,evil.example",
                       "localhost evil", " localhost", "localhost ", "localhost\t", "localhost\r\n", "localhost.evil.example", "localhost.",
                       "0.0.0.0", "*", "*.localhost", "[::]", "[::ffff:127.0.0.1]", "[127.0.0.1]", "[localhost]", "[::1]:8000:1",
                       "127.0.0.1.evil.example", "evil.example", "")

    def test_valid_authorities_work(self) -> None:
        for host in self.VALID_HOSTS:
            self.assertEqual(self.client.get("/api/health", headers={"Host": host}).status_code, 200, host)

    def test_malformed_or_foreign_authorities_are_rejected_before_any_effect(self) -> None:
        self.seed()
        before = self.state()
        for host in self.MALFORMED_HOSTS:
            for method, url in (("get", "/api/health"), ("get", "/api/projects"), ("get", "/api/projects/P0001/tasks")):
                response = getattr(self.client, method)(url, headers={"Host": host})
                self.assertEqual(response.status_code, 403, (host, url))
                self.assertEqual(response.json(), {"detail": "Host is not allowed"})
            self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=TASK, headers={"Host": host}).status_code, 403, host)
            self.assertEqual(self.client.post("/api/projects/P0001/reconcile-workspace", headers={"Host": host}).status_code, 403, host)
        self.assertEqual(self.state(), before)

    def test_parser_rejects_what_a_client_cannot_send(self) -> None:
        from rosc.app import host_name
        for value in ("localhost:\u0661\u0662\u0663", "localhost\n", None, 7, b"localhost"):
            self.assertIsNone(host_name(value), value)  # type: ignore[arg-type]
        self.assertEqual(host_name("[::1]:8000"), "::1")
        self.assertEqual(host_name("LocalHost:5173"), "localhost")

    def test_missing_and_duplicate_host_headers_are_rejected(self) -> None:
        app = create_app(self.data / "rosc.db", self.data / "workspace")
        self.assertEqual(raw_get(app, "/api/health", [b"localhost"])[0], 200)
        for hosts in ([], [b"localhost", b"localhost"], [b"localhost", b"evil.example"], [b"evil.example", b"localhost"]):
            status, body = raw_get(app, "/api/health", hosts)
            self.assertEqual(status, 403, hosts)
            self.assertIn(b"Host is not allowed", body)

    def test_explicit_overrides_are_validated_as_strictly_as_defaults(self) -> None:
        extra = TestClient(create_app(self.data / "rosc.db", self.data / "workspace", extra_allowed_hosts=frozenset({"testserver"})))
        self.assertEqual(extra.get("/api/health", headers={"Host": "testserver:8000"}).status_code, 200)
        for host in ("testserver:", "testserver:x", "testserver evil", "testserver]", "testserver:99999"):
            self.assertEqual(extra.get("/api/health", headers={"Host": host}).status_code, 403, host)
        for bad in ("local host", "a:b", "[::1]", "host\n", "user@h", "h:80"):
            with self.assertRaises(ValueError):
                create_app(self.data / "rosc.db", self.data / "workspace", extra_allowed_hosts=frozenset({bad}))

    def test_hostile_host_is_rejected_for_every_method(self) -> None:
        self.seed()
        before = self.state()
        for host in ("evil.example", "localhost.evil.example", "127.0.0.1.evil.example", "evil.example:8000", "", "0.0.0.0"):
            self.assertEqual(self.client.get("/api/projects", headers={"Host": host}).status_code, 403, host)
            self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=TASK, headers={"Host": host}).status_code, 403, host)
        self.assertEqual(self.state(), before)

    def test_loopback_hosts_with_ports_work(self) -> None:
        for host in ("localhost", "localhost:5173", "127.0.0.1:8000", "[::1]:8000", "LOCALHOST"):
            self.assertEqual(self.client.get("/api/health", headers={"Host": host}).status_code, 200, host)

    def test_null_and_foreign_origins_cannot_mutate(self) -> None:
        self.seed()
        before = self.state()
        for origin in ("null", "http://evil.example", "https://localhost:5173", "http://localhost:9999", "http://localhost.evil.example:5173", "*"):
            headers = {"Origin": origin}
            self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=TASK, headers=headers).status_code, 403, origin)
            self.assertEqual(self.client.post("/api/projects/P0001/reconcile-workspace", headers=headers).status_code, 403, origin)
            self.assertEqual(self.client.get("/api/projects", headers=headers).status_code, 403, origin)
        self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=TASK, headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.assertEqual(self.state(), before)

    def test_preflight_from_a_foreign_site_is_refused_and_no_cors_is_granted(self) -> None:
        response = self.client.options("/api/projects", headers={"Origin": "http://evil.example",
                                                                 "Access-Control-Request-Method": "POST"})
        self.assertEqual(response.status_code, 403)
        ok = self.client.get("/api/projects", headers={"Origin": "http://localhost:5173"})
        self.assertEqual(ok.status_code, 200)
        self.assertFalse([h for h in ok.headers if h.lower().startswith("access-control-")], dict(ok.headers))

    def test_vite_origins_and_originless_direct_callers_work_and_stay_local_unverified(self) -> None:
        for origin in ("http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:4173", "http://[::1]:5173"):
            self.assertEqual(self.client.get("/api/projects", headers={"Origin": origin}).status_code, 200, origin)
        self.assertEqual(self.client.get("/api/projects", headers={"Sec-Fetch-Site": "same-origin"}).status_code, 200)
        # No Origin (direct non-browser caller) is accepted by the guard, but that is not authentication: the only
        # context any request can ever receive is the provider's local_unverified one.
        self.assertEqual(self.client.post("/api/projects", json={"name": "x"}).status_code, 422)
        self.assertEqual(LocalOwnerProvider().actor().assurance, "local_unverified")

    def test_forwarded_headers_do_not_change_host_checks(self) -> None:
        response = self.client.get("/api/projects", headers={"Host": "evil.example", "X-Forwarded-Host": "localhost",
                                                              "Forwarded": "host=localhost"})
        self.assertEqual(response.status_code, 403)

    def test_test_overrides_are_explicit_and_never_wildcards(self) -> None:
        explicit = TestClient(create_app(self.data / "rosc.db", self.data / "workspace", extra_allowed_hosts=frozenset({"testserver"}),
                                         extra_allowed_origins=frozenset({"http://testserver"})))
        self.assertEqual(explicit.get("/api/health", headers={"Origin": "http://testserver"}).status_code, 200)
        self.assertEqual(TestClient(create_app(self.data / "rosc.db", self.data / "workspace")).get("/api/health").status_code, 403)
        for kwargs in ({"extra_allowed_hosts": frozenset({"*"})}, {"extra_allowed_hosts": frozenset({""})},
                       {"extra_allowed_origins": frozenset({"*"})}, {"extra_allowed_origins": frozenset({"null"})},
                       {"extra_allowed_origins": frozenset({"https://evil.example"})}):
            with self.assertRaises(ValueError):
                create_app(self.data / "rosc.db", self.data / "workspace", **kwargs)


class ArchitectServiceAuthorizationTests(ApiCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.sessions = make_session_factory(self.data / "rosc.db")
        self.service = ArchitectDocumentService(self.sessions, self.data / "workspace", LocalOwnerPolicy())
        self.docs = self.data / "workspace/projects/P0001/architect"

    def test_owner_context_works_and_provenance_records_the_actor_id(self) -> None:
        info = self.service.write_document(OWNER, 1, "notes", "# n\n")
        self.assertEqual(info.revisions[-1].actor_id, "local-owner")
        self.assertEqual(self.service.read_document(OWNER, 1, "notes").text, "# n\n")
        self.assertEqual([d.doc_id for d in self.service.list_documents(OWNER, 1) if isinstance(d, DocumentInfo)], ["notes"])

    def test_denied_actors_get_no_read_write_list_or_reconcile_and_no_side_effects(self) -> None:
        self.service.write_document(OWNER, 1, "notes", "# n\n")
        before = self.state()
        deniers = [AgentProvider().actor(), ActorContext("owner", "local-owner", "authenticated"), "local-owner", None,
                   ActorContext("owner", "x", "local_unverified")]
        for actor in deniers:
            for call in (lambda a: self.service.list_documents(a, 1), lambda a: self.service.read_document(a, 1, "notes"),
                         lambda a: self.service.write_document(a, 1, "notes", "evil", expected_revision=1),
                         lambda a: self.service.write_document(a, 1, "brand-new", "evil"),
                         lambda a: self.service.reconcile_document(a, 1, "pending:notes:x")):
                with self.assertRaises(AuthorizationDenied):
                    call(actor)
        self.assertEqual(self.state(), before)
        self.assertFalse((self.docs / "documents/brand-new").exists())

    def test_denied_before_project_lookup_so_existence_is_not_revealed(self) -> None:
        with self.assertRaises(AuthorizationDenied):
            self.service.read_document(AgentProvider().actor(), 99, "notes")
        with self.assertRaises(AuthorizationDenied):
            self.service.list_documents(OWNER, 0)  # an invalid scope is a denial, not a lookup

    def test_negative_policy_leaves_documents_unchanged(self) -> None:
        guarded = ArchitectDocumentService(self.sessions, self.data / "workspace", DenyAll())
        before = self.state()
        with self.assertRaises(AuthorizationDenied):
            guarded.write_document(OWNER, 1, "notes", "x")
        self.assertEqual(self.state(), before)


if __name__ == "__main__":
    unittest.main()
