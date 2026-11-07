"""P022: the final Architect reply is found after one or more intermediate ``finish=tool-calls`` assistant steps.

Everything is offline and synthetic: the REAL ``OpenCodeRuntime`` (and, for the end-to-end class, the real Broker +
ToolHost) talks to the test-only fake ``opencode serve``.  The fake's multi-step traces are *synthetic test-only
provider traces*, not recordings of a live provider.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from test_p019_project_context_access import ContextCase
import rosc.architect_runtime as runtime_module
from rosc.architect_runtime import (DENY_ALL, ENV_APPROVED_MODEL, ENV_BINARY, TOOLS_PERMISSIONS, OpenCodeRuntime,
                                    ReplyLookup, RuntimeAmbiguous, RuntimeRefused)
from rosc.architect_tools import TOOL_NAMES
from rosc.conversation import SAFE_RETRY_CODES

TESTS = Path(__file__).resolve().parent
FINAL = "FINAL answer for the Owner 😀"


class AdapterCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        launcher = self.bin / "opencode"
        launcher.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(TESTS)!r})\n"
                            "import fake_opencode\nfake_opencode.main()\n")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp.name, ENV_BINARY: str(launcher),
               ENV_APPROVED_MODEL: "prov/model-a"}
        self.runtime = OpenCodeRuntime(root / "data", env)
        self.started: list[tuple[str, object]] = []

    def tearDown(self) -> None:
        for session_id, handle in self.started:
            self.runtime.stop(session_id, handle)

    def trace(self, **reply) -> tuple[str, object]:
        """Start a fake session replaying the synthetic trace ``reply`` (see ``fake_opencode``) for the next prompt."""
        reply.setdefault("parts", [FINAL])
        reply.setdefault("before", ["PRIOR TURN ANSWER"])
        (self.bin / "reply.json").write_text(json.dumps(reply), encoding="utf-8")
        (self.bin / "mode").write_text("file_reply")
        session_id = str(uuid.uuid4())
        handle = self.runtime.start(session_id, "prov", "model-a")
        self.started.append((session_id, handle))
        return session_id, handle

    def send(self, handle, message_id: str = "msg_rosc_p022") -> None:
        """What ``ask`` does before waiting: deliver the prompt (the fake answers synchronously)."""
        self.runtime._call(handle.rosc_session_id, handle.port, "POST", f"/api/session/{handle.runtime_session_id}/prompt",
                           {"id": message_id, "text": "hi"})


class MultiStepLookupTests(AdapterCase):
    def test_a_single_step_reply_is_unchanged_and_replays_identically(self) -> None:
        (self.bin / "mode").write_text("ok")
        session_id = str(uuid.uuid4())
        handle = self.runtime.start(session_id, "prov", "model-a")
        self.started.append((session_id, handle))
        self.assertEqual(self.runtime.ask(handle, "msg_rosc_a", "hi", 5), "Fake architect reply")
        expected = ReplyLookup("reply", "Fake architect reply")
        self.assertEqual([self.runtime.lookup(handle, "msg_rosc_a") for _ in range(2)], [expected, expected])

    def test_b_c_intermediate_tool_call_steps_are_skipped_and_only_the_final_text_is_returned(self) -> None:
        for steps in (1, 2, 5):
            with self.subTest(steps=steps):
                _sid, handle = self.trace(intermediate=steps)
                answer = self.runtime.ask(handle, "msg_rosc_b", "hi", 5)
                self.assertEqual(answer, FINAL)
                self.assertNotIn("INTERMEDIATE", answer)
                self.assertNotIn("PRIOR TURN", answer)
                self.assertEqual([self.runtime.lookup(handle, "msg_rosc_b") for _ in range(3)],
                                 [ReplyLookup("reply", FINAL)] * 3)  # deterministic replay

    def test_d_a_real_failure_after_successful_tool_steps_is_never_delivered(self) -> None:
        for terminal in ("error", "stop_error", "length", "content-filter", "unknown", "something-new"):
            with self.subTest(terminal=terminal):
                _sid, handle = self.trace(intermediate=2, terminal=terminal)
                with self.assertRaises(RuntimeRefused) as caught:
                    self.runtime.ask(handle, "msg_rosc_d", "hi", 5)
                self.assertEqual(caught.exception.code, "provider_error")
                with self.assertRaises(RuntimeRefused):
                    self.runtime.lookup(handle, "msg_rosc_d")

    def test_d_an_error_on_an_intermediate_step_itself_is_a_failure(self) -> None:
        _sid, handle = self.trace(intermediate=1)
        self.send(handle)
        self.runtime._call(handle.rosc_session_id, handle.port, "GET", "/api/info")  # server alive
        with patch.object(self.runtime, "_page", return_value=self.page([
                {"id": "u1", "type": "user"},
                {"id": "s1", "type": "assistant", "finish": "tool-calls", "error": {"name": "x"}, "time": {"completed": 1}},
                {"id": "s2", "type": "assistant", "finish": "stop", "time": {"completed": 1},
                 "content": [{"type": "text", "text": "late"}]}])):
            with self.assertRaises(RuntimeRefused) as caught:
                self.runtime.lookup(handle, "u1")
        self.assertEqual(caught.exception.code, "provider_error")

    def test_e_a_chain_without_a_final_step_is_in_progress_never_a_reply_or_absent(self) -> None:
        for reply in ({"intermediate": 2, "terminal": "none"}, {"intermediate": 0, "terminal": "incomplete"},
                      {"intermediate": 2, "terminal": "incomplete"}):
            with self.subTest(reply=reply):
                _sid, handle = self.trace(**reply)
                with self.assertRaises(RuntimeAmbiguous) as caught:
                    self.runtime.ask(handle, "msg_rosc_e", "hi", 5)
                self.assertEqual(caught.exception.code, "timeout")
                self.assertEqual(self.runtime.lookup(handle, "msg_rosc_e"), ReplyLookup("in_progress"))

    def test_e_empty_or_invalid_final_content_is_refused(self) -> None:
        _sid, handle = self.trace(intermediate=1, parts=[])
        with self.assertRaises(RuntimeRefused) as caught:
            self.runtime.ask(handle, "msg_rosc_e2", "hi", 5)
        self.assertEqual(caught.exception.code, "empty_reply")
        _sid, handle = self.trace(intermediate=1, parts=["   \n"])
        with self.assertRaises(RuntimeRefused) as caught:
            self.runtime.ask(handle, "msg_rosc_e3", "hi", 5)
        self.assertEqual(caught.exception.code, "empty_reply")
        self.send(handle, "u_inv")
        with patch.object(self.runtime, "_page", return_value=self.page([
                {"id": "u1", "type": "user"},
                {"id": "s1", "type": "assistant", "finish": "tool-calls", "time": {"completed": 1}, "content": []},
                {"id": "s2", "type": "assistant", "finish": "stop", "time": {"completed": 1}, "content": "text"}])):
            with self.assertRaises(RuntimeRefused) as caught:
                self.runtime.lookup(handle, "u1")
        self.assertEqual(caught.exception.code, "malformed_response")

    def test_f_a_later_turn_is_never_used_as_this_turns_answer(self) -> None:
        _sid, handle = self.trace(intermediate=1, then_user=True)
        self.send(handle)
        self.assertEqual(self.runtime.lookup(handle, "msg_rosc_p022"), ReplyLookup("reply", FINAL))
        _sid, handle = self.trace(intermediate=2, terminal="none", then_user=True)
        self.send(handle)
        self.assertEqual(self.runtime.lookup(handle, "msg_rosc_p022"), ReplyLookup("uncertain"))  # not "LATER TURN ANSWER"

    @staticmethod
    def page(items: list[dict], nxt: str | None = None) -> dict:
        return {"data": items, "cursor": {"previous": None, "next": nxt}}

    def test_e_stream_shape_violations_are_uncertain_not_replies(self) -> None:
        _sid, handle = self.trace()
        step = {"id": "s1", "type": "assistant", "finish": "tool-calls", "time": {"completed": 1}, "content": []}
        user = {"id": "u1", "type": "user"}
        cases = {
            "duplicate user": [user, step, user],
            "next user boundary": [user, step, {"id": "u2", "type": "user"},
                                   {"id": "a9", "type": "assistant", "finish": "stop", "time": {"completed": 1},
                                    "content": [{"type": "text", "text": "later"}]}],
            "id of assistant type": [{"id": "u1", "type": "assistant", "finish": "stop", "time": {"completed": 1},
                                      "content": [{"type": "text", "text": "x"}]}],
        }
        for name, items in cases.items():
            with self.subTest(name), patch.object(self.runtime, "_page", return_value=self.page(items)):
                self.assertEqual(self.runtime.lookup(handle, "u1"), ReplyLookup("uncertain"))
        with patch.object(self.runtime, "_page", return_value=self.page([user, step])):
            self.assertEqual(self.runtime.lookup(handle, "u1"), ReplyLookup("in_progress"))
        with patch.object(self.runtime, "_page", return_value={"data": "x", "cursor": {}}):
            with self.assertRaises(RuntimeRefused):
                self.runtime.lookup(handle, "u1")

    def test_e_cursor_loops_and_page_limits_never_claim_a_reply(self) -> None:
        _sid, handle = self.trace()
        user, step = {"id": "u1", "type": "user"}, {"id": "s1", "type": "assistant", "finish": "tool-calls",
                                                    "time": {"completed": 1}, "content": []}
        with patch.object(self.runtime, "_page", return_value=self.page([user, step], nxt="same")):
            self.assertEqual(self.runtime.lookup(handle, "u1"), ReplyLookup("uncertain"))  # cursor loop
        counter = iter(range(10_000))
        with patch.object(self.runtime, "_page", side_effect=lambda *a, **k: self.page(
                [step | {"id": f"s{next(counter)}"}], nxt=f"c{next(counter)}")):
            with patch.object(runtime_module, "MAX_LOOKUP_PAGES", 3):
                # the user turn is never even seen within the bound: absence must not be claimed
                self.assertEqual(self.runtime.lookup(handle, "u1"), ReplyLookup("uncertain"))
        with patch.object(self.runtime, "_page", return_value=self.page([user, step, step | {"id": "s2"}])):
            with patch.object(runtime_module, "MAX_LOOKUP_MESSAGES", 2):
                self.assertEqual(self.runtime.lookup(handle, "u1"), ReplyLookup("uncertain"))

    def test_h_tool_surface_and_permissions_are_unchanged(self) -> None:
        self.assertEqual(set(TOOL_NAMES), {"briefing", "global_list", "global_read", "docs_list", "docs_search",
                                           "docs_read", "docs_create", "docs_update", "docs_delete", "docs_restore",
                                           "source_list", "source_read", "source_search", "source_git"})
        self.assertEqual(TOOLS_PERMISSIONS, DENY_ALL + [{"action": "rosc_*", "resource": "*", "effect": "allow"}])
        self.assertNotIn("provider_error", SAFE_RETRY_CODES)


class EndToEndMultiStepTests(ContextCase):
    """Real adapter + real Broker/ToolHost; the fake performs the scripted ``rosc_*`` calls, then the synthetic
    multi-step trace.  Assertions are on the stored Conversation, delivery state and Tool audit."""

    def multi(self, calls: list[dict], key: str, **trace) -> dict:
        trace.setdefault("parts", [FINAL])
        trace.setdefault("before", [])
        (self.bin / "reply.json").write_text(json.dumps(trace), encoding="utf-8")
        (self.bin / "mode").write_text("file_reply")
        self.client.post("/api/projects/P0001/architect/deactivate")
        self.activate()  # the fake builds its synthetic history when the session is created
        (self.bin / "tool_log.jsonl").unlink(missing_ok=True)
        (self.bin / "prompts.log").unlink(missing_ok=True)
        (self.bin / "tool_calls.json").write_text(json.dumps(calls))
        sent = self.client.post("/api/projects/P0001/conversation/messages", json={"client_key": key, "body": "go"})
        self.assertEqual(sent.status_code, 201, sent.text)
        self.architect.drain()
        (self.bin / "tool_calls.json").unlink()
        messages = self.client.get("/api/projects/P0001/conversation").json()["messages"]
        return {"owner": next(m for m in messages if m["message_id"] == sent.json()["message"]["message_id"]),
                "messages": messages}

    def prompts(self) -> int:
        path = self.bin / "prompts.log"
        return len(path.read_text().split()) if path.exists() else 0

    def test_b_one_tool_then_final_text_is_delivered_with_a_real_toolhost(self) -> None:
        out = self.multi([{"tool": "briefing"}], "multi-b-0001-xx", intermediate=1)
        self.assertEqual(out["owner"]["delivery"]["state"], "delivered", out["owner"]["delivery"])
        self.assertEqual([m["body"] for m in out["messages"] if m["sender"]["kind"] == "architect"], [FINAL])
        self.assertEqual([(r.tool, r.outcome) for r in self.audit()], [("briefing", "ok")])
        self.assertEqual(self.prompts(), 1)

    def test_c_two_tools_and_two_intermediate_steps_store_exactly_one_final_reply(self) -> None:
        before_tree = sorted(p.name for p in self.lib.rglob("*"))
        out = self.multi([{"tool": "briefing"},
                          {"tool": "global_read", "args": {"path": "agents/architect/rules/onboarding.md"}},
                          {"native": "shell"}, {"native": "edit"}], "multi-c-0001-xx", intermediate=3)
        self.assertEqual(out["owner"]["delivery"]["state"], "delivered")
        architect = [m["body"] for m in out["messages"] if m["sender"]["kind"] == "architect"]
        self.assertEqual(architect, [FINAL])
        self.assertNotIn("INTERMEDIATE", json.dumps(out["messages"]))
        self.assertEqual([(r.tool, r.outcome) for r in self.audit()], [("briefing", "ok"), ("global_read", "ok")])
        log = [json.loads(line) for line in (self.bin / "tool_log.jsonl").read_text().splitlines()]
        self.assertEqual([e["allowed"] for e in log], [True, True, False, False])  # native rights stay denied
        self.assertEqual(sorted(p.name for p in self.lib.rglob("*")), before_tree)  # no surprise write
        self.architect.drain()
        self.assertEqual(len([m for m in self.client.get("/api/projects/P0001/conversation").json()["messages"]
                              if m["sender"]["kind"] == "architect"]), 1)

    def test_d_tool_success_followed_by_a_provider_error_is_a_failed_delivery(self) -> None:
        for index, terminal in enumerate(("error", "stop_error", "length")):
            out = self.multi([{"tool": "briefing"}], f"multi-d-000{index}-xx", intermediate=2, terminal=terminal)
            delivery = out["owner"]["delivery"]
            self.assertEqual((delivery["state"], delivery["error_code"]), ("failed", "provider_error"), terminal)
            self.assertFalse(delivery["can_retry"])
            self.assertEqual([m for m in out["messages"] if m["sender"]["kind"] == "architect"], [])
            self.assertEqual(self.audit()[-1].outcome, "ok")  # the Tool itself did succeed
            self.assertEqual(self.prompts(), 1)  # never re-sent

    def test_e_g_unfinished_chain_is_unknown_and_reconcile_recovers_the_late_final_without_resend(self) -> None:
        out = self.multi([{"tool": "briefing"}], "multi-g-0001-xx", intermediate=2, late_final=True)
        owner = out["owner"]
        self.assertEqual((owner["delivery"]["state"], owner["delivery"]["error_code"]), ("unknown", "timeout"))
        self.assertEqual([m for m in out["messages"] if m["sender"]["kind"] == "architect"], [])
        url = f"/api/projects/P0001/conversation/messages/{owner['message_id']}"
        self.assertEqual(self.client.post(url + "/retry").status_code, 409)  # nothing is re-sent
        still = self.client.post(url + "/reconcile").json()["delivery"]
        self.assertEqual(still["state"], "unknown")  # chain still unfinished
        audits = len(self.audit())
        (self.bin / "release").write_text("go")  # the model finishes later
        done = self.client.post(url + "/reconcile").json()["delivery"]
        self.assertEqual(done["state"], "delivered")
        messages = self.client.get("/api/projects/P0001/conversation").json()["messages"]
        self.assertEqual([m["body"] for m in messages if m["sender"]["kind"] == "architect"], [FINAL])
        again = self.client.post(url + "/reconcile").json()["delivery"]
        self.assertEqual(again["state"], "delivered")
        self.assertEqual(self.prompts(), 1)  # no prompt re-sent
        self.assertEqual(len(self.audit()), audits)  # no Tool executed again
        self.assertEqual(len([m for m in self.client.get("/api/projects/P0001/conversation").json()["messages"]
                              if m["sender"]["kind"] == "architect"]), 1)


if __name__ == "__main__":
    unittest.main()
