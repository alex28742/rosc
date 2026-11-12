from __future__ import annotations

import logging
import sqlite3
import threading
import unittest
import uuid

from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient

import test_slice01 as base
from rosc import authorization as authz
from rosc.agent_registry import (CAPABILITY_ARCHITECT_DOCUMENT_READ, CAPABILITY_ARCHITECT_DOCUMENT_WRITE,
                                 CAPABILITY_PROJECT_CONTEXT_READ, CAPABILITY_TASK_CONTEXT_READ, AgentRegistryService)
from rosc.app import create_app
from rosc.authorization import (ACTOR_AGENT, ASSURANCE_LOCAL_UNVERIFIED, ActorContext, AuthorizationDecision,
                                LocalOwnerPolicy, LocalOwnerProvider, ResourceScope, evaluate)
from rosc.conversation import ConversationDenied, ConversationNotFound, InvalidConversationInput, \
    ProjectConversationService
from rosc.db import make_session_factory

OWNER = LocalOwnerProvider().actor()
TASK = {"title": "T", "description": "D", "acceptance_criteria": "A"}
ARCHITECT_CAPS = [CAPABILITY_PROJECT_CONTEXT_READ, CAPABILITY_ARCHITECT_DOCUMENT_READ,
                  CAPABILITY_ARCHITECT_DOCUMENT_WRITE]
OWNED_TABLES = ("conversations", "conversation_participants", "conversation_messages")
LEGACY_TABLES = ("projects", "project_root_bindings", "tasks", "agent_definitions", "agent_capabilities",
                 "role_assignments", "assignment_capabilities", "run_records", "run_agent_links", "report_records",
                 "evidence_records")
HEAD = "0014_worker_sessions"  # P029 added 0009; P019 added 0008; P018 added 0007; P015 added 0006; P014 tables are created by 0005


class AgentProvider:
    """An agent-kind actor claiming registered-agent authority without any trusted runtime."""

    def __init__(self, agent_id: str = "agent:claim") -> None:
        self._agent_id = agent_id

    def actor(self) -> ActorContext:
        return ActorContext(ACTOR_AGENT, self._agent_id, ASSURANCE_LOCAL_UNVERIFIED)


class DenyAll:
    def decide(self, actor, action, resource):
        return AuthorizationDecision(False, "test_denied")


class ConversationCase(base.StageCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.register(self.new_root()).status_code, 201,
                         "Project registration must work before the conversation is exercised")
        self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=TASK).status_code, 201)

    def service(self, policy=None) -> ProjectConversationService:
        return ProjectConversationService(make_session_factory(self.data / "rosc.db"), policy or LocalOwnerPolicy())

    def second_project(self) -> None:
        path = self.base / "second-project"
        path.mkdir()
        self.assertEqual(self.register(path).status_code, 201)
        self.assertEqual(self.client.post("/api/projects/P0002/tasks", json=TASK).status_code, 201)

    def send(self, key: str = "key-00000001", body: str = "Owner text", project: str = "P0001"):
        return self.client.post(f"/api/projects/{project}/conversation/messages",
                                json={"client_key": key, "body": body})

    def fetch(self, statement: str, *args) -> list[tuple]:
        with sqlite3.connect(self.data / "rosc.db") as connection:
            return connection.execute(statement, args).fetchall()

    def sql(self, statement: str, *args) -> None:
        with sqlite3.connect(self.data / "rosc.db") as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(statement, args)

    def legacy_state(self, tables: tuple[str, ...] = LEGACY_TABLES + OWNED_TABLES) -> dict:
        workspace = self.data / "workspace"
        tree = {str(p.relative_to(workspace)): p.read_bytes() for p in sorted(workspace.rglob("*")) if p.is_file()}
        with sqlite3.connect(self.data / "rosc.db") as connection:
            rows = {table: connection.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid").fetchall()
                    for table in tables}
        return {"tree": tree, "rows": rows}

    def counts(self) -> dict:
        return {table: self.fetch(f"SELECT count(*) FROM {table}")[0][0] for table in OWNED_TABLES}


