"""P046 (P043D): Owner-only HTTP bridge for real PC/TC group conversation + the evidence-loss fail-closed fix.

Isolated temporary databases migrated to head (one test downgrades to 0015), the deterministic fake Claude CLI process
runner from the P045 tests, and an app composed with that runner through the Python-level transport-factory seam.  No
real CLI, provider, tmux, network or Owner data is touched.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from test_p043b_worker_conversation_core import ARCHITECT_CAPS, OWNER, WORKER_CAPS
from test_p045_claude_conversation_transport import CrashAfterSubmit, FakeCliRunner, TransportCase

from rosc.app import create_app
from rosc.authorization import ACTOR_AGENT, ActorContext
from rosc.claude_conversation import ClaudeConversationTransport, reconcile_room
from rosc.db import make_session_factory
from rosc.group_conversation import UnavailableTransport
from rosc.worker_conversation import LOOKUP_UNKNOWN, SETTING_CATCHUP_CHARS, SETTING_PASSIVE_MISSED_THRESHOLD

ROOT = Path(__file__).resolve().parents[1]


class BlockingRunner(FakeCliRunner):
    """Holds the 'CLI' until released, so a test can observe a turn while the provider is still working."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Event()

    def spawn(self, argv, **kw):
        self.entered.set()
        self.release.wait(10)
        return super().spawn(argv, **kw)


class AgentProvider:
    def actor(self) -> ActorContext:
        return ActorContext(ACTOR_AGENT, "agent:x", "local_unverified")


class BridgeCase(TransportCase):
    def setUp(self) -> None:
        super().setUp()
        self.api = self.make_client()
        self.claude_agent = self.claude_worker("Claude")
        codex = self.registry.register_agent(OWNER, display_name="Codex", runtime_adapter="codex-cli",
                                             provider_id="openai", model_id="m", capabilities=WORKER_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=codex["agent_id"], role="worker", capabilities=WORKER_CAPS)
        self.codex = codex["agent_id"]

    def factory(self, agent):
        return ClaudeConversationTransport(make_session_factory(self.data / "rosc.db"), self.evidence,
                                           model=agent.model_id, cli=sys.executable, runner=self.runner,
                                           environ=self.environ)

    def make_client(self, **kw) -> TestClient:
        app = create_app(self.data / "rosc.db", self.data / "workspace", group_transport_factory=self.factory, **kw)
        self.app = app
        return TestClient(app, base_url="http://localhost")

    def claude_worker(self, name: str, task: int | None = None) -> str:
        agent = self.registry.register_agent(OWNER, display_name=name, runtime_adapter="claude-code",
                                             provider_id="anthropic", model_id="haiku", capabilities=WORKER_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=agent["agent_id"], role="worker", task_id=task,
                                        capabilities=WORKER_CAPS)
        return agent["agent_id"]

    def url(self, task: str | None = None) -> str:
        return f"/api/projects/P0001{'/tasks/' + task if task else ''}/conversation/group"

    def post(self, body: str, to: list[str], task: str | None = None, key: str | None = None):
        return self.api.post(f"{self.url(task)}/messages", json={"client_key": key or self.key(), "body": body, "to": to})

    def run_room(self, to: list[str], task: str | None = None, quota: int = 1, body: str = "question") -> dict:
        self.assertEqual(self.post(body, to, task).status_code, 201)
        self.assertEqual(self.api.post(f"{self.url(task)}/round", json={"agents": to, "quota": quota}).status_code, 200)
        started = self.api.post(f"{self.url(task)}/start")
        self.assertEqual(started.status_code, 200, started.text)
        status = self.api.get(self.url(task)).json()
        self.app.state.group.wait(status["conversation_id"])
        return self.api.get(self.url(task)).json()


