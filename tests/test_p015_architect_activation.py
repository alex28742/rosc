from __future__ import annotations

import os
import stat
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from alembic import command
from fastapi.testclient import TestClient

import test_slice01 as base
from rosc.agent_registry import (CAPABILITY_ARCHITECT_DOCUMENT_READ, CAPABILITY_ARCHITECT_DOCUMENT_WRITE,
                                 CAPABILITY_PROJECT_CONTEXT_READ, AgentRegistryService)
from rosc.app import create_app
import rosc.architect_runtime as runtime_module
from rosc.architect_runtime import (ENV_APPROVED_MODEL, ENV_BINARY, ENV_PASSWORD, OpenCodeRuntime, ReplyLookup,
                                    RuntimeAmbiguous, RuntimeHandle, RuntimeNotConfigured, RuntimeRefused,
                                    RuntimeStartUnresolved, RuntimeUnavailable, owned_process, process_ticks)
from rosc.architect_service import ArchitectDenied, ArchitectService
from rosc.authorization import ACTOR_AGENT, ASSURANCE_LOCAL_UNVERIFIED, ActorContext, LocalOwnerPolicy, LocalOwnerProvider
from rosc.db import make_session_factory

OWNER = LocalOwnerProvider().actor()
CAPS = [CAPABILITY_PROJECT_CONTEXT_READ, CAPABILITY_ARCHITECT_DOCUMENT_READ, CAPABILITY_ARCHITECT_DOCUMENT_WRITE]
TESTS = Path(__file__).resolve().parent


class FakeRuntime:
    """Deterministic provider used ONLY in tests. Mirrors the ``ArchitectRuntime`` protocol."""

    name = "opencode"

    def __init__(self) -> None:
        self.problem: str | None = None
        self.starts = 0
        self.asked: list[tuple[str, str]] = []
        self.alive = True
        self.reply: str | RuntimeAmbiguous | RuntimeRefused | RuntimeUnavailable = "Hello Owner"
        self.on_ask = None
        self.lookup_result = ReplyLookup("absent")
        self.stopped: list[str] = []
        self.start_error: Exception | None = None
        self.gate: threading.Event | None = None
        self.stop_result = True
        self.stop_error: Exception | None = None
        self.ask_started = threading.Event()

    def configuration_problem(self, provider_id: str, model_id: str) -> str | None:
        return self.problem

    def start(self, session_id, provider_id, model_id):
        self.starts += 1
        if self.start_error:
            raise self.start_error
        return RuntimeHandle(os.getpid(), 1, 9, "ses_fake")

    def probe(self, session_id, handle) -> bool:
        return self.alive

    def ask(self, handle, runtime_message_id, text, timeout):
        self.ask_started.set()
        if self.gate is not None:
            self.gate.wait(10)
        self.asked.append((runtime_message_id, text))
        if self.on_ask:
            self.on_ask()
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def lookup(self, handle, runtime_message_id):
        return self.lookup_result

    def stop(self, session_id, handle) -> bool:
        self.stopped.append(session_id)
        if self.stop_error:
            raise self.stop_error
        if self.stop_result:
            self.alive = False
        return self.stop_result


class ArchitectCase(base.StageCase):
    def setUp(self) -> None:
        super().setUp()
        self.runtime = FakeRuntime()
        self.client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace",
                                            architect_runtime=self.runtime), base_url="http://localhost")
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        self.sessions = make_session_factory(self.data / "rosc.db")
        self.architect: ArchitectService = self.client.app.state.architect

    def assign(self, adapter: str = "opencode", project: int = 1) -> dict:
        registry = AgentRegistryService(self.sessions, LocalOwnerPolicy())
        agent = registry.register_agent(OWNER, display_name="Architect One", runtime_adapter=adapter,
                                        provider_id="prov", model_id="model-a", capabilities=CAPS)
        assignment = registry.create_assignment(OWNER, project, agent_id=agent["agent_id"], role="architect",
                                                capabilities=CAPS)
        return {"agent": agent, "assignment": assignment}

    def send(self, key: str = "key-00000001", body: str = "Owner question", project: str = "P0001"):
        return self.client.post(f"/api/projects/{project}/conversation/messages",
                                json={"client_key": key, "body": body})

    def status(self, project: str = "P0001") -> dict:
        response = self.client.get(f"/api/projects/{project}/architect")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def activate(self, project: str = "P0001") -> dict:
        response = self.client.post(f"/api/projects/{project}/architect/activate")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def messages(self, project: str = "P0001") -> list[dict]:
        return self.client.get(f"/api/projects/{project}/conversation").json()["messages"]

    def delivery(self, message_id: str) -> dict:
        return next(m for m in self.messages() if m["message_id"] == message_id)["delivery"]


