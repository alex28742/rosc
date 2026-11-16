"""P052 r04 (Owner-approved R-15.26): bounded autonomous Agent <-> Agent group conversations.

Mandatory acceptance scenarios of the frozen prompt, on isolated temporary databases with the deterministic fake Claude
CLI runner (P045/P047) and the fake Architect runtime (P015/P047/P052).  No real CLI, provider, network, Owner data or
Owner process is touched; every "provider call" is a fake and is counted.

1. Owner DIRECT -> Architect; Architect ``@Claude`` opens ONE group round (its message spends 1/3, Claude 0/3).  Claude
   answers WITHOUT ``@Architect``: ``to=Architect``, ``reply_to`` = the Architect's question; the Architect really gets
   the next turn; Architect <-> Claude up to 3+3, never a 7th invocation; exact quota snapshot on every turn.
2. Explicit ``finish``: still addressed to the Architect, no needless Architect call; ``continue``: a second question
   without any Owner turn.
3. Worker-originated ``@Architect`` consultation after an Owner-authorized Worker chat turn (fake report fixture):
   review -> correction request -> fixed -> verified -> report to the Owner.  No Worker Run, no Task change.
4. Spent authorization: no re-invitation after a natural stop / exhaustion (repeated @name, old Owner request, restart,
   stale round); only a new Owner group message or explicit Renew gives exactly one new budget; ``@Owner`` does not;
   an UNKNOWN prior call is neither destroyed nor replayed.
5. Preserved: Owner -> one Agent direct answer, legacy queued turns, PC/TC isolation (scenario 1 runs in both).
"""
from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from test_p043b_worker_conversation_core import OWNER
from test_p047_owner_qa_corrections import GatedRunner
from test_p052_agent_mentions_quota import FIN, P052Case, envelope
from test_p052_r03_lifecycle_diagnostics import TraceMixin

from rosc.worker_conversation import (CONTROL_ABSENT, CONTROL_EXPLICIT, CONTROL_INVALID, INTENT_CONTINUE, INTENT_FINISH,
                                      WorkerConversationCore, parse_reply_control)

CONT = '\n<rosc-control next="continue"/>'


class ControlContractTests(unittest.TestCase):
    """The one provider-neutral continuation control (parsed by the Core for every adapter)."""

    def test_explicit_finish_and_continue_are_removed_from_the_visible_body(self) -> None:
        self.assertEqual(parse_reply_control("Готово." + FIN), ("Готово.", INTENT_FINISH, CONTROL_EXPLICIT))
        self.assertEqual(parse_reply_control("А мобильная?" + CONT + "\n\n"), ("А мобильная?", INTENT_CONTINUE,
                                                                               CONTROL_EXPLICIT))
        self.assertEqual(parse_reply_control('ok\n  <ROSC-CONTROL next="FINISH" />  ').intent, INTENT_FINISH)

    def test_legacy_plaintext_means_continue_and_is_unchanged(self) -> None:
        self.assertEqual(parse_reply_control("просто ответ"), ("просто ответ", INTENT_CONTINUE, CONTROL_ABSENT))

    def test_only_the_last_line_is_control_quoted_examples_are_content(self) -> None:
        text = 'Пример: <rosc-control next="finish"/>\nи дальше текст'
        self.assertEqual(parse_reply_control(text), (text, INTENT_CONTINUE, CONTROL_ABSENT))

    def test_malformed_control_is_removed_and_fails_safe_as_finish(self) -> None:
        for bad in ('<rosc-control next="maybe"/>', '<rosc-control/>', '<rosc-control next=finish>',
                    '</rosc-control>', '<rosc-control next="finish" to="Claude"/>'):
            with self.subTest(bad):
                self.assertEqual(parse_reply_control("текст\n" + bad), ("текст", INTENT_FINISH, CONTROL_INVALID))

    def test_control_only_reply_keeps_its_text_and_finishes(self) -> None:
        self.assertEqual(parse_reply_control(FIN.strip()), (FIN.strip(), INTENT_FINISH, CONTROL_INVALID))

    def test_legacy_captured_list_is_continue(self) -> None:
        class Row:
            captured_to = '["owner"]'
        self.assertEqual(WorkerConversationCore._captured(Row()), (["owner"], INTENT_CONTINUE, CONTROL_ABSENT))  # noqa: SLF001


