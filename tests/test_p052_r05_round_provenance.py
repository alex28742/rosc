"""P052 r05: trusted group-round opening provenance for the Architect and the Worker.

Isolated temporary databases, the deterministic fake Claude CLI runner and the fake Architect runtime only (no real CLI,
provider, network, Owner data or Owner process).  Every assertion about what an Agent "sees" reads the ACTUAL provider-
bound text built by the real adapters: ``ClaudeConversationTransport`` (``runner.calls[i]["prompt"]``) and
``ArchitectGroupTransport`` (``arch_runtime.asked``), both via the shared ``render_turn_prompt``.

1. Owner Continue after an exhausted 3+3 round: durable ``owner_continue`` + previous round number + ``quota_exhausted``
   in both adapters' prompts, with the fresh quota and the causal reply; no Owner message, no provider call before it.
2. Restart durability: the provenance comes from committed columns, not the latest Owner post or diagnostics.
3. Other origins (new Owner group message, Owner authorize / renew, Agent invitation from a fresh Owner direct
   request, single direct request) are never labelled ``owner_continue``; PC and TC.
4. Legacy rows / additive upgrade: unknown origin, no Owner attribution, no lineage, no crash.
5. Diagnostics: metadata mode carries only allowlisted provenance codes, never bodies; OFF writes nothing.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from alembic import command

from test_p052_agent_mentions_quota import FIN, envelope
from test_p052_r03_lifecycle_diagnostics import TraceMixin
from test_p052_r04_agent_conversation import CONT, R04Case

CONTINUED = "the Owner explicitly continued the finished group discussion"
NOT_BYPASS = "not a Core quota bypass"


def context(text: str) -> dict | None:
    return (envelope(text).get("quota") or {}).get("round_context")


class R05Case(R04Case):
    def claude_texts(self) -> list[str]:
        return [call["prompt"] for call in self.runner.calls]

    def rounds(self, task: str | None = None) -> list[tuple]:
        room = self.room_id(task)
        return self.fetch("SELECT round_no, opened_via, previous_round_id, previous_stop_reason, round_id, state "
                          "FROM group_rounds WHERE conversation_id = ? ORDER BY round_no", room)

    def owner_count(self, task: str | None = None) -> int:
        return sum(1 for m in self.messages(task) if m["sender"]["kind"] == "owner")

    def exhaust_from_architect(self, task: str | None = None) -> None:
        """Owner -> Architect (direct); Architect @Claude opens a discussion; 3+3 until Claude asks the exhausted
        Architect (the live round #15 shape)."""
        self.arch_runtime.replies = ["@Claude вопрос 1", "вопрос 2", "вопрос 3"]
        self.runner.replies = ["ответ 1", "ответ 2", "ответ 3, Architect, что дальше?"]
        self.direct("Architect, обсуди с @Claude", task=task)
        status = self.status(task)
        self.assertEqual((status["round"]["state"], status["stop_reason"]), ("awaiting_owner", "quota_exhausted"))

    def exhaust_from_worker(self, task: str | None = None) -> None:
        """Owner -> Claude (direct); Claude @Architect converts the direct request; 3+3 until the Architect asks the
        exhausted Claude."""
        self.runner.replies = ["@Architect проверь отчёт", "исправил", "готово"]
        self.arch_runtime.replies = ["нужна правка", "ещё одна правка", "Claude, подтверди итог?"]
        self.assertEqual(self.post("Claude, сделай отчёт", [self.claude_agent], task=task).status_code, 201)
        self.settle(task)
        status = self.status(task)
        self.assertEqual((status["round"]["state"], status["stop_reason"]), ("awaiting_owner", "quota_exhausted"))
        self.assertEqual([(u["agent_id"], u["reason"]) for u in status["unanswered"]],
                         [(self.claude_agent, "quota_exhausted")])


class OwnerContinueProvenanceTests(R05Case):
    def continue_after_exhaustion(self, task: str | None = None) -> None:
        self.exhaust_from_architect(task)
        calls, owners = (len(self.claude_texts()), len(self.arch_texts())), self.owner_count(task)
        last_claude = self.agent_messages(task)[-1]
        # Nothing happens without the Owner: wake/recover invoke nobody, no Owner message is written.
        self.app.state.group.recover_all()
        self.app.state.group.wake(self.room_id(task))
        self.wait_room(task)
        self.assertEqual((len(self.claude_texts()), len(self.arch_texts())), calls)
        self.arch_runtime.replies = ["Переходим к этапу 4. Подтверди?"]  # plaintext = continue, default to Claude
        self.runner.replies = ["Подтверждаю." + FIN]
        cont = self.api.post(f"{self.url(task)}/continue")
        self.assertEqual(cont.status_code, 200, cont.text)
        self.assertEqual(cont.json()["round"]["opened_via"] if "round" in cont.json() else cont.json()["opened_via"],
                         "owner_continue")
        self.settle(task)
        self.assertEqual(self.owner_count(task), owners)  # Continue is a control action, never a chat message

        # Durable, canonical state: same transaction as the round opening.
        (n1, via1, prev1, stop1, rid1, state1), (n2, via2, prev2, stop2, _rid2, _s2) = self.rounds(task)
        self.assertEqual((n1, via1, prev1, stop1, state1), (1, "agent_invitation", None, None, "closed"))
        self.assertEqual((n2, via2, prev2, stop2), (2, "owner_continue", rid1, "quota_exhausted"))

        expected = {"round_no": 2, "opened_via": "owner_continue", "authorized_by_role": "owner",
                    "previous_round_no": 1, "previous_stop_reason": "quota_exhausted"}
        # Architect / OpenCode group adapter: exactly one new call, carrying the provenance, the fresh 3/3 and the cause.
        self.assertEqual(len(self.arch_texts()), calls[1] + 1)
        arch_text = self.arch_texts()[-1]
        self.assertEqual(context(arch_text), expected)
        quota = envelope(arch_text)["quota"]
        self.assertEqual((quota["you"]["quota"], quota["you"]["published"], quota["you"]["remaining_after_this_reply"]),
                         (3, 0, 2))
        self.assertEqual(quota["reply"]["reply_to_ordinal"], last_claude["ordinal"])
        self.assertIn("ROSC ROUND (trusted): this is group round #2, opened because " + CONTINUED, arch_text)
        self.assertIn("the previous round #1 stopped because its reply allowance was exhausted", arch_text)
        self.assertIn(NOT_BYPASS, arch_text)
        # Claude CLI Worker adapter: identical opening provenance, its own current quota and causal reply.
        self.assertEqual(len(self.claude_texts()), calls[0] + 1)
        claude_text = self.claude_texts()[-1]
        self.assertEqual(context(claude_text), expected)
        quota = envelope(claude_text)["quota"]
        self.assertEqual((quota["you"]["published"], quota["you"]["remaining_after_this_reply"]), (0, 2))
        arch_reply = self.agent_messages(task)[-2]
        self.assertEqual((quota["reply"]["default_to"], quota["reply"]["reply_to_ordinal"]),
                         (self.arch, arch_reply["ordinal"]))
        self.assertEqual(next(p for p in quota["peers"] if p["agent_id"] == self.arch)["remaining"], 2)
        self.assertIn(CONTINUED, claude_text)
        self.assertIn(NOT_BYPASS, claude_text)
        # r04 behaviour unchanged: finish ends the renewed round; no self-renewal.
        status = self.status(task)
        self.assertEqual((status["stop_reason"], status["round"]["opened_via"]), ("finished", "owner_continue"))
        self.assertEqual(self.api.post(f"{self.url(task)}/continue").status_code, 409)

    def test_project_conversation_owner_continue_reaches_both_adapters(self) -> None:
        self.continue_after_exhaustion()
        # The exhausted round #1 itself was an Agent invitation: its group turns said so, never "continued".
        for text in self.arch_texts()[1:3] + self.claude_texts()[:3]:
            self.assertEqual(context(text), {"round_no": 1, "opened_via": "agent_invitation",
                                             "authorized_by_role": "owner"})
            self.assertNotIn(CONTINUED, text)

    def test_task_conversation_owner_continue_reaches_both_adapters(self) -> None:
        self.continue_after_exhaustion(task="T0001")
        self.assertIsNone(self.status()["conversation_id"])  # nothing leaked into the Project room


class RestartDurabilityTests(R05Case):
    def test_owner_continue_survives_restart_and_ignores_a_newer_owner_post(self) -> None:
        self.exhaust_from_worker()
        before = (len(self.claude_texts()), len(self.arch_texts()))
        with patch.object(self.app.state.group, "_kick", return_value=False):
            self.assertEqual(self.api.post(f"{self.url()}/continue").status_code, 200)
        # A newer Owner post that authorizes nothing (Owner-only note): provenance must not be re-derived from it.
        self.assertEqual(self.post("@Owner заметка", []).json()["dispatch"]["mode"], "none")
        self.assertEqual((len(self.claude_texts()), len(self.arch_texts())), before)
        runner = self.runner
        # Backend restart: a brand-new composition over the same temporary database.
        self.api = self.make_client()
        self.runner = runner
        self.assertEqual(self.api.post("/api/projects/P0001/architect/activate").json()["state"], "ready")
        self.app.state.architect.drain()
        self.arch_runtime.asked.clear()
        self.runner.replies = ["Подтверждаю итог, Architect, принимаешь?"]
        self.arch_runtime.replies = ["Принимаю." + FIN]
        self.app.state.group.recover_all()
        self.assertEqual(self.api.post(f"{self.url()}/start").status_code, 200)
        self.settle()
        expected = {"round_no": 2, "opened_via": "owner_continue", "authorized_by_role": "owner",
                    "previous_round_no": 1, "previous_stop_reason": "quota_exhausted"}
        self.assertEqual(len(self.claude_texts()), before[0] + 1)
        self.assertEqual(context(self.claude_texts()[-1]), expected)
        self.assertEqual(len(self.arch_texts()), 1)
        self.assertEqual(context(self.arch_texts()[-1]), expected)
        self.assertIn(CONTINUED, self.arch_texts()[-1])
        rounds = self.rounds()
        self.assertEqual([(r[0], r[1], r[5]) for r in rounds], [(1, "agent_invitation", "closed"),
                                                                 (2, "owner_continue", "awaiting_owner")])
        self.assertEqual((rounds[1][2], rounds[1][3]), (rounds[0][4], "quota_exhausted"))


class OtherOriginsTests(R05Case):
    def assert_not_continue(self, texts: list[str], via: str, round_no: int) -> None:
        self.assertTrue(texts)
        for text in texts:
            self.assertEqual(context(text), {"round_no": round_no, "opened_via": via, "authorized_by_role": "owner"})
            self.assertNotIn(CONTINUED, text)
            self.assertNotIn(NOT_BYPASS, text)
            self.assertNotIn("previous round", text)

    def check_new_owner_group_message(self, task: str | None = None) -> None:
        self.arch_runtime.replies = ["Предлагаю B."]
        self.runner.replies = ["Согласен." + FIN]
        sent = self.post("Оба: выберите вариант", [self.arch, self.claude_agent], task=task)
        self.assertEqual(sent.json()["dispatch"]["mode"], "group")
        self.settle(task)
        self.assertEqual([r[1] for r in self.rounds(task)], ["owner_group_message"])
        self.assert_not_continue(self.arch_texts() + self.claude_texts(), "owner_group_message", 1)
        self.assertIn("opened because a new Owner message addressed this group", self.claude_texts()[-1])

    def test_new_owner_group_message_project(self) -> None:
        self.check_new_owner_group_message()

    def test_new_owner_group_message_task(self) -> None:
        self.check_new_owner_group_message(task="T0001")

    def test_new_owner_group_message_after_exhaustion_is_not_continue(self) -> None:
        self.exhaust_from_architect()
        arch, claude = len(self.arch_texts()), len(self.claude_texts())
        self.arch_runtime.replies = ["Итог." + FIN]
        self.runner.replies = ["Ок." + FIN]
        self.post("Оба: подведите итог", [self.arch, self.claude_agent])
        self.settle()
        self.assertEqual([r[1] for r in self.rounds()], ["agent_invitation", "owner_group_message"])
        self.assertIsNone(self.rounds()[1][2])
        self.assert_not_continue(self.arch_texts()[arch:] + self.claude_texts()[claude:], "owner_group_message", 2)

    def test_owner_authorize_then_renew(self) -> None:
        self.runner.replies = ["Привет." + FIN]
        self.arch_runtime.replies = ["Привет." + FIN]
        self.assertEqual(self.post("@Owner заметка", []).json()["dispatch"]["mode"], "none")  # creates the room only
        auth = self.api.post(f"{self.url()}/round", json={"agents": [self.arch, self.claude_agent]})
        self.assertEqual(auth.status_code, 200, auth.text)
        self.assertEqual(auth.json()["opened_via"], "owner_authorize")
        self.app.state.group.wake(self.room_id())
        self.settle()
        self.assert_not_continue(self.arch_texts() + self.claude_texts(), "owner_authorize", 1)
        arch, claude = len(self.arch_texts()), len(self.claude_texts())
        self.runner.replies = ["Снова." + FIN]
        self.arch_runtime.replies = ["Снова." + FIN]
        renew = self.api.post(f"{self.url()}/round", json={"agents": [self.arch, self.claude_agent], "renew": True})
        self.assertEqual(renew.status_code, 200, renew.text)
        self.app.state.group.wake(self.room_id())
        self.settle()
        self.assertEqual([r[1] for r in self.rounds()], ["owner_authorize", "owner_renew"])
        self.assert_not_continue(self.arch_texts()[arch:] + self.claude_texts()[claude:], "owner_renew", 2)

    def test_architect_invitation_from_fresh_owner_direct_request(self) -> None:
        self.arch_runtime.replies = ["@Claude проверь", "Спасибо." + FIN]
        self.runner.replies = ["Проверил."]
        self.direct("Architect, спроси Claude")
        self.assertEqual([r[1] for r in self.rounds()], ["agent_invitation"])
        # The first Architect call is the legacy direct path: its direct/no-round behaviour is preserved.
        self.assertNotIn("ROSC ROUND", self.arch_texts()[0])
        self.assert_not_continue(self.claude_texts() + self.arch_texts()[1:], "agent_invitation", 1)

    def check_worker_invitation(self, task: str | None = None) -> None:
        self.runner.replies = ["@Architect проверь отчёт"]
        self.arch_runtime.replies = ["Отчёт верный." + FIN]
        self.post("Claude, подготовь отчёт", [self.claude_agent], task=task)
        self.settle(task)
        self.assertEqual([r[1] for r in self.rounds(task)], ["agent_invitation"])
        self.assertIsNone(context(self.claude_texts()[0]))  # the Worker's direct turn: not a group turn
        self.assert_not_continue(self.arch_texts(), "agent_invitation", 1)
        self.assertIn("an Agent's reply to a fresh Owner direct request invited", self.arch_texts()[0])

    def test_worker_invitation_from_fresh_owner_direct_request_project(self) -> None:
        self.check_worker_invitation()

    def test_worker_invitation_from_fresh_owner_direct_request_task(self) -> None:
        self.check_worker_invitation(task="T0001")

    def test_single_agent_direct_request_is_not_a_group(self) -> None:
        self.runner.replies = ["Ответ."]
        self.post("Claude, вопрос", [self.claude_agent])
        self.settle()
        self.assertEqual([r[1] for r in self.rounds()], ["owner_direct_request"])
        self.assertIsNone(context(self.claude_texts()[0]))
        self.assertNotIn("ROSC ROUND", self.claude_texts()[0])
        self.assertEqual(self.status()["round"]["opened_via"], "owner_direct_request")

    def test_direct_request_attached_to_running_group_does_not_forge_an_opening(self) -> None:
        with patch.object(self.app.state.group, "_kick", return_value=False):
            self.post("Оба: начните", [self.arch, self.claude_agent])
            self.post("Claude, отдельный вопрос", [self.claude_agent])
        self.assertEqual([r[1] for r in self.rounds()], ["owner_group_message"])
        self.arch_runtime.replies = ["Ок." + FIN]
        self.runner.replies = ["Ок." + FIN, "Отдельный ответ."]
        self.assertEqual(self.api.post(f"{self.url()}/start").status_code, 200)
        self.settle()
        self.assertEqual(len(self.rounds()), 1)
        for text in self.arch_texts() + self.claude_texts():
            self.assertEqual(context(text)["opened_via"], "owner_group_message")


class LegacyRoundTests(R05Case):
    def test_round_without_recorded_origin_reports_unknown_without_attribution(self) -> None:
        with patch.object(self.app.state.group, "_kick", return_value=False):
            self.post("Оба: начните", [self.arch, self.claude_agent])
        self.fetch_exec("UPDATE group_rounds SET opened_via = NULL")  # a round opened before migration 0017
        self.arch_runtime.replies = ["Ок." + FIN]
        self.runner.replies = ["Ок." + FIN]
        self.assertEqual(self.api.post(f"{self.url()}/start").status_code, 200)
        self.settle()
        for text in self.arch_texts() + self.claude_texts():
            self.assertEqual(context(text), {"round_no": 1, "opened_via": "unknown"})
            self.assertIn("ROSC has no recorded reason for how it was opened", text)
            self.assertNotIn(CONTINUED, text)
        self.assertEqual(self.status()["round"]["opened_via"], "unknown")

    def test_additive_upgrade_keeps_rows_and_fabricates_nothing(self) -> None:
        self.exhaust_from_architect()
        rows = self.fetch("SELECT round_id, round_no, state, authorizing_message_id FROM group_rounds")
        messages = self.fetch("SELECT count(*) FROM conversation_messages")
        command.downgrade(self.alembic, "0016_worker_conversation_core")
        columns = [r[1] for r in self.fetch("PRAGMA table_info(group_rounds)")]
        self.assertNotIn("opened_via", columns)
        command.upgrade(self.alembic, "head")
        self.assertEqual(self.fetch("SELECT round_id, round_no, state, authorizing_message_id FROM group_rounds"), rows)
        self.assertEqual(self.fetch("SELECT count(*) FROM conversation_messages"), messages)
        self.assertEqual(self.fetch("SELECT opened_via, previous_round_id, previous_stop_reason FROM group_rounds"),
                         [(None, None, None)])
        self.api = self.make_client()  # a fresh composition reads the upgraded legacy round
        status = self.status()
        self.assertEqual((status["round"]["opened_via"], status["stop_reason"]), ("unknown", "quota_exhausted"))


class DiagnosticsR05Tests(TraceMixin, R05Case):
    def test_metadata_mode_records_only_allowlisted_provenance(self) -> None:
        self.set_diag(enabled=True)
        self.arch_runtime.replies = ["@Claude SECRET-Q-1", "SECRET-Q-2", "SECRET-Q-3", "SECRET-Q-4"]
        self.runner.replies = ["SECRET-A-1", "SECRET-A-2", "SECRET-A-3", "SECRET-A-4" + FIN]
        self.direct("SECRET-OWNER-1 старт")
        self.assertEqual(self.api.post(f"{self.url()}/continue").status_code, 200)
        self.settle()
        events = self.trace_events()
        raw = json.dumps(events, ensure_ascii=False)
        for sentinel in ("SECRET-Q-", "SECRET-A-", "SECRET-OWNER-", "ROSC ROUND", CONTINUED):
            self.assertNotIn(sentinel, raw)
        opened = [e for e in events if e["event"] == "round_opened"][-1]
        self.assertEqual((opened["decision"], opened["opened_via"], opened["stop_reason"]),
                         ("owner_continue", "owner_continue", "quota_exhausted"))
        self.assertNotIn("authorized_by", opened)
        dispatched = [e for e in events if e["event"] == "dispatch_attempted"][-2:]
        for event in dispatched:
            self.assertEqual(event["quota_snapshot"]["round_context"],
                             {"round_no": 2, "opened_via": "owner_continue", "authorized_by_role": "owner",
                              "previous_round_no": 1, "previous_stop_reason": "quota_exhausted"})
            self.assertNotIn("request_text", event)

    def test_full_body_mode_carries_the_guidance_and_off_writes_nothing(self) -> None:
        self.exhaust_from_architect()
        self.assertFalse(self.trace_dir().exists())  # OFF: nothing written
        self.set_diag(enabled=True, include_bodies=True)
        self.arch_runtime.replies = ["Ок." + FIN]
        self.assertEqual(self.api.post(f"{self.url()}/continue").status_code, 200)
        self.settle()
        dispatched = [e for e in self.trace_events() if e["event"] == "dispatch_attempted"][-1]
        self.assertIn(CONTINUED, dispatched["guidance_text"])
        self.assertEqual(dispatched["quota_snapshot"]["round_context"]["opened_via"], "owner_continue")


if __name__ == "__main__":
    unittest.main()
