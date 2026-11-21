"""P062.1: direct Owner -> Architect provisional text in PC/TC through the SAME broker, room SSE and UI as GroupTurns.

Synthetic only: temporary SQLite, the real ``create_app`` composition (one shared ``ProvisionalBroker``), the legacy
``ConversationDelivery`` path of ``ArchitectService`` and a deterministic fake runtime that declares the provisional
capability and pushes text into whatever sink ``provisional.current()`` exposes to it (exactly what the real
``OpenCodeRuntime.ask`` does after ``session.inbox.delivered`` of its prompt id).  The real OpenCode observer and wire
are covered by ``test_p062_provisional_streaming``.  No model, provider, real OpenCode, network or Owner data.
"""
from __future__ import annotations

import json
import threading
import time
import unittest

from fastapi.testclient import TestClient

import test_p015_architect_activation as p15
from rosc import provisional
from rosc.app import create_app
from rosc.architect_runtime import RuntimeAmbiguous, RuntimeRefused
from rosc.claude_conversation import ROLE_TEXT
from rosc.architect_service import ARCHITECT_GROUP_ROLE, ArchitectService
from rosc.db import make_session_factory

from test_p061_room_events import RoomEventsCase, SwitchPolicy


class StreamingRuntime(p15.FakeRuntime):
    """Declares the capability; streams only into the sink its caller narrowed for THIS call; then waits for release."""

    def __init__(self) -> None:
        super().__init__()
        self.capability = provisional.CAP_SUPPORTED
        self.chunks = ["Гипотеза №1: ", "кэш устарел."]
        self.release = threading.Event()
        self.streamed = threading.Event()
        self.sinks: list[provisional.ProvisionalSink | None] = []
        self.sessions_seen: list[str] = []
        self.natives = 0
        self.block = True

    def provisional_capability(self) -> str:
        return self.capability

    def create_native_session(self, session_id, handle, provider_id, model_id, title, tools):
        self.natives += 1
        return f"ses_task{self.natives}"

    def ask(self, handle, runtime_message_id, text, timeout):
        sink = provisional.current()
        self.sinks.append(sink)
        self.sessions_seen.append(handle.runtime_session_id)
        if sink is not None:
            for chunk in self.chunks:
                sink.delta("msg_native:0", chunk)
        self.streamed.set()
        if self.block:
            self.release.wait(10)
        self.asked.append((runtime_message_id, text))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class DirectCase(p15.ArchitectCase):
    def setUp(self) -> None:
        p15.base.StageCase.setUp(self)
        self.runtime = StreamingRuntime()
        self.broker = provisional.ProvisionalBroker()
        self.client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace",
                                            architect_runtime=self.runtime, provisional_broker=self.broker,
                                            run_monitor_interval=None), base_url="http://localhost")
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        self.sessions = make_session_factory(self.data / "rosc.db")
        self.architect: ArchitectService = self.client.app.state.architect
        self.agent_id = self.assign()["agent"]["agent_id"]
        self.activate()

    def tearDown(self) -> None:
        self.runtime.release.set()
        self.architect.drain()
        super().tearDown()

    def room_of(self, message: dict) -> str:
        return message["conversation_id"]

    def drafts(self, room: str) -> list[provisional.DraftView]:
        return list(self.broker.room_state(room)[1])

    def wait_streamed(self) -> None:
        self.assertTrue(self.runtime.streamed.wait(5))


