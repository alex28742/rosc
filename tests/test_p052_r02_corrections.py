"""P052 r02: narrow review corrections.

A. ``@Owner``-only and unknown typed recipients never fall through to the Architect (or any) provider.
B. An Architect invitation never closes / supersedes / cancels unfinished Owner-authorized direct Worker work.
C. One Unicode ``@DisplayName`` contract: the Python parser is checked against the SAME parity cases the UI preview test
   uses (``frontend/src/presentations/default/ui/mentionParity.json``).

Isolated temporary databases, the deterministic fake Claude CLI runner and the fake Architect runtime (P047/P052).
No real CLI, provider, tmux, network or Owner data is touched.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from test_p052_agent_mentions_quota import FIN, P052Case, _fetch_exec  # noqa: F401 - P052Case.fetch_exec is attached there

from rosc.mentions import find_mentions

PARITY = Path(__file__).resolve().parents[1] / "frontend/src/presentations/default/ui/mentionParity.json"


class UnicodeParityTests(unittest.TestCase):
    def test_python_parser_matches_every_shared_parity_case(self) -> None:
        cases = json.loads(PARITY.read_text(encoding="utf-8"))["cases"]
        self.assertGreaterEqual(len(cases), 20)
        for case in cases:
            with self.subTest(case["note"] or case["text"]):
                found = find_mentions(case["text"], {ref: tuple(names) for ref, names in case["names"].items()})
                self.assertEqual((list(found.refs), list(found.ambiguous), list(found.unknown)),
                                 (case["refs"], case["ambiguous"], case["unknown"]))

    def test_expanding_fold_never_shifts_the_matched_span(self) -> None:
        # r01 sliced the folded text by the folded name length: wrong once ß -> ss changed the length before the name.
        names = {"s": "Straße", "c": "Claude"}
        self.assertEqual(find_mentions("ßß @Claude", names).refs, ("c",))
        self.assertEqual(find_mentions("@STRASSE и @claude", names).refs, ("s", "c"))
        self.assertEqual(find_mentions("@Straßex", names).refs, ())
        self.assertEqual(find_mentions("@Straße, ok", names).refs, ("s",))  # r01: missed (slice one char too long)


class OwnerOnlyTests(P052Case):
    def counts(self) -> dict:
        one = lambda sql: self.fetch(sql)[0][0]  # noqa: E731
        return {"messages": one("SELECT count(*) FROM conversation_messages"),
                "deliveries": one("SELECT count(*) FROM conversation_deliveries"),
                "rounds": one("SELECT count(*) FROM group_rounds"), "turns": one("SELECT count(*) FROM group_turns"),
                "architect_calls": len(self.arch_texts()), "worker_calls": len(self.runner.calls)}

    def assert_owner_note(self, task: str | None) -> None:
        self.assertEqual(self.api.get("/api/projects/P0001/architect").json()["state"], "ready")  # Architect IS ready
        sent = self.post("@Owner Напоминание: проверить отчёт", [], task=task, key="owner-note-1")
        self.assertEqual(sent.status_code, 201, sent.text)
        self.assertEqual(sent.json()["dispatch"], {"mode": "none", "agents": [], "round": None, "started": False,
                                                    "blocked": None})
        self.settle(task)
        msgs = self.messages(task)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["sender"]["kind"], "owner")
        self.assertEqual(msgs[0]["group"]["to"], ["owner"])  # truthful: → @Owner, never → @Architect
        self.assertNotEqual(msgs[0].get("recipient"), "architect")
        self.assertEqual(self.counts(), {"messages": 1, "deliveries": 0, "rounds": 0, "turns": 0,
                                         "architect_calls": 0, "worker_calls": 0})
        again = self.post("@Owner Напоминание: проверить отчёт", [], task=task, key="owner-note-1")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["message"]["message_id"], sent.json()["message"]["message_id"])
        self.settle(task)
        self.assertEqual(self.counts()["messages"], 1)
        self.assertEqual(self.counts()["architect_calls"] + self.counts()["worker_calls"], 0)

    def test_pc_owner_only_is_stored_once_and_calls_nobody(self) -> None:
        self.assert_owner_note(None)

    def test_tc_owner_only_is_stored_once_and_calls_nobody(self) -> None:
        self.assert_owner_note("T0001")

    def test_owner_only_on_the_legacy_project_route_is_refused_before_storage(self) -> None:
        sent = self.api.post("/api/projects/P0001/conversation/messages",
                             json={"client_key": self.key(), "body": "@Owner напоминание"})
        self.assertEqual(sent.status_code, 422, sent.text)
        self.settle()
        self.assertEqual(self.counts()["messages"], 0)
        self.assertEqual(self.counts()["architect_calls"], 0)

    def test_tc_save_vs_send_is_unchanged_for_an_owner_note(self) -> None:
        saved = self.api.post("/api/projects/P0001/tasks/T0001/conversation/messages",
                              json={"client_key": self.key(), "body": "@Owner заметка"})
        self.assertEqual(saved.status_code, 201, saved.text)  # save-only, no recipient: nothing is delivered
        self.settle("T0001")
        self.assertEqual(self.counts()["deliveries"], 0)
        self.assertEqual(self.counts()["architect_calls"], 0)

    def test_mixed_owner_and_claude_dispatches_only_claude(self) -> None:
        self.runner.replies = ["Посмотрел."]
        sent = self.post("@Owner и @Claude, глянь", [])
        self.assertEqual(sent.status_code, 201, sent.text)
        self.assertEqual(sent.json()["dispatch"]["agents"], [self.claude_agent])
        self.settle()
        owner = self.messages()[0]
        self.assertEqual(sorted(owner["group"]["to"]), sorted([self.claude_agent, "owner"]))
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.arch_texts()), 0)
        retry = self.post("@Owner и @Claude, глянь", [], key=None)
        self.assertEqual(retry.status_code, 201)  # a NEW key is a new message (sanity); identical key replays below
        self.settle()
        key = "mixed-retry-1"
        first = self.post("@Owner @Claude ещё", [self.claude_agent], key=key)
        same = self.post("@Owner @Claude ещё", [self.claude_agent], key=key)
        self.assertEqual(same.status_code, 200)
        self.assertEqual(same.json()["message"]["message_id"], first.json()["message"]["message_id"])
        self.settle()

    def test_unknown_name_is_refused_on_every_route_and_nothing_is_stored(self) -> None:
        group = self.post("@Claud, проверь", [])
        self.assertEqual(group.status_code, 422)
        self.assertIn("@Claud is not a participant", group.text)
        pc = self.api.post("/api/projects/P0001/conversation/messages",
                           json={"client_key": self.key(), "body": "@Claud, проверь"})
        self.assertEqual(pc.status_code, 422)
        tc = self.api.post("/api/projects/P0001/tasks/T0001/conversation/messages",
                           json={"client_key": self.key(), "body": "@Claud", "recipient": "architect"})
        self.assertEqual(tc.status_code, 422)
        chips = self.post("@Claud тоже", [self.claude_agent])  # even next to a valid chip
        self.assertEqual(chips.status_code, 422)
        self.settle()
        self.assertEqual(self.counts(), {"messages": 0, "deliveries": 0, "rounds": 0, "turns": 0,
                                         "architect_calls": 0, "worker_calls": 0})

    def test_ambiguous_and_out_of_scope_names_on_the_legacy_routes(self) -> None:
        self.claude_worker("CLAUDE")
        amb = self.api.post("/api/projects/P0001/conversation/messages",
                            json={"client_key": self.key(), "body": "@claude?"})
        self.assertEqual(amb.status_code, 422)
        self.assertIn("more than one", amb.text)
        self.claude_worker("Tasker", task=2)
        scope = self.api.post("/api/projects/P0001/tasks/T0001/conversation/messages",
                              json={"client_key": self.key(), "body": "@Tasker глянь", "recipient": "architect"})
        self.assertEqual(scope.status_code, 422)
        self.assertIn("not assigned", scope.text)
        self.settle()
        self.assertEqual(self.counts()["messages"], 0)
        self.assertEqual(self.counts()["architect_calls"], 0)

    def test_email_literal_and_unaddressed_text_keep_the_legacy_architect_route(self) -> None:
        self.arch_runtime.replies = ["ok", "ok", "ok"]
        for body in ("напиши на ops@Claude.dev", "декоратор `@dataclass` и путь npm/@types", "просто вопрос"):
            sent = self.direct(body)
            self.assertEqual(sent.status_code, 201, sent.text)
        self.assertEqual(len(self.arch_texts()), 3)
        self.assertEqual(len(self.runner.calls), 0)
        self.assertEqual(self.counts()["rounds"], 0)

    def test_existing_direct_architect_and_worker_routes_still_work(self) -> None:
        self.arch_runtime.replies = ["план готов"]
        self.direct("@Architect и @Owner: план?")
        self.assertEqual(len(self.arch_texts()), 1)
        self.runner.replies = ["готово"]
        sent = self.post("сделай", [self.claude_agent])
        self.assertEqual(sent.json()["dispatch"]["mode"], "direct")
        self.settle()
        self.assertEqual(len(self.runner.calls), 1)


class InvitationNonPreemptionTests(P052Case):
    """Defect B: Owner `@Claude, audit auth` (direct turn queued, not submitted) -> a separate legacy Owner message to the
    Architect -> Architect answers `@Claude, let's discuss errors`.  The queued Owner request must survive untouched."""

    def queue_direct_claude(self, task: str | None = None) -> str:
        with patch.object(self.app.state.group, "_kick", return_value=False):
            sent = self.post("@Claude, audit auth", [], task=task)
        self.assertEqual(sent.status_code, 201, sent.text)
        self.assertEqual(self.fetch("SELECT state FROM group_turns"), [("queued",)])
        return sent.json()["message"]["message_id"]

    def architect_invites(self, task: str | None = None) -> None:
        self.arch_runtime.replies = ["@Claude, давай обсудим ошибки"]
        with patch.object(self.app.state.group, "wake", return_value=False):
            self.direct("Architect, вопрос про ошибки", task=task)

    def assert_preserved(self, state: str) -> None:
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds"), [(1,)])  # no competing discussion opened
        self.assertEqual(self.fetch("SELECT state FROM group_rounds"), [("open",)])
        self.assertEqual(self.fetch("SELECT state, error_code FROM group_turns"), [(state, None)])
        self.assertEqual(self.fetch("SELECT count(*) FROM group_turns WHERE error_code = 'superseded_by_owner_message'"),
                         [(0,)])
        self.assertEqual(self.fetch("SELECT count(*) FROM group_round_grants"), [(1,)])  # quota not reset or extended

    def test_queued_direct_worker_turn_survives_an_architect_invitation_and_answers_once(self) -> None:
        self.queue_direct_claude()
        self.architect_invites()
        self.assert_preserved("queued")
        reply = self.messages()[-1]
        self.assertEqual(reply["sender"]["kind"], "architect")
        self.assertEqual(reply["body"], "@Claude, давай обсудим ошибки")  # authentic reply kept ...
        self.assertEqual(reply["group"]["to"], [self.claude_agent])  # ... with its structured addressee
        self.assertEqual(len(self.runner.calls), 0)
        self.runner.replies = ["auth проверен"]
        self.assertEqual(self.api.post(f"{self.url()}/start").status_code, 200)
        self.wait_room()
        self.api.post(f"{self.url()}/start")
        self.wait_room()
        self.assertEqual(len(self.runner.calls), 1)  # the Owner's request is answered exactly once, no duplicate
        self.assertEqual(self.fetch("SELECT state FROM group_turns"), [("published",)])
        self.assertIn("auth проверен", [m["body"] for m in self.messages()])

    def test_task_conversation_has_the_same_protection(self) -> None:
        self.queue_direct_claude("T0001")
        self.architect_invites("T0001")
        self.assert_preserved("queued")
        self.assertEqual(len(self.runner.calls), 0)

    def test_holding_turns_are_never_reset_or_retried_by_an_invitation(self) -> None:
        for state in ("reserved", "submitted", "unknown"):
            with self.subTest(state):
                self.fetch_exec("DELETE FROM group_turns")
                self.fetch_exec("DELETE FROM group_round_grants")
                self.fetch_exec("DELETE FROM group_rounds")
                self.queue_direct_claude()
                self.fetch_exec("UPDATE group_turns SET state = ?", state)
                self.architect_invites()
                self.assert_preserved(state)
                self.assertEqual(len(self.runner.calls), 0)  # no forced retry, no provider call

    def test_unknown_turn_in_a_finished_round_still_blocks_the_invitation(self) -> None:
        self.queue_direct_claude()
        self.fetch_exec("UPDATE group_turns SET state = 'unknown'")
        self.fetch_exec("UPDATE group_rounds SET state = 'awaiting_owner'")
        self.architect_invites()
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds"), [(1,)])
        self.assertEqual(self.fetch("SELECT state FROM group_turns"), [("unknown",)])
        self.assertEqual(len(self.runner.calls), 0)

    def test_invitation_still_opens_the_discussion_once_the_direct_request_finished(self) -> None:
        self.runner.replies = ["auth в порядке", "ошибки обработаны" + FIN]
        self.post("@Claude, audit auth", [])
        self.settle()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.fetch("SELECT state FROM group_rounds"), [("awaiting_owner",)])
        self.arch_runtime.replies = ["@Claude, давай обсудим ошибки"]
        self.direct("Architect, вопрос про ошибки")
        rounds = self.fetch("SELECT state FROM group_rounds ORDER BY round_no")
        self.assertEqual(rounds, [("closed",), ("awaiting_owner",)])
        self.assertEqual(len(self.runner.calls), 2)  # the invited Worker answered in the new bounded discussion
        self.assertEqual(self.fetch("SELECT count(*) FROM group_turns WHERE state = 'cancelled'"), [(0,)])


if __name__ == "__main__":
    unittest.main()
