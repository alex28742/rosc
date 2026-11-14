"""P043B (Prompt 044): Worker Conversation Core — durable sequential group turn dispatch and independent native lanes.

Every test runs on an isolated temporary database migrated to head, with a deterministic in-memory fake transport.
No provider, CLI, tmux, network or Owner data is touched.  ``restart()`` builds a brand-new Core (and session factory)
over the same file to prove that state is durable rather than held in Python memory.
"""
from __future__ import annotations

import random
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from alembic import command

import test_slice01 as base
from rosc.agent_registry import AgentRegistryService
from rosc.authorization import ACTOR_AGENT, ActorContext, LocalOwnerPolicy, LocalOwnerProvider
from rosc.conversation import (ConversationConflict, ConversationDenied, ConversationNotFound, InvalidConversationInput,
                               ProjectConversationService)
from rosc.db import make_session_factory
from rosc.models import ProjectRootBinding
from rosc.worker_conversation import (LOOKUP_NOT_RECEIVED, LOOKUP_REPLY, LOOKUP_UNKNOWN, SETTING_CATCHUP_CHARS,
                                      SETTING_PASSIVE_MISSED_THRESHOLD, SETTINGS_SCHEMA, SUBMIT_ACCEPTED,
                                      SUBMIT_NOT_ACCEPTED, SUBMIT_UNKNOWN, LookupOutcome, SubmitOutcome,
                                      WorkerConversationCore)

OWNER = LocalOwnerProvider().actor()
AGENT_ACTOR = ActorContext(ACTOR_AGENT, "agent:claim", "local_unverified")
TASK = {"title": "T", "description": "D", "acceptance_criteria": "A"}
WORKER_CAPS = ["project.context.read", "task.context.read"]
ARCHITECT_CAPS = ["project.context.read", "architect.document.read", "architect.document.write"]
ROOT = Path(__file__).resolve().parents[1]


class FakeTransport:
    """Deterministic provider double.  ``plan`` maps agent_id -> list of submit outcomes (consumed in order)."""

    assurance = "fake_transport"

    def __init__(self) -> None:
        self.submits: list = []  # (LaneRef, TurnPayload)
        self.plan: dict[str, list[SubmitOutcome]] = {}
        self.lookups: dict[str, LookupOutcome] = {}
        self.busy_lanes: set[str] = set()

    def open_lane(self, lane) -> None:  # pragma: no cover - contract only
        pass

    def busy(self, lane) -> bool:
        return lane.lane_id in self.busy_lanes

    def submit(self, lane, payload) -> SubmitOutcome:
        self.submits.append((lane, payload))
        queue = self.plan.get(lane.agent_id)
        return queue.pop(0) if queue else SubmitOutcome(SUBMIT_ACCEPTED)

    def lookup(self, lane, correlation_id) -> LookupOutcome:
        return self.lookups.get(correlation_id, LookupOutcome(LOOKUP_UNKNOWN))


class CoreCase(base.StageCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        for _ in range(2):
            self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=TASK).status_code, 201)  # T1, T2
        self.registry = AgentRegistryService(make_session_factory(self.data / "rosc.db"), LocalOwnerPolicy())
        self.A = self.worker("A")
        self.B = self.worker("B")
        self.C = self.worker("C")
        self.transport = FakeTransport()
        self.restart(seed=7)
        self.legacy = ProjectConversationService(make_session_factory(self.data / "rosc.db"), LocalOwnerPolicy())
        self.keys = 0

    def worker(self, name: str, project: int = 1) -> str:
        agent = self.registry.register_agent(OWNER, display_name=name, runtime_adapter="fake-cli", provider_id="x",
                                             model_id="m", capabilities=WORKER_CAPS)
        self.registry.create_assignment(OWNER, project, agent_id=agent["agent_id"], role="worker",
                                        capabilities=WORKER_CAPS)
        return agent["agent_id"]

    def restart(self, seed: int = 7) -> None:
        self.core = WorkerConversationCore(make_session_factory(self.data / "rosc.db"), LocalOwnerPolicy(),
                                           rng=random.Random(seed))

    def key(self) -> str:
        self.keys += 1
        return f"owner-key-{self.keys:05d}"

    def say(self, body: str = "question", to=None, task_id=None, **kw) -> dict:
        return self.core.post_owner_message(OWNER, 1, client_key=kw.pop("client_key", self.key()), body=body, to=to,
                                            task_id=task_id, **kw)["message"]

    def room(self, task_id=None) -> str:
        return self.core.room_status(OWNER, 1, task_id=task_id)["conversation_id"]

    def status(self, task_id=None) -> dict:
        return self.core.room_status(OWNER, 1, task_id=task_id)

    def fetch(self, sql: str, *args):
        with sqlite3.connect(self.data / "rosc.db") as connection:
            return connection.execute(sql, args).fetchall()

    def step(self, room: str, reply: str | None = None) -> dict:
        """Claim -> dispatch -> (if accepted) publish; returns the claim + dispatch result."""
        claim = self.core.claim_next(room)
        if claim.status != "reserved":
            return {"claim": claim}
        view = self.core.dispatch(claim.turn_id, self.transport)
        if view["state"] == "accepted":
            view = {**view, **self.core.complete(claim.turn_id, reply or f"reply from {claim.agent_id[:4]}")}
        return {"claim": claim, **view}

    def drain(self, room: str, limit: int = 50) -> list[str]:
        order = []
        for _ in range(limit):
            result = self.step(room)
            if result["claim"].status != "reserved":
                break
            if result.get("state") == "published":
                order.append(result["claim"].agent_id)
        return order

    def group(self, agents, quota=3, task_id=None, body="Group question") -> tuple[str, dict]:
        message = self.say(body, to=list(agents), task_id=task_id)
        self.core.authorize_round(OWNER, 1, agents=list(agents), quota=quota, task_id=task_id,
                                  authorizing_message_id=message["message_id"])
        return self.room(task_id), message

    def grants(self, task_id=None) -> dict:
        return {a: g["published"] for a, g in self.status(task_id)["round"]["grants"].items()}