class DirectProjectConversationTests(DirectCase):
    def test_text_is_live_before_the_reply_and_published_once_with_the_committed_id(self) -> None:
        for n, body in enumerate(("Plain owner question", "@Architect what do you think?")):
            self.runtime.streamed.clear()
            self.runtime.release.clear()
            sent = self.send(key=f"key-0000010{n}", body=body).json()["message"]
            self.wait_streamed()
            room = self.room_of(sent)
            live = [d for d in self.drafts(room) if d.state == provisional.STATE_STREAMING]
            self.assertEqual(len(live), 1)
            self.assertEqual((live[0].text, live[0].agent_id, live[0].turn_id),
                             ("Гипотеза №1: кэш устарел.", self.agent_id, "direct-" + sent["message_id"]))
            # Genuinely provisional: no reply message, delivery still pending.
            self.assertEqual(self.delivery(sent["message_id"])["state"], "pending")
            self.assertEqual([m["sender"]["kind"] for m in self.messages()].count("architect"), n)
            self.runtime.release.set()
            self.architect.drain()
            delivery = self.delivery(sent["message_id"])
            self.assertEqual(delivery["state"], "delivered")
            view = next(d for d in self.drafts(room) if d.turn_id == "direct-" + sent["message_id"])
            self.assertEqual((view.state, view.message_id), ("published", delivery["reply_message_id"]))
            replies = [m for m in self.messages() if m["sender"]["kind"] == "architect"]
            self.assertEqual(len(replies), n + 1)
            self.assertEqual(replies[-1]["body"], "Hello Owner")  # canonical: the runtime's final reply only
        # The draft never became a message, a body or a second reply.
        bodies = [m["body"] for m in self.messages()]
        self.assertNotIn("Гипотеза №1: кэш устарел.", bodies)

    def test_unsupported_capability_is_canonical_only(self) -> None:
        self.runtime.capability = provisional.CAP_UNSUPPORTED
        self.runtime.block = False
        sent = self.send(body="Question").json()["message"]
        self.architect.drain()
        self.assertEqual(self.runtime.sinks, [None])
        self.assertEqual(self.drafts(self.room_of(sent)), [])
        self.assertEqual(self.delivery(sent["message_id"])["state"], "delivered")

    def test_failed_unknown_and_lost_commit_end_the_draft_without_a_message_or_retry(self) -> None:
        cases = [(RuntimeRefused("refused"), "failed"), (RuntimeAmbiguous("timeout"), "unknown")]
        for n, (problem, state) in enumerate(cases):
            self.runtime.reply = problem
            self.runtime.streamed.clear()
            self.runtime.release.set()
            sent = self.send(key=f"key-0000020{n}", body=f"Question {n}").json()["message"]
            self.architect.drain()
            self.assertEqual(self.delivery(sent["message_id"])["state"], state)
            view = next(d for d in self.drafts(self.room_of(sent)) if d.turn_id == "direct-" + sent["message_id"])
            self.assertEqual((view.state, view.message_id), ("ended", None))
        # The session is replaced while the provider answers: the reply is discarded (UNKNOWN), never published.
        self.runtime.reply = "Late reply"
        self.runtime.release.clear()
        self.runtime.streamed.clear()
        sent = self.send(key="key-00000300", body="Lost").json()["message"]
        self.wait_streamed()
        self.assertEqual(self.client.post("/api/projects/P0001/architect/deactivate").json()["state"], "stopped")
        self.runtime.release.set()
        self.architect.drain()
        self.assertEqual(self.delivery(sent["message_id"])["state"], "unknown")
        view = next(d for d in self.drafts(self.room_of(sent)) if d.turn_id == "direct-" + sent["message_id"])
        self.assertEqual((view.state, view.message_id), ("ended", None))
        self.assertEqual([m["sender"]["kind"] for m in self.messages()].count("architect"), 0)
        self.assertEqual(len(self.runtime.asked), 3)  # nothing was re-sent by the draft machinery

    def test_draft_ends_even_when_the_runtime_raises_unexpectedly(self) -> None:
        self.runtime.reply = RuntimeError("boom")
        self.runtime.release.set()
        sent = self.send(body="Crash").json()["message"]
        self.architect.drain()
        self.assertEqual(self.delivery(sent["message_id"])["state"], "unknown")
        view = self.drafts(self.room_of(sent))[0]
        self.assertEqual(view.state, "ended")

    def test_the_sink_is_narrowed_to_the_delivery_call_only(self) -> None:
        sent = self.send(body="Question").json()["message"]
        self.wait_streamed()
        sink = self.runtime.sinks[0]
        self.assertIsNotNone(sink)
        self.assertEqual(sink.identity.lane, provisional.LANE_DIRECT)
        self.assertEqual(sink.identity.conversation_id, self.room_of(sent))
        self.assertTrue(sink.correlation_id.startswith("msg_"))  # backend-only prompt id, never sent to a browser
        self.assertIsNone(provisional.current())  # nothing leaks to this (or any other) thread
        self.runtime.release.set()
        self.architect.drain()
        sink.delta("msg_native:0", " LATE CHUNK")  # a late chunk after Core ended the draft is inert
        self.assertNotIn("LATE CHUNK", self.drafts(self.room_of(sent))[0].text)


