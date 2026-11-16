"""P052 r03: group lifecycle after a finished round + Owner-switchable Conversation Diagnostics.

A. A naturally finished round (awaiting_owner) is history: a new Owner -> Architect direct message whose authentic reply
   addresses ``@Claude`` opens ONE fresh bounded round (previous round closed, fresh N grants, Architect 1/N, Claude 0/N
   before its turn), Claude gets exactly one turn and the room ends naturally.  Every refusal leaves a durable,
   body-free reason visible in the room status without the diagnostics switch.
B. Diagnostics OFF writes nothing; metadata mode never contains bodies; body mode contains the exact texts; switches
   apply immediately without restart; filesystem failures are non-fatal; permissions, symlinks and rotation are safe.

Isolated temporary databases, the deterministic fake Claude CLI runner and the fake Architect runtime (P047/P052).
No real CLI, provider, network or Owner data is touched.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from test_p052_agent_mentions_quota import FIN, P052Case
from test_p052_r02_corrections import InvitationNonPreemptionTests

from rosc import conversation_trace as ctrace

SENTINEL_OWNER = "OWNER-SENTINEL-7f3a посчитай 17*23"
SENTINEL_ARCH = "@Claude ARCH-SENTINEL-91c2 посчитай ещё раз"
SENTINEL_CLAUDE = "CLAUDE-SENTINEL-4d10 ответ 391"


class TraceMixin:
    def factory(self, agent):
        transport = super().factory(agent)
        transport._trace = self.app.state.conversation_trace  # same composition as the product factory (trace=...)
        return transport

    def trace_dir(self) -> Path:
        return self.data / "diagnostics" / "conversations"

    def trace_events(self) -> list[dict]:
        path = self.trace_dir() / ctrace.TRACE_FILENAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def set_diag(self, **change) -> dict:
        response = self.api.put("/api/settings/conversation-diagnostics", json=change)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class LifecycleTests(TraceMixin, P052Case):
    def finished_first_round(self, task: str | None = None) -> None:
        """Round #1: Owner -> Architect, Architect @Claude, Claude answers the Architect with an explicit finish (r04)."""
        self.arch_runtime.replies = ["@Claude сколько 2+2?"]
        self.runner.replies = ["4" + FIN]
        self.direct("Architect, спроси Claude", task=task)
        status = self.status(task)
        self.assertEqual(status["round"]["state"], "awaiting_owner")
        self.assertEqual(status["stop_reason"], "finished")
        self.assertFalse(status["round_active"])  # P052 r03: finished = history, not an active conversation

    def test_owner_direct_after_finished_round_opens_one_fresh_bounded_round(self) -> None:
        for task in (None, "T0001"):
            with self.subTest(task=task or "PC"):
                self.runner.calls.clear()
                self.finished_first_round(task)
                first = self.status(task)["round"]
                calls = len(self.runner.calls)
                self.arch_runtime.replies = [SENTINEL_ARCH]
                self.runner.replies = [SENTINEL_CLAUDE + FIN]
                self.direct(SENTINEL_OWNER, task=task)
                status = self.status(task)
                fresh = status["round"]
                self.assertNotEqual(fresh["round_id"], first["round_id"])
                self.assertEqual(fresh["round_no"], first["round_no"] + 1)
                self.assertEqual(self.fetch("SELECT state FROM group_rounds WHERE round_id = ?", first["round_id"]),
                                 [("closed",)])
                # Fresh N=3 grants (never the historical quotas): Architect's invitation counted 1/3, Claude 1/3 after
                # its single turn.
                self.assertEqual({a: (g["published"], g["quota"]) for a, g in fresh["grants"].items()},
                                 {self.arch: (1, 3), self.claude_agent: (1, 3)})
                self.assertEqual(len(self.runner.calls), calls + 1)  # exactly one Claude turn, no extra request
                self.assertEqual(status["round"]["state"], "awaiting_owner")
                self.assertEqual(status["stop_reason"], "finished")
                self.assertFalse(status["round_active"])
                self.assertEqual(status["invitation"], None)  # latest Agent message is Claude's reply, not a direct one
                msgs = self.messages(task)
                self.assertEqual([m["body"] for m in msgs[-3:]], [SENTINEL_OWNER, SENTINEL_ARCH, SENTINEL_CLAUDE])
                # Claude's turn input saw the Architect's invitation (the quota snapshot started at 0/3 for Claude).
                prompt = self.runner.calls[-1]["prompt"]
                self.assertIn(SENTINEL_ARCH, prompt)
                quota = json.loads(prompt.splitlines()[prompt.splitlines().index("ROSC ENVELOPE (trusted):") + 1])["quota"]
                self.assertEqual((quota["you"]["published"], quota["you"]["quota"]), (0, 3))
                # A reload (fresh status read) shows the same truthful finished state; nothing is replayed.
                self.assertEqual(self.status(task)["round"]["round_id"], fresh["round_id"])
                self.assertEqual(len(self.runner.calls), calls + 1)

    def test_status_while_fresh_round_runs_is_active_with_fresh_quotas(self) -> None:
        self.finished_first_round()
        seen: dict = {}
        original = self.runner.spawn

        def spawn(argv, **kw):
            seen.update(self.status())
            return original(argv, **kw)
        self.runner.spawn = spawn
        self.arch_runtime.replies = ["@Claude ещё пример: 3+3?"]
        self.runner.replies = ["6" + FIN]
        self.direct("ещё раз")
        self.assertTrue(seen["round_active"])
        self.assertEqual(seen["round"]["round_no"], 2)
        self.assertEqual(seen["round"]["grants"][self.claude_agent], {"quota": 3, "published": 0, "failed": False})
        self.assertEqual(seen["round"]["grants"][self.arch]["published"], 1)
        self.assertFalse(self.status()["round_active"])

    def test_newer_owner_message_refusal_is_recorded_without_trace(self) -> None:
        self.finished_first_round()
        self.arch_runtime.replies = ["@Claude проверь"]
        original = self.app.state.group._core.bridge_direct_reply

        def newer_first(session, **kw):  # an Owner message lands between the provider reply and its commit
            from rosc.models import ConversationMessage, ConversationParticipant
            owner = session.query(ConversationParticipant).filter_by(
                conversation_id=kw["owner_message"].conversation_id, kind="owner").first()
            session.add(ConversationMessage(message_id="00000000-0000-4000-8000-000000000099",
                                            conversation_id=kw["owner_message"].conversation_id,
                                            sender_participant_id=owner.participant_id,
                                            ordinal=kw["reply"].ordinal + 1, body="позже", client_key="later-key-01"))
            session.flush()
            return original(session, **kw)
        with patch.object(self.app.state.group._core, "bridge_direct_reply", side_effect=newer_first):
            self.direct("Architect, вопрос")
        status = self.status()
        self.assertEqual(status["invitation"]["decision"], "declined")
        self.assertEqual(status["invitation"]["reason"], "newer_owner_message")
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds"), [(1,)])
        self.assertEqual(len(self.runner.calls), 1)  # nothing new dispatched
        self.assertEqual(self.trace_events(), [])  # truthful without the diagnostics switch

    def test_pause_and_brake_and_oversize_refusals_are_visible(self) -> None:
        self.finished_first_round()
        for name, setup, reply in (
                ("paused", lambda: self.api.post(f"{self.url()}/controls", json={"paused": True}), "@Claude давай"),
                ("emergency_brake", lambda: self.api.post(f"{self.url()}/controls", json={"emergency_brake": True}),
                 "@Claude стоп"),
                ("reply_too_long", lambda: None, "@Claude " + "x" * 3100)):
            with self.subTest(name):
                self.fetch_exec("DELETE FROM conversation_dispatch_controls")
                setup()
                self.arch_runtime.replies = [reply]
                calls = len(self.runner.calls)
                self.direct(f"вопрос {name}")
                status = self.status()
                self.assertEqual(status["invitation"]["reason"], name)
                self.assertEqual([(u["agent_id"], u["reason"]) for u in status["unanswered"]],
                                 [(self.claude_agent, name)])
                self.assertEqual(len(self.runner.calls), calls)

    def test_unavailable_peer_and_no_mention_and_owner_mention(self) -> None:
        self.fetch_exec("UPDATE role_assignments SET state = 'revoked', revoked_at = CURRENT_TIMESTAMP WHERE agent_id = ?",
                        self.claude_agent)
        self.arch_runtime.replies = ["@Claude ты тут?"]
        self.direct("вопрос")
        self.assertEqual(self.messages()[-1]["group"], None)  # Claude is no longer in scope: no structured addressee
        self.assertEqual(self.status()["invitation"], None)
        self.fetch_exec("UPDATE role_assignments SET state = 'active', revoked_at = NULL WHERE agent_id = ?",
                        self.claude_agent)
        self.arch_runtime.replies = ["просто ответ без упоминаний"]
        self.direct("вопрос 2")
        self.assertEqual(self.status()["invitation"], None)
        self.arch_runtime.replies = ["@Owner готово"]
        self.direct("вопрос 3")
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds"), [(0,)])
        self.assertEqual(len(self.runner.calls), 0)

    def test_released_peer_is_declined_with_its_reason(self) -> None:
        self.finished_first_round()
        self.api.post(f"{self.url()}/members/{self.claude_agent}/release")
        self.arch_runtime.replies = ["@Claude снова"]
        self.direct("вопрос")
        status = self.status()
        self.assertEqual(status["invitation"]["reason"], "peer_unavailable")
        self.assertEqual([(u["agent_id"], u["reason"]) for u in status["unanswered"]], [(self.claude_agent, "released")])
        self.assertEqual(len(self.runner.calls), 1)

    def test_failed_wake_keeps_the_round_durable_and_is_traced(self) -> None:
        self.set_diag(enabled=True)
        self.finished_first_round()
        self.arch_runtime.replies = ["@Claude ещё"]
        with patch.object(self.app.state.group, "wake", side_effect=RuntimeError("boom")):
            self.direct("вопрос")
        status = self.status()
        self.assertEqual(status["round"]["round_no"], 2)
        self.assertEqual(status["round"]["state"], "open")
        self.assertEqual(status["waiting"], [self.claude_agent])
        events = [e for e in self.trace_events() if e["event"] == "wake_failed"]
        self.assertEqual([e["reason"] for e in events], ["wake_exception"])
        self.assertNotIn("boom", json.dumps(self.trace_events()))