class EmptyStateTests(ConversationCase):
    def test_opening_a_project_creates_no_conversation_and_changes_nothing(self) -> None:
        before = self.legacy_state()
        response = self.client.get("/api/projects/P0001/conversation")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["project_id"], "P0001")
        self.assertIsNone(payload["conversation"])
        self.assertEqual(payload["messages"], [])
        self.assertEqual(payload["page"], {"limit": 50, "has_more": False, "next_before": None})
        self.assertEqual(self.legacy_state(), before)
        self.assertEqual(self.counts(), {table: 0 for table in OWNED_TABLES})
        # The Project still reports no running agent: a conversation read is not an activation.
        project = self.client.get("/api/projects").json()[0]
        self.assertEqual((project["architect"], project["active_workers"]), ("not_active", 0))
        self.assertEqual(self.client.get("/api/health").json()["agent_runtime"], "not_started")

    def test_unknown_and_malformed_project_ids_are_not_found(self) -> None:
        for path in ("P0009", "P01", "P1", "x", "P0001x"):
            response = self.client.get(f"/api/projects/{path}/conversation")
            self.assertEqual(response.status_code, 404, path)
        self.assertEqual(self.counts(), {table: 0 for table in OWNED_TABLES})


class OwnerMessageTests(ConversationCase):
    def test_owner_message_is_durable_attributed_and_survives_restart(self) -> None:
        before = self.legacy_state()
        created = self.send(body="Первый вопрос по проекту")
        self.assertEqual(created.status_code, 201, created.text)
        payload = created.json()
        self.assertFalse(payload["replayed"])
        message = payload["message"]
        self.assertTrue(authz.is_canonical_uuid4(message["message_id"]))
        self.assertTrue(authz.is_canonical_uuid4(message["conversation_id"]))
        self.assertEqual(message["ordinal"], 1)
        self.assertEqual(message["body"], "Первый вопрос по проекту")
        self.assertEqual(message["created_at"].endswith("Z"), True)
        self.assertEqual(message["sender"]["kind"], "owner")
        self.assertEqual(message["sender"]["actor_id"], "local-owner")
        self.assertEqual(message["sender"]["assurance"], "local_unverified")
        self.assertIsNone(message["sender"]["agent_id"])

        read = self.client.get("/api/projects/P0001/conversation").json()
        self.assertEqual(read["conversation"]["conversation_id"], message["conversation_id"])
        self.assertEqual(read["conversation"]["kind"], "project")
        self.assertIsNone(read["conversation"]["task_id"])
        self.assertEqual([m["message_id"] for m in read["messages"]], [message["message_id"]])

        after = self.legacy_state()
        for table in LEGACY_TABLES:
            self.assertEqual(after["rows"][table], before["rows"][table], table)
        self.assertEqual(after["tree"], before["tree"], "message text must never reach the Workspace")

        restarted = TestClient(create_app(self.data / "rosc.db", self.data / "workspace"), base_url="http://localhost")
        again = restarted.get("/api/projects/P0001/conversation").json()
        self.assertEqual([(m["message_id"], m["ordinal"], m["body"]) for m in again["messages"]],
                         [(message["message_id"], 1, "Первый вопрос по проекту")])
        self.assertEqual(restarted.get("/api/health").json()["agent_runtime"], "not_started")
        self.assertEqual(self.client.get("/api/projects").json()[0]["active_workers"], 0)

    def test_outer_whitespace_is_trimmed_and_newlines_are_preserved(self) -> None:
        created = self.send(body="  строка один\nстрока два\tконец  ")
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["message"]["body"], "строка один\nстрока два\tконец")

    def test_project_metadata_change_does_not_fork_the_conversation(self) -> None:
        first = self.send().json()["message"]
        self.sql("UPDATE projects SET name = 'Renamed', description = 'New text' WHERE id = 1")
        read = self.client.get("/api/projects/P0001/conversation").json()
        self.assertEqual(read["conversation"]["conversation_id"], first["conversation_id"])
        self.assertEqual([m["message_id"] for m in read["messages"]], [first["message_id"]])
        self.assertEqual(self.fetch("SELECT count(*) FROM conversations"), [(1,)])