class R04Case(P052Case):
    def agent_messages(self, task: str | None = None) -> list[dict]:
        return [m for m in self.messages(task) if m["sender"]["kind"] != "owner"]

    def room_id(self, task: str | None = None) -> str:
        return self.status(task)["conversation_id"]

    def run_counts(self) -> tuple:
        return (self.fetch("SELECT count(*) FROM run_records")[0][0], self.fetch("SELECT count(*) FROM worker_sessions")[0][0],
                tuple(self.fetch("SELECT id, status FROM tasks ORDER BY id")))


class Scenario1RequesterRoutingTests(R04Case):
    """Owner DIRECT -> Architect; Architect <-> Claude without further @mentions, up to 3+3."""

    def walk(self, task: str | None = None, *, room_route: bool = False) -> None:
        self.arch_runtime.replies = ["@Claude какой отступ у карточек?", "А на мобильной ширине?",
                                     "Итог: берём 16px?"]
        self.runner.replies = ["16px на десктопе.", "На 360px тоже 16px.", "Да, 16px везде."]
        if room_route:
            sent = self.post("Architect, выясни у Claude отступы", [self.arch], task=task)
            self.assertEqual(sent.json()["dispatch"]["mode"], "direct")
            self.settle(task)
        else:
            self.direct("Architect, выясни у Claude отступы", task=task)

    def assert_walk(self, task: str | None = None) -> None:
        msgs = self.messages(task)
        owner = msgs[-7]
        agents = msgs[-6:]
        self.assertEqual(owner["sender"]["kind"], "owner")
        self.assertEqual([m["sender"]["agent_id"] for m in agents], [self.arch, self.claude_agent] * 3)
        # No model text needed an @mention after the invitation: every reply goes to its requester, never the Owner,
        # and answers exactly the message that triggered it.
        expected_to = [[self.claude_agent], [self.arch], [self.claude_agent], [self.arch], [self.claude_agent],
                       [self.arch]]
        self.assertEqual([m["group"]["to"] for m in agents], expected_to)
        self.assertEqual(agents[0]["group"]["reply_to"], owner["message_id"])
        for previous, current in zip(agents, agents[1:]):
            self.assertEqual(current["group"]["reply_to"], previous["message_id"])
            self.assertEqual(current["group"]["reply_to_ordinal"], previous["ordinal"])
        self.assertTrue(all("rosc-control" not in m["body"] for m in agents))
        # Hard ceiling 3+3: exactly 3 Claude processes, 3 Architect calls, no 7th invocation.
        self.assertEqual(len(self.runner.calls), 3)
        self.assertEqual(len(self.arch_texts()), 3)
        self.assertEqual(self.grants(task), {self.arch: (3, 3), self.claude_agent: (3, 3)})
        status = self.status(task)
        self.assertEqual(status["round"]["state"], "awaiting_owner")
        self.assertEqual(status["stop_reason"], "quota_exhausted")
        self.assertEqual([(u["agent_id"], u["reason"]) for u in status["unanswered"]], [(self.arch, "quota_exhausted")])
        self.assertFalse(status["round_active"])

    def assert_snapshots(self) -> None:
        # Claude turns: (published before, remaining after this reply, Architect remaining, Architect can answer).
        msgs = self.agent_messages()
        for i, (call, expect) in enumerate(zip(self.runner.calls, [(0, 2, 2, True), (1, 1, 1, True), (2, 0, 0, False)])):
            quota = envelope(call["prompt"])["quota"]
            self.assertEqual((quota["you"]["published"], quota["you"]["remaining_after_this_reply"]), expect[:2])
            arch = next(p for p in quota["peers"] if p["agent_id"] == self.arch)
            self.assertEqual((arch["remaining"], arch["can_reply_after_your_message"]), expect[2:])
            reply = quota["reply"]
            self.assertEqual(reply["default_to"], self.arch)
            self.assertEqual(reply["reply_to_ordinal"], msgs[2 * i]["ordinal"])
            self.assertEqual(reply["default_to_can_answer"], expect[3])
            self.assertIn("You are answering @Architect", call["prompt"])
            self.assertIn("rosc-control", call["prompt"])
            self.assertEqual(quota["remaining_total_after_this_reply"], quota["remaining_total_before_this_reply"] - 1)
        # Architect group turns 2 and 3: (published before, after), default recipient Claude who can still answer.
        for text, (published, after, claude_left) in zip(self.arch_texts()[1:], [(1, 1, 2), (2, 0, 1)]):
            quota = envelope(text)["quota"]
            self.assertEqual((quota["you"]["published"], quota["you"]["remaining_after_this_reply"]), (published, after))
            self.assertEqual(quota["reply"]["default_to"], self.claude_agent)
            self.assertEqual(quota["reply"]["default_to_remaining"], claude_left)
            self.assertTrue(quota["reply"]["default_to_can_answer"])
            self.assertIn("You are answering @Claude", text)

    def test_legacy_direct_architect_project_conversation(self) -> None:
        self.walk()
        self.assert_walk()
        self.assert_snapshots()
        # The initiating message spent 1/3 on the Architect; the Owner's message is the durable provenance.
        self.assertEqual(self.status()["round"]["authorizing_message_id"], self.messages()[-7]["message_id"])

    def test_legacy_direct_architect_task_conversation_is_isolated(self) -> None:
        self.walk(task="T0001")
        self.assert_walk("T0001")
        self.assertIsNone(self.status()["conversation_id"])  # nothing leaked into the Project room

    def test_room_route_owner_direct_to_architect(self) -> None:
        self.walk(room_route=True)
        self.assert_walk()
        self.assert_snapshots()