class MigrationAndLegacyTests(CoreCase):
    def test_legacy_upgrade_keeps_messages_ordinals_recipients_and_deliveries(self) -> None:
        command.downgrade(self.alembic, "0015_worker_bootstrap")
        self.assertEqual(self.client.post("/api/projects/P0001/conversation/messages",
                                          json={"client_key": "legacy-0001", "body": "pc"}).status_code, 201)
        tc = "/api/projects/P0001/tasks/T0001/conversation/messages"
        self.client.post(tc, json={"client_key": "legacy-0002", "body": "tc-arch", "recipient": "architect"})
        self.client.post(tc, json={"client_key": "legacy-0003", "body": "tc-none"})
        message_id = self.fetch("SELECT message_id FROM conversation_messages WHERE body='tc-arch'")[0][0]
        with sqlite3.connect(self.data / "rosc.db") as connection:
            connection.execute("INSERT INTO conversation_deliveries (message_id, state, attempts, created_at, updated_at)"
                               " VALUES (?, 'failed', 1, '2026-01-01', '2026-01-01')", (message_id,))
        before = self.fetch("SELECT message_id, conversation_id, sender_participant_id, ordinal, body, client_key, "
                            "recipient FROM conversation_messages ORDER BY rowid")
        participants = self.fetch("SELECT * FROM conversation_participants ORDER BY rowid")
        deliveries = self.fetch("SELECT * FROM conversation_deliveries")
        command.upgrade(self.alembic, "head")
        self.assertEqual(self.fetch("SELECT message_id, conversation_id, sender_participant_id, ordinal, body, "
                                    "client_key, recipient FROM conversation_messages ORDER BY rowid"), before)
        self.assertEqual(self.fetch("SELECT * FROM conversation_participants ORDER BY rowid"), participants)
        self.assertEqual(self.fetch("SELECT * FROM conversation_deliveries"), deliveries)
        self.restart()
        # Legacy rows read back through the old API and through the new #ordinal lookup.
        page = self.client.get("/api/projects/P0001/tasks/T0001/conversation").json()
        self.assertEqual([(m["ordinal"], m["recipient"]) for m in page["messages"]], [(1, "architect"), (2, None)])
        self.assertEqual(page["messages"][0]["delivery"]["state"], "failed")
        legacy = self.core.lookup_message(OWNER, 1, 1, task_id=1)
        self.assertEqual((legacy["body"], legacy["to"], legacy["legacy_recipient"]), ("tc-arch", ["architect"], "architect"))
        self.assertEqual(self.core.lookup_message(OWNER, 1, 2, task_id=1)["to"], [])
        # New inserts continue the same per-room ordinal sequence; no second numbering system.
        new = self.say("new", to=[self.A], task_id=1)
        self.assertEqual(new["ordinal"], 3)
        self.assertEqual(self.core.lookup_message(OWNER, 1, 3, task_id=1)["message_id"], new["message_id"])
        self.assertEqual(self.core.lookup_message(OWNER, 1, 1)["body"], "pc")
        with self.assertRaises(ConversationNotFound):
            self.core.lookup_message(OWNER, 1, 99, task_id=1)

    def test_migration_round_trip_and_new_tables_are_empty(self) -> None:
        command.downgrade(self.alembic, "0015_worker_bootstrap")
        command.upgrade(self.alembic, "head")
        for table in ("group_turns", "group_rounds", "agent_native_lanes", "agent_inbound_cursors", "rosc_settings",
                      "conversation_members", "conversation_message_parties"):
            self.assertEqual(self.fetch(f"SELECT count(*) FROM {table}"), [(0,)])

    def test_legacy_owner_api_still_works_beside_group_messages(self) -> None:
        self.say("group", to=[self.A])
        response = self.client.post("/api/projects/P0001/conversation/messages",
                                    json={"client_key": "legacy-key-9", "body": "legacy"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["message"]["ordinal"], 2)
        page = self.client.get("/api/projects/P0001/conversation").json()
        self.assertEqual([m["body"] for m in page["messages"]], ["group", "legacy"])
        self.assertEqual(self.client.get("/api/health").json()["agent_runtime"], "not_started")


class LaneTests(CoreCase):
    def test_same_worker_gets_independent_lanes_per_room_and_reuses_them(self) -> None:
        self.say("pc", to=[self.A])
        self.say("ta", to=[self.A], task_id=1)
        self.say("tb", to=[self.A], task_id=2)
        rooms = [self.room(), self.room(1), self.room(2)]
        lanes = [self.core.get_or_create_native_lane(r, self.A) for r in rooms]
        self.assertEqual(len({lane.native_session_id for lane in lanes}), 3)
        self.restart()
        again = [self.core.get_or_create_native_lane(r, self.A) for r in rooms]
        self.assertEqual([lane.native_session_id for lane in again], [lane.native_session_id for lane in lanes])
        other = self.core.get_or_create_native_lane(rooms[0], self.B)
        self.assertNotEqual(other.native_session_id, lanes[0].native_session_id)

    def test_other_project_agent_cannot_get_a_lane_here(self) -> None:
        path = self.base / "second"
        path.mkdir()
        self.assertEqual(self.register(path).status_code, 201)
        foreign = self.worker("F", project=2)
        self.say("pc", to=[self.A])
        with self.assertRaises(InvalidConversationInput):
            self.core.get_or_create_native_lane(self.room(), foreign)

    def test_root_binding_change_invalidates_lane_instead_of_resuming(self) -> None:
        room, _ = self.group([self.A], quota=2)
        first = self.step(room)
        old_lane = first["lane_id"]
        sessions = make_session_factory(self.data / "rosc.db")
        with sessions() as session:
            binding = session.query(ProjectRootBinding).filter_by(project_id=1).one()
            binding.state = "superseded"
            session.flush()
            session.add(ProjectRootBinding(project_id=1, version=2, root_path=binding.root_path + "-moved",
                                           mode="existing", state="current"))
            session.commit()
        second = self.step(room)
        self.assertNotEqual(second["lane_id"], old_lane)
        rows = dict(self.fetch("SELECT lane_id, state FROM agent_native_lanes"))
        self.assertEqual(rows[old_lane], "invalidated")
        self.assertEqual(rows[second["lane_id"]], "active")
        # The fresh native context does not inherit the old lane's cursor (rehydration is a separate path).
        self.assertEqual(self.transport.submits[-1][1].input_after_ordinal, 0)

    def test_binding_change_between_claim_and_dispatch_never_submits_to_stale_lane(self) -> None:
        room, _ = self.group([self.A], quota=1)
        claim = self.core.claim_next(room)
        sessions = make_session_factory(self.data / "rosc.db")
        with sessions() as session:
            binding = session.query(ProjectRootBinding).filter_by(project_id=1).one()
            binding.state = "superseded"
            session.flush()
            session.add(ProjectRootBinding(project_id=1, version=2, root_path=binding.root_path + "-x",
                                           mode="existing", state="current"))
            session.commit()
        self.assertEqual(self.core.dispatch(claim.turn_id, self.transport)["status"], "not_dispatched")
        self.assertEqual(self.transport.submits, [])


class AtomicityTests(CoreCase):
    def test_db_partial_index_forbids_two_holding_turns_in_one_room(self) -> None:
        room, _ = self.group([self.A, self.B])
        claim = self.core.claim_next(room)
        self.assertEqual(claim.status, "reserved")
        with self.assertRaises(sqlite3.IntegrityError):
            with sqlite3.connect(self.data / "rosc.db") as connection:
                connection.execute("UPDATE group_turns SET state='reserved' WHERE state='queued'")

    def test_concurrent_claims_in_one_room_reserve_exactly_one(self) -> None:
        room, _ = self.group([self.A, self.B, self.C])
        results, barrier = [], threading.Barrier(4)

        def claim() -> None:
            core = WorkerConversationCore(make_session_factory(self.data / "rosc.db", busy_timeout_ms=10000),
                                          LocalOwnerPolicy())
            barrier.wait()
            results.append(core.claim_next(room).status)
        threads = [threading.Thread(target=claim) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["busy", "busy", "busy", "reserved"])
        self.assertEqual(self.fetch("SELECT count(*) FROM group_turns WHERE state='reserved'"), [(1,)])

    def test_separate_processes_claim_one_room_once_and_other_room_independently(self) -> None:
        room, _ = self.group([self.A, self.B])
        task_room, _ = self.group([self.A], task_id=1)
        script = ("import sys, random; sys.path.insert(0, %r); from pathlib import Path\n"
                  "from rosc.db import make_session_factory\nfrom rosc.authorization import LocalOwnerPolicy\n"
                  "from rosc.worker_conversation import WorkerConversationCore\n"
                  "core = WorkerConversationCore(make_session_factory(Path(sys.argv[1]), busy_timeout_ms=10000), "
                  "LocalOwnerPolicy())\nprint(core.claim_next(sys.argv[2]).status)\n") % str(ROOT / "src")
        procs = [subprocess.Popen([sys.executable, "-c", script, str(self.data / "rosc.db"), r],
                                  stdout=subprocess.PIPE, text=True) for r in (room, room, task_room)]
        outputs = [p.communicate(timeout=60)[0].strip() for p in procs]
        self.assertEqual(sorted(outputs[:2]), ["busy", "reserved"])
        self.assertEqual(outputs[2], "reserved")  # an independent room is not blocked by the other room

    def test_simulated_task_a_busy_does_not_block_pc_room(self) -> None:
        task_room, _ = self.group([self.A], task_id=1)
        pc_room, _ = self.group([self.A])
        busy = self.core.claim_next(task_room)
        self.core.dispatch(busy.turn_id, self.transport)  # A is "working" in Task A (accepted, no reply yet)
        self.assertEqual(self.core.claim_next(pc_room).status, "reserved")


class OwnerMessageTests(CoreCase):
    def test_owner_message_saved_once_with_addressing_and_audience(self) -> None:
        first = self.core.post_owner_message(OWNER, 1, client_key="client-k-01", body="hello", to=[self.A, self.B])
        self.assertFalse(first["replayed"])
        message = first["message"]
        self.assertEqual(sorted(message["to"]), sorted([self.A, self.B]))
        self.assertEqual(sorted(message["audience"]), sorted(["owner", self.A, self.B]))
        self.assertEqual(message["sender"]["kind"], "owner")
        replay = self.core.post_owner_message(OWNER, 1, client_key="client-k-01", body="hello", to=[self.B, self.A])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["message"]["message_id"], message["message_id"])
        for changed in ({"body": "other", "to": [self.A, self.B]}, {"body": "hello", "to": [self.A]}):
            with self.assertRaises(ConversationConflict):
                self.core.post_owner_message(OWNER, 1, client_key="client-k-01", **changed)
        self.assertEqual(self.fetch("SELECT count(*) FROM conversation_messages"), [(1,)])
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds"), [(0,)])
        self.assertEqual(self.fetch("SELECT count(*) FROM group_turns"), [(0,)])

    def test_invalid_targets_and_reply_to_are_refused_atomically(self) -> None:
        path = self.base / "second"
        path.mkdir()
        self.register(path)
        foreign = self.worker("F", project=2)
        task_only = self.registry.register_agent(OWNER, display_name="TW", runtime_adapter="fake-cli",
                                                 provider_id="x", model_id="m", capabilities=WORKER_CAPS)["agent_id"]
        self.registry.create_assignment(OWNER, 1, agent_id=task_only, role="worker", task_id=2,
                                        capabilities=WORKER_CAPS)
        bad_targets = ([self.A, "00000000-0000-4000-8000-000000000000"], [foreign], [self.A, self.A], [], "x",
                       [task_only])
        for to in bad_targets:
            with self.assertRaises(InvalidConversationInput, msg=str(to)):
                self.say("x", to=to)
        with self.assertRaises(InvalidConversationInput):
            self.say("x", to=[task_only], task_id=1)  # assigned to T2 only
        self.say("ok", to=[task_only], task_id=2)
        other_room = self.say("pc", to=[self.A])
        with self.assertRaises(InvalidConversationInput):
            self.say("x", to=[self.A], task_id=1, reply_to=other_room["message_id"])
        self.assertEqual(self.fetch("SELECT count(*) FROM conversation_messages"), [(2,)])

    def test_agent_actor_and_forged_owner_are_denied(self) -> None:
        forged = ActorContext("owner", "local-owner", "authenticated")
        for actor in (AGENT_ACTOR, forged):
            with self.assertRaises(ConversationDenied):
                self.core.post_owner_message(actor, 1, client_key="client-k-02", body="x", to=[self.A])
            with self.assertRaises(ConversationDenied):
                self.core.authorize_round(actor, 1, agents=[self.A])
            with self.assertRaises(ConversationDenied):
                self.core.set_dispatch_controls(actor, 1, paused=False)
            with self.assertRaises(ConversationDenied):
                self.core.update_setting(actor, SETTING_CATCHUP_CHARS, 10)
        self.assertEqual(self.fetch("SELECT count(*) FROM conversation_messages"), [(0,)])

    def test_cross_project_and_task_scope_is_not_found(self) -> None:
        path = self.base / "second"
        path.mkdir()
        self.register(path)
        self.client.post("/api/projects/P0002/tasks", json=TASK)  # T3 belongs to P2
        with self.assertRaises(ConversationNotFound):
            self.say("x", to=[self.A], task_id=3)
        with self.assertRaises(ConversationNotFound):
            self.core.room_status(OWNER, 2)