class EvidenceLossFailClosedTests(TransportCase):
    """Section 3 of the prompt: a Core-submitted turn whose private evidence vanished must stay UNKNOWN."""

    def unknown_turn(self) -> tuple[str, str, Path]:
        room, _ = self.group([self.A], quota=1)
        result = run_turn_with(self.core, CrashAfterSubmit(self.claude), room)
        self.assertEqual(result["state"], "unknown")
        dirs = self.turn_dirs()
        self.assertEqual(len(dirs), 1)
        return room, result["turn_id"], dirs[0]

    def assert_held_without_side_effects(self, room: str, before_messages: int) -> None:
        report = reconcile_room(self.core, self.claude, room)
        self.assertEqual([r["state"] for r in report["reconciled"]], ["unknown"])
        self.assertEqual(self.core.claim_next(room).status, "busy")  # the room stays held
        self.assertEqual(len(self.runner.calls), 1)  # no second model process
        self.assertEqual(self.grants()[self.A], 0)  # no quota debit
        self.assertEqual(len(self.messages(room)), before_messages)  # no ghost message

    def test_deleted_evidence_directory_keeps_unknown(self) -> None:
        room, _turn, turn_dir = self.unknown_turn()
        before = len(self.messages(room))
        shutil.rmtree(turn_dir)
        self.assert_held_without_side_effects(room, before)

    def test_deleted_intent_keeps_unknown(self) -> None:
        room, _turn, turn_dir = self.unknown_turn()
        before = len(self.messages(room))
        (turn_dir / "intent.json").unlink()
        (turn_dir / "stdout.jsonl").unlink()
        self.assert_held_without_side_effects(room, before)

    def test_corrupted_or_inconsistent_intent_keeps_unknown(self) -> None:
        room, _turn, turn_dir = self.unknown_turn()
        before = len(self.messages(room))
        (turn_dir / "stdout.jsonl").write_text("")
        (turn_dir / "intent.json").write_text("{not json")
        self.assert_held_without_side_effects(room, before)
        (turn_dir / "intent.json").write_text(json.dumps({"lane_id": "x", "native_session_id": "y",
                                                           "launch": "spawn_failed"}))
        self.assert_held_without_side_effects(room, before)

    def test_genuine_prespawn_failure_stays_safely_retryable(self) -> None:
        room, _ = self.group([self.A], quota=1)
        self.runner.modes = ["exec_fail"]
        first = run_turn_with(self.core, self.claude, room)
        self.assertEqual((first["state"], first["error_code"]), ("deferred", "launch_failed"))
        retry = run_turn_with(self.core, self.claude, room)
        self.assertEqual(retry["state"], "published")
        self.assertEqual(len(self.runner.calls), 1)

    def test_lookup_without_any_evidence_is_unknown_not_never_launched(self) -> None:
        room, _ = self.group([self.A], quota=1)
        lane = self.core.get_or_create_native_lane(room, self.A)
        self.assertEqual(self.claude.lookup(lane, "8a8b2f3e-6e2c-4a8a-9d55-2a8b7c1d0e9f").status, LOOKUP_UNKNOWN)


def run_turn_with(core, transport, room: str) -> dict:
    claim = core.claim_next(room)
    assert claim.status == "reserved", claim
    view = core.dispatch(claim.turn_id, transport)
    view.pop("payload", None)
    if view["state"] == "accepted":
        view = core.reconcile(claim.turn_id, transport)
    return view