class DirectTaskConversationTests(DirectCase):
    def setUp(self) -> None:
        super().setUp()
        for title in ("First task", "Second task"):
            created = self.client.post("/api/projects/P0001/tasks", json={
                "title": title, "description": f"{title} description", "acceptance_criteria": "Done"})
            self.assertEqual(created.status_code, 201, created.text)

    def tc(self, task: str) -> str:
        return f"/api/projects/P0001/tasks/{task}/conversation"

    def save(self, body: str, task: str = "T0001") -> dict:
        response = self.client.post(self.tc(task) + "/messages", json={
            "client_key": f"tc-{task}-{time.monotonic_ns()}", "body": body, "recipient": "architect"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["message"]

    def test_save_only_never_streams_and_explicit_send_streams_in_the_task_native_session(self) -> None:
        saved = self.save("Draft only")
        self.architect.drain()
        self.assertEqual(self.runtime.sinks, [])
        self.assertEqual(self.drafts(self.room_of(saved)), [])
        self.assertEqual(self.client.post(f"{self.tc('T0001')}/messages/{saved['message_id']}/send").status_code, 200)
        self.wait_streamed()
        self.assertEqual(self.runtime.sessions_seen, ["ses_task1"])  # the Task's own native session
        room = self.room_of(saved)
        self.assertEqual([d.text for d in self.drafts(room)], ["Гипотеза №1: кэш устарел."])
        self.assertEqual(set(self.broker._rooms), {room})  # noqa: SLF001 - no draft in the PC or another room
        other = self.save("Other task", task="T0002")
        self.assertEqual(self.drafts(self.room_of(other)), [])  # Task isolation
        self.runtime.release.set()
        self.architect.drain()
        task_messages = self.client.get(self.tc("T0001")).json()["messages"]
        replies = [m for m in task_messages if m["sender"]["kind"] == "architect"]
        self.assertEqual(len(replies), 1)
        view = self.drafts(room)[0]
        self.assertEqual((view.state, view.message_id), ("published", replies[0]["message_id"]))


class LaneCoexistenceTests(unittest.TestCase):
    def ident(self, turn: str, agent: str, lane: str = provisional.LANE_GROUP) -> provisional.TurnIdentity:
        return provisional.TurnIdentity(1, "room-1", None, turn, agent, "c-" + turn, lane)

    def test_direct_and_group_drafts_coexist_and_supersede_only_within_their_lane(self) -> None:
        broker = provisional.ProvisionalBroker()
        worker = broker.open(self.ident("g1", "worker"))
        worker.delta("k", "worker text")
        direct = broker.open(self.ident("direct-m1", "architect", provisional.LANE_DIRECT))
        direct.delta("k", "architect text")
        states = {v.turn_id: (v.state, v.text) for v in broker.room_state("room-1")[1]}
        self.assertEqual(states, {"g1": ("streaming", "worker text"), "direct-m1": ("streaming", "architect text")})
        worker2 = broker.open(self.ident("g2", "worker"))  # the next group turn ends only the older group draft
        worker2.delta("k", "next")
        states = {v.turn_id: v.state for v in broker.room_state("room-1")[1]}
        self.assertEqual(states, {"g1": "ended", "direct-m1": "streaming", "g2": "streaming"})
        broker.finish(direct, published=True, message_id="reply-1")
        broker.finish(worker2, published=False)
        states = {v.turn_id: (v.state, v.message_id) for v in broker.room_state("room-1")[1]}
        self.assertEqual(states["direct-m1"], ("published", "reply-1"))
        self.assertEqual(states["g2"], ("ended", None))

    def test_capacity_evicts_finished_drafts_before_another_lane_live_draft(self) -> None:
        broker = provisional.ProvisionalBroker(max_drafts_per_room=2)
        direct = broker.open(self.ident("direct-m1", "architect", provisional.LANE_DIRECT))
        direct.delta("k", "still typing")
        for n in range(3):
            sink = broker.open(self.ident(f"g{n}", "worker"))
            broker.finish(sink, published=True, message_id=f"r{n}")
        sink = broker.open(self.ident("g9", "worker"))
        turns = {v.turn_id for v in broker.room_state("room-1")[1]}
        self.assertEqual(turns, {"direct-m1", "g9"})

    def test_direct_turn_id_never_equals_a_group_uuid(self) -> None:
        self.assertEqual(provisional.direct_turn_id("abc"), "direct-abc")
        self.assertEqual(provisional.TurnIdentity(1, "r", None, "t", "a", "c").lane, provisional.LANE_GROUP)


class DirectSseTests(DirectCase):
    run_stream = RoomEventsCase.run_stream

    def setUp(self) -> None:
        p15.base.StageCase.setUp(self)
        from rosc.room_events import RoomEventStreams
        self.runtime = StreamingRuntime()
        self.broker = provisional.ProvisionalBroker()
        self.policy = SwitchPolicy()
        self.streams = RoomEventStreams(interval=0.05, heartbeat=0.3, max_seconds=5.0, draft_tick=0.01)
        self.app = create_app(self.data / "rosc.db", self.data / "workspace", architect_runtime=self.runtime,
                              provisional_broker=self.broker, authorization_policy=self.policy,
                              run_monitor_interval=None, room_event_streams=self.streams)
        self.client = TestClient(self.app, base_url="http://localhost")
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        self.sessions = make_session_factory(self.data / "rosc.db")
        self.architect = self.app.state.architect
        self.agent_id = self.assign()["agent"]["agent_id"]
        self.activate()

    def test_direct_draft_reaches_the_same_room_stream_then_published(self) -> None:
        sent = self.send(body="Question").json()["message"]
        self.wait_streamed()

        def release():
            self.runtime.release.set()
            self.architect.drain()

        result = self.run_stream("/api/projects/P0001/conversation/group/events",
                                 [("draft", release), ("draft", None)], linger=0.3)
        self.assertEqual(result["status"], 200)
        drafts = [json.loads(data) for name, data in result["frames"] if name == "draft"]
        self.assertEqual(drafts[0]["text"], "Гипотеза №1: кэш устарел.")
        self.assertEqual(drafts[0]["turn"], "direct-" + sent["message_id"])
        self.assertEqual(drafts[-1]["state"], "published")
        self.assertEqual(drafts[-1]["message"], self.delivery(sent["message_id"])["reply_message_id"])
        self.assertEqual({k for d in drafts for k in d},
                         {"turn", "agent", "offset", "text", "state", "message", "truncated"})
        self.assertNotIn("msg_", result["body"])  # the provider prompt id stays in the backend

    def test_revoked_reader_gets_no_direct_draft_text(self) -> None:
        self.send(body="Question").json()
        self.wait_streamed()
        self.policy.allow = False
        denied = self.run_stream("/api/projects/P0001/conversation/group/events")
        self.policy.allow = True
        self.assertEqual(denied["status"], 403)
        self.assertNotIn("Гипотеза", denied["body"])


class FinalAnswerCompletenessReachTests(unittest.TestCase):
    """The rule reaches every conversational route through its REAL trusted prompt (not a file nobody loads)."""

    def test_group_architect_and_conversational_worker_prompts_carry_the_rule(self) -> None:
        for text in (ARCHITECT_GROUP_ROLE, ROLE_TEXT):
            self.assertIn("FINAL ANSWER COMPLETENESS", text)
            self.assertIn("may not see intermediate", text)

    def test_task_native_session_preface_carries_the_rule(self) -> None:
        from types import SimpleNamespace
        task = SimpleNamespace(id=1, title="T", status="ready", description="d", acceptance_criteria="a")
        project = SimpleNamespace(id=1, name="P")
        text = ArchitectService._task_preface(task, project, "hello")
        self.assertIn("FINAL ANSWER COMPLETENESS", text)
        self.assertTrue(text.endswith("Owner message:\nhello"))


if __name__ == "__main__":
    unittest.main()
