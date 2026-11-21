"""P062: real partial/provisional agent text for PC/TC — runtime/model/role agnostic.

Synthetic only: temporary SQLite migrated to head, the P043B Core, deterministic fake runtimes (a fake Claude CLI
process that writes its ``stream-json`` progressively, a fake loopback OpenCode HTTP server with a native ``/api/event``
SSE stream, and two fake adapters with unrelated native formats) and the real FastAPI app driven at the ASGI level.
No model, provider, real CLI/OpenCode, network or Owner data is touched.  (The native event formats themselves were
verified separately against the installed binaries in an offline network namespace; see the P062 report.)
"""
from __future__ import annotations

import json
import random
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rosc import provisional
from rosc.architect_runtime import OpenCodeRuntime, OpenCodeTextObserver, RuntimeHandle
from rosc.authorization import LocalOwnerPolicy
from rosc.claude_conversation import ClaudeConversationTransport, ClaudePartialObserver, read_evidence, parse_stream
from rosc.db import make_session_factory
from rosc.group_conversation import GroupConversationBridge
from rosc.room_events import RoomEventStreams
from rosc.worker_conversation import (LOOKUP_REPLY, LOOKUP_UNKNOWN, SUBMIT_ACCEPTED, SUBMIT_UNKNOWN, LookupOutcome,
                                      SubmitOutcome, WorkerConversationCore)

from test_p043b_worker_conversation_core import ARCHITECT_CAPS, OWNER, CoreCase
from test_p046_group_conversation_bridge import BridgeCase
from test_p061_room_events import RoomEventsCase, frames

SID = "11111111-2222-4333-8444-555555555555"