class ActivationTests(ArchitectCase):
    def test_opening_project_and_status_never_start_a_runtime(self) -> None:
        self.assign()
        self.client.get("/api/projects")
        self.client.get("/api/projects/P0001/conversation")
        view = self.status()
        self.assertEqual((view["state"], view["can_activate"]), ("stopped", True))
        self.assertEqual(self.runtime.starts, 0)
        self.assertEqual(self.client.get("/api/health").json()["agent_runtime"], "not_started")
        with self.sessions() as session:
            self.assertEqual(session.connection().exec_driver_sql("SELECT count(*) FROM architect_sessions")
                             .fetchone()[0], 0)

    def test_no_assignment_or_wrong_adapter_or_unconfigured_runtime_is_not_configured(self) -> None:
        self.assertEqual(self.status()["detail"], "no_active_architect_assignment")
        self.assertEqual(self.activate()["state"], "not_configured")
        self.assign(adapter="manual")
        self.assertEqual(self.status()["detail"], "unsupported_runtime_adapter")
        self.assertEqual(self.runtime.starts, 0)

    def test_unconfigured_runtime_never_reports_active(self) -> None:
        self.assign()
        self.runtime.problem = "runtime_credential_missing"
        view = self.activate()
        self.assertEqual((view["state"], view["detail"], view["can_activate"]),
                         ("not_configured", "runtime_credential_missing", False))
        self.assertEqual(self.runtime.starts, 0)

    def test_explicit_activation_reaches_ready_only_after_probe(self) -> None:
        self.assign()
        view = self.activate()
        self.assertEqual((view["state"], view["can_deactivate"]), ("ready", True))
        self.assertIsNotNone(view["session"]["verified_at"])
        self.assertEqual(self.runtime.starts, 1)
        self.assertEqual(self.activate()["state"], "ready")  # idempotent: no second runtime
        self.assertEqual(self.runtime.starts, 1)

    def test_failed_start_is_an_honest_error_and_recoverable(self) -> None:
        self.assign()
        self.runtime.start_error = RuntimeUnavailable("start_timeout")
        view = self.activate()
        self.assertEqual((view["state"], view["detail"]), ("error", "start_timeout"))
        self.runtime.start_error = None
        self.assertEqual(self.activate()["state"], "ready")

    def test_not_alive_after_start_is_error_and_stale_ready_is_reconciled(self) -> None:
        self.assign()
        self.runtime.alive = False
        self.assertEqual(self.activate()["state"], "error")
        self.runtime.alive = True
        self.assertEqual(self.activate()["state"], "ready")
        self.runtime.alive = False  # provider died: a database row must not keep claiming ready
        view = self.status()
        self.assertEqual((view["state"], view["detail"], view["can_activate"]),
                         ("error", "runtime_status_unknown", False))

    def test_unconfirmed_stop_blocks_duplicate_until_proven_termination(self) -> None:
        self.assign()
        self.activate()
        self.runtime.stop_result = False
        view = self.client.post("/api/projects/P0001/architect/deactivate").json()
        self.assertEqual((view["state"], view["detail"], view["can_activate"], view["can_deactivate"]),
                         ("error", "stop_unconfirmed", False, True))
        self.assertEqual(self.activate()["state"], "error")
        self.assertEqual(self.runtime.starts, 1)
        self.runtime.stop_result = True
        self.assertEqual(self.client.post("/api/projects/P0001/architect/deactivate").json()["state"], "stopped")
        self.runtime.alive = True
        self.assertEqual(self.activate()["state"], "ready")
        self.assertEqual(self.runtime.starts, 2)

    def test_unresolved_start_persists_identity_and_blocks_replacement(self) -> None:
        self.assign()
        handle = RuntimeHandle(321, 654, 987, "")
        self.runtime.start_error = RuntimeStartUnresolved("start_cleanup_unconfirmed", handle)
        view = self.activate()
        self.assertEqual((view["state"], view["detail"], view["can_activate"]),
                         ("error", "start_cleanup_unconfirmed", False))
        with self.sessions() as session:
            row = session.connection().exec_driver_sql(
                "SELECT state, process_pid, process_ticks, port, runtime_session_id FROM architect_sessions"
            ).fetchone()
        self.assertEqual(tuple(row), ("starting", 321, 654, 987, ""))
        self.activate()
        self.assertEqual(self.runtime.starts, 1)

    def test_unexpected_adapter_start_failure_blocks_when_spawn_outcome_is_unknown(self) -> None:
        self.assign()
        self.runtime.start_error = ValueError("adapter bug")
        view = self.activate()
        self.assertEqual((view["state"], view["detail"], view["can_activate"]),
                         ("error", "runtime_failure", False))
        self.activate()
        self.assertEqual(self.runtime.starts, 1)

    def test_restart_preserves_an_interrupted_start_as_a_live_blocker(self) -> None:
        self.assign()
        self.activate()
        with self.sessions() as session:
            session.connection().exec_driver_sql(
                "UPDATE architect_sessions SET state='starting', error_code=NULL"
            )
            session.commit()
        restarted = ArchitectService(self.sessions, LocalOwnerPolicy(), self.runtime)
        restarted.recover()
        view = restarted.status(OWNER, 1)
        self.assertEqual((view["state"], view["detail"], view["can_activate"]),
                         ("error", "interrupted", False))
        restarted.activate(OWNER, 1)
        self.assertEqual(self.runtime.starts, 1)

    def test_revoked_assignment_with_failed_stop_keeps_manual_blocker(self) -> None:
        made = self.assign()
        self.activate()
        self.runtime.stop_result = False
        self.client.post(f"/api/projects/P0001/assignments/{made['assignment']['assignment_id']}/revoke",
                         json={"expected_version": made["assignment"]["version"]})
        view = self.status()
        self.assertEqual((view["state"], view["detail"], view["can_activate"]),
                         ("error", "stop_unconfirmed", False))

    def test_revoked_assignment_stops_the_session(self) -> None:
        made = self.assign()
        self.activate()
        revoked = self.client.post(f"/api/projects/P0001/assignments/{made['assignment']['assignment_id']}/revoke",
                                   json={"expected_version": made["assignment"]["version"]})
        self.assertEqual(revoked.status_code, 200, revoked.text)
        view = self.status()
        self.assertEqual(view["state"], "not_configured")
        self.assertEqual(len(self.runtime.stopped), 1)

    def test_deactivate_and_concurrent_activation_start_one_runtime(self) -> None:
        self.assign()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.client.post(
            "/api/projects/P0001/architect/activate").json()["state"])) for _ in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(self.runtime.starts, 1)
        self.assertEqual(set(results), {"ready"})
        self.assertEqual(self.client.post("/api/projects/P0001/architect/deactivate").json()["state"], "stopped")

    def test_unknown_project_is_404_and_agent_actor_is_denied(self) -> None:
        self.assertEqual(self.client.get("/api/projects/P0009/architect").status_code, 404)
        self.assertEqual(self.client.post("/api/projects/P0009/architect/activate").status_code, 404)
        agent = ActorContext(ACTOR_AGENT, "agent:claim", ASSURANCE_LOCAL_UNVERIFIED)
        for call in (self.architect.status, self.architect.activate, self.architect.deactivate):
            with self.assertRaises(ArchitectDenied):
                call(agent, 1)
        self.assertEqual(self.runtime.starts, 0)