class Scenario2FinishContinueTests(R04Case):
    def test_explicit_finish_stays_addressed_to_the_architect_and_calls_nobody(self) -> None:
        self.arch_runtime.replies = ["@Claude проверь, пожалуйста, ширину колонки"]
        self.runner.replies = ["Проверил: 320px, всё сходится." + FIN]
        self.direct("Architect, проверь колонку через Claude")
        final = self.agent_messages()[-1]
        self.assertEqual(final["body"], "Проверил: 320px, всё сходится.")  # the Owner sees the final result
        self.assertEqual(final["group"]["to"], [self.arch])
        self.assertEqual(final["group"]["reply_to"], self.agent_messages()[0]["message_id"])
        self.assertEqual(final["group"]["next"], "finish")
        self.assertEqual(len(self.arch_texts()), 1)  # no needless Architect call
        self.assertEqual(len(self.runner.calls), 1)
        status = self.status()
        self.assertEqual((status["round"]["state"], status["stop_reason"], status["unanswered"]),
                         ("awaiting_owner", "finished", []))
        self.assertEqual(self.grants(), {self.arch: (1, 3), self.claude_agent: (1, 3)})

    def test_continue_lets_the_architect_ask_a_second_question_without_the_owner(self) -> None:
        self.arch_runtime.replies = ["@Claude какая ширина колонки?", "А на планшете?" + CONT, "Спасибо, принято." + FIN]
        self.runner.replies = ["320px." + CONT, "На планшете 480px."]  # second: legacy plaintext = continue
        self.direct("Architect, выясни ширину")
        owner_messages = [m for m in self.messages() if m["sender"]["kind"] == "owner"]
        self.assertEqual(len(owner_messages), 1)  # the Owner took no turn
        agents = self.agent_messages()
        self.assertEqual([m["sender"]["agent_id"] for m in agents], [self.arch, self.claude_agent] * 2 + [self.arch])
        self.assertEqual(agents[2]["body"], "А на планшете?")
        self.assertEqual(agents[2]["group"]["to"], [self.claude_agent])  # default to the requester, no @mention
        self.assertEqual(agents[-1]["group"]["to"], [self.claude_agent])
        self.assertEqual(agents[-1]["group"]["next"], "finish")
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(len(self.arch_texts()), 3)
        self.assertEqual(self.status()["stop_reason"], "finished")

    def test_explicit_owner_redirect_ends_the_peer_chain(self) -> None:
        self.arch_runtime.replies = ["@Claude сколько тестов?"]
        self.runner.replies = ["@Owner 42 теста, всё зелёное."]
        self.direct("Architect, узнай про тесты")
        self.assertEqual(self.agent_messages()[-1]["group"]["to"], ["owner"])  # an explicit @name redirects
        self.assertEqual(len(self.arch_texts()), 1)
        self.assertEqual(self.status()["stop_reason"], "natural")


