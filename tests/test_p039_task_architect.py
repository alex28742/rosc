"""P039: one logical Project Architect host, separate native conversations for the Project Conversation (PCA) and every
Task Conversation (TCA), an explicit send action, and read-only Task/Run Tools.  Real ``OpenCodeRuntime`` + the test-only
fake ``opencode serve`` (echo mode: the reply names the native session that produced it)."""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

import test_p019_project_context_access as p19
from rosc.architect_runtime import OpenCodeRuntime, RuntimeAmbiguous

TASK = {"title": "Second task", "description": "Second task description SYNTH-T2", "acceptance_criteria": "Done when SYNTH-T2-OK"}
TASK1 = {"title": "First task", "description": "First task description SYNTH-T1", "acceptance_criteria": "Done when SYNTH-T1-OK"}


class TaskArchitectCase(p19.ContextCase):
    def setUp(self) -> None:
        super().setUp()
        for body in (TASK1, TASK):
            self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=body).status_code, 201)  # T0001, T0002
        self.assertEqual(self.client.post("/api/projects/P0002/tasks", json=TASK).status_code, 201)  # T0003
        (self.bin / "mode").write_text("echo")
        self.n = 0

    def activate_clean(self) -> str:
        sid = self.activate()
        (self.bin / "prompts_by_session.log").unlink(missing_ok=True)
        return sid

    def tc(self, task: str = "T0001", project: str = "P0001") -> str:
        return f"/api/projects/{project}/tasks/{task}/conversation"

    def save(self, body: str, task: str = "T0001", recipient: str | None = "architect", project: str = "P0001") -> dict:
        self.n += 1
        response = self.client.post(self.tc(task, project) + "/messages",
                                    json={"client_key": f"p039-key-{self.n:05d}", "body": body, "recipient": recipient})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["message"]

    def send(self, message: dict, task: str = "T0001", project: str = "P0001"):
        return self.client.post(f"{self.tc(task, project)}/messages/{message['message_id']}/send")

    def ask(self, body: str, task: str = "T0001") -> dict:
        message = self.save(body, task)
        response = self.send(message, task)
        self.assertEqual(response.status_code, 200, response.text)
        self.architect.drain()
        return message

    def bodies(self, task: str | None = "T0001") -> list[tuple[str, str]]:
        url = "/api/projects/P0001/conversation" if task is None else self.tc(task)
        return [(m["sender"]["kind"], m["body"]) for m in self.client.get(url).json()["messages"]]

    def by_session(self) -> list[dict]:
        path = self.bin / "prompts_by_session.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def rows(self, sql: str) -> list[tuple]:
        with sqlite3.connect(self.data / "rosc.db") as connection:
            return connection.execute(sql).fetchall()


