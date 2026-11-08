"""P024 / R1: opt-in, privacy-safe Architect latency diagnostics.

Offline only: the REAL ``OpenCodeRuntime`` / Broker / ToolHost against the test-only fake ``opencode serve``.  The
end-to-end class subclasses the P022 end-to-end tests, so every P022 behaviour (final reply after ``tool-calls`` steps,
provider error, ``unknown(timeout)`` -> ``reconcile``, no re-send, exactly-once commit) is re-asserted WITH tracing on.
Every private marker is synthetic.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import test_p022_architect_multistep_reply as p22
import rosc.architect_runtime as runtime_module
from rosc.app import create_app
from rosc.architect_latency import (DISABLED, ENV_TRACE, FIELDS, LatencyTrace, OUTCOME_OTHER, OUTCOMES,
                                    TRACE_FILENAME)
from rosc.architect_runtime import OpenCodeRuntime
from rosc.architect_service import runtime_message_id
from rosc.architect_tools import ToolHost
from scripts import architect_latency_summary as summary

HEAD_KEYS = {"schema_version", "event", "origin", "seq", "at_utc", "t_ms"}
SECRET_BODY = "SYNTH-OWNER-BODY-SECRET-aa11"
SECRET_PATH = "agents/architect/rules/onboarding.md"


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]


class SinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_disabled_by_default_writes_nothing(self) -> None:
        for env in ({}, {ENV_TRACE: ""}, {ENV_TRACE: "0"}, {ENV_TRACE: "false"}, {ENV_TRACE: "off"}):
            trace = LatencyTrace.from_environment(self.root, env)
            self.assertIs(trace, DISABLED)
            trace.emit("submit", outcome="scheduled")
            with trace.scope(phase="deliver"):
                trace.emit("probe", duration_ms=1)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_enabled_creates_private_dir_and_file(self) -> None:
        trace = LatencyTrace.from_environment(self.root, {ENV_TRACE: "1"})
        self.assertTrue(trace.enabled)
        trace.emit("submit", outcome="scheduled", duration_ms=1.5)
        directory = self.root / "diagnostics"
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((directory / TRACE_FILENAME).stat().st_mode), 0o600)
        events = read_events(directory / TRACE_FILENAME)
        self.assertEqual([e["event"] for e in events], ["trace_started", "submit"])
        self.assertEqual(events[1]["duration_ms"], 1.5)
        self.assertLess(events[0]["t_ms"], events[1]["t_ms"] + 1)

    def test_only_allowlisted_validated_values_are_recorded(self) -> None:
        trace = LatencyTrace(self.root / "d")
        trace.emit("submit", body=SECRET_BODY, path="/etc/passwd", outcome="Bad Free Text: secret",
                   turn_id="msg_roscNOTHEX", rosc_session_id="not-a-uuid", duration_ms=float("nan"),
                   chars=-1, pages=True, tool="../x", state="exploded", phase="elsewhere", project_id="1",
                   correlation="maybe")
        trace.emit("not_an_event", outcome="ok")
        trace.emit("probe", outcome="ok", duration_ms=2, chars=7, project_id=3, tool="briefing",
                   turn_id="msg_rosc" + "a" * 32, rosc_session_id="12345678-1234-1234-1234-123456789abc")
        text = (self.root / "d" / TRACE_FILENAME).read_text()
        for leaked in (SECRET_BODY, "passwd", "Bad Free", "NOTHEX", "not-a-uuid", "exploded", "elsewhere", "maybe"):
            self.assertNotIn(leaked, text)
        events = read_events(self.root / "d" / TRACE_FILENAME)
        self.assertEqual(len(events), 3)  # trace_started, submit, probe; unknown event ignored
        self.assertEqual(set(events[1]) - HEAD_KEYS, {"outcome"})  # every other field was dropped
        self.assertEqual(events[1]["outcome"], "other_error")  # free text is projected, never stored
        self.assertEqual(set(events[2]) - HEAD_KEYS - set(FIELDS), set())
        self.assertEqual(events[2]["chars"], 7)

    def test_lowercase_snake_case_secrets_are_not_stored_as_outcomes(self) -> None:
        trace = LatencyTrace(self.root / "d")
        for value in ("privateapikey", "secret_value_123", "token_like_value", "ok_but_secret", "http_999x", "HTTP_500",
                      "http_200_secret", "", " ok", "ok\n", "a" * 49):
            trace.emit("probe", outcome=value, duration_ms=1)
        trace.emit("probe", outcome=b"ok", duration_ms=1)  # not a string: the field is dropped, never coerced
        text = (self.root / "d" / TRACE_FILENAME).read_text()
        for leaked in ("privateapikey", "secret_value", "token_like", "ok_but", "http_999", "HTTP_500", "http_200", "aaaa"):
            self.assertNotIn(leaked, text)
        probes = [e for e in read_events(self.root / "d" / TRACE_FILENAME) if e["event"] == "probe"]
        self.assertEqual([e.get("outcome") for e in probes], ["other_error"] * 11 + [None])

    def test_known_safe_outcomes_stay_distinguishable(self) -> None:
        trace = LatencyTrace(self.root / "d")
        known = ["ok", "idle", "reply", "scheduled", "delivered", "failed", "unknown", "timeout", "connection_lost",
                 "lookup_failed", "provider_error", "not_received", "interrupted", "in_progress", "not_committed",
                 "rules_snapshot_unavailable", "session_changed", "invalid_argument", "revision_conflict",
                 "source_other_project_denied"]
        for value in known:
            trace.emit("probe", outcome=value)
        trace.emit("probe", outcome="http_404")
        trace.emit("probe", outcome="http_503")
        got = [e["outcome"] for e in read_events(self.root / "d" / TRACE_FILENAME) if e["event"] == "probe"]
        self.assertEqual(got, known + ["http_4xx", "http_5xx"])
        self.assertNotIn(OUTCOME_OTHER, got)

    def test_every_outcome_the_emitters_can_produce_is_in_the_finite_set(self) -> None:
        """Static cross-check: each code literal ROSC raises/stores as an error code must be a member, so a legitimate
        diagnostic is never silently reported as ``other_error``."""
        import re
        import rosc.architect_service as service_module
        import rosc.project_context as context_module
        sources = [Path(runtime_module.__file__).read_text(), Path(service_module.__file__).read_text(),
                   Path(context_module.__file__).read_text()]
        codes = set()
        for text in sources:
            codes |= set(re.findall(r"Runtime[A-Za-z]+\(\"([a-z0-9_]+)\"", text))
            codes |= set(re.findall(r"ToolError\(\"([a-z0-9_]+)\"", text))
            codes |= set(re.findall(r"_set\(message_id, DELIVERY_[A-Z]+, \"([a-z0-9_]+)\"", text))
            codes |= set(re.findall(r"\"code\": \"([a-z0-9_]+)\"", text))
        codes |= {"assignment_changed", "session_not_ready", "runtime_failure"}
        self.assertGreater(len(codes), 40)
        self.assertEqual(sorted(codes - OUTCOMES), [])

    def test_the_tool_field_only_ever_carries_dispatch_allowlisted_names(self) -> None:
        trace = LatencyTrace(self.root / "d")
        calls = []
        host = ToolHost(lambda session, tool, args: calls.append(tool) or {"ok": True}, trace)
        session = "11111111-1111-1111-1111-111111111111"
        for name in ("privateapikey", "../etc/passwd", "briefing_x", "BRIEFING", "", "secret_value_123"):
            reply = host.handle(session, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": name, "arguments": {}}})
            self.assertEqual(reply["error"]["message"], "Unknown tool")  # rejected before dispatch and before tracing
        self.assertEqual(calls, [])
        host.handle(session, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "briefing", "arguments": {}}})
        tools = [e.get("tool") for e in read_events(self.root / "d" / TRACE_FILENAME) if e["event"] == "tool_call"]
        self.assertEqual(tools, ["briefing"])

    def test_tool_error_codes_and_unrecognised_codes_are_projected(self) -> None:
        trace = LatencyTrace(self.root / "d")
        outcomes = iter([{"ok": False, "error": {"code": "not_found"}},
                         {"ok": False, "error": {"code": "secret_value_123"}},
                         {"ok": False, "error": {"code": 5}}, {"ok": False}])
        host = ToolHost(lambda session, tool, args: next(outcomes), trace)
        for _ in range(4):
            host.handle("11111111-1111-1111-1111-111111111111", {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "briefing", "arguments": {}}})
        got = [e["outcome"] for e in read_events(self.root / "d" / TRACE_FILENAME) if e["event"] == "tool_call"]
        self.assertEqual(got, ["not_found", "other_error", "error", "error"])
        self.assertNotIn("secret_value", (self.root / "d" / TRACE_FILENAME).read_text())

    def test_unsafe_destinations_are_refused_without_raising(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "link").symlink_to(outside)
        loose = self.root / "loose"
        loose.mkdir(mode=0o755)
        loose.chmod(0o755)
        blocked = self.root / "blocked"
        blocked.write_text("a regular file where a directory is needed")
        fileonly = self.root / "fd"
        fileonly.mkdir(mode=0o700)
        (fileonly / TRACE_FILENAME).symlink_to(outside / "target")
        wide = self.root / "wide"
        wide.mkdir(mode=0o700)
        (wide / TRACE_FILENAME).write_text("")
        (wide / TRACE_FILENAME).chmod(0o644)
        for target in (self.root / "link", loose, blocked / "sub", fileonly, wide, self.root / "missing" / "deep"):
            with self.subTest(target=target.name):
                trace = LatencyTrace(target)
                trace.emit("submit", outcome="scheduled")  # must not raise
                self.assertGreater(trace.dropped, 0)
        self.assertEqual(list(outside.iterdir()), [])  # nothing was written through any symlink
        self.assertEqual((wide / TRACE_FILENAME).read_text(), "")

    def test_failure_log_is_content_free_and_single(self) -> None:
        secret_dir = self.root / "SYNTH-PRIVATE-DIR-NAME"
        (self.root / "SYNTH-PRIVATE-DIR-NAME").write_text("file")
        with self.assertLogs("rosc.architect_latency", level="WARNING") as logs:
            trace = LatencyTrace(secret_dir / "d")
            for _ in range(5):
                trace.emit("submit", outcome="scheduled")
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("SYNTH-PRIVATE", logs.output[0])

    def test_size_cap_stops_without_rotation_or_deletion(self) -> None:
        trace = LatencyTrace(self.root / "d", max_bytes=600)
        for _ in range(50):
            trace.emit("submit", outcome="scheduled", duration_ms=1)
        path = self.root / "d" / TRACE_FILENAME
        self.assertLessEqual(path.stat().st_size, 600)
        self.assertGreater(trace.dropped, 0)
        self.assertEqual(sorted(p.name for p in (self.root / "d").iterdir()), [TRACE_FILENAME])
        for event in read_events(path):  # no truncated record
            self.assertIn("event", event)

    def test_concurrent_writers_produce_whole_unique_records(self) -> None:
        trace = LatencyTrace(self.root / "d")

        def work(index: int) -> None:
            with trace.scope(project_id=index):
                for _ in range(100):
                    trace.emit("probe", duration_ms=1, outcome="ok")

        threads = [threading.Thread(target=work, args=(i,)) for i in range(1, 7)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        events = read_events(self.root / "d" / TRACE_FILENAME)
        self.assertEqual(len(events), 601)
        self.assertEqual(len({e["seq"] for e in events}), 601)
        per_project = {}
        for e in events[1:]:
            per_project[e["project_id"]] = per_project.get(e["project_id"], 0) + 1
        self.assertEqual(per_project, {i: 100 for i in range(1, 7)})  # scopes never bleed between threads

    def test_tool_correlation_is_per_session_and_never_invented(self) -> None:
        trace = LatencyTrace(self.root / "d")
        host = ToolHost(lambda session, tool, args: {"ok": True, "result": {}}, trace)
        sess_a, sess_b, sess_c = ("11111111-1111-1111-1111-11111111111%d" % i for i in (1, 2, 3))
        turn_a, turn_b = "msg_rosc" + "a" * 32, "msg_rosc" + "b" * 32
        trace.begin_turn(sess_a, turn_a, 1)
        trace.begin_turn(sess_b, turn_b, 2)
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "briefing", "arguments": {}}}
        threads = [threading.Thread(target=host.handle, args=(s, call)) for s in (sess_a, sess_b, sess_c) * 10]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        trace.end_turn(sess_b, turn_b)
        host.handle(sess_b, call)  # after the turn ended: unverified
        by_session: dict = {}
        for e in read_events(self.root / "d" / TRACE_FILENAME):
            if e["event"] == "tool_call":
                by_session.setdefault(e["rosc_session_id"], []).append(e)
        self.assertEqual({(e["correlation"], e.get("turn_id"), e.get("project_id")) for e in by_session[sess_a]},
                         {("verified", turn_a, 1)})
        self.assertEqual({(e["correlation"], e.get("turn_id")) for e in by_session[sess_c]}, {("unverified", None)})
        self.assertEqual([(e["correlation"], e.get("turn_id")) for e in by_session[sess_b]][-1], ("unverified", None))
        self.assertEqual({(e["correlation"], e.get("turn_id")) for e in by_session[sess_b][:-1]},
                         {("verified", turn_b)})
        self.assertTrue(all(e["tool"] == "briefing" and e["outcome"] == "ok" for v in by_session.values() for e in v))


class RuntimeTraceTests(p22.AdapterCase):
    def setUp(self) -> None:
        super().setUp()
        self.out = Path(self.tmp.name) / "diag"
        self.diag = LatencyTrace(self.out)
        self.runtime.attach_diagnostics(self.diag)

    def events(self, name: str | None = None) -> list[dict]:
        found = read_events(self.out / TRACE_FILENAME)
        return [e for e in found if name is None or e["event"] == name]

    def test_ask_records_admission_wait_and_lookup_separately(self) -> None:
        (self.bin / "mode").write_text("ok")
        import uuid
        session_id = str(uuid.uuid4())
        handle = self.runtime.start(session_id, "prov", "model-a")
        self.started.append((session_id, handle))
        self.assertEqual(self.runtime.ask(handle, "msg_rosc_a", "SYNTH-PROMPT-TEXT", 5), "Fake architect reply")
        names = [e["event"] for e in self.events()]
        self.assertEqual(names, ["trace_started", "prompt_admitted", "wait_returned", "lookup"])
        wait = self.events("wait_returned")[0]
        self.assertEqual((wait["outcome"], wait["timeout_ms"]), ("idle", 5000.0))
        lookup = self.events("lookup")[0]
        self.assertEqual((lookup["outcome"], lookup["requests"], lookup["fallbacks"]), ("reply", 1, 0))
        self.assertGreater(lookup["bytes"], 0)
        self.assertNotIn("SYNTH-PROMPT-TEXT", (self.out / TRACE_FILENAME).read_text())

    def test_large_history_counts_pages_messages_and_fallbacks(self) -> None:
        (self.bin / "mode").write_text("long_match")
        import uuid
        session_id = str(uuid.uuid4())
        handle = self.runtime.start(session_id, "prov", "model-a")
        self.started.append((session_id, handle))
        self.assertEqual(self.runtime.ask(handle, "msg_rosc_long", "hi", 10), "Fake architect reply")
        lookup = self.events("lookup")[-1]
        self.assertGreaterEqual(lookup["messages"], 450)
        self.assertGreaterEqual(lookup["requests"], 3)
        self.assertEqual(lookup["fallbacks"], 0)
        real_page = self.runtime._page

        def too_large(handle_, query, max_bytes=runtime_module.MAX_RESPONSE_BYTES):
            if max_bytes is not None:  # the capped control call "does not fit"; one-message reads are uncapped
                raise runtime_module.RuntimeRefused("response_too_large")
            return real_page(handle_, query, max_bytes=max_bytes)

        with patch.object(self.runtime, "_page", too_large):
            self.assertEqual(self.runtime.lookup(handle, "msg_rosc_long").status, "reply")
        fallback = self.events("lookup")[-1]
        self.assertGreater(fallback["fallbacks"], 0)
        self.assertGreater(fallback["requests"], lookup["requests"])

    def test_timeout_is_a_wait_outcome_and_lookup_status_is_never_a_model_claim(self) -> None:
        _sid, handle = self.trace(intermediate=2, terminal="none")
        with self.assertRaises(runtime_module.RuntimeAmbiguous):
            self.runtime.ask(handle, "msg_rosc_t", "hi", 5)
        self.assertEqual(self.events("lookup")[-1]["outcome"], "in_progress")
        self.assertEqual(self.events("wait_returned")[-1]["outcome"], "idle")

    def test_refused_prompt_and_errors_use_closed_codes_not_exception_text(self) -> None:
        _sid, handle = self.trace()
        with patch.object(self.runtime, "_call", side_effect=runtime_module.RuntimeRefused("http_500")):
            with self.assertRaises(runtime_module.RuntimeRefused):
                self.runtime.ask(handle, "msg_rosc_r", "hi", 5)
        with patch.object(self.runtime, "_call", side_effect=ValueError("SYNTH-RAW-EXCEPTION-TEXT /secret/path")):
            with self.assertRaises(ValueError):
                self.runtime.ask(handle, "msg_rosc_r", "hi", 5)
        outcomes = [e["outcome"] for e in self.events("prompt_admitted")]
        self.assertEqual(outcomes, ["http_5xx", "exception"])  # a dynamic HTTP status is reduced to its class
        self.assertNotIn("SYNTH-RAW", (self.out / TRACE_FILENAME).read_text())

    def test_cancellation_never_emits_a_finished_lookup(self) -> None:
        class Cancel(BaseException):
            pass

        _sid, handle = self.trace()
        with patch.object(self.runtime, "_page", side_effect=Cancel()):
            with self.assertRaises(Cancel):
                self.runtime.lookup(handle, "msg_rosc_x")
        self.assertEqual(self.events("lookup")[-1]["outcome"], "interrupted")

    def test_disabled_runtime_writes_nothing_and_behaves_identically(self) -> None:
        plain = OpenCodeRuntime(Path(self.tmp.name) / "plain", self.runtime._environ)
        (self.bin / "mode").write_text("ok")
        import uuid
        session_id = str(uuid.uuid4())
        handle = plain.start(session_id, "prov", "model-a")
        try:
            self.assertEqual(plain.ask(handle, "msg_rosc_plain", "hi", 5), "Fake architect reply")
        finally:
            plain.stop(session_id, handle)
        self.assertFalse((Path(self.tmp.name) / "plain" / "diagnostics").exists())


class TracedEndToEnd(p22.EndToEndMultiStepTests):
    """All P022 end-to-end tests again, with tracing enabled, plus the P024 assertions."""

    def restart(self, first: bool = False) -> None:
        if first:
            self.trace = LatencyTrace(self.data / "diagnostics")
        super().restart(first)

    def events(self, name: str | None = None) -> list[dict]:
        found = read_events(self.trace.path)
        return [e for e in found if name is None or e["event"] == name]

    def setUp(self) -> None:
        # ContextCase.restart builds the app itself; wrap create_app so the traced instance is always injected
        self._patch = patch("test_p019_project_context_access.create_app", lambda *a, **k: create_app(
            *a, latency_trace=self.trace, **k))
        self._patch.start()
        self.addCleanup(self._patch.stop)
        super().setUp()

    def turn_events(self, turn_id: str) -> list[dict]:
        return [e for e in self.events() if e.get("turn_id") == turn_id]

    def test_p024_one_tool_turn_has_every_phase_in_order_with_verified_tool_correlation(self) -> None:
        out = self.multi([{"tool": "briefing"}], "p024-a-0001-xx", intermediate=1)
        self.assertEqual(out["owner"]["delivery"]["state"], "delivered")
        turn = runtime_message_id(out["owner"]["message_id"])
        names = [e["event"] for e in self.turn_events(turn)]
        expected = ["owner_commit", "submit", "worker_started", "turn_lock_acquired", "probe", "context_built",
                    "prepare", "tool_call", "prompt_admitted", "wait_returned", "lookup", "ask_finished", "commit",
                    "delivery_state", "turn_finished"]
        self.assertEqual(sorted(names), sorted(expected), names)
        # the scripted tool call happens while the fake handles /prompt, i.e. before prompt_admitted returns
        self.assertLess(names.index("prepare"), names.index("prompt_admitted"))
        self.assertLess(names.index("wait_returned"), names.index("lookup"))
        self.assertLess(names.index("lookup"), names.index("ask_finished"))
        self.assertLess(names.index("ask_finished"), names.index("commit"))
        self.assertEqual(names[-1], "turn_finished")
        events = self.turn_events(turn)
        self.assertEqual([e["seq"] for e in events], sorted(e["seq"] for e in events))
        tool = next(e for e in events if e["event"] == "tool_call")
        self.assertEqual((tool["tool"], tool["outcome"], tool["correlation"]), ("briefing", "ok", "verified"))
        ctx = next(e for e in events if e["event"] == "context_built")
        self.assertEqual(ctx["total_chars"], ctx["identity_chars"] + ctx["rules_chars"] + ctx["tool_guidance_chars"]
                         + ctx["recap_chars"] + ctx["framing_chars"])
        self.assertEqual(ctx["rules_chars"], 0)  # P027: default composition has no RulesSnapshot block
        self.assertEqual(ctx["recap_messages"], 1)
        for e in events:
            for key in ("duration_ms", "t_ms"):
                if key in e:
                    self.assertTrue(0 <= e[key] < 1e9)
        final = events[-1]
        self.assertEqual((final["state"], final["outcome"]), ("delivered", "delivered"))
        self.assertEqual(self.prompts(), 1)

    def test_p024_summary_reports_complete_and_incomplete_turns_honestly(self) -> None:
        out = self.multi([{"tool": "briefing"}], "p024-s-0001-xx", intermediate=1)
        turn = runtime_message_id(out["owner"]["message_id"])
        with self.trace.path.open("a", encoding="ascii") as handle:  # a crashed turn: started, never finished
            ghost = "msg_rosc" + "c" * 32
            handle.write(json.dumps({"schema_version": 1, "event": "submit", "origin": "dead", "seq": 1,
                                     "at_utc": "2026-01-01T00:00:00.000Z", "t_ms": 1.0, "turn_id": ghost,
                                     "outcome": "scheduled"}) + "\n" + '{"event": "tru')  # and a torn last line
        result = summary.summarize(summary.load(self.trace.path))
        rows = {r["turn_id"]: r for r in result["rows"]}
        self.assertTrue(rows[turn]["complete"])
        self.assertEqual((rows[turn]["final_state"], rows[turn]["tools"], rows[turn]["tools_verified"]), ("delivered", 1, 1))
        self.assertGreaterEqual(rows[turn]["total_ms"], rows[turn]["ask_ms"])
        self.assertFalse(rows[ghost]["complete"])
        self.assertEqual(result["incomplete"], 1)
        self.assertIn("time to first token", result["not_measured"])
        self.assertIn("incomplete", summary.render(result))

    def test_p024_two_tools_are_both_recorded_for_the_one_turn(self) -> None:
        out = self.multi([{"tool": "briefing"}, {"tool": "global_read", "args": {"path": SECRET_PATH}}],
                         "p024-b-0001-xx", intermediate=2)
        turn = runtime_message_id(out["owner"]["message_id"])
        tools = [e for e in self.turn_events(turn) if e["event"] == "tool_call"]
        self.assertEqual([(e["tool"], e["correlation"]) for e in tools],
                         [("briefing", "verified"), ("global_read", "verified")])
        self.assertEqual(len(self.audit()), 2)  # canonical Tool audit is untouched and still the only record of targets

    def test_p024_provider_error_is_recorded_as_failed_with_no_resend(self) -> None:
        out = self.multi([{"tool": "briefing"}], "p024-c-0001-xx", intermediate=2, terminal="error")
        turn = runtime_message_id(out["owner"]["message_id"])
        events = self.turn_events(turn)
        self.assertEqual(next(e for e in events if e["event"] == "ask_finished")["outcome"], "provider_error")
        self.assertEqual(next(e for e in events if e["event"] == "delivery_state")["state"], "failed")
        self.assertEqual(events[-1]["event"], "turn_finished")
        self.assertNotIn("commit", [e["event"] for e in events])
        self.assertEqual(self.prompts(), 1)

    def test_p024_timeout_unknown_then_reconcile_delivers_without_resend_or_tool(self) -> None:
        out = self.multi([{"tool": "briefing"}], "p024-d-0001-xx", intermediate=2, late_final=True)
        owner = out["owner"]
        turn = runtime_message_id(owner["message_id"])
        self.assertEqual(owner["delivery"]["error_code"], "timeout")
        first = self.turn_events(turn)
        self.assertEqual(next(e for e in first if e["event"] == "delivery_state")["outcome"], "timeout")
        self.assertEqual(next(e for e in first if e["event"] == "ask_finished")["outcome"], "timeout")
        url = f"/api/projects/P0001/conversation/messages/{owner['message_id']}"
        tools_before = len([e for e in self.events("tool_call")])
        self.client.post(url + "/reconcile")  # still unfinished
        (self.bin / "release").write_text("go")
        self.assertEqual(self.client.post(url + "/reconcile").json()["delivery"]["state"], "delivered")
        finished = [e for e in self.events("reconcile_finished") if e["turn_id"] == turn]
        self.assertEqual([e["outcome"] for e in finished], ["unknown", "delivered"])
        recon = [e for e in self.turn_events(turn) if e.get("phase") == "reconcile"]
        self.assertIn("lookup", [e["event"] for e in recon])
        self.assertIn("commit", [e["event"] for e in recon])
        self.assertEqual(len(self.events("tool_call")), tools_before)  # reconcile executed no Tool
        self.assertEqual(len([e for e in self.events("prompt_admitted")]), 1)  # and issued no second prompt
        self.assertEqual(self.prompts(), 1)

    def test_p024_no_private_content_reaches_the_trace(self) -> None:
        (self.bin / "reply.json").write_text(json.dumps({"parts": [p22.FINAL], "before": [], "intermediate": 1}))
        (self.bin / "mode").write_text("file_reply")
        self.client.post("/api/projects/P0001/architect/deactivate")
        self.activate()
        (self.bin / "tool_calls.json").write_text(json.dumps([
            {"tool": "global_read", "args": {"path": SECRET_PATH}}, {"tool": "docs_search", "args": {"query": SECRET_BODY}}]))
        sent = self.client.post("/api/projects/P0001/conversation/messages",
                                json={"client_key": "p024-priv-0001", "body": SECRET_BODY + " " + str(self.base)})
        self.assertEqual(sent.status_code, 201)
        self.architect.drain()
        text = self.trace.path.read_text()
        for private in (SECRET_BODY, SECRET_PATH, "onboarding", p22.FINAL, "Alpha", str(self.base), "SYNTH-", "tmp",
                        "Authorization", "Bearer", "http://", "/"):  # no path separator can occur in any record
            self.assertNotIn(private, text, private)
        allowed = HEAD_KEYS | set(FIELDS)
        for event in read_events(self.trace.path):
            self.assertLessEqual(set(event), allowed, event)

    def test_p024_unwritable_destination_never_changes_delivery_truth(self) -> None:
        blocked = self.base / "blocked-file"
        blocked.write_text("not a directory")
        bad = LatencyTrace(blocked / "diag")
        self.host._trace = self.architect._trace = self.runtime._diag = bad
        out = self.multi([{"tool": "briefing"}], "p024-e-0001-xx", intermediate=1)
        self.assertEqual(out["owner"]["delivery"]["state"], "delivered")
        self.assertEqual([m["body"] for m in out["messages"] if m["sender"]["kind"] == "architect"], [p22.FINAL])
        self.assertGreater(bad.dropped, 0)
        self.assertEqual(self.prompts(), 1)

    def test_p024_interrupted_turn_has_no_fictitious_finish(self) -> None:
        class Cancel(BaseException):
            pass

        (self.bin / "mode").write_text("ok")
        self.client.post("/api/projects/P0001/architect/deactivate")
        self.activate()
        with patch.object(self.architect._runtime, "ask", side_effect=Cancel()):
            sent = self.client.post("/api/projects/P0001/conversation/messages",
                                    json={"client_key": "p024-int-0001", "body": "go"})
            self.assertEqual(sent.status_code, 201)
            with self.assertRaises(Cancel):
                self.architect.drain()
        turn = runtime_message_id(sent.json()["message"]["message_id"])
        names = [e["event"] for e in self.turn_events(turn)]
        self.assertNotIn("ask_finished", names)
        self.assertNotIn("commit", names)
        self.assertNotIn("delivery_state", names)
        last = self.turn_events(turn)[-1]
        self.assertEqual((last["event"], last["outcome"]), ("turn_finished", "interrupted"))
        self.architect._futures.clear()


class DisabledEndToEnd(p22.ContextCase):
    def test_default_app_writes_no_diagnostics_and_keeps_response_shape(self) -> None:
        self.activate()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_TRACE, None)
            self.assertFalse(self.architect.trace.enabled)
        sent = self.client.post("/api/projects/P0001/conversation/messages",
                                json={"client_key": "p024-off-0001", "body": "hello"})
        self.assertEqual(sent.status_code, 201)
        self.architect.drain()
        body = sent.json()["message"]
        self.assertEqual(set(body["delivery"]), {"state", "error_code", "reply_message_id", "can_retry", "can_reconcile"})
        self.assertFalse((self.data / "diagnostics").exists())
        self.assertEqual(self.architect._clocks, {})


if __name__ == "__main__":
    unittest.main()