class Scenario3WorkerConsultationTests(R04Case):
    """Owner observes a Worker-originated review/correction loop; group-chat acceptance only (no real Task Run)."""

    def test_worker_report_review_correction_and_verified_report_to_owner(self) -> None:
        root = Path(self.fetch("SELECT root_path FROM project_root_bindings ORDER BY version DESC LIMIT 1")[0][0])
        report = root / "fake-report-p052r04.md"
        report.write_text("# Report\n\n## Summary\nok\n")  # broken: the approved template requires ## Validation
        self.addCleanup(lambda: report.unlink(missing_ok=True))
        before = self.run_counts()

        def architect_reviews() -> None:  # the fake Architect "reads" the fixture and decides
            ok = "## Validation" in report.read_text()
            self.arch_runtime.reply = ("@Owner отчёт проверен: раздел Validation на месте, всё в порядке." + FIN if ok
                                       else "В отчёте нет раздела ## Validation. Добавь его (в рамках задачи)." + CONT)
        self.arch_runtime.on_ask = architect_reviews
        original = self.runner.spawn

        def worker(argv, **kw):
            if len(self.runner.calls) == 1:  # the Architect's correction request: the Worker fixes the fixture
                report.write_text(report.read_text() + "\n## Validation\npassed\n")
            return original(argv, **kw)
        self.runner.spawn = worker
        self.runner.replies = [f"Готово, отчет лежит {report.name}. @Architect проверь, пожалуйста.",
                               "Исправил: добавил раздел Validation."]
        sent = self.post("Claude, подготовь отчёт", [self.claude_agent])
        self.assertEqual(sent.json()["dispatch"]["mode"], "direct")
        self.settle()
        msgs = self.messages()
        self.assertEqual([m["sender"]["kind"] for m in msgs], ["owner", "worker", "architect", "worker", "architect"])
        owner, w1, a1, w2, a2 = msgs
        self.assertEqual(w1["group"]["to"], [self.arch])
        self.assertEqual(a1["group"]["to"], [self.claude_agent])  # correction goes back to the Worker by default
        self.assertEqual(a1["group"]["reply_to"], w1["message_id"])
        self.assertEqual(w2["group"]["to"], [self.arch])
        self.assertEqual(w2["group"]["reply_to"], a1["message_id"])
        self.assertEqual(a2["group"]["to"], ["owner"])  # verified report to the Owner (explicit redirect) ...
        self.assertEqual(a2["group"]["next"], "finish")  # ... and the discussion ends
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(len(self.arch_texts()), 2)
        self.assertIn("## Validation", report.read_text())
        self.assertEqual(self.status()["stop_reason"], "finished")
        self.assertEqual(self.grants(), {self.claude_agent: (2, 3), self.arch: (2, 3)})
        # Chat quota is dialogue only: no Worker Run, no worker session, no Task acceptance/state change.
        self.assertEqual(self.run_counts(), before)
        self.assertIn("never start a Worker Run", self.arch_texts()[0])