class SaveVersusSendTests(TaskArchitectCase):
    def test_saving_never_starts_or_sends_and_send_needs_a_live_architect(self) -> None:
        message = self.save("hello architect")
        self.assertEqual(message["delivery"]["state"], "not_sent")
        self.assertEqual(self.rows("SELECT count(*) FROM conversation_deliveries"), [(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM architect_sessions"), [(0,)])
        response = self.send(message)
        self.assertEqual(response.status_code, 409, response.text)  # explicit send, Architect not active
        self.assertEqual(self.rows("SELECT count(*) FROM conversation_deliveries"), [(0,)])

    def test_old_saved_messages_are_not_replayed_and_only_addressed_owner_messages_send(self) -> None:
        old = self.save("saved before activation")
        plain = self.save("no addressee", recipient=None)
        self.activate_clean()
        fresh = self.ask("fresh question")
        self.assertEqual([p["id"] for p in self.by_session()], ["msg_rosc" + fresh["message_id"].replace("-", "")])
        views = {m["message_id"]: m for m in self.client.get(self.tc()).json()["messages"]}
        self.assertEqual(views[old["message_id"]]["delivery"]["state"], "not_sent")
        self.assertEqual(views[plain["message_id"]]["delivery"]["state"], "not_sent")
        self.assertEqual(self.send(plain).status_code, 422)  # no addressee: not sendable
        reply = next(m for m in views.values() if m["sender"]["kind"] == "architect")
        self.assertEqual(self.send(reply).status_code, 422)  # an Architect reply is never sent

    def test_send_is_scoped_to_its_task_and_project(self) -> None:
        self.activate_clean()
        message = self.save("t1 text")
        self.assertEqual(self.send(message, task="T0002").status_code, 404)  # another Task's URL
        self.assertEqual(self.send(message, project="P0002").status_code, 404)
        self.assertEqual(self.client.post(f"{self.tc()}/messages/not-a-message/send").status_code, 404)
        self.assertEqual(self.by_session(), [])

    def test_a_second_send_of_the_same_message_returns_the_same_delivery(self) -> None:
        self.activate_clean()
        message = self.ask("once")
        again = self.send(message)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["delivery"]["state"], "delivered")
        self.architect.drain()
        self.assertEqual(len(self.by_session()), 1)
        self.assertEqual(len([b for b in self.bodies() if b[0] == "architect"]), 1)


class IsolatedContextsTests(TaskArchitectCase):
    def test_separate_native_sessions_with_replies_in_the_right_conversation(self) -> None:
        self.activate_clean()
        pca = self.rows("SELECT runtime_session_id FROM architect_sessions WHERE state='ready'")[0][0]
        self.ask("question about one", "T0001")
        self.ask("question about two", "T0002")
        sent = self.client.post("/api/projects/P0001/conversation/messages",
                                json={"client_key": "p039-pc-0000001", "body": "project level question"})
        self.assertEqual(sent.status_code, 201)
        self.architect.drain()
        sessions = {p["text"].split("Owner message:\n")[-1]: p["session"] for p in self.by_session()}
        self.assertEqual(len({sessions["question about one"], sessions["question about two"],
                              sessions["project level question"]}), 3)
        self.assertEqual(sessions["project level question"], pca)
        # every reply names the native session that produced it, and landed only in its own conversation
        self.assertIn(f"ECHO[{sessions['question about one']}] ", self.bodies("T0001")[-1][1])
        self.assertIn(f"ECHO[{sessions['question about two']}] ", self.bodies("T0002")[-1][1])
        self.assertIn(f"ECHO[{pca}] ", self.bodies(None)[-1][1])
        self.assertEqual([k for k, _ in self.bodies("T0001")], ["owner", "architect"])
        self.assertEqual([k for k, _ in self.bodies("T0002")], ["owner", "architect"])
        self.assertEqual([k for k, _ in self.bodies(None)], ["owner", "architect"])
        # the Project session never saw Task text, and no Task session saw the Project question
        for prompt in self.by_session():
            if prompt["session"] == pca:
                self.assertNotIn("SYNTH-T1", prompt["text"])
                self.assertNotIn("SYNTH-T2", prompt["text"])
                self.assertNotIn("question about", prompt["text"])
            else:
                self.assertNotIn("project level question", prompt["text"])
        self.assertEqual(self.rows("SELECT count(*) FROM conversation_native_sessions"), [(2,)])

    def test_first_turn_carries_short_task_context_and_later_turns_only_the_message(self) -> None:
        self.activate_clean()
        self.ask("first", "T0001")
        self.ask("second", "T0001")
        first, second = self.by_session()
        self.assertEqual(first["session"], second["session"])
        for needle in ("T0001", "First task", "SYNTH-T1", "SYNTH-T1-OK", "Owner message:\nfirst"):
            self.assertIn(needle, first["text"])
        self.assertNotIn("SYNTH-T2", first["text"])
        self.assertEqual(second["text"], "second")  # warm: no Task context, no history, no ADRs
        self.assertEqual(self.rows("SELECT primed FROM conversation_native_sessions"), [(1,)])
        self.assertEqual(self.rows("SELECT DISTINCT context_categories FROM conversation_deliveries ORDER BY 1"),
                         [("owner_message",), ("task_first_turn",)])

    def test_restart_of_rosc_reuses_the_same_native_session(self) -> None:
        self.activate_clean()
        self.ask("before restart")
        before = self.by_session()[0]["session"]
        self.restart()
        self.ask("after restart")
        sessions = [p["session"] for p in self.by_session()]
        self.assertEqual(sessions, [before, before])
        self.assertEqual(self.by_session()[1]["text"], "after restart")

    def test_a_lost_native_session_is_replaced_with_a_short_context_again(self) -> None:
        self.activate_clean()
        self.ask("one")
        old = self.rows("SELECT runtime_session_id FROM conversation_native_sessions")[0][0]
        real = OpenCodeRuntime.probe

        def probe(runtime, session_id, handle):
            return False if handle.runtime_session_id == old else real(runtime, session_id, handle)

        with patch.object(OpenCodeRuntime, "probe", probe):
            self.ask("two")
        new = self.rows("SELECT runtime_session_id FROM conversation_native_sessions")[0][0]
        self.assertNotEqual(old, new)
        last = self.by_session()[-1]
        self.assertEqual(last["session"], new)
        self.assertIn("SYNTH-T1", last["text"])  # a different context: the short Task spec is sent again
        self.assertEqual(self.rows("SELECT count(*) FROM conversation_native_sessions"), [(1,)])


class DeliveryStatesTests(TaskArchitectCase):
    def delivery_of(self, message: dict, task: str = "T0001") -> dict:
        return next(m for m in self.client.get(self.tc(task)).json()["messages"]
                    if m["message_id"] == message["message_id"])["delivery"]

    def test_native_session_creation_failure_is_a_safe_retry_and_nothing_was_prompted(self) -> None:
        self.activate_clean()
        (self.bin / "mode").write_text("refuse_new_session")
        message = self.save("will fail")
        self.assertEqual(self.send(message).status_code, 200)
        self.architect.drain()
        state = self.delivery_of(message)
        self.assertEqual((state["state"], state["error_code"], state["can_retry"]), ("failed", "session_not_ready", True))
        self.assertEqual(self.by_session(), [])
        (self.bin / "mode").write_text("echo")
        retried = self.client.post(f"/api/projects/P0001/conversation/messages/{message['message_id']}/retry")
        self.assertEqual(retried.status_code, 200, retried.text)
        self.architect.drain()
        self.assertEqual(self.delivery_of(message)["state"], "delivered")
        self.assertEqual(len(self.by_session()), 1)

    def test_an_ambiguous_turn_becomes_unknown_is_never_resent_and_reconciles_without_a_duplicate(self) -> None:
        self.activate_clean()
        with patch.object(OpenCodeRuntime, "ask", side_effect=RuntimeAmbiguous("timeout")):
            message = self.save("ambiguous")
            self.assertEqual(self.send(message).status_code, 200)
            self.architect.drain()
        state = self.delivery_of(message)
        self.assertEqual((state["state"], state["error_code"], state["can_reconcile"]), ("unknown", "timeout", True))
        self.assertEqual(self.rows("SELECT primed FROM conversation_native_sessions"), [(1,)])
        self.assertEqual(self.client.post(f"/api/projects/P0001/conversation/messages/{message['message_id']}/retry")
                         .status_code, 409)  # unknown is never blindly re-sent
        reconciled = self.client.post(f"/api/projects/P0001/conversation/messages/{message['message_id']}/reconcile")
        self.assertEqual(reconciled.status_code, 200, reconciled.text)
        self.assertEqual(reconciled.json()["delivery"]["state"], "failed")  # the provider never received it: absent
        self.assertEqual(self.by_session(), [])
        self.assertEqual([k for k, _ in self.bodies()], ["owner"])

    def test_unknown_delivery_with_a_reply_at_the_provider_is_recovered_in_the_same_task(self) -> None:
        self.activate_clean()
        real = OpenCodeRuntime.ask

        def ask_then_lose(runtime, handle, runtime_id, text, timeout):
            real(runtime, handle, runtime_id, text, timeout)  # the provider answered ...
            raise RuntimeAmbiguous("connection_lost")  # ... but ROSC lost the connection

        with patch.object(OpenCodeRuntime, "ask", ask_then_lose):
            message = self.save("lost connection")
            self.send(message)
            self.architect.drain()
        self.assertEqual(self.delivery_of(message)["state"], "unknown")
        done = self.client.post(f"/api/projects/P0001/conversation/messages/{message['message_id']}/reconcile")
        self.assertEqual(done.json()["delivery"]["state"], "delivered")
        self.assertEqual([k for k, _ in self.bodies()], ["owner", "architect"])
        self.assertEqual(self.bodies(None), [])
        self.assertEqual(len(self.by_session()), 1)


class TaskRunToolsTests(TaskArchitectCase):
    def test_project_binding_comes_from_the_session_and_task_id_is_only_a_target(self) -> None:
        sid = self.activate_clean()
        ok = self.direct(sid, "task_read", task_id="T0001")
        self.assertTrue(ok["ok"], ok)
        self.assertEqual((ok["result"]["title"], ok["result"]["project_id"], ok["result"]["access"]),
                         ("First task", "P0001", "read_only"))
        self.assertIn("SYNTH-T1-OK", ok["result"]["acceptance_criteria"])
        for bad, code in (("T0003", "not_found"), ("T9999", "not_found"), ("x", "invalid_argument"), (None, "invalid_argument")):
            denied = self.direct(sid, "task_read", task_id=bad)
            self.assertEqual((denied["ok"], denied["error"]["code"]), (False, code), bad)
        listed = self.direct(sid, "task_list")
        self.assertEqual([t["task_id"] for t in listed["result"]["tasks"]], ["T0001", "T0002"])  # never P0002's T0003
        self.assertEqual(self.direct(sid, "run_list", task_id="T0003")["error"]["code"], "not_found")
        self.assertEqual(self.direct(sid, "run_list")["result"]["runs"], [])
        self.assertEqual(self.direct(sid, "run_read", run_id="0" * 36)["error"]["code"], "not_found")

    def test_a_task_native_session_really_calls_the_tools_through_the_transport(self) -> None:
        sid = self.activate_clean()
        (self.bin / "tool_calls.json").write_text(json.dumps([
            {"tool": "task_read", "args": {"task_id": "T0001"}}, {"tool": "docs_list", "args": {}},
            {"native": "shell"}]))
        (self.bin / "tool_log.jsonl").unlink(missing_ok=True)
        self.ask("use tools")
        log = [json.loads(line) for line in (self.bin / "tool_log.jsonl").read_text().splitlines()]
        pca = self.rows(f"SELECT runtime_session_id FROM architect_sessions WHERE session_id='{sid}'")[0][0]
        self.assertTrue(all(e["session"] != pca for e in log))  # called from the TCA's own native session
        self.assertEqual([e["allowed"] for e in log], [True, True, False])
        self.assertEqual(log[0]["result"]["result"]["title"], "First task")
        audit = [(a.tool, a.target, a.outcome) for a in self.audit() if a.tool in ("task_read", "docs_list")]
        self.assertEqual(audit, [("task_read", "P0001:T0001", "ok"), ("docs_list", "P0001", "ok")])

    def test_the_project_architect_reads_task_state_from_its_own_context(self) -> None:
        self.activate_clean()
        entry = self.model_calls([{"tool": "task_list"}, {"tool": "task_read", "args": {"task_id": "T0002"}}])
        self.assertTrue(all(e["allowed"] for e in entry))
        self.assertEqual(entry[1]["result"]["result"]["title"], "Second task")
        self.assertEqual(self.rows("SELECT count(*) FROM conversation_native_sessions"), [(0,)])  # no TCA was involved


class ToolsConnectionTests(TaskArchitectCase):
    def test_a_registration_lost_by_the_server_is_reported_honestly_and_repaired_later(self) -> None:
        sid = self.activate_clean()
        row = self.rows("SELECT port, process_pid, process_ticks, runtime_session_id FROM architect_sessions")[0]
        from rosc.architect_runtime import RuntimeHandle
        handle = RuntimeHandle(row[1], row[2], row[0], row[3], sid)
        self.assertTrue(self.runtime.tools_connected(sid, handle))
        (self.bin / "mode").write_text("mcp_lost")
        self.assertFalse(self.runtime.tools_connected(sid, handle))
        self.assertTrue(self.host.active(sid))  # the ROSC-side credential alone claims nothing about the server
        with patch("rosc.architect_runtime.MCP_CONNECT_TIMEOUT", 1.0):
            self.ask("conversation never depends on Tools")  # the turn still goes through
        self.assertEqual(self.bodies()[-1][0], "architect")
        status = self.client.get("/api/projects/P0001/architect").json()
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["tools"], {"state": "inactive", "detail": "tools_unavailable"})
        (self.bin / "mode").write_text("echo")  # the server answers again
        with patch("rosc.architect_service.TOOLS_RETRY_SECONDS", 0.0):
            self.architect._tools_retry.clear()
            self.assertEqual(self.client.get("/api/projects/P0001/architect").json()["tools"]["state"], "active")


if __name__ == "__main__":
    unittest.main()