class GroupApiFlowTests(BridgeCase):
    def test_reads_never_launch_and_only_explicit_start_runs_one_real_turn(self) -> None:
        empty = self.api.get(self.url())
        self.assertEqual(empty.status_code, 200)
        self.assertIsNone(empty.json()["conversation_id"])
        roster = {r["display_name"]: r for r in empty.json()["roster"]}
        self.assertTrue(roster["Claude"]["group_supported"])
        self.assertFalse(roster["Codex"]["group_supported"])  # no Codex native adapter in v1.0
        self.assertEqual(roster["Codex"]["unsupported_reason"], "adapter_not_supported")
        self.assertEqual(self.post("Привет, Claude", [self.claude_agent]).status_code, 201)
        self.assertEqual(self.api.post(f"{self.url()}/round", json={"agents": [self.claude_agent], "quota": 1}).status_code, 200)
        for _ in range(3):
            self.api.get(self.url())
            self.api.get("/api/projects/P0001/conversation")
        self.assertEqual(self.runner.calls, [])  # GET/poll never wakes a model
        self.api.post(f"{self.url()}/start")
        self.app.state.group.wait(self.api.get(self.url()).json()["conversation_id"])
        status = self.api.get(self.url()).json()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(status["round"]["grants"][self.claude_agent]["published"], 1)
        self.assertEqual(status["round"]["state"], "awaiting_owner")  # quota spent: waits for the Owner
        self.assertEqual(status["driver"]["last"]["reason"], "awaiting_owner")
        page = self.api.get("/api/projects/P0001/conversation").json()
        owner, reply = page["messages"][-2:]
        self.assertEqual(owner["group"]["to"], [self.claude_agent])
        self.assertEqual(reply["sender"]["kind"], "worker")
        self.assertEqual(reply["sender"]["agent_id"], self.claude_agent)
        self.assertEqual(reply["sender"]["agent_display_name"], "Claude")
        self.assertEqual(reply["group"]["to"], ["owner"])
        self.assertEqual(reply["ordinal"], owner["ordinal"] + 1)
        self.assertTrue(reply["body"].startswith("reply #1"))
        # Quota exhausted: another start launches nothing.
        self.api.post(f"{self.url()}/start")
        self.app.state.group.wait(status["conversation_id"])
        self.assertEqual(len(self.runner.calls), 1)

    def test_task_room_is_isolated_from_project_room(self) -> None:
        self.run_room([self.claude_agent], body="PC-ONLY-TEXT")
        status = self.run_room([self.claude_agent], task="T0001", body="TC question")
        self.assertEqual(status["round"]["grants"][self.claude_agent]["published"], 1)
        self.assertEqual(len(self.runner.calls), 2)
        self.assertNotEqual(self.runner.calls[0]["native"], self.runner.calls[1]["native"])
        self.assertNotIn("PC-ONLY-TEXT", self.runner.calls[1]["prompt"])
        tc = self.api.get("/api/projects/P0001/tasks/T0001/conversation").json()["messages"]
        self.assertEqual([m["body"] for m in tc][0], "TC question")
        self.assertNotIn("PC-ONLY-TEXT", json.dumps(tc))
        self.assertEqual(self.api.get(self.url("T0002")).json()["conversation_id"], None)

    def test_idempotent_send_and_conflicting_reuse(self) -> None:
        first = self.post("same", [self.claude_agent], key="owner-key-fixed1")
        replay = self.post("same", [self.claude_agent], key="owner-key-fixed1")
        self.assertEqual((first.status_code, replay.status_code), (201, 200))
        self.assertEqual(first.json()["message"]["ordinal"], replay.json()["message"]["ordinal"])
        self.assertEqual(self.post("different", [self.claude_agent], key="owner-key-fixed1").status_code, 409)
        self.assertEqual(len(self.api.get("/api/projects/P0001/conversation").json()["messages"]), 1)

    def test_owner_message_mid_round_does_not_reset_quota(self) -> None:
        self.run_room([self.claude_agent], quota=2)
        before = self.api.get(self.url()).json()["round"]
        self.assertEqual(self.post("follow-up", [self.claude_agent]).status_code, 201)
        after = self.api.get(self.url()).json()["round"]
        self.assertEqual(before["round_id"], after["round_id"])
        self.assertEqual(after["grants"][self.claude_agent]["published"], before["grants"][self.claude_agent]["published"])

    def test_pause_stops_launch_and_unpause_allows_next_explicit_start(self) -> None:
        self.post("q", [self.claude_agent])
        self.api.post(f"{self.url()}/round", json={"agents": [self.claude_agent], "quota": 1})
        self.assertEqual(self.api.post(f"{self.url()}/controls", json={"paused": True}).json()["paused"], True)
        self.api.post(f"{self.url()}/start")
        room = self.api.get(self.url()).json()["conversation_id"]
        self.app.state.group.wait(room)
        status = self.api.get(self.url()).json()
        self.assertEqual(status["driver"]["last"]["reason"], "paused")
        self.assertEqual(self.runner.calls, [])
        self.api.post(f"{self.url()}/controls", json={"paused": False, "emergency_brake": True})
        self.api.post(f"{self.url()}/start")
        self.app.state.group.wait(room)
        self.assertEqual(self.runner.calls, [])
        self.api.post(f"{self.url()}/controls", json={"emergency_brake": False})
        self.api.post(f"{self.url()}/start")
        self.app.state.group.wait(room)
        self.assertEqual(len(self.runner.calls), 1)

    def test_start_requires_round_and_renew_requires_current(self) -> None:
        self.post("q", [self.claude_agent])
        self.assertEqual(self.api.post(f"{self.url()}/start").status_code, 409)
        self.assertEqual(self.api.post(f"{self.url()}/round", json={"agents": [self.claude_agent], "renew": True}).status_code, 409)
        self.assertEqual(self.api.post(f"{self.url()}/round", json={"agents": [self.claude_agent], "quota": 1}).status_code, 200)
        self.assertEqual(self.api.post(f"{self.url()}/round", json={"agents": [self.claude_agent]}).status_code, 409)
        renewed = self.api.post(f"{self.url()}/round", json={"agents": [self.claude_agent], "quota": 2, "renew": True})
        self.assertEqual((renewed.status_code, renewed.json()["round_no"]), (200, 2))

    def test_in_flight_status_is_truthful_and_duplicate_start_or_reconcile_is_refused(self) -> None:
        self.runner = BlockingRunner()
        self.post("q", [self.claude_agent])
        self.api.post(f"{self.url()}/round", json={"agents": [self.claude_agent], "quota": 1})
        self.api.post(f"{self.url()}/start")
        self.assertTrue(self.runner.entered.wait(5))
        status = self.api.get(self.url()).json()
        self.assertTrue(status["driver"]["running"])
        self.assertEqual(status["holding_turn"]["state"], "submitted")
        self.assertIsNone(status["answering"])  # never "answering" before provider evidence
        self.assertEqual(status["activity"], "awaiting_confirmation")
        self.assertEqual(self.api.post(f"{self.url()}/start").json(), {"started": False, "running": True})
        self.assertEqual(self.api.post(f"{self.url()}/reconcile").status_code, 409)
        self.runner.release.set()
        self.app.state.group.wait(status["conversation_id"])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertIsNone(self.api.get(self.url()).json()["holding_turn"])

    def test_unknown_after_restart_is_explicit_and_reconcile_never_resends(self) -> None:
        self.post("q", [self.claude_agent])
        self.api.post(f"{self.url()}/round", json={"agents": [self.claude_agent], "quota": 1})
        room = self.api.get(self.url()).json()["conversation_id"]
        self.runner.modes = ["hang"]
        self.api.post(f"{self.url()}/start")
        self.app.state.group.wait(room)
        status = self.api.get(self.url()).json()
        self.assertEqual(status["holding_turn"]["state"], "unknown")
        self.assertEqual(status["driver"]["last"]["reason"], "unknown")
        result = self.api.post(f"{self.url()}/reconcile").json()
        self.assertEqual(result["reconciled"][0]["state"], "unknown")
        self.api.post(f"{self.url()}/start")
        self.app.state.group.wait(room)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.api.get(self.url()).json()["driver"]["last"]["reason"], "busy")

    def test_passive_missed_keep_release_and_large_backlog_skip_gap(self) -> None:
        second = self.claude_worker("Claude-2")
        self.core.update_setting(OWNER, SETTING_PASSIVE_MISSED_THRESHOLD, 2)
        self.core.update_setting(OWNER, SETTING_CATCHUP_CHARS, 50)
        self.run_room([self.claude_agent, second], quota=1, body="both")
        for i in range(3):
            self.post(f"only first {i} " + "x" * 30, [self.claude_agent])
        members = {m["agent_id"]: m for m in self.api.get(self.url()).json()["members"]}
        self.assertEqual(members[second]["state"], "passive")
        missed = members[second]["missed"]
        self.assertIn(missed, (3, 4))  # 3 Owner messages (+ the peer reply when the queue put it after Claude-2's turn)
        self.assertEqual(members[second]["release_offer"]["threshold"], 2)
        kept = self.api.post(f"{self.url()}/members/{second}/keep").json()
        self.assertIsNone(kept["release_offer"])
        # Address the Passive member again; its backlog (> 50 chars) now needs an explicit Owner decision.
        self.post("Ты тут?", [second])
        self.api.post(f"{self.url()}/round", json={"agents": [second], "quota": 1, "renew": True})
        status = self.api.get(self.url()).json()
        catchup = status["catchup"]
        self.assertEqual(catchup["agent_id"], second)
        self.assertGreater(catchup["chars"], 50)
        self.assertEqual(catchup["limit"], 50)
        self.assertEqual(catchup["count"], missed)
        calls = len(self.runner.calls)
        self.api.post(f"{self.url()}/start")
        self.app.state.group.wait(status["conversation_id"])
        self.assertEqual(len(self.runner.calls), calls)  # nothing launched before the decision
        self.assertEqual(self.api.get(self.url()).json()["driver"]["last"]["reason"], "catchup_choice_required")
        choice = self.api.post(f"{self.url()}/turns/{catchup['turn_id']}/catchup", json={"choice": "skip_backlog"})
        self.assertEqual(choice.status_code, 200, choice.text)
        member = {m["agent_id"]: m for m in self.api.get(self.url()).json()["members"]}[second]
        self.assertEqual(member["omitted_ranges"], [[catchup["from"], catchup["through"]]])
        self.api.post(f"{self.url()}/start")
        self.app.state.group.wait(status["conversation_id"])
        self.assertEqual(len(self.runner.calls), calls + 1)
        prompt = self.runner.calls[-1]["prompt"]
        self.assertNotIn("only first 0", prompt)
        self.assertIn("omitted_ranges_not_shown", prompt)
        released = self.api.post(f"{self.url()}/members/{self.claude_agent}/release").json()
        self.assertEqual(released["state"], "released")


