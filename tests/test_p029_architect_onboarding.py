"""P029: one-time Tools-connected onboarding of a NEW native session, warm Owner turns, and conversation that does not
depend on MCP Tools.  Real ``OpenCodeRuntime`` + the test-only fake ``opencode serve``; no model or provider is involved."""
from __future__ import annotations

import json
import unittest

import test_p019_project_context_access as p19
from rosc.architect_runtime import RuntimeProblem

BRIEFING = {"tool": "briefing", "args": {}}
ROLE = {"tool": "global_read", "args": {"path": "agents/architect/rules/role.md"}}


class OnboardingCase(p19.ContextCase):
    def script(self, parts: list[str], calls: list[dict] | None = None) -> None:
        (self.bin / "mode").write_text("file_reply")
        (self.bin / "reply.json").write_text(json.dumps({"parts": parts, "before": []}), encoding="utf-8")
        path = self.bin / "tool_calls.json"
        path.write_text(json.dumps(calls)) if calls is not None else path.unlink(missing_ok=True)

    def unscript(self) -> None:
        (self.bin / "tool_calls.json").unlink(missing_ok=True)
        (self.bin / "mode").write_text("ok")

    def prompts(self) -> list[str]:
        path = self.bin / "prompts.log"
        return path.read_text().splitlines() if path.exists() else []

    def status(self) -> dict:
        return self.client.get("/api/projects/P0001/architect").json()

    def activate_raw(self) -> dict:
        response = self.client.post("/api/projects/P0001/architect/activate")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def conversation_count(self) -> int:
        with self.sessions() as session:
            return session.connection().exec_driver_sql("SELECT count(*) FROM conversation_messages").scalar()

    def send(self, key: str, body: str) -> None:
        sent = self.client.post("/api/projects/P0001/conversation/messages", json={"client_key": key, "body": body})
        self.assertEqual(sent.status_code, 201, sent.text)
        self.architect.drain()

    def delivery(self) -> tuple:
        with self.sessions() as session:
            return tuple(session.connection().exec_driver_sql(
                "SELECT state, error_code FROM conversation_deliveries ORDER BY created_at DESC").fetchone())


class NewSessionOnboardingTests(OnboardingCase):
    def test_one_bootstrap_after_tools_follows_the_role_route_and_is_ready_only_on_evidence(self) -> None:
        self.script(["Read the briefing and the role.\nONBOARDING_STATUS: ready"], [BRIEFING, ROLE])
        view = self.activate_raw()
        self.assertEqual(view["state"], "ready")
        self.assertEqual(view["onboarding"]["state"], "initializing")  # truthful while the model step runs
        self.architect.drain()
        self.assertEqual(self.status()["onboarding"], {"state": "ready", "detail": None})
        sent = self.prompts()
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("msg_roscob"))
        prompt = (self.bin / "last_prompt.txt").read_text()
        for needle in ("rosc_briefing", "rosc_global_read", "agents/architect/rules/role.md", "rosc_source_read",
                       "Docs entrypoint in the bound checkout: README.md"):
            self.assertIn(needle, prompt)
        self.assertNotIn("Handoff", prompt)
        self.assertNotIn("project.initialize", prompt)
        log = [json.loads(line) for line in (self.bin / "tool_log.jsonl").read_text().splitlines()]
        self.assertTrue(all(entry["allowed"] and entry["result"]["ok"] for entry in log), log)
        self.assertEqual(self.conversation_count(), 0)  # onboarding is never written into the Project Conversation

    def test_a_claimed_ready_without_tool_evidence_is_blocked(self) -> None:
        self.script(["Done.\nONBOARDING_STATUS: ready"], [BRIEFING])  # role.md never read
        self.activate_raw()
        self.architect.drain()
        self.assertEqual(self.status()["onboarding"], {"state": "blocked", "detail": "evidence_missing"})

    def test_architect_reported_block_and_missing_marker_are_never_ready(self) -> None:
        self.script(["role.md not found.\nONBOARDING_STATUS: blocked - role file missing"], [BRIEFING, ROLE])
        self.activate_raw()
        self.architect.drain()
        self.assertEqual(self.status()["onboarding"], {"state": "blocked", "detail": "reported_by_architect"})
        self.client.post("/api/projects/P0001/architect/deactivate")
        self.script(["I looked around."], [BRIEFING, ROLE])
        self.activate_raw()
        self.architect.drain()
        self.assertEqual(self.status()["onboarding"], {"state": "unknown", "detail": "no_status_marker"})

    def test_tools_failure_before_the_first_model_call_sends_no_bootstrap(self) -> None:
        self.script(["x\nONBOARDING_STATUS: ready"], [BRIEFING, ROLE])
        host_active = self.host.active
        self.host.active = lambda _sid: False  # type: ignore[method-assign]
        self.addCleanup(lambda: setattr(self.host, "active", host_active))

        def refuse(*_a, **_k):
            raise RuntimeProblem("tools_unavailable")

        self.runtime.reconnect_tools = refuse  # type: ignore[method-assign]
        self.activate_raw()
        self.architect.drain()
        self.assertEqual(self.status()["onboarding"], {"state": "blocked", "detail": "tools_unavailable"})
        self.assertEqual(self.prompts(), [])  # the model was never asked


