"""P053: verifiable Architect onboarding readiness and Owner-initiated completion in the SAME native session.

Real ``OpenCodeRuntime`` + the test-only fake ``opencode serve`` (scripted Tool calls through the real MCP transport,
audited by ``ProjectContextBroker``); disposable SQLite.  No model or provider is involved."""
from __future__ import annotations

import json
import unittest

from alembic import command

from test_p029_architect_onboarding import ALL, BRIEFING, DOCS, ROLE, RULES, OnboardingCase

READY = "Read what was required.\nONBOARDING_STATUS: ready"
BASE = "/api/projects/P0001/architect/onboarding"


class P053Case(OnboardingCase):
    def view(self) -> dict:
        return self.status()["onboarding"]

    def steps(self, view: dict | None = None) -> dict:
        return {s["id"]: (s["state"], s["code"]) for s in (view or self.view())["steps"]}

    def check(self) -> dict:
        response = self.client.post(f"{BASE}/check")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["onboarding"]

    def complete(self, expect: int = 200) -> dict:
        response = self.client.post(f"{BASE}/complete")
        self.assertEqual(response.status_code, expect, response.text)
        self.architect.drain()
        return response.json()

    def set_entrypoint(self, value: str | None) -> None:
        with self.sessions() as session:
            session.connection().exec_driver_sql(
                "UPDATE project_root_bindings SET docs_entrypoint = ? WHERE project_id = 1", (value,))
            session.connection().exec_driver_sql("UPDATE projects SET docs_entrypoint = ? WHERE id = 1", (value,))
            session.commit()

    def sessions_used(self) -> list[str]:
        path = self.bin / "prompts_by_session.log"
        return [json.loads(line)["session"] for line in path.read_text().splitlines()] if path.exists() else []

    def late(self, parts: list[str], calls: list[dict]) -> None:
        """The model keeps working after ROSC stopped waiting: its final step appears once ``release`` exists."""
        (self.bin / "mode").write_text("file_reply")
        (self.bin / "reply.json").write_text(json.dumps({"parts": parts, "late_final": True}), encoding="utf-8")
        (self.bin / "tool_calls.json").write_text(json.dumps(calls))
        (self.bin / "release").unlink(missing_ok=True)

    def release(self) -> None:
        (self.bin / "release").write_text("1")


class ReadinessTests(P053Case):
    def test_new_project_without_docs_entrypoint_is_ready_on_three_audited_reads(self) -> None:
        self.set_entrypoint(None)
        self.script([READY], [BRIEFING, ROLE, RULES])
        self.activate_raw()
        self.architect.drain()
        view = self.view()
        self.assertEqual((view["state"], view["detail"]), ("ready", None))
        self.assertEqual(self.steps(view), {"briefing": ("done", None), "role": ("done", None),
                                            "onboarding_rules": ("done", None)})  # no docs step at all
        self.assertEqual(view["final_status"], "ready")
        self.assertFalse(view["can_complete"])
        self.assertEqual(self.conversation_count(), 0)  # nothing written into the Project Conversation
        tools = [json.loads(line)["call"].get("tool", "") for line in (self.bin / "tool_log.jsonl").read_text().splitlines()]
        self.assertFalse([t for t in tools if "docs_create" in t or "docs_update" in t])  # no auto documents
        prompt = (self.bin / "last_prompt.txt").read_text()
        self.assertIn("not a reason to report blocked", prompt)  # familiarization, not Project initialization

    def test_recorded_docs_entrypoint_is_required(self) -> None:
        self.script([READY], [BRIEFING, ROLE, RULES])  # README.md never read
        self.activate_raw()
        self.architect.drain()
        view = self.view()
        self.assertEqual((view["state"], view["detail"]), ("blocked", "missing_evidence"))
        self.assertEqual(self.steps(view)["docs_entrypoint"], ("missing", None))
        self.assertTrue(view["can_complete"])

    def test_unreadable_docs_entrypoint_is_a_concrete_failed_step(self) -> None:
        self.set_entrypoint("MISSING.md")
        self.script([READY], [BRIEFING, ROLE, RULES, {"tool": "source_read", "args": {"path": "MISSING.md"}}])
        self.activate_raw()
        self.architect.drain()
        view = self.view()
        self.assertEqual((view["state"], view["detail"]), ("blocked", "missing_evidence"))
        state, code = self.steps(view)["docs_entrypoint"]
        self.assertEqual(state, "failed")
        self.assertTrue(code)

    def test_full_evidence_is_ready_and_a_missing_one_is_never_ready(self) -> None:
        self.script([READY], ALL)
        self.activate_raw()
        self.architect.drain()
        self.assertEqual(self.view()["state"], "ready")
        self.client.post("/api/projects/P0001/architect/deactivate")
        self.script([READY], [BRIEFING, RULES, DOCS])  # role.md not read
        self.activate_raw()
        self.architect.drain()
        view = self.view()
        self.assertEqual((view["state"], view["detail"]), ("blocked", "missing_evidence"))
        self.assertEqual(self.steps(view)["role"], ("missing", None))

    def test_a_later_chat_tool_call_never_makes_an_old_onboarding_ready(self) -> None:
        self.script([READY], [BRIEFING])
        self.activate_raw()
        self.architect.drain()
        self.assertEqual(self.view()["detail"], "missing_evidence")
        self.unscript()
        self.model_calls([ROLE, RULES, DOCS])  # an ordinary Owner turn that happens to read the same files
        before = len(self.prompts())
        view = self.check()
        self.assertEqual(len(self.prompts()), before)
        self.assertEqual((view["state"], view["detail"]), ("blocked", "missing_evidence"))
        self.assertEqual(self.steps(view)["role"], ("missing", None))