class PendingRequestReasonTests(InvitationNonPreemptionTests):
    """r02 non-preemption + r03: the refusal is explained (pending_owner_request) and the Worker request is untouched."""

    def architect_invites(self, task: str | None = None) -> None:
        self._task = task
        super().architect_invites(task)

    def assert_preserved(self, state: str) -> None:
        super().assert_preserved(state)
        invitation = self.status(getattr(self, "_task", None))["invitation"]
        self.assertEqual((invitation["decision"], invitation["reason"]), ("declined", "pending_owner_request"))


class DiagnosticsTests(TraceMixin, P052Case):
    def exchange(self, task: str | None = None) -> None:
        self.arch_runtime.replies = ["@Claude первый"]
        self.runner.replies = ["первый ответ" + FIN]
        self.direct("старт", task=task)
        self.arch_runtime.replies = [SENTINEL_ARCH]
        self.runner.replies = [SENTINEL_CLAUDE + FIN]
        self.direct(SENTINEL_OWNER, task=task)

    def test_defaults_are_off_and_off_writes_nothing(self) -> None:
        view = self.api.get("/api/settings/conversation-diagnostics").json()
        self.assertEqual((view["enabled"], view["include_bodies"], view["effective"]["mode"]), (False, False, "off"))
        self.exchange()
        self.assertFalse(self.trace_dir().exists())

    def test_metadata_mode_traces_the_decision_chain_without_bodies(self) -> None:
        for task in (None, "T0001"):
            with self.subTest(task=task or "PC"):
                self.set_diag(enabled=True)
                self.exchange(task)
                raw = (self.trace_dir() / ctrace.TRACE_FILENAME).read_text(encoding="utf-8")
                for sentinel in ("SENTINEL", "посчитай", "первый", "старт"):  # never digit-only (ids are hex)
                    self.assertNotIn(sentinel, raw)
                events = self.trace_events()
                names = [e["event"] for e in events]
                for required in ("owner_message_committed", "architect_request_sent", "architect_reply_received",
                                 "mentions_resolved", "invitation_checked", "round_closed", "round_opened",
                                 "turn_queued", "turn_reserved", "dispatch_attempted", "transport_request",
                                 "transport_outcome", "reply_captured", "reply_published", "round_awaiting_owner",
                                 "driver_started", "driver_stopped"):
                    self.assertIn(required, names)
                accepted = [e for e in events if e["event"] == "invitation_checked" and e.get("decision") == "accepted"]
                self.assertTrue(accepted)
                last = accepted[-1]
                self.assertEqual(last["invited"], [self.claude_agent])
                self.assertTrue(last["this_reply_counts"])
                opened = [e for e in events if e["event"] == "round_opened"][-1]
                self.assertEqual({g["agent_id"]: (g["published"], g["quota"]) for g in opened["grants"]},
                                 {self.arch: (1, 3), self.claude_agent: (0, 3)})
                dispatched = [e for e in events if e["event"] == "dispatch_attempted"][-1]
                self.assertEqual(dispatched["quota_snapshot"]["you"]["published"], 0)
                self.assertGreater(dispatched["request_text_chars"], 0)
                self.assertNotIn("request_text", dispatched)
                self.assertTrue(all(e["capture"] == "metadata" for e in events))
                if task:
                    self.assertTrue(any(e.get("task_id") == 1 for e in events))
                self.set_diag(enabled=False)
                (self.trace_dir() / ctrace.TRACE_FILENAME).unlink()

    def test_body_mode_contains_exact_texts_and_switches_apply_immediately(self) -> None:
        self.set_diag(enabled=True, include_bodies=True)
        self.exchange()
        events = self.trace_events()
        owner = [e for e in events if e["event"] == "owner_message_committed"][-1]
        self.assertEqual(owner["body"], SENTINEL_OWNER)
        sent = [e for e in events if e["event"] == "architect_request_sent"][-1]
        self.assertTrue(sent["request_text"].rstrip().endswith(SENTINEL_OWNER))  # exact text handed to the runtime
        self.assertEqual(sent["request_text"], self.arch_texts()[-1])
        self.assertIn("ROSC QUOTA", sent["guidance_text"])
        reply = [e for e in events if e["event"] == "architect_reply_received"][-1]
        self.assertEqual(reply["response_text"], SENTINEL_ARCH)
        transport = [e for e in events if e["event"] == "transport_request" and e["transport"] == "claude_cli"][-1]
        self.assertEqual(transport["request_text"], self.runner.calls[-1]["prompt"])  # exact CLI stdin prompt
        dispatched = [e for e in events if e["event"] == "dispatch_attempted"][-1]
        self.assertIn(SENTINEL_ARCH, dispatched["request_text"])
        captured = [e for e in events if e["event"] == "reply_captured"][-1]
        self.assertEqual(captured["response_text"], SENTINEL_CLAUDE)
        self.assertTrue(all(e["capture"] == "full" for e in events if e["event"] != "trace_mode_changed"))
        # Dynamic switch: unchecked -> metadata only for the very next events, no restart.
        self.set_diag(include_bodies=False)
        count = len(self.trace_events())
        self.arch_runtime.replies = ["ответ без адресата"]
        self.direct("SECOND-SENTINEL-55")
        later = self.trace_events()[count:]
        self.assertTrue(later)
        self.assertNotIn("SECOND-SENTINEL-55", json.dumps(later, ensure_ascii=False))
        # OFF: nothing more is written; earlier files are kept.
        self.set_diag(enabled=False)
        size = (self.trace_dir() / ctrace.TRACE_FILENAME).stat().st_size
        self.direct("после выключения")
        self.assertEqual((self.trace_dir() / ctrace.TRACE_FILENAME).stat().st_size, size)
        view = self.api.get("/api/settings/conversation-diagnostics").json()
        self.assertEqual((view["enabled"], view["include_bodies"], view["effective"]["mode"]), (False, False, "off"))

    def test_settings_persist_and_bodies_alone_do_not_enable_capture(self) -> None:
        view = self.set_diag(include_bodies=True)
        self.assertEqual((view["enabled"], view["include_bodies"], view["effective"]["mode"]), (False, True, "off"))
        self.direct("ничего не пишется")
        self.assertFalse(self.trace_dir().exists())
        self.set_diag(enabled=True)
        # A fresh application over the same database reads the stored switches (persisted across restarts).
        fresh = self.make_client()
        self.assertEqual(fresh.get("/api/settings/conversation-diagnostics").json()["effective"]["mode"], "full")

    def test_settings_api_rejects_non_booleans_and_unknown_fields(self) -> None:
        for body in ({"enabled": 1}, {"enabled": "true"}, {"include_bodies": None, "other": True}):
            with self.subTest(body):
                self.assertEqual(self.api.put("/api/settings/conversation-diagnostics", json=body).status_code, 422)
        self.assertEqual(self.api.get("/api/settings/conversation-diagnostics").json()["enabled"], False)

    def test_no_app_owned_secret_material_in_any_mode(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-SECRET-ENV-123"}):
            self.set_diag(enabled=True, include_bodies=True)
            self.exchange()
        raw = (self.trace_dir() / ctrace.TRACE_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn("SECRET-ENV", raw)
        self.assertNotIn("--append-system-prompt", raw)  # never argv
        self.assertNotIn("Authorization", raw)

    def test_filesystem_failure_is_non_fatal_and_reported(self) -> None:
        self.set_diag(enabled=True)
        with patch.object(ctrace.ConversationTrace, "_open_file", side_effect=OSError(28, "No space left")):
            self.exchange()
        self.assertEqual(len(self.runner.calls), 2)  # conversation unaffected, no retries
        self.assertEqual(self.fetch("SELECT count(*) FROM group_turns WHERE state = 'published'"), [(2,)])
        effective = self.api.get("/api/settings/conversation-diagnostics").json()["effective"]
        self.assertGreater(effective["dropped"], 0)
        self.assertEqual(effective["problem"], "disk_full")


class SinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        os.chmod(self.data, 0o700)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_off_is_a_no_op_and_on_creates_private_files(self) -> None:
        trace = ctrace.ConversationTrace(self.data)
        trace.emit("owner_message_committed", body="x")
        self.assertFalse((self.data / "diagnostics").exists())
        trace.configure(enabled=True, bodies=False)
        trace.emit("owner_message_committed", body="secret body", message_id="not-a-uuid", reason="Hello world!")
        directory = self.data / "diagnostics" / "conversations"
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        path = directory / ctrace.TRACE_FILENAME
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        record = json.loads(path.read_text().splitlines()[-1])
        self.assertNotIn("body", record)
        self.assertEqual(record["body_chars"], 11)
        self.assertNotIn("message_id", record)  # invalid id dropped, never stored verbatim
        self.assertEqual(record["reason"], "other")  # free text is never a reason code
        trace.emit("not_an_event", reason="x")
        self.assertEqual(len(path.read_text().splitlines()), 2)  # mode change + one event

    def test_symlinked_directory_or_file_is_refused(self) -> None:
        target = self.data / "elsewhere"
        target.mkdir(mode=0o700)
        (self.data / "diagnostics").mkdir(mode=0o700)
        os.symlink(target, self.data / "diagnostics" / "conversations")
        trace = ctrace.ConversationTrace(self.data)
        trace.configure(enabled=True, bodies=True)
        trace.emit("owner_message_committed", body="x")
        self.assertEqual(list(target.iterdir()), [])
        self.assertGreater(trace.dropped, 0)
        os.unlink(self.data / "diagnostics" / "conversations")
        directory = self.data / "diagnostics" / "conversations"
        directory.mkdir(mode=0o700)
        os.symlink(target / "victim.jsonl", directory / ctrace.TRACE_FILENAME)
        trace2 = ctrace.ConversationTrace(self.data)
        trace2.configure(enabled=True, bodies=True)
        trace2.emit("owner_message_committed", body="x")
        self.assertFalse((target / "victim.jsonl").exists())

    def test_bounded_rotation_only_touches_its_own_files(self) -> None:
        trace = ctrace.ConversationTrace(self.data, max_file_bytes=2000, max_files=2)
        trace.configure(enabled=True, bodies=True)
        directory = self.data / "diagnostics" / "conversations"
        (directory / "keep-me.txt").write_text("owner data")
        for i in range(60):
            trace.emit("owner_message_committed", body="y" * 200, ordinal=i)
        names = sorted(p.name for p in directory.iterdir())
        rotated = [n for n in names if ctrace.ROTATED.match(n)]
        self.assertLessEqual(len(rotated), 2)
        self.assertIn("keep-me.txt", names)
        self.assertIn(ctrace.TRACE_FILENAME, names)
        for name in names:
            if name.endswith(".jsonl"):
                self.assertLessEqual((directory / name).stat().st_size, 2000)

    def test_concurrent_emits_produce_whole_lines(self) -> None:
        trace = ctrace.ConversationTrace(self.data)
        trace.configure(enabled=True, bodies=True)
        threads = [threading.Thread(target=lambda: [trace.emit("turn_queued", body="z" * 500) for _ in range(50)])
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = (self.data / "diagnostics" / "conversations" / ctrace.TRACE_FILENAME).read_text().splitlines()
        self.assertEqual(len(lines), 401)
        seqs = [json.loads(line)["seq"] for line in lines]
        self.assertEqual(sorted(seqs), list(range(1, 402)))


if __name__ == "__main__":
    unittest.main()