class GroupApiBoundaryTests(BridgeCase):
    def test_architect_and_unsupported_or_foreign_addressees_are_refused(self) -> None:
        architect = self.registry.register_agent(OWNER, display_name="Arch", runtime_adapter="opencode",
                                                 provider_id="x", model_id="m", capabilities=ARCHITECT_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=architect["agent_id"], role="architect",
                                        capabilities=ARCHITECT_CAPS)
        roster = {r["agent_id"]: r for r in self.api.get(self.url()).json()["roster"]}
        self.assertEqual(roster[architect["agent_id"]]["unsupported_reason"], "architect_group_not_integrated")
        for to in ([architect["agent_id"]], [self.codex], [self.claude_agent, self.codex], ["00000000-0000-4000-8000-000000000000"]):
            self.assertEqual(self.post("q", to).status_code, 422, to)
        task_only = self.claude_worker("T2 only", task=2)
        self.assertEqual(self.post("q", [task_only], task="T0001").status_code, 422)
        self.assertEqual(self.post("q", [task_only], task="T0002").status_code, 201)
        self.assertEqual(self.api.post(f"{self.url()}/round", json={"agents": [self.codex]}).status_code, 422)
        self.assertEqual(self.runner.calls, [])

    def test_scope_ids_are_canonical_and_foreign_tasks_404(self) -> None:
        for path in ("/api/projects/1/conversation/group", "/api/projects/P0001/tasks/1/conversation/group",
                     "/api/projects/P0001/tasks/T0099/conversation/group", "/api/projects/P0099/conversation/group"):
            self.assertEqual(self.api.get(path).status_code, 404, path)
        self.assertEqual(self.api.post(f"{self.url()}/members/not-a-uuid/keep").status_code, 404)
        self.assertEqual(self.api.post(f"{self.url()}/turns/not-a-uuid/catchup", json={"choice": "skip_backlog"}).status_code, 404)
        bad = self.api.post(f"{self.url()}/messages", json={"client_key": "k" * 9, "body": "SECRET-BODY", "to": []})
        self.assertEqual(bad.status_code, 422)
        self.assertNotIn("SECRET-BODY", bad.text)

    def test_every_endpoint_refuses_non_owner_actor(self) -> None:
        self.post("q", [self.claude_agent])
        self.api.post(f"{self.url()}/round", json={"agents": [self.claude_agent], "quota": 1})
        agent_api = self.make_client(auth_provider=AgentProvider())
        calls = [("get", self.url(), None),
                 ("post", f"{self.url()}/messages", {"client_key": "agent-key-1", "body": "x", "to": [self.claude_agent]}),
                 ("post", f"{self.url()}/round", {"agents": [self.claude_agent], "renew": True}),
                 ("post", f"{self.url()}/start", None), ("post", f"{self.url()}/controls", {"paused": True}),
                 ("post", f"{self.url()}/members/{self.claude_agent}/keep", None),
                 ("post", f"{self.url()}/members/{self.claude_agent}/release", None),
                 ("post", f"{self.url()}/turns/{self.claude_agent}/catchup", {"choice": "skip_backlog"}),
                 ("post", f"{self.url()}/reconcile", None), ("get", self.url("T0001"), None)]
        for method, path, body in calls:
            response = getattr(agent_api, method)(path, **({"json": body} if body is not None else {}))
            self.assertEqual(response.status_code, 403, path)
        self.assertEqual(self.runner.calls, [])

    def test_unsupported_transport_never_launches(self) -> None:
        transport = UnavailableTransport("adapter_not_supported")
        self.assertEqual(transport.submit(None, None).status, "not_accepted")
        self.assertIsNone(transport.activity(None, "x"))


class LegacyCompatibilityTests(unittest.TestCase):
    def test_legacy_conversation_read_works_on_a_database_not_upgraded_to_0016(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            root = Path(tmp) / "root"
            root.mkdir()
            with patch.dict("os.environ", {"ROSC_DATA_DIR": str(data)}):
                config = Config(str(ROOT / "alembic.ini"))
                config.set_main_option("script_location", str(ROOT / "migrations"))
                command.upgrade(config, "0015_worker_bootstrap")
                client = TestClient(create_app(data / "rosc.db", data / "workspace"), base_url="http://localhost")
                self.assertEqual(client.post("/api/projects", json={
                    "name": "Old", "description": "d", "root_path": str(root), "mode": "new"}).status_code, 201)
                sent = client.post("/api/projects/P0001/conversation/messages",
                                   json={"client_key": "legacy-key-1", "body": "hello"})
                self.assertEqual(sent.status_code, 201, sent.text)
                page = client.get("/api/projects/P0001/conversation")
                self.assertEqual(page.status_code, 200)
                self.assertIsNone(page.json()["messages"][0]["group"])
                self.assertEqual(client.get("/api/projects/P0001/conversation/group").status_code, 503)


if __name__ == "__main__":
    unittest.main()