class CheckTests(P053Case):
    def test_check_makes_no_model_call_and_recovers_a_late_reply_without_resend(self) -> None:
        self.late([READY], ALL)
        self.activate_raw()
        self.architect.drain()
        self.assertEqual(self.view()["state"], "unknown")  # ROSC stopped waiting; never re-sent
        sid = self.status()["session"]["session_id"]
        sent = self.prompts()
        self.assertEqual(len(sent), 1)
        view = self.check()  # the provider is still working: reported as such, no second turn
        self.assertEqual((view["state"], view["detail"], view["check"]), ("unknown", "in_progress", "provider_unresolved"))
        self.assertEqual(self.prompts(), sent)
        self.release()
        view = self.check()
        self.assertEqual((view["state"], view["detail"]), ("ready", None))
        self.assertEqual(self.prompts(), sent)  # recovered by lookup, 0 model calls
        self.assertEqual(self.status()["session"]["session_id"], sid)  # same native session
        self.assertEqual(len(set(self.sessions_used())), 1)

    def test_check_on_a_legacy_session_reports_nothing_recorded(self) -> None:
        self.activate()
        with self.sessions() as session:
            session.connection().exec_driver_sql(
                "UPDATE architect_sessions SET onboarding_state = 'legacy_unverified', onboarding_detail = NULL, "
                "onboarding_attempt = 0, onboarding_sent_at = NULL, onboarding_settled_at = NULL, "
                "onboarding_evidence = NULL")
            session.commit()
        view = self.check()
        self.assertEqual((view["state"], view["check"]), ("legacy_unverified", "nothing_recorded"))
        self.assertEqual({s["state"] for s in view["steps"]}, {"not_verified"})
        self.assertEqual(self.prompts(), [])


class CompleteTests(P053Case):
    def test_completion_sends_only_the_missing_reads_once_in_the_same_session(self) -> None:
        self.script([READY], [BRIEFING, ROLE])
        self.activate_raw()
        self.architect.drain()
        self.assertEqual(self.view()["detail"], "missing_evidence")
        first_session = self.sessions_used()
        self.script([READY], [RULES, DOCS])
        self.complete()
        view = self.view()
        self.assertEqual((view["state"], view["detail"], view["attempt"]), ("ready", None, 2))
        self.assertEqual(len(self.prompts()), 2)  # exactly one new provider turn
        self.assertEqual(set(self.sessions_used()), set(first_session))  # the same native session
        prompt = (self.bin / "last_prompt.txt").read_text()
        self.assertIn("agents/architect/rules/onboarding.md", prompt)
        self.assertIn("README.md", prompt)
        self.assertNotIn("agents/architect/rules/role.md", prompt)
        self.assertNotIn("rosc_briefing", prompt)
        self.assertEqual(self.conversation_count(), 0)
        self.complete()  # nothing missing any more: no model call
        self.assertEqual(len(self.prompts()), 2)

    def test_complete_while_running_sends_nothing(self) -> None:
        self.script([READY], [BRIEFING])
        self.activate_raw()
        self.architect.drain()
        with self.sessions() as session:
            session.connection().exec_driver_sql("UPDATE architect_sessions SET onboarding_state = 'initializing'")
            session.commit()
        before = self.prompts()
        self.assertEqual(self.complete()["onboarding"]["state"], "initializing")
        self.assertEqual(self.prompts(), before)

    def test_unresolved_previous_attempt_blocks_a_second_send_across_restart(self) -> None:
        self.script([READY], [BRIEFING])
        self.activate_raw()
        self.architect.drain()
        self.late([READY], [ROLE, RULES, DOCS])
        self.complete()
        self.assertEqual(self.view()["state"], "unknown")
        sent = self.prompts()
        self.assertEqual(len(sent), 2)
        self.restart()
        self.architect.recover()
        self.complete()  # the provider has not finished attempt 2: no new turn
        view = self.view()
        self.assertEqual((view["state"], view["detail"]), ("unknown", "in_progress"))
        self.assertEqual(self.prompts(), sent)
        self.release()
        self.complete()  # attempt 2 is recovered by lookup and proves everything: still no new turn
        view = self.view()
        self.assertEqual((view["state"], view["detail"]), ("ready", None))
        self.assertEqual(self.prompts(), sent)

    def test_without_tools_or_session_completion_is_refused_and_chat_still_works(self) -> None:
        self.script([READY], [BRIEFING])
        self.activate_raw()
        self.architect.drain()
        sid = self.status()["session"]["session_id"]
        self.host.revoke(sid)
        host_active = self.host.active
        self.host.active = lambda _sid: False  # type: ignore[method-assign]
        self.addCleanup(lambda: setattr(self.host, "active", host_active))
        self.assertFalse(self.view()["can_complete"])
        before = self.prompts()
        self.complete(expect=409)
        self.assertEqual(self.prompts(), before)
        self.unscript()
        self.send("p053-chat-0001", "тут?")
        self.assertEqual(self.delivery(), ("delivered", None))  # ordinary chat is not blocked
        self.host.active = host_active  # type: ignore[method-assign]
        self.client.post("/api/projects/P0001/architect/deactivate")
        self.complete(expect=409)

    def test_legacy_session_completes_only_on_explicit_owner_request(self) -> None:
        self.activate()
        with self.sessions() as session:
            session.connection().exec_driver_sql(
                "UPDATE architect_sessions SET onboarding_state = 'legacy_unverified', onboarding_detail = NULL, "
                "onboarding_attempt = 0, onboarding_sent_at = NULL, onboarding_settled_at = NULL, "
                "onboarding_evidence = NULL")
            session.commit()
        self.restart()
        self.architect.recover()
        self.architect.drain()
        self.assertEqual(self.prompts(), [])  # never on restart
        self.script([READY], ALL)
        self.complete()
        self.assertEqual(len(self.prompts()), 1)
        prompt = (self.bin / "last_prompt.txt").read_text()
        self.assertIn("predates verified onboarding", prompt)
        self.assertEqual(self.view()["state"], "ready", self.view())