class QuotaAndQueueTests(CoreCase):
    def test_quota_counts_only_published_agent_replies_and_owner_texts_never_reset(self) -> None:
        room, _ = self.group([self.A, self.B, self.C], quota=3)
        self.assertEqual(self.grants(), {self.A: 0, self.B: 0, self.C: 0})
        first = self.step(room)
        spent = {self.A: 0, self.B: 0, self.C: 0}
        spent[first["claim"].agent_id] = 1
        self.assertEqual(self.grants(), spent)
        before_round = self.status()["round"]
        for i in range(10):
            self.say(f"more {i}", to=[self.A, self.B, self.C])
        after_round = self.status()["round"]
        self.assertEqual(self.grants(), spent)
        self.assertEqual((after_round["round_id"], after_round["current_pass"]),
                         (before_round["round_id"], before_round["current_pass"]))
        self.assertEqual([t["turn_id"] for t in after_round["turns"]], [t["turn_id"] for t in before_round["turns"]])
        order = self.drain(room)
        self.assertEqual(len(order) + 1, 9)
        self.assertEqual(self.grants(), {self.A: 3, self.B: 3, self.C: 3})
        self.assertEqual(self.status()["round"]["state"], "awaiting_owner")
        self.assertEqual(self.core.claim_next(room).status, "awaiting_owner")
        for i in range(10):
            self.say(f"after {i}", to=[self.A])
        self.assertEqual(self.core.claim_next(room).status, "awaiting_owner")
        self.assertEqual(self.fetch("SELECT count(*) FROM conversation_messages WHERE client_key LIKE 'group-reply:%'"),
                         [(9,)])
        renewed = self.core.authorize_round(OWNER, 1, agents=[self.A, self.C], quota=1, renew=True)
        self.assertEqual({a: g["published"] for a, g in renewed["grants"].items()}, {self.A: 0, self.C: 0})
        self.assertEqual(sorted(self.drain(room)), sorted([self.A, self.C]))
        members = {m["agent_id"]: m["state"] for m in self.status()["members"]}
        self.assertEqual(members[self.B], "passive")

    def test_plain_send_never_creates_a_round_and_second_authorize_needs_renew(self) -> None:
        self.say("hi", to=[self.A])
        self.assertEqual(self.core.claim_next(self.room()).status, "idle")
        self.core.authorize_round(OWNER, 1, agents=[self.A])
        with self.assertRaises(ConversationConflict):
            self.core.authorize_round(OWNER, 1, agents=[self.A])
        self.assertEqual(self.status()["round"]["per_agent_quota"], SETTINGS_SCHEMA["group_reply_quota_default"].default)

    def test_sequential_three_agents_each_sees_previous_published_replies(self) -> None:
        room, question = self.group([self.A, self.B, self.C], quota=1)
        expected: list[str] = []  # canonical bodies published after the question, in room order
        for i in range(3):
            claim = self.core.claim_next(room)
            self.assertEqual(claim.status, "reserved")
            self.assertEqual(self.core.claim_next(room).status, "busy")  # nobody else while one holds the room
            view = self.core.dispatch(claim.turn_id, self.transport)
            seen = [e.body for e in view["payload"].events]
            self.assertEqual(seen, ["Group question"] + expected)
            self.assertEqual(self.core.claim_next(room).status, "busy")  # accepted, not yet published
            if i == 1:  # arrives while B generates: stored now, NOT in B's frozen snapshot, delivered to C
                self.say("owner interjects", to=[self.A, self.B, self.C])
                expected.append("owner interjects")
            self.core.complete(claim.turn_id, f"answer {claim.agent_id}")
            expected.append(f"answer {claim.agent_id}")
        bodies = [e.body for e in self.transport.submits[2][1].events]
        self.assertIn("owner interjects", bodies)
        self.assertEqual(self.core.claim_next(room).status, "awaiting_owner")

    def test_randomized_passes_never_start_with_last_publisher_across_seeds(self) -> None:
        for seed in range(20):
            with self.subTest(seed=seed):
                with tempfile.TemporaryDirectory() as tmp:
                    db = Path(tmp) / "rosc.db"
                    self._check_seed(db, seed)

    def _check_seed(self, db: Path, seed: int) -> None:
        # Copy the fully prepared fixture DB, then run 3 passes with the given seed.
        import shutil
        shutil.copy(self.data / "rosc.db", db)
        core = WorkerConversationCore(make_session_factory(db), LocalOwnerPolicy(), rng=random.Random(seed))
        core.post_owner_message(OWNER, 1, client_key="seed-key-1", body="go", to=[self.A, self.B, self.C])
        core.authorize_round(OWNER, 1, agents=[self.A, self.B, self.C], quota=3)
        room = core.room_status(OWNER, 1)["conversation_id"]
        publishers: list[str] = []
        transport = FakeTransport()
        for _ in range(9):
            claim = core.claim_next(room)
            self.assertEqual(claim.status, "reserved")
            core.dispatch(claim.turn_id, transport)
            core.complete(claim.turn_id, "r")
            publishers.append(claim.agent_id)
            if len(publishers) == 3:  # restart exactly at the pass boundary
                core = WorkerConversationCore(make_session_factory(db), LocalOwnerPolicy(), rng=random.Random(seed + 7))
            if len(publishers) == 4:  # restart mid-pass: stored order must not be reshuffled
                stored = [t["agent_id"] for t in core.room_status(OWNER, 1)["round"]["turns"] if t["pass"] == 1]
                core = WorkerConversationCore(make_session_factory(db), LocalOwnerPolicy(), rng=random.Random(seed + 99))
                self.assertEqual([t["agent_id"] for t in core.room_status(OWNER, 1)["round"]["turns"]
                                  if t["pass"] == 1], stored)
        passes = [publishers[i:i + 3] for i in (0, 3, 6)]
        for previous, current in zip(passes, passes[1:]):
            self.assertNotEqual(current[0], previous[-1])
            self.assertEqual(sorted(current), sorted([self.A, self.B, self.C]))
        for a, b in zip(publishers, publishers[1:]):
            self.assertNotEqual(a, b)

    def test_ten_owner_messages_while_in_flight_create_no_dispatch_and_reach_next_agent(self) -> None:
        room, _ = self.group([self.A, self.B, self.C], quota=1)
        claim = self.core.claim_next(room)
        self.core.dispatch(claim.turn_id, self.transport)
        turns_before = self.status()["round"]["turns"]
        posted = [self.say(f"while waiting {i}", to=[self.A] if i % 2 else None) for i in range(10)]
        self.assertEqual(len(self.transport.submits), 1)
        self.assertEqual(self.status()["round"]["turns"], turns_before)
        self.assertEqual(self.grants(), {self.A: 0, self.B: 0, self.C: 0})
        self.core.complete(claim.turn_id, "first")
        nxt = self.core.claim_next(room)
        payload = self.core.dispatch(nxt.turn_id, self.transport)["payload"]
        ordinals = [e.ordinal for e in payload.events]
        self.assertEqual(ordinals, sorted(ordinals))
        self.assertTrue({m["ordinal"] for m in posted} <= set(ordinals))

    def test_revoked_assignment_cancels_queued_turn(self) -> None:
        room, _ = self.group([self.A, self.B], quota=1)
        queued = [t["agent_id"] for t in self.status()["round"]["turns"]]
        victim = queued[1]
        assignment = [a for a in self.registry.list_assignments(OWNER, 1) if a["agent_id"] == victim][0]
        self.registry.revoke_assignment(OWNER, 1, assignment["assignment_id"], expected_version=assignment["version"])
        self.assertEqual(self.drain(room), [queued[0]])
        cancelled = [t for t in self.status()["round"]["turns"] if t["agent_id"] == victim]
        self.assertEqual(cancelled[0]["error_code"], "assignment_changed")

    def test_two_agents_and_single_agent_pass_boundaries(self) -> None:
        room, _ = self.group([self.A, self.B], quota=3)
        order = self.drain(room)
        self.assertEqual(len(order), 6)
        for a, b in zip(order, order[1:]):
            self.assertNotEqual(a, b)
        task_room, _ = self.group([self.C], quota=3, task_id=1)
        self.assertEqual(self.drain(task_room), [self.C] * 3)  # the single-agent exception

    def test_adjacency_uses_last_actual_publisher_after_retry(self) -> None:
        room, _ = self.group([self.A, self.B, self.C], quota=2)
        pass0 = [t["agent_id"] for t in self.status()["round"]["turns"]]
        first = pass0[0]
        self.transport.plan[first] = [SubmitOutcome(SUBMIT_NOT_ACCEPTED, "connection_failed")]
        published = self.drain(room, limit=4)  # first fails -> others -> first retried at end of the pass
        self.assertEqual(published[:3], pass0[1:] + [first])
        nxt = self.step(room)
        self.assertNotEqual(nxt["claim"].agent_id, first)