class Scenario4SpentAuthorizationTests(R04Case):
    def finish_round(self) -> dict:
        self.arch_runtime.replies = ["@Claude проверь"]
        self.runner.replies = ["Проверено." + FIN]
        self.direct("старт")
        return self.status()

    def delayed_reply(self, core: WorkerConversationCore, owner_message_id: str, body: str) -> dict | None:
        """A late/duplicate authenticated Architect reply to an OLD Owner message, committed through the real bridge."""
        from rosc.models import ConversationMessage, ConversationParticipant
        with self.app.state.group._sessions() as session:  # noqa: SLF001
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            owner = session.get(ConversationMessage, owner_message_id)
            participant = session.query(ConversationParticipant).filter_by(
                conversation_id=owner.conversation_id, kind="architect").first()
            ordinal = max(m.ordinal for m in session.query(ConversationMessage).filter_by(
                conversation_id=owner.conversation_id)) + 1
            reply = ConversationMessage(message_id=str(uuid.uuid4()), conversation_id=owner.conversation_id,
                                        sender_participant_id=participant.participant_id, ordinal=ordinal, body=body,
                                        client_key="late-" + uuid.uuid4().hex[:12])
            session.add(reply)
            session.flush()
            result = core.bridge_direct_reply(session, owner_message=owner, reply=reply, agent_id=self.arch,
                                              actor_id=OWNER.actor_id, actor_assurance=OWNER.assurance)
            session.commit()
        return result

    def test_old_owner_request_cannot_be_reused_even_after_restart(self) -> None:
        first = self.finish_round()
        owner_id = self.messages()[0]["message_id"]
        rounds = self.fetch("SELECT count(*) FROM group_rounds")
        result = self.delayed_reply(self.app.state.group._core, owner_id, "@Claude ещё раз!")  # noqa: SLF001
        self.assertEqual(result["not_invited"], "authorization_spent")
        self.assertIsNone(result["round_id"])
        # Backend restart: a brand-new process composition over the same database still refuses.
        self.api = self.make_client()
        self.app.state.architect.drain()
        result = self.delayed_reply(self.app.state.group._core, owner_id, "@Claude и снова!")  # noqa: SLF001
        self.assertEqual(result["not_invited"], "authorization_spent")
        self.app.state.group.recover_all()
        self.app.state.group.wake(self.room_id())
        self.wait_room()
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds"), rounds)
        self.assertEqual(self.status()["round"]["round_id"], first["round"]["round_id"])
        self.assertEqual(len(self.runner.calls), 1)  # no provider invocation

    def test_exhausted_round_repeated_at_name_owner_note_and_renewal(self) -> None:
        self.arch_runtime.replies = ["@Claude вопрос", "@Claude ещё", "@Claude и ещё"]
        self.runner.replies = ["@Architect ответ 1", "@Architect ответ 2", "@Architect ответ 3, ответь!"]
        self.direct("старт")
        self.assertEqual(self.status()["stop_reason"], "quota_exhausted")
        calls = (len(self.runner.calls), len(self.arch_texts()))
        rounds = self.fetch("SELECT count(*) FROM group_rounds")[0][0]
        # The Agent repeated @Architect; recovery + wake still invoke nobody (restart: see the test above).
        self.app.state.group.recover_all()
        self.app.state.group.wake(self.room_id())
        self.wait_room()
        self.assertEqual((len(self.runner.calls), len(self.arch_texts())), calls)
        # An @Owner note is stored but refills nothing.
        note = self.post("@Owner заметка для себя", [])
        self.assertEqual(note.json()["dispatch"]["mode"], "none")
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds")[0][0], rounds)
        self.assertEqual((len(self.runner.calls), len(self.arch_texts())), calls)
        # A NEW Owner message addressed into the group discussion: exactly one fresh budget.
        self.arch_runtime.replies = ["Принято." + FIN]
        self.runner.replies = ["Понял." + FIN]
        sent = self.post("Оба: подведите итог", [self.arch, self.claude_agent])
        self.assertEqual(sent.json()["dispatch"]["mode"], "group")
        self.settle()
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds")[0][0], rounds + 1)
        self.assertEqual(self.grants(), {self.arch: (1, 3), self.claude_agent: (1, 3)})
        self.assertEqual((len(self.runner.calls), len(self.arch_texts())), (calls[0] + 1, calls[1] + 1))

    def test_explicit_owner_renew_gives_one_new_budget(self) -> None:
        self.arch_runtime.replies = ["@Claude вопрос", "@Claude ещё", "@Claude и ещё"]
        self.runner.replies = ["ответ 1", "ответ 2", "ответ 3"]
        self.direct("старт")
        self.assertEqual(self.status()["stop_reason"], "quota_exhausted")
        self.arch_runtime.replies = ["Итог принят." + FIN]
        self.assertEqual(self.api.post(f"{self.url()}/continue").status_code, 200)
        self.settle()
        self.assertEqual(self.grants(), {self.arch: (1, 3), self.claude_agent: (0, 3)})
        self.assertEqual(self.api.post(f"{self.url()}/continue").status_code, 409)  # never self-renews
        self.assertEqual(len(self.runner.calls), 3)

    def test_stale_round_message_never_earns_a_turn_in_the_current_round(self) -> None:
        self.finish_round()
        room = self.room_id()
        core = self.app.state.group._core  # noqa: SLF001
        old_round = self.status()["round"]["round_id"]
        self.arch_runtime.replies = ["Ок." + FIN]
        self.runner.replies = ["Ок." + FIN]
        self.post("Оба, коротко", [self.arch, self.claude_agent])
        self.settle()
        current = self.status()["round"]["round_id"]
        # A late Architect message charged to the OLD round that addresses Claude (with continue) ...
        from rosc.models import ConversationMessage, ConversationParticipant, GroupRound, MessageEnvelope, MessageParty
        with self.app.state.group._sessions() as session:  # noqa: SLF001
            participant = session.query(ConversationParticipant).filter_by(conversation_id=room, kind="architect").first()
            ordinal = max(m.ordinal for m in session.query(ConversationMessage).filter_by(conversation_id=room)) + 1
            late = ConversationMessage(message_id=str(uuid.uuid4()), conversation_id=room, ordinal=ordinal,
                                       sender_participant_id=participant.participant_id, body="@Claude поздний вопрос",
                                       client_key="late-stale-0001")
            session.add(late)
            session.flush()
            session.add(MessageEnvelope(message_id=late.message_id, reply_to_message_id=None, round_id=old_round,
                                        turn_id=None, sender_agent_id=self.arch))
            session.add(MessageParty(message_id=late.message_id, role="to", party_ref=self.claude_agent,
                                     party_kind="worker", agent_id=self.claude_agent))
            session.commit()
            rnd = session.get(GroupRound, current)
            eligible, refused = core._next_pass_triggers(session, rnd)  # noqa: SLF001
        self.assertNotIn(self.claude_agent, eligible)  # ... never earns a turn in the current round
        self.assertEqual(refused[self.claude_agent], "not_addressed")

    def test_unknown_prior_call_is_neither_destroyed_nor_replayed_by_owner_intervention(self) -> None:
        self.runner.modes = ["crash_no_result"]
        self.post("Claude, вопрос", [self.claude_agent])
        self.settle()
        held = self.status()["holding_turn"]
        self.assertEqual(held["state"], "unknown")
        calls = len(self.runner.calls)
        sent = self.post("Claude и Architect, новый вопрос", [self.claude_agent, self.arch])
        self.assertEqual(sent.json()["dispatch"]["blocked"], "unknown_turn")  # held, visible reason, nothing sent
        self.settle()
        self.assertEqual(self.fetch("SELECT state FROM group_turns WHERE turn_id = ?", held["turn_id"]), [("unknown",)])
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(self.arch_texts(), [])