class IdempotencyTests(ConversationCase):
    def test_same_key_and_body_replays_without_writing(self) -> None:
        first = self.send("key-aaaaaaa1", "Повтор")
        self.assertEqual(first.status_code, 201)
        before = self.legacy_state()
        replay = self.send("key-aaaaaaa1", "Повтор")
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["message"]["message_id"], first.json()["message"]["message_id"])
        self.assertEqual(self.legacy_state(), before)
        self.assertEqual(self.counts()["conversation_messages"], 1)

    def test_same_key_with_altered_body_conflicts_without_content_leak(self) -> None:
        self.assertEqual(self.send("key-bbbbbbb1", "исходный текст").status_code, 201)
        before = self.legacy_state()
        altered = self.send("key-bbbbbbb1", "другой текст")
        self.assertEqual(altered.status_code, 409)
        detail = altered.json()["detail"]
        self.assertNotIn("исходный текст", detail)
        self.assertNotIn("другой текст", detail)
        self.assertEqual(self.legacy_state(), before)

    def test_a_new_key_creates_a_new_ordered_message(self) -> None:
        bodies = ["первое", "второе", "третье"]
        for index, body in enumerate(bodies, start=1):
            self.assertEqual(self.send(f"key-cccccc0{index}", body).status_code, 201)
        messages = self.client.get("/api/projects/P0001/conversation").json()["messages"]
        self.assertEqual([(m["ordinal"], m["body"]) for m in messages],
                         [(index, body) for index, body in enumerate(bodies, start=1)])
        self.assertEqual(len({m["message_id"] for m in messages}), 3)


class IsolationTests(ConversationCase):
    def test_project_conversations_never_leak_between_projects(self) -> None:
        self.second_project()
        first = self.send("key-ddddddd1", "только для P0001").json()["message"]
        other = self.client.get("/api/projects/P0002/conversation").json()
        self.assertIsNone(other["conversation"])
        self.assertEqual(other["messages"], [])

        second = self.send("key-ddddddd2", "только для P0002", project="P0002").json()["message"]
        self.assertNotEqual(first["conversation_id"], second["conversation_id"])

        read_one = self.client.get("/api/projects/P0001/conversation").json()
        read_two = self.client.get("/api/projects/P0002/conversation").json()
        self.assertEqual([m["body"] for m in read_one["messages"]], ["только для P0001"])
        self.assertEqual([m["body"] for m in read_two["messages"]], ["только для P0002"])

        # A direct Core call is guarded exactly like the HTTP route: knowing another Project's id is not access.
        service = self.service()
        self.assertEqual([m["body"] for m in service.read_conversation(OWNER, 2)["messages"]], ["только для P0002"])
        with self.assertRaises(ConversationNotFound):
            service.read_conversation(OWNER, 999)
        self.assertEqual(self.counts(), {"conversations": 2, "conversation_participants": 2,
                                         "conversation_messages": 2})

    def test_read_is_bounded_and_pages_towards_older_messages(self) -> None:
        service = self.service()
        for index in range(1, 56):
            service.create_message(OWNER, 1, client_key=f"key-page{index:05d}", body=f"message {index:02d}")
        latest = self.client.get("/api/projects/P0001/conversation").json()
        self.assertEqual(len(latest["messages"]), 50)
        self.assertEqual([m["ordinal"] for m in latest["messages"]], list(range(6, 56)))
        self.assertEqual(latest["page"], {"limit": 50, "has_more": True, "next_before": 6})

        older = self.client.get("/api/projects/P0001/conversation?before=6").json()
        self.assertEqual([m["ordinal"] for m in older["messages"]], [1, 2, 3, 4, 5])
        self.assertEqual(older["page"], {"limit": 50, "has_more": False, "next_before": None})

        self.assertEqual(self.client.get("/api/projects/P0001/conversation?limit=100").status_code, 200)
        for query in ("limit=0", "limit=101", "before=0", "before=-2", "before=x", "limit=x"):
            self.assertEqual(self.client.get(f"/api/projects/P0001/conversation?{query}").status_code, 422, query)