class CursorTests(CoreCase):
    def test_own_reply_never_advances_inbound_cursor_two_clocks(self) -> None:
        room, _ = self.group([self.A, self.B], quota=2)
        # Make A first deterministically: inspect the persisted order and act on whoever is first.
        claim = self.core.claim_next(room)
        first = claim.agent_id
        second = self.B if first == self.A else self.A
        view = self.core.dispatch(claim.turn_id, self.transport)
        accepted_through = view["input_through_ordinal"]
        o2 = self.say("owner 2", to=[self.A, self.B])
        o3 = self.say("owner 3", to=[self.A, self.B])
        published = self.core.complete(claim.turn_id, "first reply")
        status = {m["agent_id"]: m for m in self.status()["members"]}
        self.assertEqual(status[first]["applied_through"], accepted_through)
        self.assertNotEqual(status[first]["applied_through"], published["ordinal"])
        self.assertEqual(self.grants()[first], 1)
        # Second agent receives everything after its cursor incl. first's reply and the interjections.
        claim2 = self.core.claim_next(room)
        self.assertEqual(claim2.agent_id, second)
        payload2 = self.core.dispatch(claim2.turn_id, self.transport)["payload"]
        self.assertEqual([e.ordinal for e in payload2.events], list(range(1, published["ordinal"] + 1)))
        self.core.complete(claim2.turn_id, "second reply")
        # First agent's next turn: only unseen #o2, #o3 and the peer reply, never its own reply.
        claim3 = self.core.claim_next(room)
        self.assertEqual(claim3.agent_id, first)
        payload3 = self.core.dispatch(claim3.turn_id, self.transport)["payload"]
        ordinals = [e.ordinal for e in payload3.events]
        self.assertEqual(ordinals[:2], [o2["ordinal"], o3["ordinal"]])
        self.assertNotIn(published["ordinal"], ordinals)
        self.assertTrue(all(e.sender_agent_id != first for e in payload3.events))
        self.assertEqual(payload3.input_after_ordinal, accepted_through)

    def test_unknown_and_safe_failure_do_not_advance_cursor(self) -> None:
        room, _ = self.group([self.A], quota=2)
        self.transport.plan[self.A] = [SubmitOutcome(SUBMIT_NOT_ACCEPTED, "not_received"),
                                       SubmitOutcome(SUBMIT_UNKNOWN, "timeout")]
        self.step(room)
        self.step(room)
        member = self.status()["members"][0]
        self.assertEqual(member["applied_through"], 0)
        self.assertEqual(self.status()["holding_turn"]["state"], "unknown")

    def test_just_in_time_delta_excludes_already_applied(self) -> None:
        room, _ = self.group([self.A, self.B], quota=2)
        for _ in range(2):
            self.step(room)
        b = {m["agent_id"]: m for m in self.status()["members"]}[self.B]["applied_through"]
        self.assertGreater(b, 0)
        later = [self.say(f"late {i}", to=[self.B]) for i in range(3)]
        self.drain(room, limit=2)
        b_payload = [p for lane, p in self.transport.submits if lane.agent_id == self.B][-1]
        self.assertEqual(b_payload.input_after_ordinal, b)
        self.assertTrue(all(e.ordinal > b for e in b_payload.events))
        self.assertTrue({m["ordinal"] for m in later} <= {e.ordinal for e in b_payload.events})