class Scenario5PreservedBehaviorTests(R04Case):
    def test_owner_direct_to_worker_gets_one_answer_addressed_to_the_owner(self) -> None:
        self.runner.replies = ["Ответ Owner."]
        sent = self.post("Claude, вопрос", [self.claude_agent])
        self.settle()
        reply = self.agent_messages()[-1]
        self.assertEqual(reply["group"]["to"], ["owner"])
        self.assertEqual(reply["group"]["reply_to"], sent.json()["message"]["message_id"])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.arch_texts(), [])
        self.assertEqual(self.grants(), {self.claude_agent: (1, 1)})

    def test_legacy_queued_turn_without_causal_trigger_keeps_the_old_owner_default(self) -> None:
        self.runner.replies = ["ok"]
        with patch.object(self.app.state.group, "_kick", return_value=False):
            self.post("Claude, вопрос", [self.claude_agent])
        self.fetch_exec("UPDATE group_turns SET captured_reply_to = NULL")  # a turn queued before r04
        self.assertEqual(self.api.post(f"{self.url()}/start").status_code, 200)
        self.settle()
        reply = self.agent_messages()[-1]
        self.assertEqual((reply["group"]["to"], reply["group"]["reply_to"]), (["owner"], None))

    def test_legacy_captured_list_row_publishes_unchanged(self) -> None:
        self.runner.replies = ["ok"]
        gate = GatedRunner()
        gate.replies = ["ok"]
        self.runner = gate
        self.post("Claude, вопрос", [self.claude_agent])
        self.assertTrue(gate.entered.wait(5))
        gate.release.set()
        self.settle()
        turn = self.fetch("SELECT turn_id, captured_to FROM group_turns WHERE state = 'published'")[0]
        self.assertEqual(json.loads(turn[1])["next"], "continue")
        self.fetch_exec("UPDATE group_turns SET captured_to = '[\"owner\"]' WHERE turn_id = ?", turn[0])
        self.assertEqual(self.agent_messages()[-1]["group"]["next"], "continue")  # legacy list row reads as continue