class AuthorizationTests(ConversationCase):
    def test_architect_assignment_grants_no_conversation_authority(self) -> None:
        registry = AgentRegistryService(make_session_factory(self.data / "rosc.db"), LocalOwnerPolicy())
        agent = registry.register_agent(OWNER, display_name="Architect One", runtime_adapter="manual",
                                        provider_id="local", model_id="model-a", capabilities=ARCHITECT_CAPS)
        assignment = registry.create_assignment(OWNER, 1, agent_id=agent["agent_id"], role="architect",
                                                capabilities=ARCHITECT_CAPS)
        self.assertEqual(assignment["state"], "active")
        before = self.legacy_state()
        service = self.service()
        agent_actor = ActorContext(ACTOR_AGENT, agent["agent_id"], ASSURANCE_LOCAL_UNVERIFIED)
        with self.assertRaises(ConversationDenied):
            service.read_conversation(agent_actor, 1)
        with self.assertRaises(ConversationDenied):
            service.create_message(agent_actor, 1, client_key="key-eeeeeee1", body="agent claim")
        self.assertEqual(self.legacy_state(), before)
        self.assertEqual(self.counts(), {table: 0 for table in OWNED_TABLES})

    def test_http_agent_actor_is_denied_without_side_effects(self) -> None:
        app = create_app(self.data / "rosc.db", self.data / "workspace", auth_provider=AgentProvider())
        agent_client = TestClient(app, base_url="http://localhost")
        self.assertEqual(agent_client.get("/api/projects/P0001/conversation").status_code, 403)
        self.assertEqual(agent_client.post("/api/projects/P0001/conversation/messages",
                                           json={"client_key": "key-fffffff1", "body": "hi"}).status_code, 403)
        self.assertEqual(self.counts(), {table: 0 for table in OWNED_TABLES})

    def test_deny_all_policy_denies_both_operations(self) -> None:
        service = self.service(DenyAll())
        with self.assertRaises(ConversationDenied):
            service.read_conversation(OWNER, 1)
        with self.assertRaises(ConversationDenied):
            service.create_message(OWNER, 1, client_key="key-ggggggg1", body="hi")
        self.assertEqual(self.counts(), {table: 0 for table in OWNED_TABLES})

    def test_scope_shape_rejects_an_extra_or_missing_project(self) -> None:
        self.assertFalse(authz.evaluate(LocalOwnerPolicy(), OWNER, authz.CONVERSATION_READ, ResourceScope()).allowed)
        self.assertFalse(authz.evaluate(LocalOwnerPolicy(), OWNER, authz.CONVERSATION_READ,
                                        ResourceScope(project_id=1, doc_id="x")).allowed)
        self.assertFalse(authz.evaluate(LocalOwnerPolicy(), OWNER, authz.CONVERSATION_MESSAGE_CREATE,
                                        ResourceScope(project_id=True)).allowed)
        self.assertTrue(authz.evaluate(LocalOwnerPolicy(), OWNER, authz.CONVERSATION_MESSAGE_CREATE,
                                       ResourceScope(project_id=1)).allowed)