def wait_for(predicate, timeout: float = 5.0, step: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not reached")


def identity(turn="t1", room="room-1", agent="agent-1", corr="c1") -> provisional.TurnIdentity:
    return provisional.TurnIdentity(1, room, None, turn, agent, corr)


# -- shared contract ----------------------------------------------------------------------------------------------------
class BrokerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [0.0]
        self.broker = provisional.ProvisionalBroker(max_chars=50, idle_ttl=100, ended_ttl=10,
                                                    clock=lambda: self.now[0])

    def text(self, room="room-1") -> list[tuple[str, str, str]]:
        return [(v.turn_id, v.text, v.state) for v in self.broker.room_state(room)[1]]

    def test_segments_replace_and_control_line_hidden(self) -> None:
        sink = self.broker.open(identity())
        sink.segment("m1:0")
        sink.delta("m1:0", "Hello")
        sink.delta("m1:0", " world")
        sink.replace("m1:0", "Hello world")  # an authoritative snapshot never doubles the text
        sink.delta("m2:0", "Second <rosc-con")
        self.assertEqual(self.text(), [("t1", "Hello world\n\nSecond", "streaming")])  # partial control tag held back
        sink.delta("m2:0", 'trol next="finish"/>\n')
        self.assertEqual(self.text(), [("t1", "Hello world\n\nSecond", "streaming")])
        self.assertEqual(provisional.visible_text("a <b>c</b>"), "a <b>c</b>")

    def test_only_core_finish_ends_and_sink_is_then_inert(self) -> None:
        sink = self.broker.open(identity())
        sink.delta("k", "draft")
        self.broker.finish(sink, published=True, message_id=None)  # no committed id -> not "published"
        self.assertEqual(self.text(), [("t1", "draft", "ended")])
        sink.delta("k", " more")  # an adapter can neither continue nor re-open an ended draft
        self.assertEqual(self.text(), [("t1", "draft", "ended")])
        other = self.broker.open(identity("t2", corr="c2"))
        self.broker.finish(other, published=True, message_id="msg-9")
        self.assertEqual(self.broker.room_state("room-1")[1][-1].message_id, "msg-9")

    def test_new_turn_ends_previous_streaming_draft_and_rooms_are_isolated(self) -> None:
        first = self.broker.open(identity("t1"))
        first.delta("k", "one")
        second = self.broker.open(identity("t2", corr="c2"))
        second.delta("k", "two")
        first.delta("k", " late")  # the stale turn cannot write any more
        self.assertEqual(self.text(), [("t1", "one", "ended"), ("t2", "two", "streaming")])
        self.broker.open(identity("t9", room="room-2", corr="c9")).delta("k", "other room")
        self.assertEqual([t for t, _, _ in self.text()], ["t1", "t2"])
        self.assertEqual(self.text("room-2"), [("t9", "other room", "streaming")])

    def test_bounded_length_and_lifetime(self) -> None:
        sink = self.broker.open(identity())
        sink.delta("k", "x" * 80)
        view = self.broker.room_state("room-1")[1][0]
        self.assertEqual(len(view.text), 50)
        self.assertTrue(view.truncated)
        self.now[0] = 101  # idle streaming draft (lost process) expires
        self.assertEqual(self.text(), [])
        sink2 = self.broker.open(identity("t2", corr="c2"))
        self.broker.finish(sink2, published=False)
        self.now[0] = 112
        self.assertEqual(self.text(), [])

    def test_cursor_appends_from_offset_replaces_and_reports_gone(self) -> None:
        cursor = provisional.DraftCursor()
        sink = self.broker.open(identity())
        sink.delta("k", "Hel")
        first = cursor.frames(self.broker.room_state("room-1"))
        self.assertEqual([(f["offset"], f["text"], f["state"]) for f in first], [(0, "Hel", "streaming")])
        self.assertEqual(cursor.frames(self.broker.room_state("room-1")), [])  # unchanged -> nothing
        sink.delta("k", "lo")
        self.assertEqual([(f["offset"], f["text"]) for f in cursor.frames(self.broker.room_state("room-1"))],
                         [(3, "lo")])
        sink.replace("k", "Help")
        self.assertEqual([(f["offset"], f["text"]) for f in cursor.frames(self.broker.room_state("room-1"))],
                         [(0, "Help")])
        self.broker.finish(sink, published=True, message_id="m1")
        end = cursor.frames(self.broker.room_state("room-1"))
        self.assertEqual([(f["state"], f["message"], f["text"]) for f in end], [("published", "m1", "")])
        self.assertEqual(set(end[0]), {"turn", "agent", "offset", "text", "state", "message", "truncated"})
        self.now[0] = 50
        self.assertEqual([f["state"] for f in cursor.frames(self.broker.room_state("room-1"))], ["gone"])
        fresh = provisional.DraftCursor()  # a reconnect starts from the current snapshot (nothing left here)
        self.assertEqual(fresh.frames(self.broker.room_state("room-1")), [])

    def test_capability_is_declared_by_the_adapter_only(self) -> None:
        class Yes:
            def provisional_capability(self):
                return "supported"

        class Broken:
            def provisional_capability(self):
                raise RuntimeError

        class Odd:
            def provisional_capability(self):
                return "maybe"

        self.assertEqual(provisional.capability_of(Yes()), "supported")
        self.assertEqual(provisional.capability_of(object()), "unsupported")
        self.assertEqual(provisional.capability_of(Broken()), "unverified")
        self.assertEqual(provisional.capability_of(Odd()), "unverified")

    def test_sink_binding_is_correlation_exact_and_narrowed(self) -> None:
        sink = self.broker.open(identity(corr="c1"))
        self.assertIsNone(provisional.current_for("c1"))
        with provisional.scoped(sink):
            self.assertIs(provisional.current_for("c1"), sink)
            self.assertIsNone(provisional.current_for("c2"))
            self.assertIsNone(provisional.current())  # a payload-less runtime call sees nothing unless narrowed
            with provisional.narrowed("c2"):
                self.assertIsNone(provisional.current())
            with provisional.narrowed("c1"):
                self.assertIs(provisional.current(), sink)
            self.assertIsNone(provisional.current())
        self.assertIsNone(provisional.current_for("c1"))


# -- Claude Code adapter: native stream-json parsing ----------------------------------------------------------------------
def se(native: dict, msg="api_1", uuid=None, parent=None, session=SID) -> dict:
    return {"type": "stream_event", "event": native, "api_message_id": msg, "session_id": session,
            "parent_tool_use_id": parent, "uuid": uuid or f"u{id(native)}{random.random()}"}


def text_events(msg: str, index: int, pieces: list[str]) -> list[dict]:
    out = [se({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}}, msg)]
    out += [se({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": p}}, msg)
            for p in pieces]
    return out


class RecordingSink:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.text: dict[str, str] = {}

    def segment(self, key):
        self.calls.append(("segment", key))
        self.text.setdefault(key, "")

    def delta(self, key, text):
        self.calls.append(("delta", key, text))
        self.text[key] = self.text.get(key, "") + text

    def replace(self, key, text):
        self.calls.append(("replace", key, text))
        self.text[key] = text


class ClaudeObserverTests(unittest.TestCase):
    def lines(self, events) -> bytes:
        return b"".join(json.dumps(e, ensure_ascii=False).encode() + b"\n" for e in events)

    def test_only_main_agent_text_deltas_reach_the_sink(self) -> None:
        events = [
            {"type": "system", "subtype": "init", "session_id": SID, "tools": ["Bash"]},
            se({"type": "message_start", "message": {"id": "api_1"}}),
            se({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
            se({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "SECRET"}}),
            se({"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIG"}}),
            *text_events("api_1", 1, ["Привет", ", мир"]),
            se({"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "name": "Bash"}}),
            se({"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta",
                                                                     "partial_json": '{"command":"cat .env"}'}}),
            se({"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "TOOLTEXT"}}),
            {"type": "user", "session_id": SID, "message": {"content": [{"type": "tool_result", "content": "SECRET2"}]}},
            *[dict(e, parent_tool_use_id="toolu_1") for e in text_events("api_8", 0, ["SUBAGENT2"])],
            *[dict(e, session_id="other-session") for e in text_events("api_7", 0, ["FOREIGN"])],
        ]
        sink = RecordingSink()
        ClaudePartialObserver(SID, sink).feed(self.lines(events))
        joined = json.dumps(sink.calls, ensure_ascii=False)
        self.assertEqual(sink.text["api_1:1"], "Привет, мир")
        for leaked in ("SECRET", "SIG", "cat .env", "TOOLTEXT", "SECRET2", "SUBAGENT2", "FOREIGN"):
            self.assertNotIn(leaked, joined)

    def test_split_jsonl_split_utf8_duplicates_and_snapshot_correction(self) -> None:
        events = text_events("api_1", 0, ["Привет", ", мир"])
        dup = dict(events[1])  # the same CLI event uuid again: ignored
        raw = self.lines(events + [dup])
        snapshot = {"type": "assistant", "session_id": SID, "parent_tool_use_id": None, "uuid": "snap-1",
                    "message": {"id": "api_1", "content": [{"type": "text", "text": "Привет, мир!"}]}}
        raw += self.lines([snapshot])
        sink = RecordingSink()
        observer = ClaudePartialObserver(SID, sink)
        cut = raw.index("Привет".encode()) + 1  # inside a multi-byte UTF-8 character and inside a JSON line
        for chunk in (raw[:cut], raw[cut:cut + 7], raw[cut + 7:]):
            observer.feed(chunk)
        # Verified CLI shape: the snapshot carries ONE block at list index 0 whatever its stream index; for a streamed
        # message it is ignored, so it can neither double nor overwrite the text.
        self.assertEqual(sink.text, {"api_1:0": "Привет, мир"})
        self.assertEqual(sum(1 for c in sink.calls if c[0] == "delta"), 2)
        observer.feed(b'{"type": "stream_event", "event": {"type": "content_block_delta"')  # incomplete: buffered
        self.assertEqual(sink.text, {"api_1:0": "Привет, мир"})
        unstreamed = dict(snapshot, uuid="snap-2", message={"id": "api_2", "content": [{"type": "text", "text": "B"}]})
        observer.feed(self.lines([unstreamed, unstreamed]))  # a message without partial events: shown once
        self.assertEqual([v for k, v in sink.text.items() if k.startswith("api_2")], ["B"])

    def test_evidence_read_keeps_init_and_result_of_a_large_stream(self) -> None:
        import rosc.claude_conversation as cc
        path = Path(self.id().replace(".", "_") + ".jsonl")
        tmp = Path(__import__("tempfile").mkdtemp()) / path
        init = {"type": "system", "subtype": "init", "session_id": SID}
        filler = se({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x" * 200}})
        result = {"type": "result", "subtype": "success", "is_error": False, "session_id": SID, "result": "FINAL"}
        line = (json.dumps(filler) + "\n").encode()
        with open(tmp, "wb") as out:
            out.write((json.dumps(init) + "\n").encode())
            for _ in range(2 * cc.MAX_STDOUT_BYTES // len(line) + 50):
                out.write(line)
            out.write((json.dumps(result) + "\n").encode())
        facts = parse_stream(read_evidence(tmp))
        self.assertEqual((facts.init_session_id, facts.result_text, facts.malformed_lines), (SID, "FINAL", 0))
        self.assertLessEqual(len(read_evidence(tmp)), 2 * cc.MAX_STDOUT_BYTES)


class StreamingHandle:
    def __init__(self, thread: threading.Thread, code: int = 0) -> None:
        self.pid, self._thread, self._code = 41000, thread, code

    def wait(self, timeout: float):
        self._thread.join(timeout)
        return None if self._thread.is_alive() else self._code

    def kill(self) -> None:
        pass


class StreamingCliRunner:
    """A fake ``claude -p --output-format stream-json --include-partial-messages``: writes the native lines over time,
    in split chunks, and only writes the final ``result`` after the test releases it."""

    def __init__(self, pieces=("Привет", ", это ", "поток"), final="FINAL canonical reply", result="success") -> None:
        self.pieces, self.final, self.result = list(pieces), final, result
        self.release = threading.Event()
        self.argv: list[str] = []

    def spawn(self, argv, *, cwd, env, stdin_path, stdout_path, stderr_path, pass_fds):
        self.argv = list(argv)
        flag = "--resume" if "--resume" in argv else "--session-id"
        native = argv[argv.index(flag) + 1]
        partial = "--include-partial-messages" in argv

        def run() -> None:
            def write(data: bytes) -> None:
                with open(stdout_path, "ab") as out:
                    out.write(data)

            def emit(event: dict) -> None:
                raw = json.dumps(event, ensure_ascii=False).encode() + b"\n"
                write(raw[:len(raw) // 2])  # a JSON line split across writes
                time.sleep(0.01)
                write(raw[len(raw) // 2:])

            emit({"type": "system", "subtype": "init", "session_id": native, "model": "fake", "tools": [],
                  "mcp_servers": []})
            if partial:
                for event in [se({"type": "content_block_start", "index": 0,
                                  "content_block": {"type": "thinking", "thinking": ""}}, session=native),
                              se({"type": "content_block_delta", "index": 0,
                                  "delta": {"type": "thinking_delta", "thinking": "HIDDEN-THOUGHT"}}, session=native),
                              *[dict(e, session_id=native) for e in text_events("api_1", 1, self.pieces)]]:
                    emit(event)
            self.release.wait(10)
            if self.result == "success":
                emit({"type": "result", "subtype": "success", "is_error": False, "session_id": native,
                      "result": self.final, "total_cost_usd": 0.001})
            else:
                emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "session_id": native, "result": "API Error", "total_cost_usd": 0.0})

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return StreamingHandle(thread)


class ClaudeStreamingBridgeTests(BridgeCase):
    """Worker via the real Claude transport + real bridge/Core; the fake CLI streams before its final result."""

    def setUp(self) -> None:
        self.stream_runner = StreamingCliRunner()
        self.broker = provisional.ProvisionalBroker()
        super().setUp()

    def factory(self, agent):
        return ClaudeConversationTransport(make_session_factory(self.data / "rosc.db"), self.evidence,
                                           model=agent.model_id, cli=sys.executable, runner=self.stream_runner,
                                           environ=self.environ, partial_messages=True)

    def make_client(self, **kw):
        kw.setdefault("provisional_broker", self.broker)
        return super().make_client(**kw)

    def drafts(self, room: str):
        return self.broker.room_state(room)[1]

    def agent_messages(self, room: str) -> list[str]:
        return [body for _o, kind, _a, body in self.messages(room) if kind != "owner"]

    def test_partial_text_is_visible_before_result_and_core_commit_then_replaced_once(self) -> None:
        sent = self.post("question", [self.claude_agent])
        self.assertEqual(sent.status_code, 201, sent.text)
        room = sent.json()["message"]["conversation_id"]
        view = wait_for(lambda: next((d for d in self.drafts(room) if d.text == "Привет, это поток"), None))
        self.assertEqual(view.state, "streaming")
        self.assertEqual(self.agent_messages(room), [])  # nothing canonical yet
        self.assertEqual(self.fetch("SELECT state FROM group_turns WHERE turn_id = ?", view.turn_id)[0][0], "submitted")
        self.assertIn("--include-partial-messages", self.stream_runner.argv)
        self.assertNotIn("HIDDEN-THOUGHT", view.text)
        self.stream_runner.release.set()
        self.app.state.group.wait(room)
        self.assertEqual(self.agent_messages(room), ["FINAL canonical reply"])  # from the final result only
        final = self.drafts(room)[-1]
        message_id = self.fetch("SELECT reply_message_id FROM group_turns WHERE turn_id = ?", view.turn_id)[0][0]
        self.assertEqual((final.state, final.message_id), ("published", message_id))

    def test_provider_error_after_partial_text_never_publishes_the_fragments(self) -> None:
        self.stream_runner.result = "error"
        sent = self.post("question", [self.claude_agent])
        room = sent.json()["message"]["conversation_id"]
        wait_for(lambda: any(d.text for d in self.drafts(room)))
        quota_before = self.fetch("SELECT COUNT(*) FROM conversation_messages")[0][0]
        self.stream_runner.release.set()
        self.app.state.group.wait(room)
        self.assertEqual(self.agent_messages(room), [])
        self.assertEqual(self.fetch("SELECT COUNT(*) FROM conversation_messages")[0][0], quota_before)
        self.assertEqual(self.fetch("SELECT state FROM group_turns WHERE conversation_id = ?", room)[0][0], "unknown")
        self.assertEqual([(d.state, d.message_id) for d in self.drafts(room)], [("ended", None)])

    def test_cli_without_partial_support_is_canonical_only(self) -> None:
        transport = ClaudeConversationTransport(make_session_factory(self.data / "rosc.db"), self.evidence,
                                                model="haiku", cli=sys.executable, runner=self.runner,
                                                environ=self.environ)
        self.assertEqual(transport.provisional_capability(), "unsupported")  # `python --help` lists no such flag
        self.assertNotIn("--include-partial-messages", transport.argv(self.lane_stub(), False))

    def lane_stub(self):
        from rosc.worker_conversation import LaneRef
        return LaneRef("00000000-0000-4000-8000-000000000001", 1, "a", "c", "claude-code", SID, 1)


# -- OpenCode adapter: native /api/event ---------------------------------------------------------------------------------
def oc(kind: str, session="ses_A", **data) -> dict:
    return {"id": f"evt_{random.random()}", "type": kind, "data": {"sessionID": session, **data}}


class OpenCodeObserverTests(unittest.TestCase):
    def test_binding_to_exact_session_prompt_and_steps(self) -> None:
        sink = RecordingSink()
        observer = OpenCodeTextObserver("ses_A", "msg_mine", sink)
        for frame in [
            oc("session.text.delta", assistantMessageID="m0", ordinal=0, delta="BEFORE-DELIVERY"),
            oc("session.inbox.delivered", inboxID="msg_other"),
            oc("session.step.started", assistantMessageID="m0"),
            oc("session.text.delta", assistantMessageID="m0", ordinal=0, delta="OTHER-PROMPT"),
            oc("session.inbox.delivered", inboxID="msg_mine"),
            oc("session.text.delta", assistantMessageID="m0", ordinal=0, delta="OLD-STEP"),
            oc("session.step.started", assistantMessageID="m1"),
            oc("session.reasoning.delta", assistantMessageID="m1", ordinal=0, delta="THINKING"),
            oc("session.tool.input.delta", assistantMessageID="m1", ordinal=0, delta='{"cmd":"x"}'),
            oc("session.text.started", assistantMessageID="m1", ordinal=0),
            oc("session.text.delta", assistantMessageID="m1", ordinal=0, delta="Привет"),
            oc("session.text.delta", session="ses_B", assistantMessageID="m1", ordinal=0, delta="FOREIGN"),
            oc("session.text.delta", assistantMessageID="m1", ordinal=0, delta=", мир"),
            oc("session.text.ended", assistantMessageID="m1", ordinal=0, text="Привет, мир."),
            oc("session.execution.succeeded"),
            oc("session.step.started", assistantMessageID="m2"),
            oc("session.text.delta", assistantMessageID="m2", ordinal=0, delta="AFTER-END"),
            {"type": "session.text.delta", "data": "garbage"}, "junk", None,
        ]:
            observer.event(frame)
        self.assertEqual(sink.text, {"m1:0": "Привет, мир."})
        joined = json.dumps(sink.calls, ensure_ascii=False)
        for leaked in ("BEFORE", "OTHER-PROMPT", "OLD-STEP", "THINKING", "cmd", "FOREIGN", "AFTER-END"):
            self.assertNotIn(leaked, joined)


class FakeOpenCode:
    """Loopback fake of the verified OpenCode 2.2.6 HTTP surface used by ``ask`` (prompt/wait/message + /api/event)."""

    def __init__(self) -> None:
        self.prompted = threading.Event()
        self.release = threading.Event()
        self.event_paths: list[str] = []
        self.auth: list[str | None] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, value, status=200):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.auth.append(self.headers.get("Authorization"))
                if self.path.startswith("/api/event"):
                    fake.event_paths.append(self.path)
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()

                    def chunk(text: str) -> None:
                        data = text.encode()
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()

                    def send(frame) -> None:
                        raw = "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n"
                        chunk(raw[:7])  # frames split across HTTP chunks
                        chunk(raw[7:])

                    try:
                        send({"id": "evt_0", "type": "server.connected", "data": {}})
                        chunk(": heartbeat\n\n")
                        fake.prompted.wait(5)
                        for frame in [oc("session.inbox.delivered", inboxID="msg_x"),
                                      oc("session.step.started", assistantMessageID="m1"),
                                      oc("session.reasoning.delta", assistantMessageID="m1", ordinal=0, delta="THINK"),
                                      oc("session.text.started", assistantMessageID="m1", ordinal=0),
                                      oc("session.text.delta", assistantMessageID="m1", ordinal=0, delta="Пото"),
                                      oc("session.text.delta", assistantMessageID="m1", ordinal=0, delta="к идёт")]:
                            send(frame)
                        fake.release.wait(5)
                        send(oc("session.text.ended", assistantMessageID="m1", ordinal=0, text="Поток идёт."))
                        send(oc("session.execution.succeeded"))
                        time.sleep(0.5)
                    except OSError:
                        pass
                    return
                if "/message" in self.path:
                    self._json({"data": [
                        {"id": "msg_x", "type": "user", "text": "hi"},
                        {"id": "m1", "type": "assistant", "finish": "stop", "time": {"completed": 1},
                         "content": [{"type": "reasoning", "text": "THINK"}, {"type": "text", "text": "Final answer"}]},
                    ], "cursor": {"next": None}})
                    return
                self._json({}, 404)

            def do_POST(self):
                fake.auth.append(self.headers.get("Authorization"))
                length = int(self.headers.get("content-length") or 0)
                self.rfile.read(length)
                if self.path.endswith("/prompt"):
                    fake.prompted.set()
                    self._json({"data": {"id": "msg_x", "type": "user"}})
                elif self.path.endswith("/wait"):
                    fake.release.wait(5)
                    time.sleep(0.2)
                    self._json({})
                else:
                    self._json({}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class OpenCodeWireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeOpenCode()
        self.runtime = OpenCodeRuntime(Path(__import__("tempfile").mkdtemp()), environ={"PATH": "/nonexistent"})
        self.runtime._passwords["sess-1"] = "s" * 24  # noqa: SLF001 - the loopback credential seam
        self.handle = RuntimeHandle(1, 1, self.fake.port, "ses_A", "sess-1")
        self.broker = provisional.ProvisionalBroker()

    def tearDown(self) -> None:
        self.fake.close()

    def test_live_text_before_wait_returns_and_reply_stays_the_lookup_result(self) -> None:
        sink = self.broker.open(identity(corr="c1"))
        result: dict = {}

        def call() -> None:
            with provisional.scoped(sink), provisional.narrowed("c1"):
                result["reply"] = self.runtime.ask(self.handle, "msg_x", "hi", 10)

        thread = threading.Thread(target=call)
        thread.start()
        wait_for(lambda: self.broker.room_state("room-1")[1][0].text == "Поток идёт")
        self.assertNotIn("reply", result)  # the provider has not finished: the text is genuinely provisional
        self.fake.release.set()
        thread.join(10)
        self.assertEqual(result["reply"], "Final answer")  # canonical: lookup, never the fragments
        self.assertEqual(self.broker.room_state("room-1")[1][0].text, "Поток идёт.")
        self.assertIn("types=session.text.delta", self.fake.event_paths[0])
        self.assertNotIn("reasoning", self.fake.event_paths[0])
        self.assertTrue(all(a and a.startswith("Basic ") for a in self.fake.auth))
        self.assertEqual([t.name for t in threading.enumerate() if t.name == "rosc-opencode-events"], [])

    def test_without_a_narrowed_sink_no_subscription_is_opened(self) -> None:
        sink = self.broker.open(identity(corr="c1"))
        self.fake.release.set()
        self.fake.prompted.set()
        with provisional.scoped(sink):  # bound by Core, but no role transport narrowed it for this call
            self.assertEqual(self.runtime.ask(self.handle, "msg_x", "hi", 10), "Final answer")
        self.assertEqual(self.fake.event_paths, [])
        self.assertEqual(self.broker.room_state("room-1")[1][0].text, "")


# -- adapter neutrality: role/model/runtime independent ----------------------------------------------------------------
class TokenListAdapter:
    """Fake runtime A: native events are {"k": "tok", "v": ...} token chunks plus junk it must filter."""

    assurance = "fake_a"
    reply = "Hello, world"

    def __init__(self, capability="supported", outcome=SUBMIT_ACCEPTED) -> None:
        self.capability, self.outcome, self.seen = capability, outcome, []

    def provisional_capability(self):
        return self.capability

    def open_lane(self, lane):
        pass

    def busy(self, lane):
        return False

    def activity(self, lane, correlation_id):
        return None

    def ready_problem(self, project_id):
        return None

    def native(self):
        return [{"k": "tok", "v": "Hello"}, {"k": "secret", "v": "NATIVE-A-SECRET"}, {"k": "tok", "v": ", world"}]

    def submit(self, lane, payload):
        sink = provisional.current_for(payload.correlation_id)
        if sink is not None:
            for event in self.native():
                if event["k"] == "tok":
                    sink.delta("a", event["v"])
            self.seen.append(sink.identity)
        return SubmitOutcome(self.outcome, None if self.outcome == SUBMIT_ACCEPTED else "timeout")

    def lookup(self, lane, correlation_id):
        return LookupOutcome(LOOKUP_REPLY, text=self.reply) if self.outcome == SUBMIT_ACCEPTED else \
            LookupOutcome(LOOKUP_UNKNOWN)


class SnapshotAdapter(TokenListAdapter):
    """Fake runtime B: cumulative snapshots in a completely different native shape."""

    assurance = "fake_b"

    def submit(self, lane, payload):
        sink = provisional.current_for(payload.correlation_id)
        if sink is not None:
            for snap in ({"cumulative": "H", "meta": "NATIVE-B-SECRET"}, {"cumulative": "Hello, wor"},
                         {"cumulative": "Hello, world"}):
                sink.replace("b", snap["cumulative"])
            self.seen.append(sink.identity)
        return SubmitOutcome(SUBMIT_ACCEPTED)


class AdapterNeutralityTests(CoreCase):
    def worker(self, name: str, project: int = 1) -> str:  # a group-capable Worker; its transport is the fake
        from test_p043b_worker_conversation_core import WORKER_CAPS
        agent = self.registry.register_agent(OWNER, display_name=name, runtime_adapter="claude-code",
                                             provider_id="anthropic", model_id="m", capabilities=WORKER_CAPS)
        self.registry.create_assignment(OWNER, project, agent_id=agent["agent_id"], role="worker",
                                        capabilities=WORKER_CAPS)
        return agent["agent_id"]

    def setUp(self) -> None:
        super().setUp()
        arch = self.registry.register_agent(OWNER, display_name="Arch", runtime_adapter="opencode", provider_id="x",
                                            model_id="m", capabilities=ARCHITECT_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=arch["agent_id"], role="architect",
                                        capabilities=ARCHITECT_CAPS)
        self.arch = arch["agent_id"]

    def run_room(self, worker_adapter, architect_adapter, task_id=None):
        broker = provisional.ProvisionalBroker()
        core = WorkerConversationCore(make_session_factory(self.data / "rosc.db"), LocalOwnerPolicy(),
                                      rng=random.Random(3))
        bridge = GroupConversationBridge(make_session_factory(self.data / "rosc.db"), LocalOwnerPolicy(), core,
                                         lambda agent: worker_adapter, architect_transport=architect_adapter,
                                         drafts=broker)
        cursor_frames: list[dict] = []
        original = broker.finish

        def finish(sink, **kw):  # capture the streamed DTOs exactly before Core's outcome is applied
            cursor_frames.extend(cursor.frames(broker.room_state(sink.identity.conversation_id)))
            original(sink, **kw)

        cursor = provisional.DraftCursor()
        broker.finish = finish
        sent = bridge.post_message(OWNER, 1, task_id, client_key=self.key(), body="Both of you, please.",
                                   to=[self.A, self.arch])
        room = sent["message"]["conversation_id"]
        bridge.wait(room)
        cursor_frames.extend(cursor.frames(broker.room_state(room)))
        return room, cursor_frames

    def shape(self, frames_):
        return sorted((f["state"], f["offset"], f["text"]) for f in frames_ if f["text"] or f["state"] != "streaming")

    def test_two_native_formats_same_dto_for_worker_and_architect_in_pc_and_tc(self) -> None:
        results = []
        for task in (None, 1):
            for worker, arch in ((TokenListAdapter(), SnapshotAdapter()), (SnapshotAdapter(), TokenListAdapter())):
                room, got = self.run_room(worker, arch, task)
                by_agent = {}
                for frame in got:
                    by_agent.setdefault(frame["agent"], []).append(frame)
                self.assertEqual(set(by_agent), {self.A, self.arch})
                for agent_frames in by_agent.values():
                    streamed = [f for f in agent_frames if f["state"] == "streaming"]
                    self.assertEqual("".join(f["text"] for f in streamed), "Hello, world")
                    self.assertEqual(agent_frames[-1]["state"], "published")
                    self.assertTrue(agent_frames[-1]["message"])
                blob = json.dumps(got)
                self.assertNotIn("NATIVE-A-SECRET", blob)
                self.assertNotIn("NATIVE-B-SECRET", blob)
                self.assertEqual({k for f in got for k in f}, {"turn", "agent", "offset", "text", "state", "message",
                                                               "truncated"})
                bodies = [b for (b,) in self.fetch("SELECT body FROM conversation_messages WHERE conversation_id = ?"
                                                   " ORDER BY ordinal", room)]
                self.assertEqual(bodies[-2:], ["Hello, world", "Hello, world"])
                results.append(sorted(f["state"] for f in got))
        self.assertEqual(len(set(map(tuple, results))), 1)  # identical UX whichever runtime serves whichever role

    def test_unsupported_adapter_is_canonical_only_and_others_still_stream(self) -> None:
        room, got = self.run_room(TokenListAdapter(capability="unsupported"), SnapshotAdapter())
        self.assertEqual({f["agent"] for f in got}, {self.arch})
        bodies = [b for (b,) in self.fetch("SELECT body FROM conversation_messages WHERE conversation_id = ?", room)]
        self.assertEqual(bodies.count("Hello, world"), 2)

    def test_unknown_outcome_never_publishes_or_forwards_fragments(self) -> None:
        worker = TokenListAdapter(outcome=SUBMIT_UNKNOWN)
        room, got = self.run_room(worker, TokenListAdapter(outcome=SUBMIT_UNKNOWN))
        bodies = [b for (b,) in self.fetch("SELECT body FROM conversation_messages WHERE conversation_id = ?", room)]
        self.assertNotIn("Hello, world", bodies)
        self.assertEqual([f["state"] for f in got if f["state"] != "streaming"], ["ended"])
        self.assertIsNone(got[-1]["message"])
        states = [s for (s,) in self.fetch("SELECT state FROM group_turns WHERE conversation_id = ?", room)]
        self.assertIn("unknown", states)


# -- SSE: the same P061 stream carries typed draft events -----------------------------------------------------------------
class DraftSseTests(RoomEventsCase):
    def setUp(self) -> None:
        super().setUp()
        self.broker = provisional.ProvisionalBroker()
        from rosc.app import create_app
        self.streams = RoomEventStreams(interval=0.05, heartbeat=0.3, max_seconds=5.0, draft_tick=0.01)
        self.app = create_app(self.data / "rosc.db", self.data / "workspace", authorization_policy=self.policy,
                              group_transport_factory=lambda agent: (_ for _ in ()).throw(AssertionError()),
                              run_monitor_interval=None, room_event_streams=self.streams,
                              provisional_broker=self.broker)

    def drafts_of(self, result) -> list[dict]:
        return [json.loads(data) for name, data in result["frames"] if name == "draft"]

    def test_draft_frames_reach_only_their_room_and_invalidate_still_works(self) -> None:
        room, _ = self.worker_says("Done.")
        task_room, _ = self.worker_says("Task done.", task_id=1)
        sink = self.broker.open(provisional.TurnIdentity(1, room, None, "turn-x", self.A, "corr-x"))
        other = self.broker.open(provisional.TurnIdentity(1, task_room, 1, "turn-y", self.A, "corr-y"))
        sink.delta("k", "Hel")
        other.delta("k", "TASK-ROOM-ONLY")

        def more():
            sink.delta("k", "lo")

        def done():
            self.broker.finish(sink, published=True, message_id="msg-final")

        result = self.run_stream(self.PC, [("draft", more), ("draft", done), ("draft", None)],
                                 linger=0.2)
        self.assertEqual(result["status"], 200)
        hello = json.loads(next(data for name, data in result["frames"] if name == "hello"))
        self.assertEqual(hello, {"v": 1, "drafts": True})
        drafts = self.drafts_of(result)
        text = "".join(d["text"] for d in drafts if d["state"] == "streaming")
        self.assertEqual(text, "Hello")
        self.assertEqual(drafts[-1]["state"], "published")
        self.assertEqual(drafts[-1]["message"], "msg-final")
        self.assertNotIn("TASK-ROOM-ONLY", result["body"])
        self.assertNotIn("corr-x", result["body"])  # correlation stays internal
        tc = self.run_stream(self.tc(1), [("draft", None)])
        self.assertIn("TASK-ROOM-ONLY", tc["body"])
        self.assertNotIn("Hello", tc["body"])

    def test_reconnect_gets_snapshot_and_revocation_closes_stream(self) -> None:
        room, _ = self.worker_says("Done.")
        sink = self.broker.open(provisional.TurnIdentity(1, room, None, "turn-x", self.A, "corr-x"))
        sink.delta("k", "partial text")
        first = self.run_stream(self.PC, [("draft", None)])
        again = self.run_stream(self.PC, [("draft", None)])
        for result in (first, again):
            self.assertEqual([(d["offset"], d["text"]) for d in self.drafts_of(result)][:1], [(0, "partial text")])

        def revoke():
            self.policy.allow = False
            sink.delta("k", " AFTER-REVOKE")

        revoked = self.run_stream(self.PC, [("draft", revoke)], linger=0.5)
        self.assertTrue(revoked["closed"])
        self.assertEqual(self.streams.active, 0)
        self.policy.allow = True

    def count_decisions(self) -> list[int]:
        calls = [0]
        decide = self.policy.decide

        def counted(*args, **kwargs):
            calls[0] += 1
            return decide(*args, **kwargs)

        self.policy.decide = counted
        return calls

    def test_revocation_between_probes_never_sends_new_draft_text(self) -> None:
        """r01: no DB probe is due (interval 30 s), access is revoked, then a delta arrives: it is NOT written."""
        self.streams.interval = 30.0
        room, _ = self.worker_says("Done.")
        sink = self.broker.open(provisional.TurnIdentity(1, room, None, "turn-x", self.A, "corr-x"))
        sink.delta("k", "before ")

        def revoke_then_delta():
            self.policy.allow = False
            sink.delta("k", "AFTER-REVOKE")

        result = self.run_stream(self.PC, [("draft", revoke_then_delta)], linger=0.5)
        self.policy.allow = True
        self.assertEqual(result["status"], 200)
        self.assertIn("\"text\":\"before\"", result["body"])
        self.assertNotIn("AFTER-REVOKE", result["body"])
        self.assertTrue(result["closed"])  # the server ended the stream itself
        self.assertEqual(self.streams.active, 0)

    def test_changed_draft_is_authorized_but_idle_ticks_are_not(self) -> None:
        self.streams.interval = 30.0
        room, _ = self.worker_says("Done.")
        sink = self.broker.open(provisional.TurnIdentity(1, room, None, "turn-x", self.A, "corr-x"))
        sink.delta("k", "one")
        calls = self.count_decisions()
        marks: dict[str, int] = {}

        def idle_then_delta():
            time.sleep(0.3)  # ~30 empty 10-ms draft ticks, no probe due
            marks["idle"] = calls[0]
            sink.delta("k", " two")

        stream = self.run_stream(self.PC, [("draft", idle_then_delta), ("draft", None)], linger=0.1)
        self.assertEqual(stream["status"], 200)
        self.assertEqual(marks["idle"], 1)  # idle ticks did no authorization at all
        self.assertIn(" two", stream["body"])  # a changed draft still flows while access holds
        self.assertGreaterEqual(calls[0], 2)  # ...after its own read-only check

    def test_denied_request_never_gets_a_stream(self) -> None:
        self.worker_says("Done.")
        self.policy.allow = False
        denied = self.run_stream(self.PC)
        self.assertEqual(denied["status"], 403)
        self.assertNotIn("draft", denied["body"])


if __name__ == "__main__":
    unittest.main()