class DiagnosticsR04Tests(TraceMixin, R04Case):
    def test_metadata_records_recipient_trigger_intent_and_stop_without_bodies(self) -> None:
        self.set_diag(enabled=True)
        self.arch_runtime.replies = ["@Claude SECRET-Q-17 проверь"]
        self.runner.replies = ["SECRET-A-23 проверено." + FIN]
        self.direct("SECRET-OWNER-5 старт")
        events = self.trace_events()
        raw = json.dumps(events, ensure_ascii=False)
        for sentinel in ("SECRET-Q-17", "SECRET-A-23", "SECRET-OWNER-5"):
            self.assertNotIn(sentinel, raw)
        question = self.agent_messages()[0]
        captured = [e for e in events if e["event"] == "reply_captured"][-1]
        self.assertEqual((captured["to"], captured["reply_to_id"], captured["intent"], captured["intent_source"],
                          captured["to_source"]), ([self.arch], question["message_id"], "finish", "explicit", "requester"))
        decided = [e for e in events if e["event"] == "next_turn_decided"][-1]
        self.assertEqual(decided["next_turn"], [])
        self.assertIn({"agent_id": self.arch, "reason": "finish_requested"}, decided["declined"])
        stop = [e for e in events if e["event"] == "round_awaiting_owner"][-1]
        self.assertEqual(stop["reason"], "finish_requested")
        dispatched = [e for e in events if e["event"] == "dispatch_attempted"][-1]
        self.assertEqual(dispatched["quota_snapshot"]["reply"]["default_to"], self.arch)
        # A refused reuse of the spent Owner authorization is traced with its reason.
        Scenario4SpentAuthorizationTests.delayed_reply(self, self.app.state.group._core,  # noqa: SLF001
                                                       self.messages()[0]["message_id"], "@Claude снова")
        checked = [e for e in self.trace_events() if e["event"] == "invitation_checked"][-1]
        self.assertEqual((checked["decision"], checked["reason"]), ("declined", "authorization_spent"))


if __name__ == "__main__":
    unittest.main()