class MigrationTests(P053Case):
    def test_upgrade_backfills_the_p029_attempt_and_bounds_its_window(self) -> None:
        self.script([READY], [BRIEFING])
        self.activate_raw()
        self.architect.drain()
        self.unscript()
        self.send("p053-mig-0001", "first chat")
        self.client.post("/api/projects/P0001/architect/deactivate")
        self.client.close()
        command.downgrade(self.alembic, "0017_group_round_provenance")
        command.upgrade(self.alembic, "head")
        with self.sessions() as session:
            row = session.connection().exec_driver_sql(
                "SELECT s.onboarding_attempt, s.onboarding_sent_at = s.created_at, s.onboarding_settled_at = "
                "(SELECT min(created_at) FROM conversation_deliveries), s.onboarding_evidence, s.onboarding_state "
                "FROM architect_sessions s").fetchone()
        self.assertEqual(tuple(row), (1, 1, 1, None, "blocked"))
        self.restart()

    def test_database_not_upgraded_to_0018_keeps_p029_status_and_refuses_the_new_actions(self) -> None:
        self.script([READY], ALL)
        self.activate_raw()
        self.architect.drain()
        self.client.close()
        command.downgrade(self.alembic, "0017_group_round_provenance")  # new code started before the Owner upgrades
        self.restart()
        self.architect.recover()
        self.unscript()
        before = self.prompts()
        shown = self.status()
        self.assertEqual((shown["state"], shown["onboarding"]), ("ready", {"state": "ready", "detail": None}))
        self.assertEqual(self.client.post(f"{BASE}/check").status_code, 409)
        self.assertEqual(self.client.post(f"{BASE}/complete").status_code, 409)
        self.send("p053-old-0001", "тут?")
        self.assertEqual(self.delivery(), ("delivered", None))  # ordinary chat is unaffected
        self.client.post("/api/projects/P0001/architect/deactivate")
        self.client.close()
        command.upgrade(self.alembic, "head")
        self.restart()


class GovernedRulesConsistencyTests(unittest.TestCase):
    """P053 micro-fix: the governed rules never make future Project Initialization a condition of familiarization."""

    def test_rules_separate_session_familiarization_from_project_initialization(self) -> None:
        import yaml
        from pathlib import Path
        lib = Path(__file__).resolve().parents[1] / "global" / "agents" / "architect"
        role = (lib / "rules/role.md").read_text()
        onboarding = (lib / "rules/onboarding.md").read_text()
        standard_text = (lib / "documentation-standard.yaml").read_text()
        for text in (role, onboarding):
            self.assertIn("Session Familiarization", text)
            self.assertIn("Project Initialization", text)
        self.assertNotIn("Mark onboarding READY only after", onboarding)
        self.assertIn("Mark Project Initialization READY only after", onboarding)
        for needle in ("rosc_briefing", "agents/architect/rules/role.md", "rosc_source_read"):
            self.assertIn(needle, onboarding)  # the implemented familiarization set is stated in the rules themselves
        self.assertIn("does not block familiarization", onboarding)
        self.assertNotIn("asks ROSC Tools to create **required** Handoff and Pipeline;", role)
        self.assertIn("NOT conditions of the current Session", standard_text)
        standard = yaml.safe_load(standard_text)  # the target architecture itself is unchanged
        self.assertEqual(standard["onboarding"]["readiness"], "mandatory_resources_valid_and_next_action_known")
        self.assertTrue(standard["required_resources"]["pipeline"]["create_on_onboarding"])


if __name__ == "__main__":
    unittest.main()