class DeliveryTests(ArchitectCase):
    def setUp(self) -> None:
        super().setUp()
        self.assign()

    def test_inactive_send_is_an_owner_only_save_with_no_delivery(self) -> None:
        created = self.send()
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["message"]["delivery"]["state"], "not_sent")
        self.architect.drain()
        self.assertEqual(self.runtime.asked, [])
        self.assertEqual(len(self.messages()), 1)

    def test_activation_does_not_replay_history_and_new_message_gets_one_reply(self) -> None:
        old = self.send(key="key-00000001", body="Before activation").json()["message"]
        self.activate()
        self.architect.drain()
        self.assertEqual(self.runtime.asked, [])
        self.assertEqual(self.delivery(old["message_id"])["state"], "not_sent")

        sent = self.send(key="key-00000002", body="After activation")
        self.assertEqual(sent.json()["message"]["delivery"]["state"], "pending")
        self.architect.drain()
        self.assertEqual(len(self.runtime.asked), 1)
        context = self.runtime.asked[0][1]
        self.assertIn("After activation", context)
        self.assertNotIn(str(self.base), context)  # no host path leaves ROSC
        messages = self.messages()
        self.assertEqual([m["sender"]["kind"] for m in messages], ["owner", "owner", "architect"])
        reply = messages[2]
        self.assertEqual(reply["body"], "Hello Owner")
        self.assertEqual(reply["sender"]["display_name"], "Architect")
        self.assertEqual(reply["sender"]["assurance"], "adapter_verified")
        self.assertEqual(reply["sender"]["agent_id"], self.status()["agent"]["agent_id"])
        self.assertNotIn("delivery", reply)
        delivery = messages[1]["delivery"]
        self.assertEqual((delivery["state"], delivery["reply_message_id"]), ("delivered", reply["message_id"]))

    def test_same_client_key_never_creates_a_second_message_or_reply(self) -> None:
        self.activate()
        first = self.send(key="key-00000002")
        self.architect.drain()
        again = self.send(key="key-00000002")
        self.architect.drain()
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertTrue(again.json()["replayed"])
        self.assertEqual(again.json()["message"]["delivery"]["state"], "delivered")
        self.assertEqual(len(self.runtime.asked), 1)
        self.assertEqual(len(self.messages()), 2)

    def test_reply_survives_restart_without_duplicates(self) -> None:
        self.activate()
        self.send()
        self.architect.drain()
        restarted = TestClient(create_app(self.data / "rosc.db", self.data / "workspace",
                                          architect_runtime=self.runtime), base_url="http://localhost")
        with restarted:
            messages = restarted.get("/api/projects/P0001/conversation").json()["messages"]
        self.assertEqual([m["sender"]["kind"] for m in messages], ["owner", "architect"])
        self.assertEqual(len(self.runtime.asked), 1)

    def test_provider_failures_keep_the_owner_message_and_claim_no_answer(self) -> None:
        self.activate()
        cases = [(RuntimeRefused("provider_error"), "failed", "provider_error"),
                 (RuntimeAmbiguous("timeout"), "unknown", "timeout"),
                 (RuntimeUnavailable("connection_failed"), "failed", "connection_failed")]
        for index, (reply, state, code) in enumerate(cases):
            self.runtime.reply = reply
            sent = self.send(key=f"key-0000010{index}", body=f"Q{index}").json()["message"]
            self.architect.drain()
            delivery = self.delivery(sent["message_id"])
            self.assertEqual((delivery["state"], delivery["error_code"]), (state, code))
        self.assertEqual([m["sender"]["kind"] for m in self.messages()], ["owner"] * 3)
        self.runtime.reply = "   "
        sent = self.send(key="key-00000200").json()["message"]
        self.architect.drain()
        self.assertEqual(self.delivery(sent["message_id"])["error_code"], "malformed_reply")
        self.runtime.reply = "x" * 4001
        sent = self.send(key="key-00000201").json()["message"]
        self.architect.drain()
        self.assertEqual(self.delivery(sent["message_id"])["error_code"], "malformed_reply")

    def test_ambiguous_attempt_is_never_auto_resent_and_reconcile_persists_a_seen_reply(self) -> None:
        self.activate()
        self.runtime.reply = RuntimeAmbiguous("timeout")
        sent = self.send().json()["message"]
        self.architect.drain()
        self.assertEqual(self.delivery(sent["message_id"])["state"], "unknown")
        self.assertEqual(self.client.post(f"/api/projects/P0001/conversation/messages/{sent['message_id']}/retry")
                         .status_code, 409)
        self.assertEqual(len(self.runtime.asked), 1)
        self.runtime.lookup_result = ReplyLookup("in_progress")
        outcome = self.client.post(f"/api/projects/P0001/conversation/messages/{sent['message_id']}/reconcile")
        self.assertEqual(outcome.json()["delivery"]["state"], "unknown")
        self.runtime.lookup_result = ReplyLookup("uncertain")
        outcome = self.client.post(f"/api/projects/P0001/conversation/messages/{sent['message_id']}/reconcile")
        self.assertEqual((outcome.json()["delivery"]["state"], outcome.json()["delivery"]["can_retry"]),
                         ("unknown", False))
        self.runtime.lookup_result = ReplyLookup("reply", "Late answer")
        outcome = self.client.post(f"/api/projects/P0001/conversation/messages/{sent['message_id']}/reconcile")
        self.assertEqual(outcome.json()["delivery"]["state"], "delivered")
        self.assertEqual(self.messages()[-1]["body"], "Late answer")
        again = self.client.post(f"/api/projects/P0001/conversation/messages/{sent['message_id']}/reconcile")
        self.assertEqual(again.json()["delivery"]["state"], "delivered")
        self.assertEqual(len(self.messages()), 2)
        self.assertEqual(len(self.runtime.asked), 1)

    def test_absent_at_provider_allows_one_safe_retry(self) -> None:
        self.activate()
        self.runtime.reply = RuntimeAmbiguous("connection_lost")
        sent = self.send().json()["message"]
        self.architect.drain()
        self.runtime.lookup_result = ReplyLookup("absent")
        url = f"/api/projects/P0001/conversation/messages/{sent['message_id']}"
        self.assertEqual(self.client.post(url + "/reconcile").json()["delivery"]["error_code"], "not_received")
        self.runtime.reply = "Second try"
        self.assertEqual(self.client.post(url + "/retry").json()["delivery"]["state"], "pending")
        self.architect.drain()
        self.assertEqual(self.delivery(sent["message_id"])["state"], "delivered")
        self.assertEqual(len(self.messages()), 2)

    def test_restart_turns_pending_into_unknown_and_resends_nothing(self) -> None:
        self.activate()
        sent = self.send().json()["message"]
        self.architect.drain()
        with self.sessions() as session:  # simulate a crash between request and persisting the reply
            connection = session.connection()
            connection.exec_driver_sql("UPDATE conversation_deliveries SET state='pending', reply_message_id=NULL")
            session.commit()
        self.architect.recover()
        self.assertEqual(self.delivery(sent["message_id"])["state"], "unknown")
        self.assertEqual(len(self.runtime.asked), 1)

    def test_session_replaced_or_assignment_revoked_during_the_turn_discards_the_reply(self) -> None:
        self.activate()
        with self.sessions() as session:
            row = session.connection().exec_driver_sql("SELECT assignment_id, version FROM role_assignments").fetchone()

        def revoke():
            self.client.post(f"/api/projects/P0001/assignments/{row[0]}/revoke", json={"expected_version": row[1]})

        self.runtime.on_ask = revoke
        sent = self.send().json()["message"]
        self.architect.drain()
        delivery = self.delivery(sent["message_id"])
        self.assertEqual((delivery["state"], delivery["error_code"]), ("unknown", "session_changed"))
        self.assertEqual([m["sender"]["kind"] for m in self.messages()], ["owner"])

    def test_deactivate_and_status_during_in_flight_reply_cannot_commit_it(self) -> None:
        self.activate()
        self.runtime.gate = threading.Event()
        sent = self.send().json()["message"]
        self.assertTrue(self.runtime.ask_started.wait(2))
        self.assertEqual(self.status()["state"], "ready")
        stopped = self.client.post("/api/projects/P0001/architect/deactivate").json()
        self.assertEqual(stopped["state"], "stopped")
        self.runtime.gate.set()
        self.architect.drain()
        self.assertEqual((self.delivery(sent["message_id"])["state"],
                          self.delivery(sent["message_id"])["error_code"]),
                         ("unknown", "session_changed"))
        self.assertEqual([m["sender"]["kind"] for m in self.messages()], ["owner"])

    def test_turns_for_one_project_are_serialized(self) -> None:
        self.activate()
        self.runtime.gate = threading.Event()
        first = self.send(key="key-00000301").json()["message"]
        second = self.send(key="key-00000302").json()["message"]
        self.assertEqual(self.delivery(second["message_id"])["state"], "pending")
        self.runtime.gate.set()
        self.architect.drain()
        self.assertEqual(len(self.runtime.asked), 2)
        self.assertEqual(self.delivery(first["message_id"])["state"], "delivered")
        self.assertEqual(self.delivery(second["message_id"])["state"], "delivered")

    def test_cross_project_leakage_is_impossible(self) -> None:
        path = self.base / "second"
        path.mkdir()
        self.assertEqual(self.register(path).status_code, 201)
        self.activate()
        sent = self.send().json()["message"]
        self.architect.drain()
        url = f"/api/projects/P0002/conversation/messages/{sent['message_id']}"
        self.assertEqual(self.client.post(url + "/reconcile").status_code, 404)
        self.assertEqual(self.client.post(url + "/retry").status_code, 404)
        self.assertEqual(self.client.get("/api/projects/P0002/conversation").json()["messages"], [])
        self.assertEqual(self.status("P0002")["state"], "not_configured")
        other = self.send(project="P0002", key="key-00000900")
        self.assertEqual(other.json()["message"]["delivery"]["state"], "not_sent")
        self.assertEqual(len(self.runtime.asked), 1)

    def test_request_cannot_choose_sender_or_session(self) -> None:
        self.activate()
        response = self.client.post("/api/projects/P0001/conversation/messages", json={
            "client_key": "key-00000400", "body": "forged", "sender": "architect", "participant_id": "x",
            "session_id": "y", "role": "architect"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["message"]["sender"]["kind"], "owner")
        self.architect.drain()
        self.assertEqual(self.client.post("/api/projects/P0001/architect/reply", json={"body": "x"}).status_code, 404)

    def test_no_worker_task_or_run_is_created(self) -> None:
        self.activate()
        self.send()
        self.architect.drain()
        with self.sessions() as session:
            for table in ("tasks", "run_records", "run_agent_links"):
                self.assertEqual(session.connection().exec_driver_sql(f"SELECT count(*) FROM {table}").fetchone()[0], 0)


class OpenCodeAdapterTests(unittest.TestCase):
    """Real adapter against the test-only fake server: Basic auth, process identity and restricted-mode checks."""

    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        launcher = self.bin / "opencode"
        launcher.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(TESTS)!r})\n"
                            "import fake_opencode\nfake_opencode.main()\n")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
        self.env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp.name, ENV_BINARY: str(launcher),
                    ENV_APPROVED_MODEL: "prov/model-a"}
        self.runtime = OpenCodeRuntime(root / "data", self.env)
        self.handles: list[tuple[str, RuntimeHandle]] = []

    def tearDown(self) -> None:
        for session_id, handle in self.handles:
            self.runtime.stop(session_id, handle)

    def start(self, mode: str = "ok") -> tuple[str, RuntimeHandle]:
        (self.bin / "mode").write_text(mode)
        session_id = str(uuid.uuid4())
        handle = self.runtime.start(session_id, "prov", "model-a")
        self.handles.append((session_id, handle))
        return session_id, handle

    def test_configuration_is_honest(self) -> None:
        self.assertIsNone(self.runtime.configuration_problem("prov", "model-a"))
        self.assertEqual(self.runtime.configuration_problem("prov", "other"), "model_not_approved")
        for key, problem in ((ENV_BINARY, "runtime_binary_missing"), (ENV_APPROVED_MODEL, "model_not_approved")):
            env = {k: v for k, v in self.env.items() if k != key}
            if key == ENV_BINARY:
                env["PATH"] = ""
            self.assertEqual(OpenCodeRuntime(Path(self.tmp.name), env).configuration_problem("prov", "model-a"),
                             problem)
        unapproved = {k: v for k, v in self.env.items() if k != ENV_APPROVED_MODEL}
        with self.assertRaises(RuntimeNotConfigured):
            OpenCodeRuntime(Path(self.tmp.name), unapproved).start(str(uuid.uuid4()), "prov", "model-a")

    def test_no_ambient_password_is_needed_and_configuration_never_creates_a_secret(self) -> None:
        self.assertNotIn(ENV_PASSWORD, self.env)
        self.assertIsNone(self.runtime.configuration_problem("prov", "model-a"))
        self.assertFalse((Path(self.tmp.name) / "data" / "architect-runtime" / ".secrets").exists())

    def test_start_proves_identity_probe_ask_and_ownership_stop(self) -> None:
        session_id, handle = self.start()
        self.assertTrue(self.runtime.probe(session_id, handle))
        self.assertEqual(self.runtime.ask(handle, "msg_rosc1", "hi", 5), "Fake architect reply")
        self.assertEqual(self.runtime.lookup(handle, "msg_rosc1"), ReplyLookup("reply", "Fake architect reply"))
        self.assertEqual(self.runtime.lookup(handle, "msg_other").status, "absent")
        self.assertFalse(self.runtime.probe(str(uuid.uuid4()), handle))  # a different session marker is not ours
        self.assertTrue(self.runtime.stop(session_id, handle))
        self.assertFalse(self.runtime.probe(session_id, handle))

    def test_cursor_lookup_covers_long_history_and_repeated_end_match(self) -> None:
        session_id, handle = self.start("long_match")
        self.assertEqual(self.runtime.ask(handle, "msg_rosc_long", "hi", 5), "Fake architect reply")
        expected = ReplyLookup("reply", "Fake architect reply")
        self.assertEqual(self.runtime.lookup(handle, "msg_rosc_long"), expected)
        self.assertEqual(self.runtime.lookup(handle, "msg_rosc_long"), expected)
        self.assertEqual(self.runtime.lookup(handle, "msg_missing"), ReplyLookup("absent"))

    def test_incomplete_lookup_never_claims_absence_and_page_failure_is_ambiguous(self) -> None:
        session_id, handle = self.start("long_history")
        with patch.object(runtime_module, "MAX_LOOKUP_MESSAGES", 300):
            self.assertEqual(self.runtime.lookup(handle, "msg_missing"), ReplyLookup("uncertain"))
        self.runtime.stop(session_id, handle)
        self.handles.remove((session_id, handle))
        session_id, handle = self.start("pagination_error")
        with self.assertRaises(RuntimeAmbiguous) as caught:
            self.runtime.lookup(handle, "msg_missing")
        self.assertEqual(caught.exception.code, "lookup_failed")

    def test_password_is_not_in_the_process_command_line(self) -> None:
        session_id, handle = self.start()
        cmdline = Path(f"/proc/{handle.pid}/cmdline").read_bytes()
        self.assertNotIn(self.secret(session_id).encode(), cmdline)
        self.assertIn(b"127.0.0.1", cmdline)

    def test_child_home_xdg_and_logs_are_session_local_and_private(self) -> None:
        session_id, handle = self.start()
        environ = Path(f"/proc/{handle.pid}/environ").read_bytes().split(b"\0")
        values = dict(item.split(b"=", 1) for item in environ if b"=" in item)
        workdir = Path(self.tmp.name) / "data" / "architect-runtime" / session_id
        self.assertEqual(values[b"HOME"], str(workdir / "home").encode())
        for key, directory in ((b"XDG_CONFIG_HOME", "config"), (b"XDG_DATA_HOME", "data"),
                               (b"XDG_CACHE_HOME", "cache"), (b"XDG_STATE_HOME", "state")):
            self.assertEqual(values[key], str(workdir / directory).encode())
        self.assertEqual(stat.S_IMODE(workdir.stat().st_mode), 0o700)
        self.assertFalse((workdir.parent / "logs" / f"{session_id}.log").exists())

    def test_unrestricted_session_is_refused_before_any_model_request(self) -> None:
        with self.assertRaises(RuntimeUnavailable) as caught:
            self.start("unrestricted")
        self.assertEqual(caught.exception.code, "restricted_mode_unconfirmed")
        with self.assertRaises(RuntimeUnavailable) as caught:
            self.start("leaky_permission")
        self.assertEqual(caught.exception.code, "restricted_mode_unconfirmed")

    def test_provider_error_is_a_refusal_not_a_reply(self) -> None:
        session_id, handle = self.start("refuse")
        with self.assertRaises(RuntimeRefused):
            self.runtime.ask(handle, "msg_rosc2", "hi", 5)

    def test_foreign_process_is_never_owned_or_stopped(self) -> None:
        mine = os.getpid()
        self.assertFalse(owned_process(mine, process_ticks(mine), str(uuid.uuid4())))
        self.assertFalse(self.runtime.stop(str(uuid.uuid4()), RuntimeHandle(mine, process_ticks(mine) or 0, 9, "x")))
        self.assertIsNone(process_ticks(2 ** 22 + 12345))

    def test_signal_and_identity_failures_never_report_a_stop(self) -> None:
        handle = RuntimeHandle(12345, 678, 9, "ses")
        with patch.object(runtime_module.OpenCodeRuntime, "_termination_confirmed", return_value=False), \
                patch.object(runtime_module, "owned_group_leader", return_value=False), \
                patch.object(runtime_module.os, "killpg") as killpg:
            self.assertFalse(self.runtime.stop("owner", handle))
            killpg.assert_not_called()
        with patch.object(runtime_module.OpenCodeRuntime, "_termination_confirmed", return_value=False), \
                patch.object(runtime_module, "owned_group_leader", return_value=True), \
                patch.object(runtime_module.OpenCodeRuntime, "_wait_group_absent", return_value=False), \
                patch.object(runtime_module.os, "killpg", side_effect=[None, PermissionError()]):
            self.assertFalse(self.runtime.stop("owner", handle))
        with patch.object(runtime_module.OpenCodeRuntime, "_termination_confirmed", return_value=False), \
                patch.object(runtime_module, "owned_group_leader", return_value=True), \
                patch.object(runtime_module.OpenCodeRuntime, "_wait_group_absent", return_value=False), \
                patch.object(runtime_module.os, "killpg") as killpg:
            self.assertFalse(self.runtime.stop("owner", handle))
            self.assertEqual(killpg.call_count, 2)

    def test_reused_or_unreadable_pid_is_never_signalled(self) -> None:
        handle = RuntimeHandle(12345, 678, 9, "ses")
        with patch.object(runtime_module.OpenCodeRuntime, "_group_absent", return_value=True), \
                patch.object(runtime_module, "process_group_identity", return_value=(999, 2, 2)), \
                patch.object(runtime_module.os, "killpg") as killpg:
            self.assertTrue(self.runtime.stop("owner", handle))  # original identity is proven absent
            killpg.assert_not_called()
        with patch.object(runtime_module.OpenCodeRuntime, "_group_absent", return_value=True), \
                patch.object(runtime_module, "process_group_identity", return_value=None), \
                patch.object(runtime_module.os, "kill", side_effect=PermissionError()), \
                patch.object(runtime_module.os, "killpg") as killpg:
            self.assertFalse(self.runtime.stop("owner", handle))
            killpg.assert_not_called()

    def secrets_dir(self) -> Path:
        return Path(self.tmp.name) / "data" / "architect-runtime" / ".secrets"

    def secret(self, session_id: str) -> str:
        return (self.secrets_dir() / session_id).read_text()

    def restarted(self, env: dict | None = None) -> OpenCodeRuntime:
        """A new backend object over the same data directory: no in-memory state survives."""
        fresh = OpenCodeRuntime(Path(self.tmp.name) / "data", self.env if env is None else env)
        # Only so the test process can reap the child it spawned (a real orphan is reaped by init); no secret is shared.
        fresh._children = self.runtime._children
        return fresh

    def test_wrong_secret_cannot_talk_to_the_server(self) -> None:
        session_id, handle = self.start()
        other_id, other_handle = self.start()  # a second Project's runtime has its own, different secret
        self.assertNotEqual(self.secret(session_id), self.secret(other_id))
        fresh = self.restarted()
        fresh._passwords[session_id] = self.secret(other_id)  # present Project B's secret to Project A's server
        with self.assertRaises(RuntimeRefused) as caught:
            fresh._call(session_id, handle.port, "GET", "/api/info")
        self.assertEqual(caught.exception.code, "http_401")
        self.assertFalse(fresh.probe(session_id, handle))
        # no Authorization header at all is rejected as well
        import urllib.error
        import urllib.request
        with self.assertRaises(urllib.error.HTTPError) as unauth:
            urllib.request.urlopen(f"http://127.0.0.1:{handle.port}/api/info", timeout=3)
        self.assertEqual(unauth.exception.code, 401)

    def test_secret_is_generated_per_incarnation_strong_and_private(self) -> None:
        first_id, first = self.start()
        second_id, second = self.start()
        a, b = self.secret(first_id), self.secret(second_id)
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 43)  # 32 random bytes, urlsafe base64
        self.assertEqual(stat.S_IMODE(self.secrets_dir().stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.secrets_dir() / first_id).stat().st_mode), 0o600)
        child_env = dict(item.split(b"=", 1) for item in Path(f"/proc/{first.pid}/environ").read_bytes().split(b"\0")
                         if b"=" in item)
        self.assertEqual(child_env[b"OPENCODE_PASSWORD"].decode(), a)  # the client uses the very same value
        self.assertEqual(self.runtime.ask(first, "msg_rosc1", "hi", 5), "Fake architect reply")

    def test_ambient_password_is_ignored_and_never_shared(self) -> None:
        ambient = dict(self.env, **{ENV_PASSWORD: "ambient-" + "x" * 24, "OPENCODE_SERVER_PASSWORD": "y" * 24})
        runtime = OpenCodeRuntime(Path(self.tmp.name) / "data", ambient)
        (self.bin / "mode").write_text("ok")
        sid = str(uuid.uuid4())
        handle = runtime.start(sid, "prov", "model-a")
        self.addCleanup(runtime.stop, sid, handle)
        self.assertNotEqual(self.secret(sid), ambient[ENV_PASSWORD])
        environ = Path(f"/proc/{handle.pid}/environ").read_bytes()
        self.assertNotIn(b"ambient-", environ)
        self.assertNotIn(b"OPENCODE_SERVER_PASSWORD", environ)

    def test_restarted_backend_recovers_probe_and_stop_with_the_private_secret(self) -> None:
        session_id, handle = self.start()
        fresh = self.restarted()
        self.assertTrue(fresh.probe(session_id, handle))
        self.assertEqual(fresh.ask(handle, "msg_rosc9", "hi", 5), "Fake architect reply")
        self.assertTrue(fresh.stop(session_id, handle))
        self.assertFalse((self.secrets_dir() / session_id).exists())  # removed only after confirmed termination
        self.assertFalse(Path(f"/proc/{handle.pid}").exists())

    def test_missing_or_unsafe_recovery_secret_fails_closed_but_stop_still_works(self) -> None:
        session_id, handle = self.start()
        path = self.secrets_dir() / session_id
        good = path.read_text()
        for damage in ("missing", "world_readable", "symlink", "short"):
            path.unlink(missing_ok=True)
            if damage == "world_readable":
                path.write_text(good)
                path.chmod(0o644)
            elif damage == "symlink":
                target = Path(self.tmp.name) / "elsewhere"
                target.write_text(good)
                target.chmod(0o600)
                path.symlink_to(target)
            elif damage == "short":
                path.write_text("abc")
                path.chmod(0o600)
            fresh = self.restarted()
            self.assertFalse(fresh.probe(session_id, handle), damage)  # not proof the child died
            with self.assertRaises(RuntimeNotConfigured) as caught:
                fresh.ask(handle, "msg_rosc3", "hi", 5)
            self.assertEqual(caught.exception.code, "runtime_secret_missing")
            self.assertTrue(owned_process(handle.pid, handle.ticks, session_id), damage)  # still running
        path.unlink(missing_ok=True)
        path.write_text(good)
        path.chmod(0o600)
        self.assertTrue(self.restarted().probe(session_id, handle))
        path.unlink()
        self.assertTrue(self.restarted().stop(session_id, handle))  # /proc identity, not the password, authorizes stop

    def test_secret_path_cannot_be_traversed_or_overwritten(self) -> None:
        for bad in ("../x", "a/b", "", "..", "not-a-uuid"):
            self.assertIsNone(self.runtime._secret_path(bad))
            with self.assertRaises(RuntimeUnavailable) as caught:
                self.runtime._provision_secret(bad)
            self.assertEqual(caught.exception.code, "secret_provisioning_failed")
        session_id = str(uuid.uuid4())
        self.runtime._provision_secret(session_id)
        with self.assertRaises(RuntimeUnavailable):  # exclusive create: a collision is never reused
            self.runtime._provision_secret(session_id)
        self.runtime._discard_secret(session_id)

    def test_csprng_failure_spawns_nothing(self) -> None:
        (self.bin / "mode").write_text("ok")
        sid = str(uuid.uuid4())
        with patch.object(runtime_module.secrets, "token_urlsafe", side_effect=NotImplementedError()), \
                patch.object(runtime_module.subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeUnavailable) as caught:
                self.runtime.start(sid, "prov", "model-a")
        self.assertEqual(caught.exception.code, "secret_provisioning_failed")
        popen.assert_not_called()
        self.assertFalse((self.secrets_dir() / sid).exists())

    def test_failed_startup_with_confirmed_stop_removes_the_secret(self) -> None:
        sid = str(uuid.uuid4())
        (self.bin / "mode").write_text("unrestricted")
        with self.assertRaises(RuntimeUnavailable):
            self.runtime.start(sid, "prov", "model-a")
        self.assertFalse((self.secrets_dir() / sid).exists())

    def test_unconfirmed_stop_keeps_the_secret_for_later_recovery(self) -> None:
        session_id, handle = self.start()
        with patch.object(runtime_module.OpenCodeRuntime, "_terminate", return_value=False):
            self.assertFalse(self.runtime.stop(session_id, handle))
        self.assertTrue((self.secrets_dir() / session_id).exists())

    def test_server_rejecting_our_secret_is_reported_distinctly_and_quickly(self) -> None:
        sid = str(uuid.uuid4())
        (self.bin / "mode").write_text("ok")
        real = runtime_module.OpenCodeRuntime._provision_secret

        def provision_then_diverge(runtime, session_id):
            real(runtime, session_id)
            runtime._passwords[session_id] = "z" * 43  # the client now disagrees with the child
            return real_secret(runtime, session_id)

        real_secret = lambda runtime, session_id: "c" * 43  # noqa: E731  (child receives a value the client lacks)
        with patch.object(runtime_module.OpenCodeRuntime, "_provision_secret", provision_then_diverge):
            started = time.monotonic()
            with self.assertRaises(RuntimeUnavailable) as caught:
                self.runtime.start(sid, "prov", "model-a")
        self.assertEqual(caught.exception.code, "server_auth_failed")
        self.assertLess(time.monotonic() - started, 10)

    def test_http_client_sends_the_secret_only_to_loopback_without_following_redirects(self) -> None:
        session_id, handle = self.start()
        sent = []

        def fake_urlopen(request, timeout=None):
            sent.append((request.full_url, dict(request.header_items())))
            raise urllib.error.URLError("stop")
        import urllib.error
        with patch.object(runtime_module.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(RuntimeUnavailable):
                self.runtime._call(session_id, handle.port, "GET", "/api/info")
        self.assertTrue(sent[0][0].startswith(f"http://127.0.0.1:{handle.port}/"))
        self.assertNotIn(self.secret(session_id), sent[0][0])


class MigrationTests(ArchitectCase):
    def test_0006_is_additive_and_keeps_existing_conversations(self) -> None:
        self.assign()
        before = self.send(body="Pre-existing").json()["message"]
        command.downgrade(self.alembic, "0005_project_conversation_foundation")
        import sqlite3
        with sqlite3.connect(self.data / "rosc.db") as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("architect_sessions", tables)
            self.assertNotIn("conversation_deliveries", tables)
            self.assertEqual(connection.execute("SELECT count(*) FROM conversation_messages").fetchone()[0], 1)
        command.upgrade(self.alembic, "head")
        self.assertEqual([m["message_id"] for m in self.messages()], [before["message_id"]])
        self.assertEqual(self.messages()[0]["delivery"]["state"], "not_sent")


if __name__ == "__main__":
    unittest.main()