class InputValidationTests(ConversationCase):
    def test_body_and_key_constraints_are_enforced_without_writing(self) -> None:
        service = self.service()
        bad_bodies = ["", "   ", "\n\t ", "\x00", "bell\x07", "carriage\rreturn", "x" * 4001, 42, None]
        for body in bad_bodies:
            with self.assertRaises(InvalidConversationInput, msg=repr(body)[:40]):
                service.create_message(OWNER, 1, client_key="key-hhhhhh01", body=body)
        bad_keys = ["", "short", "x" * 129, "has space", "has/slash", "ellipsis…", 7, None]
        for key in bad_keys:
            with self.assertRaises(InvalidConversationInput, msg=repr(key)[:40]):
                service.create_message(OWNER, 1, client_key=key, body="text")
        with self.assertRaises(InvalidConversationInput):
            service.create_message(OWNER, 1, client_key="key-hhhhhh01", body="\ud800")
        self.assertEqual(self.counts(), {table: 0 for table in OWNED_TABLES})

        via_http = self.client.post("/api/projects/P0001/conversation/messages",
                                    json={"client_key": "short", "body": "text"})
        self.assertEqual(via_http.status_code, 422)
        oversized = self.client.post("/api/projects/P0001/conversation/messages",
                                     json={"client_key": "key-hhhhhh01", "body": "x" * 4001})
        self.assertEqual(oversized.status_code, 422)
        self.assertEqual(self.counts(), {table: 0 for table in OWNED_TABLES})

    def test_rejected_conversation_posts_never_echo_private_input(self) -> None:
        """P014.1: FastAPI's default 422 put the rejected body/key into ``input``; conversation routes must not."""
        marker = "PRIVATE-MARKER-7f3a"
        url = "/api/projects/P0001/conversation/messages"
        cases = {
            "oversized body": {"client_key": "key-jjjjjj01", "body": marker + "x" * 4001},
            "object body": {"client_key": "key-jjjjjj01", "body": {"secret": marker}},
            "number body": {"client_key": "key-jjjjjj01", "body": 123456789},
            "missing body": {"client_key": "key-jjjjjj01" + marker[:8]},
            "missing key": {"body": marker},
            "empty body": {"client_key": "key-jjjjjj01", "body": ""},
            "oversized key": {"client_key": marker + "k" * 130, "body": "text"},
            "key of wrong type": {"client_key": [marker], "body": marker},
            "key with forbidden characters": {"client_key": "has space " + marker, "body": marker},
            "control character body": {"client_key": "key-jjjjjj01", "body": marker + "\x00"},
        }
        records: list[logging.LogRecord] = []

        class Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler, root = Collect(level=logging.DEBUG), logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(previous)))
        for label, payload in cases.items():
            response = self.client.post(url, json=payload)
            self.assertEqual(response.status_code, 422, label)
            self.assertNotIn(marker, response.text, label)
            self.assertNotIn(marker[:8], response.text, label)
            self.assertNotIn('"input"', response.text, label)
        malformed = self.client.post(url, content=('{"body": "' + marker + '", broken').encode(),
                                     headers={"content-type": "application/json"})
        self.assertEqual(malformed.status_code, 422)
        self.assertNotIn(marker, malformed.text)
        self.assertNotIn('"input"', malformed.text)
        self.assertEqual(self.counts(), {table: 0 for table in OWNED_TABLES})
        self.assertTrue(records, "the log capture must be live for the absence check to mean anything")
        for record in records:
            self.assertNotIn(marker, record.getMessage())
            self.assertNotIn(marker[:8], str(record.args))
        # Sanitized errors keep the standard locator so a client can still tell which field failed.
        detail = self.client.post(url, json={"client_key": "key-jjjjjj01", "body": 5}).json()["detail"]
        self.assertEqual(detail[0]["loc"], ["body", "body"])
        self.assertEqual(detail[0]["type"], "string_type")

    def test_conversation_sanitizing_does_not_change_other_validation_contracts(self) -> None:
        response = self.client.post("/api/projects/P0001/tasks", json={"title": "only a title"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("input", response.json()["detail"][0])
        self.assertEqual(self.send("key-jjjjjj02", "ok").status_code, 201)
        self.assertEqual(self.send("key-jjjjjj02", "changed").status_code, 409)
        self.assertEqual(self.send("key-jjjjjj02", "ok").json()["replayed"], True)

    def test_maximum_length_body_is_accepted(self) -> None:
        body = "я" * 4000
        created = self.send("key-iiiiii01", body)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(len(created.json()["message"]["body"]), 4000)


class FailureAndIntegrityTests(ConversationCase):
    def test_failed_message_insert_rolls_back_conversation_and_participant(self) -> None:
        self.sql("CREATE TRIGGER fail_conversation_message BEFORE INSERT ON conversation_messages "
                 "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        try:
            response = self.send()
            self.assertEqual(response.status_code, 503, response.text)
        finally:
            self.sql("DROP TRIGGER fail_conversation_message")
        self.assertEqual(self.counts(), {table: 0 for table in OWNED_TABLES})
        self.assertEqual(self.client.get("/api/projects/P0001/conversation").json()["conversation"], None)
        self.assertEqual(self.send().status_code, 201)

    def test_conversation_records_are_immutable_via_direct_sql(self) -> None:
        self.send().json()
        for statement in (
            "UPDATE conversation_messages SET body = 'tampered'",
            "DELETE FROM conversation_messages",
            "UPDATE conversation_participants SET actor_id = 'other'",
            "DELETE FROM conversation_participants",
            "UPDATE conversations SET kind = 'task'",
            "DELETE FROM conversations",
        ):
            with self.assertRaises(sqlite3.IntegrityError, msg=statement):
                self.sql(statement)
        self.assertEqual(self.fetch("SELECT body FROM conversation_messages"), [("Owner text",)])
        self.assertEqual(self.fetch("SELECT count(*) FROM conversations"), [(1,)])

    def test_message_sender_must_belong_to_the_conversation(self) -> None:
        self.send()
        self.second_project()
        self.send("key-jjjjjjj1", "other project", project="P0002")
        participant = self.fetch("SELECT participant_id FROM conversation_participants ORDER BY rowid")[1][0]
        conversation = self.fetch("SELECT conversation_id FROM conversations ORDER BY rowid")[0][0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO conversation_messages (message_id, conversation_id, sender_participant_id, ordinal, "
                     "body, client_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     str(uuid.uuid4()), conversation, participant, 9, "cross", "key-kkkkkkk1",
                     "2026-11-03 00:00:00")

    def test_corrupt_project_identity_fails_closed(self) -> None:
        self.send()
        before = self.legacy_state()
        self.sql("UPDATE projects SET project_uuid = 'not-a-uuid' WHERE id = 1")
        corrupted = self.legacy_state()
        self.assertEqual(self.client.get("/api/projects/P0001/conversation").status_code, 503)
        self.assertEqual(self.send("key-lllllll1", "after corruption").status_code, 503)
        self.assertNotEqual(before, corrupted)
        self.assertEqual(self.legacy_state(), corrupted)
        self.assertEqual(self.counts()["conversation_messages"], 1)

    def test_missing_current_binding_fails_closed(self) -> None:
        before = self.legacy_state()
        self.sql("DELETE FROM project_root_bindings")
        self.assertNotEqual(self.legacy_state(), before)
        self.assertEqual(self.client.get("/api/projects/P0001/conversation").status_code, 503)
        self.assertEqual(self.send().status_code, 503)
        self.assertEqual(self.counts(), {table: 0 for table in OWNED_TABLES})


class ConcurrencyTests(ConversationCase):
    def test_concurrent_first_messages_keep_one_conversation_and_stable_order(self) -> None:
        barrier = threading.Barrier(2)
        results: list[dict] = []

        def attempt(index: int) -> None:
            service = self.service()
            barrier.wait()
            results.append(service.create_message(OWNER, 1, client_key=f"key-race000{index}", body=f"race {index}"))

        threads = [threading.Thread(target=attempt, args=(index,)) for index in (1, 2)]
        [thread.start() for thread in threads]
        [thread.join() for thread in threads]
        self.assertEqual(len(results), 2)
        self.assertEqual(len({r["message"]["conversation_id"] for r in results}), 1)
        self.assertEqual({r["message"]["ordinal"] for r in results}, {1, 2})
        self.assertEqual(self.fetch("SELECT count(*) FROM conversations"), [(1,)])
        self.assertEqual(self.fetch("SELECT count(*) FROM conversation_participants"), [(1,)])
        expected = sorted((r["message"]["ordinal"], r["message"]["body"]) for r in results)
        self.assertEqual(self.fetch("SELECT ordinal, body FROM conversation_messages ORDER BY ordinal"), expected)

    def test_concurrent_identical_retry_writes_one_message(self) -> None:
        barrier = threading.Barrier(3)
        results: list[dict] = []

        def attempt() -> None:
            service = self.service()
            barrier.wait()
            results.append(service.create_message(OWNER, 1, client_key="key-same0001", body="once"))

        threads = [threading.Thread(target=attempt) for _ in range(3)]
        [thread.start() for thread in threads]
        [thread.join() for thread in threads]
        self.assertEqual(len({r["message"]["message_id"] for r in results}), 1)
        self.assertEqual(sum(1 for r in results if not r["replayed"]), 1)
        self.assertEqual(self.fetch("SELECT count(*) FROM conversation_messages"), [(1,)])


class MigrationTests(ConversationCase):
    def test_single_head_and_additive_upgrade_from_populated_0004(self) -> None:
        script = ScriptDirectory.from_config(self.alembic)
        self.assertEqual(script.get_heads(), [HEAD])
        self.assertEqual(self.fetch("SELECT version_num FROM alembic_version"), [(HEAD,)])

        # Populate 0004-era state, then snapshot rows and Workspace bytes before adding the P014 tables.
        registry = AgentRegistryService(make_session_factory(self.data / "rosc.db"), LocalOwnerPolicy())
        agent = registry.register_agent(OWNER, display_name="Architect One", runtime_adapter="manual",
                                        provider_id="local", model_id="model-a", capabilities=ARCHITECT_CAPS)
        registry.create_assignment(OWNER, 1, agent_id=agent["agent_id"], role="architect", capabilities=ARCHITECT_CAPS)
        command.downgrade(self.alembic, "0004_run_provenance_foundation")
        self.assertEqual(self.fetch("SELECT version_num FROM alembic_version"),
                         [("0004_run_provenance_foundation",)])
        with sqlite3.connect(self.data / "rosc.db") as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE 'conversation%'").fetchone()[0], 0)
        before = self.legacy_state(LEGACY_TABLES)
        command.upgrade(self.alembic, "head")
        after = self.legacy_state()
        for table in LEGACY_TABLES:
            self.assertEqual(after["rows"][table], before["rows"][table], table)
        self.assertEqual(after["tree"], before["tree"])
        for table in OWNED_TABLES:
            self.assertEqual(after["rows"][table], [], table)
        self.assertEqual(self.fetch("SELECT version_num FROM alembic_version"), [(HEAD,)])
        self.assertEqual(self.client.get("/api/projects/P0001/conversation").json()["conversation"], None)
        self.assertEqual(self.send().status_code, 201)


if __name__ == "__main__":
    unittest.main()