class MembershipTests(CoreCase):
    def test_passive_members_get_no_turns_and_release_offers_follow_threshold(self) -> None:
        room, _ = self.group([self.A, self.B, self.C], quota=1)
        self.drain(room)
        submits = len(self.transport.submits)
        for i in range(10):
            self.say(f"to A {i}", to=[self.A])
        status = {m["agent_id"]: m for m in self.status()["members"]}
        self.assertEqual(status[self.A]["state"], "active")
        self.assertEqual((status[self.B]["state"], status[self.C]["state"]), ("passive", "passive"))
        self.assertEqual(len(self.transport.submits), submits)  # zero passive model calls
        self.assertEqual(self.core.claim_next(room).status, "awaiting_owner")
        b = status[self.B]
        self.assertGreaterEqual(b["missed"], 10)
        self.assertIsNotNone(b["release_offer"])
        self.core.keep_member(OWNER, 1, self.B)
        self.assertIsNone({m["agent_id"]: m for m in self.status()["members"]}[self.B]["release_offer"])
        for i in range(5):
            self.say(f"more {i}", to=[self.A])
        missed = {m["agent_id"]: m for m in self.status()["members"]}[self.B]["missed"]
        offer = {m["agent_id"]: m for m in self.status()["members"]}[self.B]["release_offer"]
        self.assertEqual(offer is not None, missed // 10 > (missed - 5) // 10)
        while {m["agent_id"]: m for m in self.status()["members"]}[self.B]["missed"] % 10 != 0:
            self.say("fill", to=[self.A])
        self.assertIsNotNone({m["agent_id"]: m for m in self.status()["members"]}[self.B]["release_offer"])
        # Notifications are typed state, never chat rows: every message has an owner/agent sender.
        self.assertEqual(self.fetch("SELECT count(*) FROM conversation_messages m JOIN conversation_participants p "
                                    "ON p.participant_id=m.sender_participant_id WHERE p.kind NOT IN "
                                    "('owner','worker','architect')"), [(0,)])

    def test_release_is_owner_only_keeps_history_and_reinvite_starts_epoch(self) -> None:
        room, _ = self.group([self.A, self.B], quota=1)
        self.drain(room)
        history = self.fetch("SELECT count(*) FROM conversation_messages")
        with self.assertRaises(ConversationDenied):
            self.core.release_member(AGENT_ACTOR, 1, self.B)
        released = self.core.release_member(OWNER, 1, self.B)
        self.assertEqual(released["state"], "released")
        self.assertEqual(self.fetch("SELECT count(*) FROM conversation_messages"), history)
        for i in range(3):
            self.say(f"while released {i}", to=[self.A])
        audience = self.core.lookup_message(OWNER, 1, self.fetch(
            "SELECT max(ordinal) FROM conversation_messages")[0][0])["audience"]
        self.assertNotIn(self.B, audience)
        back = self.say("welcome back", to=[self.A, self.B])
        member = {m["agent_id"]: m for m in self.status()["members"]}[self.B]
        self.assertEqual((member["state"], member["epoch"]), ("active", 2))
        self.assertTrue(any(r[1] == back["ordinal"] - 1 for r in member["omitted_ranges"]))
        self.core.authorize_round(OWNER, 1, agents=[self.B], quota=1, renew=True)
        claim = self.core.claim_next(room)
        payload = self.core.dispatch(claim.turn_id, self.transport)["payload"]
        self.assertEqual([e.body for e in payload.events], ["welcome back"])  # no mandatory backfill

    def test_changing_addressees_mid_round_keeps_queue_and_grants(self) -> None:
        room, _ = self.group([self.A, self.B, self.C], quota=1)
        queued = [t["agent_id"] for t in self.status()["round"]["turns"]]
        self.say("only to first", to=[queued[0]])
        self.assertEqual(sorted(self.drain(room)), sorted(queued))  # passive status did not drop queued turns

    def test_release_cancels_only_that_agents_queued_turns(self) -> None:
        room, _ = self.group([self.A, self.B, self.C], quota=1)
        queued = [t["agent_id"] for t in self.status()["round"]["turns"]]
        self.core.release_member(OWNER, 1, queued[1])
        self.assertEqual(self.drain(room), [queued[0], queued[2]])


class SettingsAndCatchupTests(CoreCase):
    def test_settings_defaults_update_validate_and_survive_restart(self) -> None:
        self.assertEqual(self.core.read_settings(OWNER)[SETTING_PASSIVE_MISSED_THRESHOLD], 10)
        self.assertEqual(self.core.read_settings(OWNER)[SETTING_CATCHUP_CHARS], 2000)
        self.core.update_setting(OWNER, SETTING_PASSIVE_MISSED_THRESHOLD, 7)
        self.core.update_setting(OWNER, SETTING_CATCHUP_CHARS, 3000)
        for bad in (("nope", 1), (SETTING_CATCHUP_CHARS, 0), (SETTING_CATCHUP_CHARS, True), (SETTING_CATCHUP_CHARS, "5")):
            with self.assertRaises(InvalidConversationInput):
                self.core.update_setting(OWNER, *bad)
        self.restart()
        self.assertEqual(self.core.read_settings(OWNER)[SETTING_PASSIVE_MISSED_THRESHOLD], 7)
        self.assertEqual(self.core.read_settings(OWNER)[SETTING_CATCHUP_CHARS], 3000)

    def test_release_offer_recomputes_with_new_threshold_without_stale_duplicate(self) -> None:
        self.say("both", to=[self.A, self.B])
        for i in range(10):
            self.say(f"a {i}", to=[self.A])
        b = lambda: {m["agent_id"]: m for m in self.status()["members"]}[self.B]
        self.assertEqual(b()["missed"], 11)  # "both" + ten more, none applied (no turn ever ran)
        self.core.keep_member(OWNER, 1, self.B)
        self.core.update_setting(OWNER, SETTING_PASSIVE_MISSED_THRESHOLD, 7)
        self.restart()
        self.assertIsNone(b()["release_offer"])  # 11//7 == 1 == acked 11//7
        for i in range(3):
            self.say(f"x {i}", to=[self.A])
        self.assertEqual(b()["release_offer"]["step"], 2)  # 14//7

    def test_large_backlog_requires_owner_choice_and_skip_is_audited(self) -> None:
        self.core.update_setting(OWNER, SETTING_CATCHUP_CHARS, 50)
        for i in range(3):
            self.say("x" * 30, to=[self.A])
        room, question = self.group([self.B], quota=1, body="what do you think?")
        claim = self.core.claim_next(room)
        self.assertEqual(claim.status, "catchup_choice_required")
        self.assertEqual((claim.detail["chars"], claim.detail["limit"]), (90, 50))
        self.assertEqual(self.transport.submits, [])
        self.core.choose_catchup(OWNER, 1, claim.turn_id, "skip_backlog")
        claim = self.core.claim_next(room)
        view = self.core.dispatch(claim.turn_id, self.transport)
        self.assertEqual([e.body for e in view["payload"].events], ["what do you think?"])
        self.assertEqual(view["payload"].omitted_ranges, ((1, 3),))
        member = {m["agent_id"]: m for m in self.status()["members"]}[self.B]
        self.assertEqual(member["omitted_ranges"], [[1, 3]])
        self.assertEqual(self.fetch("SELECT count(*) FROM conversation_messages"), [(4,)])  # nothing deleted

    def test_send_full_backlog_submits_delta_and_current_message(self) -> None:
        self.core.update_setting(OWNER, SETTING_CATCHUP_CHARS, 50)
        for i in range(3):
            self.say("y" * 30, to=[self.A])
        room, _ = self.group([self.B], quota=1)
        claim = self.core.claim_next(room)
        self.core.choose_catchup(OWNER, 1, claim.turn_id, "send_full")
        with self.assertRaises(ConversationConflict):
            self.core.choose_catchup(OWNER, 1, claim.turn_id, "skip_backlog")
        claim = self.core.claim_next(room)
        view = self.core.dispatch(claim.turn_id, self.transport)
        self.assertEqual(len(view["payload"].events), 4)
        self.assertEqual(view["payload"].omitted_ranges, ())


class ControlsAndRecoveryTests(CoreCase):
    def test_pause_and_brake_persist_and_block_new_claims_only(self) -> None:
        room, _ = self.group([self.A, self.B], quota=1)
        claim = self.core.claim_next(room)
        self.core.dispatch(claim.turn_id, self.transport)  # in flight
        self.core.set_dispatch_controls(OWNER, 1, paused=True)
        self.restart()
        self.assertEqual(self.status()["holding_turn"]["state"], "accepted")  # not falsely cancelled
        self.core.complete(claim.turn_id, "finished anyway")
        self.assertEqual(self.core.claim_next(room).status, "paused")
        self.say("stored while paused", to=[self.A])
        self.core.set_dispatch_controls(OWNER, 1, paused=False, emergency_brake=True)
        self.restart()
        self.assertEqual(self.core.claim_next(room).status, "paused")
        self.core.set_dispatch_controls(OWNER, 1, emergency_brake=False)
        self.assertEqual(self.core.claim_next(room).status, "reserved")

    def test_pause_between_claim_and_dispatch_sends_nothing(self) -> None:
        room, _ = self.group([self.A], quota=1)
        claim = self.core.claim_next(room)
        self.core.set_dispatch_controls(OWNER, 1, paused=True)
        self.assertEqual(self.core.dispatch(claim.turn_id, self.transport)["status"], "not_dispatched")
        self.assertEqual(self.transport.submits, [])
        self.assertIsNone(self.status()["holding_turn"])

    def test_safe_failure_defers_once_then_fails_visibly_without_blocking_room(self) -> None:
        room, _ = self.group([self.A, self.B, self.C], quota=1)
        first = self.status()["round"]["turns"][0]["agent_id"]
        self.transport.plan[first] = [SubmitOutcome(SUBMIT_NOT_ACCEPTED, "connection_failed")] * 2
        published = self.drain(room)
        self.assertEqual(len(published), 2)
        self.assertNotIn(first, published)
        failed = self.status()["failed"]
        self.assertEqual([(f["agent_id"], f["attempt"], f["error_code"]) for f in failed],
                         [(first, 2, "connection_failed")])
        first_submits = [p for lane, p in self.transport.submits if lane.agent_id == first]
        self.assertEqual(len(first_submits), 2)
        self.assertGreater(first_submits[1].input_through_ordinal, first_submits[0].input_through_ordinal)
        self.assertEqual(self.core.claim_next(room).status, "awaiting_owner")

    def test_unknown_holds_room_until_evidence_and_is_never_resubmitted(self) -> None:
        room, _ = self.group([self.A, self.B], quota=1)
        claim = self.core.claim_next(room)
        self.transport.plan[claim.agent_id] = [SubmitOutcome(SUBMIT_UNKNOWN, "timeout")]
        view = self.core.dispatch(claim.turn_id, self.transport)
        self.assertEqual(view["state"], "unknown")
        self.assertEqual(self.core.claim_next(room).status, "busy")
        self.assertEqual(self.core.reconcile(claim.turn_id, self.transport)["state"], "unknown")  # inconclusive
        self.assertEqual(len(self.transport.submits), 1)
        self.core.set_dispatch_controls(OWNER, 1, paused=True)
        self.assertEqual(self.status()["holding_turn"]["state"], "unknown")
        self.core.set_dispatch_controls(OWNER, 1, paused=False)
        self.transport.lookups[self.transport.submits[0][1].correlation_id] = LookupOutcome(LOOKUP_REPLY, "late reply")
        done = self.core.reconcile(claim.turn_id, self.transport)
        self.assertEqual(done["state"], "published")
        self.assertEqual(self.grants()[claim.agent_id], 1)
        member = {m["agent_id"]: m for m in self.status()["members"]}[claim.agent_id]
        self.assertEqual(member["applied_through"], view["input_through_ordinal"])
        self.assertEqual(len(self.transport.submits), 1)

    def test_unknown_resolved_as_not_received_becomes_safe_deferral(self) -> None:
        room, _ = self.group([self.A, self.B], quota=1)
        claim = self.core.claim_next(room)
        self.transport.plan[claim.agent_id] = [SubmitOutcome(SUBMIT_UNKNOWN)]
        self.core.dispatch(claim.turn_id, self.transport)
        self.transport.lookups[self.transport.submits[0][1].correlation_id] = LookupOutcome(LOOKUP_NOT_RECEIVED)
        self.assertEqual(self.core.reconcile(claim.turn_id, self.transport)["state"], "deferred")
        self.assertEqual(sorted(self.drain(room)), sorted([self.A, self.B]))

    def test_crash_in_publish_transaction_never_debits_or_advances_and_recovers_once(self) -> None:
        room, _ = self.group([self.A, self.B], quota=2)
        claim = self.core.claim_next(room)
        self.core.dispatch(claim.turn_id, self.transport)
        self.core.capture_reply(claim.turn_id, "captured")

        def boom() -> None:
            raise sqlite3.OperationalError("simulated crash")
        self.core.before_publish_commit = boom
        from rosc.conversation import ConversationStoreError
        with self.assertRaises((ConversationStoreError, sqlite3.OperationalError)):
            self.core.publish(claim.turn_id)
        self.assertEqual(self.grants()[claim.agent_id], 0)
        self.assertEqual(self.fetch("SELECT count(*) FROM conversation_messages WHERE client_key LIKE 'group-reply:%'"),
                         [(0,)])
        self.assertEqual(self.core.claim_next(room).status, "busy")  # room lease held; next Agent not invoked
        self.restart()
        self.assertEqual(self.core.recover(room)["published"], 1)
        self.assertEqual(self.core.recover(room)["published"], 0)
        self.assertEqual(self.core.publish(claim.turn_id)["state"], "published")  # idempotent completion
        with self.assertRaises(ConversationConflict):
            self.core.capture_reply(claim.turn_id, "different text")
        self.assertEqual(self.grants()[claim.agent_id], 1)
        self.assertEqual(self.fetch("SELECT count(*) FROM conversation_messages WHERE client_key LIKE 'group-reply:%'"),
                         [(1,)])
        self.assertEqual(len(self.transport.submits), 1)

    def test_restart_recovery_of_reserved_and_submitted(self) -> None:
        room, _ = self.group([self.A, self.B], quota=1)
        claim = self.core.claim_next(room)
        self.restart()
        self.assertEqual(self.core.recover(room)["requeued"], 1)
        claim = self.core.claim_next(room)
        with sqlite3.connect(self.data / "rosc.db") as connection:  # simulate crash right after "submitted" commit
            connection.execute("UPDATE group_turns SET state='submitted', input_after_ordinal=0, "
                               "input_through_ordinal=1 WHERE turn_id=?", (claim.turn_id,))
        self.restart()
        self.assertEqual(self.core.recover(room)["unknown"], 1)
        self.assertEqual(self.status()["holding_turn"]["state"], "unknown")
        self.assertEqual(self.transport.submits, [])
        self.assertEqual({m["agent_id"]: m["applied_through"] for m in self.status()["members"]}[claim.agent_id], 0)

    def test_answering_indicator_only_after_acceptance(self) -> None:
        room, _ = self.group([self.A, self.B], quota=1)
        self.assertIsNone(self.status()["answering"])
        claim = self.core.claim_next(room)
        self.assertIsNone(self.status()["answering"])
        self.assertEqual(len(self.status()["waiting"]), 1)
        self.core.dispatch(claim.turn_id, self.transport)
        self.assertEqual(self.status()["answering"], claim.agent_id)
        messages = self.fetch("SELECT count(*) FROM conversation_messages")
        self.status()
        self.assertEqual(self.fetch("SELECT count(*) FROM conversation_messages"), messages)

    def test_reply_targets_and_reply_to_are_validated(self) -> None:
        room, question = self.group([self.A, self.B], quota=1)
        claim = self.core.claim_next(room)
        self.core.dispatch(claim.turn_id, self.transport)
        other = self.B if claim.agent_id == self.A else self.A
        later = self.say("posted after submit", to=[self.A, self.B])
        for kw in ({"to": [claim.agent_id]}, {"to": ["00000000-0000-4000-8000-000000000000"]},
                   {"reply_to": later["message_id"]}, {"reply_to": "nope"}):
            with self.assertRaises(InvalidConversationInput, msg=str(kw)):
                self.core.capture_reply(claim.turn_id, "x", **kw)
        result = self.core.complete(claim.turn_id, "to you", to=["owner", other], reply_to=question["message_id"])
        message = self.core.lookup_message(OWNER, 1, result["ordinal"])
        self.assertEqual((message["sender"]["agent_id"], sorted(message["to"]), message["reply_to"]),
                         (claim.agent_id, sorted(["owner", other]), question["message_id"]))

    def test_publish_and_capture_require_correct_state(self) -> None:
        room, _ = self.group([self.A], quota=1)
        claim = self.core.claim_next(room)
        with self.assertRaises(ConversationConflict):
            self.core.capture_reply(claim.turn_id, "too early")
        with self.assertRaises(ConversationConflict):
            self.core.publish(claim.turn_id)
        self.transport.busy_lanes.add(self.core.get_or_create_native_lane(room, self.A).lane_id)
        self.assertEqual(self.core.dispatch(claim.turn_id, self.transport)["state"], "deferred")
        self.assertEqual(self.transport.submits, [])


if __name__ == "__main__":
    unittest.main()