class RestartAndLegacyTests(OnboardingCase):
    def test_restart_never_repeats_or_invents_an_onboarding(self) -> None:
        self.script(["ok\nONBOARDING_STATUS: ready"], [BRIEFING, ROLE])
        self.activate_raw()
        self.architect.drain()
        self.unscript()
        before = self.prompts()
        self.restart()
        self.architect.recover()
        self.architect.drain()
        self.assertEqual(self.prompts(), before)
        self.assertEqual(self.status()["onboarding"], {"state": "ready", "detail": None})
        self.assertEqual(self.status()["tools"]["state"], "active")  # P028 reconnect, same session

    def test_legacy_session_is_not_onboarded_on_restart_and_still_converses(self) -> None:
        sid = self.activate()
        with self.sessions() as session:  # a session that predates P029 (migration default)
            session.connection().exec_driver_sql(
                "UPDATE architect_sessions SET onboarding_state = 'legacy_unverified', onboarding_detail = NULL")
            session.commit()
        self.restart()
        self.architect.recover()
        self.architect.drain()
        self.assertEqual(self.prompts(), [])
        self.assertEqual(self.status()["onboarding"], {"state": "legacy_unverified", "detail": None})
        self.assertEqual(self.status()["session"]["session_id"], sid)
        self.send("legacy-0001-aa", "тут?")
        self.assertEqual(self.delivery(), ("delivered", None))

    def test_interrupted_onboarding_becomes_unknown_and_is_not_resent(self) -> None:
        self.activate()
        with self.sessions() as session:
            session.connection().exec_driver_sql("UPDATE architect_sessions SET onboarding_state = 'initializing'")
            session.commit()
        self.restart()
        self.architect.recover()
        self.architect.drain()
        self.assertEqual(self.status()["onboarding"], {"state": "unknown", "detail": "interrupted"})
        self.assertEqual(self.prompts(), [])


class WarmTurnsWithoutToolsTests(OnboardingCase):
    def test_owner_message_reaches_the_prompt_endpoint_while_tools_are_inactive(self) -> None:
        sid = self.activate()
        self.host.revoke(sid)  # an MCP outage: the native session itself is unchanged

        def refuse(*_a, **_k):
            raise RuntimeProblem("tools_unavailable")

        self.runtime.reconnect_tools = refuse  # type: ignore[method-assign]
        shown = self.status()
        self.assertEqual((shown["state"], shown["tools"]["state"]), ("ready", "inactive"))
        self.send("warm-0001-aa", "тут?")
        self.assertEqual(self.delivery(), ("delivered", None))
        self.assertEqual((self.bin / "last_prompt.txt").read_text(), "тут?")
        self.send("warm-0002-aa", "и ещё вопрос")
        self.assertEqual((self.bin / "last_prompt.txt").read_text(), "и ещё вопрос")  # no recap of earlier turns
        self.assertEqual(len(self.prompts()), 2)

    def test_lost_session_identity_is_not_masked_as_a_delivery(self) -> None:
        self.activate()
        with self.sessions() as session:
            session.connection().exec_driver_sql("UPDATE architect_sessions SET process_ticks = process_ticks + 7")
            session.commit()

        def restore() -> None:
            with self.sessions() as session:
                session.connection().exec_driver_sql("UPDATE architect_sessions SET process_ticks = process_ticks - 7")
                session.commit()

        self.addCleanup(restore)
        self.send("lost-0001-aa", "anyone?")
        self.assertEqual(self.delivery(), ("failed", "session_not_ready"))
        self.assertEqual(self.prompts(), [])


if __name__ == "__main__":
    unittest.main()
