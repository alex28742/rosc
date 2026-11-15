"""P052: Agent-initiated @mentions + quota-aware bounded group conversation (Project / Task Conversation).

Isolated temporary databases, the deterministic fake Claude CLI runner (P045/P047) and the fake Architect runtime
(P015/P047).  No real CLI, provider, tmux, network or Owner data is touched; every "provider call" is a fake.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from test_p015_architect_activation import CAPS as ARCH_CAPS
from test_p043b_worker_conversation_core import OWNER, CoreCase, FakeTransport
from test_p046_group_conversation_bridge import AgentProvider
from test_p047_owner_qa_corrections import NativeFakeRuntime, P047Case

from rosc.mentions import find_mentions
from rosc.worker_conversation import (GROUP_REPLY_CONTINUE_MAX_CHARS, SUBMIT_NOT_ACCEPTED, SubmitOutcome)


class ScriptedArchitect(NativeFakeRuntime):
    """Fake Architect runtime answering from ``replies`` in order (direct AND group turns share it)."""

    def __init__(self) -> None:
        super().__init__()
        self.replies: list[str] = []

    def ask(self, handle, runtime_message_id, text, timeout):
        if self.replies:
            self.reply = self.replies.pop(0)
        return super().ask(handle, runtime_message_id, text, timeout)


def envelope(text: str) -> dict:
    lines = text.splitlines()
    return json.loads(lines[lines.index("ROSC ENVELOPE (trusted):") + 1])


class MentionParserTests(unittest.TestCase):
    NAMES = {"a": "Claude", "b": "Claude-2", "c": "Claude Opus", "d": "Ärchi", "owner": "Owner"}

    def refs(self, text: str, names=None) -> tuple:
        return find_mentions(text, names or self.NAMES).refs

    def test_exact_boundaries_longest_match_and_unicode_casing(self) -> None:
        self.assertEqual(self.refs("@Claude, посмотри"), ("a",))
        self.assertEqual(self.refs("@claude-2 и @CLAUDE"), ("b", "a"))
        self.assertEqual(self.refs("@Claude Opus ответь"), ("c",))  # never also @Claude
        self.assertEqual(self.refs("@Claudette"), ())  # no prefix guess
        self.assertEqual(self.refs("@Claude-3"), ())  # '-' continues a name
        self.assertEqual(self.refs("mail a@Claude"), ())  # e-mail, not a mention
        self.assertEqual(self.refs("@äRCHI!"), ("d",))
        self.assertEqual(self.refs("@Owner, done"), ("owner",))
        self.assertEqual(self.refs("no mention here"), ())

    def test_duplicate_display_names_are_ambiguous_and_address_nobody(self) -> None:
        result = find_mentions("@claude hi", {"a": "Claude", "z": "CLAUDE"})
        self.assertEqual(result.refs, ())
        self.assertEqual(result.ambiguous, ("claude",))


class P052Case(P047Case):
    def make_client(self, **kw):
        self.arch_runtime = ScriptedArchitect()
        return super().make_client(architect_runtime=self.arch_runtime, **kw)

    def setUp(self) -> None:
        super().setUp()
        architect = self.registry.register_agent(OWNER, display_name="Architect", runtime_adapter="opencode",
                                                 provider_id="prov", model_id="model-a", capabilities=ARCH_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=architect["agent_id"], role="architect",
                                        capabilities=ARCH_CAPS)
        self.arch = architect["agent_id"]
        self.assertEqual(self.api.post("/api/projects/P0001/architect/activate").json()["state"], "ready")
        self.app.state.architect.drain()
        self.arch_runtime.asked.clear()

    # -- helpers ------------------------------------------------------------------------------------------------
    def direct(self, body: str, key: str | None = None, task: str | None = None):
        """Owner -> Architect over the established legacy direct path (PC: create = deliver; TC: save + send)."""
        if task is None:
            sent = self.api.post("/api/projects/P0001/conversation/messages",
                                 json={"client_key": key or self.key(), "body": body})
        else:
            sent = self.api.post(f"/api/projects/P0001/tasks/{task}/conversation/messages",
                                 json={"client_key": key or self.key(), "body": body, "recipient": "architect"})
            self.assertIn(sent.status_code, (200, 201), sent.text)
            self.api.post(f"/api/projects/P0001/tasks/{task}/conversation/messages/"
                          f"{sent.json()['message']['message_id']}/send")
        self.assertIn(sent.status_code, (200, 201), sent.text)
        self.settle(task)
        return sent

    def settle(self, task: str | None = None) -> None:
        self.app.state.architect.drain()
        self.wait_room(task)
        self.app.state.architect.drain()

    def status(self, task: str | None = None) -> dict:
        return self.api.get(self.url(task)).json()

    def grants(self, task: str | None = None) -> dict:
        return {a: (g["published"], g["quota"]) for a, g in self.status(task)["round"]["grants"].items()}

    def arch_texts(self) -> list[str]:
        return [text for _rid, text in self.arch_runtime.asked]


class SixTurnWalkthroughTests(P052Case):
    def run_six(self, task: str | None = None) -> None:
        self.arch_runtime.replies = ["Вариант B. @Claude, проверь отступы?", "@Claude а мобильная ширина?",
                                     "@Claude итог: берём B, ок?"]
        self.runner.replies = ["@Architect проверил: 16px. Что с мобильной?", "@Architect на 360px всё влезает.",
                               "@Architect согласен, B. Вопросов нет."]
        self.direct("Architect, спроси @Claude про отступы и реши", task=task)

    def test_owner_direct_to_architect_six_turns_exact_quota_and_no_seventh_request(self) -> None:
        self.run_six()
        msgs = self.messages()
        agents = [m for m in msgs if m["sender"]["kind"] != "owner"]
        self.assertEqual([m["sender"]["agent_id"] for m in agents], [self.arch, self.claude_agent] * 3)
        self.assertEqual(len(self.arch_texts()), 3)  # 1 direct + 2 group turns
        self.assertEqual(len(self.runner.calls), 3)  # no 7th request
        self.assertEqual(self.grants(), {self.arch: (3, 3), self.claude_agent: (3, 3)})
        # The first (direct) Architect reply got its structured addressee and counts as 1/3.
        self.assertEqual(agents[0]["group"]["to"], [self.claude_agent])
        final = agents[-1]
        self.assertEqual(final["group"]["to"], [self.arch])  # stored with its addressee ...
        status = self.status()
        self.assertEqual(status["round"]["state"], "awaiting_owner")
        self.assertEqual(status["stop_reason"], "quota_exhausted")
        self.assertEqual([(u["agent_id"], u["reason"]) for u in status["unanswered"]], [(self.arch, "quota_exhausted")])
        self.assertEqual(status["engagement"], "group")
        # ... and the round's durable provenance is the Owner's direct message.
        owner = msgs[0]
        self.assertEqual(status["round"]["authorizing_message_id"], owner["message_id"])
        self.assertEqual(self.fetch("SELECT authorized_by FROM group_rounds")[0][0], OWNER.actor_id)

    def test_every_turn_sees_truthful_before_and_after_counts(self) -> None:
        self.run_six()
        direct_note = self.arch_texts()[0]
        self.assertIn("ROSC QUOTA (trusted", direct_note)
        self.assertIn("@Claude", direct_note)
        self.assertIn("each participant gets 3 replies and this reply counts as your first (2 left after it)",
                      direct_note)
        self.assertTrue(direct_note.rstrip().endswith("Architect, спроси @Claude про отступы и реши"))
        expected_claude = [(0, 3, 2, 2), (1, 2, 1, 1), (2, 1, 0, 0)]  # (published, remaining, after, peer remaining)
        for call, (published, remaining, after, peer) in zip(self.runner.calls, expected_claude):
            quota = envelope(call["prompt"])["quota"]
            self.assertEqual(quota["you"]["published"], published)
            self.assertEqual(quota["you"]["remaining"], remaining)
            self.assertEqual(quota["you"]["remaining_after_this_reply"], after)
            arch = next(p for p in quota["peers"] if p["agent_id"] == self.arch)
            self.assertEqual(arch["display_name"], "Architect")
            self.assertEqual(arch["remaining"], peer)
            self.assertEqual(arch["can_reply_after_your_message"], peer > 0)
            self.assertEqual(quota["remaining_total_after_this_reply"], quota["remaining_total_before_this_reply"] - 1)
        last = self.runner.calls[2]["prompt"]
        self.assertIn("@Architect cannot answer after your message (quota_exhausted)", last)
        self.assertIn("give your final answer", last)
        for text, (published, after) in zip(self.arch_texts()[1:], [(1, 1), (2, 0)]):
            quota = envelope(text)["quota"]
            self.assertEqual((quota["you"]["published"], quota["you"]["remaining_after_this_reply"]), (published, after))

    def test_task_conversation_direct_architect_reply_starts_the_same_bounded_discussion(self) -> None:
        self.run_six(task="T0001")
        self.assertEqual(self.grants("T0001"), {self.arch: (3, 3), self.claude_agent: (3, 3)})
        self.assertEqual(len(self.runner.calls), 3)
        self.assertIn("now in the separate Task Conversation of T0001", self.arch_texts()[0])  # first Task turn preface
        self.assertIn("ROSC QUOTA (trusted", self.arch_texts()[0])
        self.assertIsNone(self.status()["conversation_id"])  # nothing leaked into the Project room

    def test_owner_continue_delivers_the_unanswered_final_message_once(self) -> None:
        self.run_six()
        final = self.messages()[-1]["body"]
        self.arch_runtime.replies = ["Принято, B утверждён."]
        cont = self.api.post(f"{self.url()}/continue")
        self.assertEqual(cont.status_code, 200, cont.text)
        self.settle()
        self.assertEqual(len(self.arch_texts()), 4)
        self.assertIn(final, self.arch_texts()[3])
        self.assertEqual(len(self.runner.calls), 3)  # Claude was not addressed again: no turn
        status = self.status()
        self.assertEqual(status["stop_reason"], "natural")
        self.assertEqual(status["round"]["grants"][self.arch]["published"], 1)
        self.assertEqual(self.api.post(f"{self.url()}/continue").status_code, 409)  # never self-renews
        bodies = [m["body"] for m in self.messages()]
        self.assertEqual(bodies.count(final), 1)  # the old message was not re-published

    def test_new_owner_direct_message_carries_the_unseen_final_once_then_never_again(self) -> None:
        self.run_six()
        final = self.messages()[-1]["body"]
        self.arch_runtime.replies = ["Ок, фиксирую B.", "Готово."]
        self.direct("Итог?")
        self.assertIn(final, self.arch_texts()[3])
        self.assertIn("ROSC ROOM EVENTS you have not seen yet", self.arch_texts()[3])
        self.direct("Ещё что-то?")
        self.assertNotIn(final, self.arch_texts()[4])
        self.assertEqual(len(self.runner.calls), 3)

    def test_agent_actor_cannot_continue(self) -> None:
        self.run_six()
        agent_api = self.make_client(auth_provider=AgentProvider())
        self.assertEqual(agent_api.post(f"{self.url()}/continue").status_code, 403)


class InvitationRulesTests(P052Case):
    def test_legacy_direct_reply_without_peer_mention_is_unchanged(self) -> None:
        self.arch_runtime.replies = ["Готово, Owner."]
        self.direct("Просто вопрос")
        msgs = self.messages()
        self.assertIsNone(msgs[-1].get("group"))  # legacy shape, no envelope
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds"), [(0,)])
        self.assertEqual(len(self.runner.calls), 0)

    def test_no_peer_in_scope_keeps_the_exact_legacy_owner_text(self) -> None:
        self.fetch_exec("UPDATE role_assignments SET state = 'revoked', revoked_at = '2026-01-01 00:00:00' WHERE role = 'worker'")
        self.arch_runtime.replies = ["ok"]
        self.direct("Только текст")
        self.assertEqual(self.arch_texts()[0], "Только текст")

    def test_owner_to_worker_direct_then_at_architect_invites_once(self) -> None:
        self.runner.replies = ["@Architect посмотри, пожалуйста, вариант A."]
        self.arch_runtime.replies = ["Вариант A подходит."]  # no mention -> natural stop
        sent = self.post("Claude, как лучше?", [self.claude_agent])
        self.assertEqual(sent.json()["dispatch"]["mode"], "direct")
        self.settle()
        self.assertEqual(self.grants(), {self.claude_agent: (1, 3), self.arch: (1, 3)})
        self.assertEqual(len(self.arch_texts()), 1)
        quota = envelope(self.arch_texts()[0])["quota"]
        self.assertEqual(quota["you"]["remaining_after_this_reply"], 2)
        status = self.status()
        self.assertEqual(status["stop_reason"], "natural")
        self.assertEqual(status["unanswered"], [])
        invite = envelope(self.runner.calls[0]["prompt"])["quota"]["invitation"]
        self.assertTrue({"Architect", "Claude-2"} <= {a["display_name"] for a in invite["agents"]})
        self.assertNotIn("Codex", {a["display_name"] for a in invite["agents"]})  # unsupported: not invitable
        self.assertEqual(invite["your_remaining_after_this_reply_if_you_invite"], 2)

    def test_direct_reply_inviting_two_workers_opens_one_three_agent_discussion(self) -> None:
        self.arch_runtime.replies = ["@Claude и @Claude-2, сравните варианты."]
        self.runner.replies = ["Вариант A.", "Вариант B."]  # both answer the Owner: natural stop
        self.direct("Спроси обоих")
        self.assertEqual(self.grants(), {self.arch: (1, 3), self.claude_agent: (1, 3), self.second: (1, 3)})
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds"), [(1,)])
        self.assertEqual(self.status()["stop_reason"], "natural")

    def test_released_member_is_never_re_invited_by_an_agent(self) -> None:
        self.arch_runtime.replies = ["@Claude проверь", "@Claude ещё раз"]
        self.runner.replies = ["ok"]
        self.direct("первый")
        self.assertEqual(self.api.post(f"{self.url()}/members/{self.claude_agent}/release").status_code, 200)
        self.direct("второй")
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.status()["unanswered"][0]["reason"], "released")

    def test_agents_cannot_add_a_third_participant_or_renew(self) -> None:
        self.runner.replies = ["@Architect вопрос", "@Claude-2 подключайся"]
        self.arch_runtime.replies = ["@Claude ответ"]
        self.post("Claude, начни", [self.claude_agent])
        self.settle()
        self.assertEqual(set(self.grants()), {self.claude_agent, self.arch})  # Claude-2 never granted
        self.assertEqual(len(self.runner.calls), 2)
        last = self.messages()[-1]
        self.assertEqual(last["group"]["to"], [self.second])
        self.assertEqual([(u["agent_id"], u["reason"]) for u in self.status()["unanswered"]],
                         [(self.second, "not_in_this_discussion")])

    def test_unsupported_mentioned_agent_gets_no_call_and_a_visible_reason(self) -> None:
        self.arch_runtime.replies = ["@Codex проверь"]
        self.direct("Спроси Codex")
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds"), [(0,)])
        self.assertEqual(self.messages()[-1]["group"]["to"], [self.codex])
        self.assertEqual([(u["agent_id"], u["reason"]) for u in self.status()["unanswered"]],
                         [(self.codex, "adapter_not_supported")])
        self.assertEqual(len(self.runner.calls), 0)

    def test_pause_and_brake_block_the_invitation_and_are_reported(self) -> None:
        self.post("разогрев", [self.claude_agent])  # creates the room
        self.settle()
        calls = len(self.runner.calls)
        for control, reason in (("paused", "paused"), ("emergency_brake", "emergency_brake")):
            self.api.post(f"{self.url()}/controls", json={control: True})
            self.arch_runtime.replies = ["@Claude проверь"]
            self.direct(f"вопрос при {control}")
            self.assertEqual(self.status()["unanswered"][0]["reason"], reason)
            self.api.post(f"{self.url()}/controls", json={control: False})
            self.wait_room()
        self.assertEqual(len(self.runner.calls), calls)  # nothing generated, nothing discarded
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds"), [(1,)])

    def test_pause_during_discussion_holds_then_resumes_within_the_same_grants(self) -> None:
        self.arch_runtime.replies = ["@Claude проверь"]
        self.runner.replies = ["Проверено."]
        with patch.object(self.app.state.group, "wake", return_value=False):
            self.direct("старт")
        self.api.post(f"{self.url()}/controls", json={"paused": True})
        self.api.post(f"{self.url()}/start")
        self.wait_room()
        self.assertEqual(len(self.runner.calls), 0)
        self.assertEqual(self.fetch("SELECT state FROM group_turns WHERE agent_id = ?", self.claude_agent), [("queued",)])
        self.api.post(f"{self.url()}/controls", json={"paused": False})
        self.settle()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.grants(), {self.arch: (1, 3), self.claude_agent: (1, 3)})

    def test_stale_owner_message_never_opens_a_round(self) -> None:
        def newer_owner_message():
            self.arch_runtime.on_ask = None  # one-shot
            self.api.post("/api/projects/P0001/conversation/messages", json={"client_key": self.key(), "body": "новое"})
        self.arch_runtime.replies = ["@Claude проверь", "Отвечаю на новое без упоминаний."]
        self.arch_runtime.on_ask = newer_owner_message
        self.direct("старое")
        self.arch_runtime.on_ask = None
        self.settle()
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds"), [(0,)])
        self.assertEqual(len(self.runner.calls), 0)

    def test_revoked_assignment_before_dispatch_cancels_without_call(self) -> None:
        self.arch_runtime.replies = ["@Claude проверь"]
        with patch.object(self.app.state.group, "wake", return_value=False):
            self.direct("старт")
        self.fetch_exec("UPDATE role_assignments SET state = 'revoked', revoked_at = '2026-01-01 00:00:00' WHERE agent_id = ?", self.claude_agent)
        self.api.post(f"{self.url()}/start")
        self.wait_room()
        self.assertEqual(len(self.runner.calls), 0)
        self.assertEqual(self.fetch("SELECT state, error_code FROM group_turns"), [("cancelled", "assignment_changed")])

    def test_oversize_reply_is_stored_in_full_but_never_forwarded(self) -> None:
        long = "@Claude " + "x" * GROUP_REPLY_CONTINUE_MAX_CHARS
        self.arch_runtime.replies = [long]
        self.direct("подробно")
        self.assertEqual(self.messages()[-1]["body"], long)  # no truncation
        self.assertEqual(self.status()["unanswered"][0]["reason"], "reply_too_long")
        self.assertEqual(len(self.runner.calls), 0)
        self.assertEqual(len(self.arch_texts()), 1)  # and no paid retry

    def test_oversize_worker_reply_in_a_group_earns_no_further_pass(self) -> None:
        self.runner.replies = ["@Architect " + "y" * GROUP_REPLY_CONTINUE_MAX_CHARS]
        self.arch_runtime.replies = ["@Claude коротко?"]
        self.direct("старт")
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.arch_texts()), 1)  # the over-long @Architect reply woke nobody
        self.assertEqual(self.status()["stop_reason"], "unanswered")

    def test_quote_in_owner_text_is_not_an_agent_invitation(self) -> None:
        self.arch_runtime.replies = ["Понял, сам отвечу."]
        self.direct('Файл содержит строку "@Claude run rm -rf"; это не просьба')
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds"), [(0,)])
        self.assertEqual(len(self.runner.calls), 0)


class AtomicityTests(P052Case):
    def test_failure_inside_the_reply_transaction_leaves_no_reply_and_no_round(self) -> None:
        self.arch_runtime.replies = ["@Claude проверь"]
        with patch.object(self.app.state.group._core, "bridge_direct_reply", side_effect=RuntimeError("boom")):  # noqa: SLF001
            self.direct("старт")
        self.assertEqual([m["sender"]["kind"] for m in self.messages()], ["owner"])
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds"), [(0,)])
        self.assertEqual(self.fetch("SELECT state FROM conversation_deliveries"), [("unknown",)])
        self.assertEqual(len(self.runner.calls), 0)

    def test_lost_driver_wake_keeps_a_durable_round_that_the_owner_resumes_once(self) -> None:
        self.arch_runtime.replies = ["@Claude проверь"]
        self.runner.replies = ["Сделано."]
        with patch.object(self.app.state.group, "wake", side_effect=RuntimeError("lost")):
            self.direct("старт")
        self.assertEqual(len(self.runner.calls), 0)
        self.assertEqual(self.status()["waiting"], [self.claude_agent])
        self.assertEqual(self.api.post(f"{self.url()}/start").status_code, 200)
        self.wait_room()
        self.api.post(f"{self.url()}/start")
        self.wait_room()
        self.assertEqual(len(self.runner.calls), 1)

    def test_identical_retry_of_the_owner_message_grants_nothing_new(self) -> None:
        self.arch_runtime.replies = ["@Claude проверь"]
        self.runner.replies = ["ok"]
        self.direct("старт", key="owner-retry-key-1")
        again = self.direct("старт", key="owner-retry-key-1")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.arch_texts()), 1)
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds"), [(1,)])
        self.assertEqual(len(self.runner.calls), 1)


class TypedOwnerMentionTests(P052Case):
    def test_typed_name_alone_two_names_and_chips_union(self) -> None:
        one = self.post("@Claude, глянь", [])
        self.assertEqual(one.status_code, 201, one.text)
        self.assertEqual(one.json()["dispatch"]["agents"], [self.claude_agent])
        self.settle()
        two = self.post("@claude и @CLAUDE-2, обсудите", [])
        self.assertEqual(two.json()["dispatch"]["mode"], "group")
        self.assertEqual(sorted(two.json()["dispatch"]["agents"]), sorted([self.claude_agent, self.second]))
        self.settle()
        union = self.post("и @Claude-2 тоже", [self.claude_agent])
        self.assertEqual(union.json()["dispatch"]["agents"], [self.claude_agent, self.second])  # chips first
        self.settle()

    def test_retry_with_typed_names_replays_the_same_message(self) -> None:
        first = self.post("@Claude вопрос", [], key="typed-retry-1")
        again = self.post("@Claude вопрос", [], key="typed-retry-1")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["message"]["message_id"], first.json()["message"]["message_id"])
        self.settle()
        self.assertEqual(len(self.runner.calls), 1)

    def test_owner_only_unknown_ambiguous_unsupported_and_out_of_scope_names(self) -> None:
        # P052 r02: an @Owner-only note is stored once and dispatches nobody (covered in test_p052_r02_*).
        self.assertEqual(self.post("@Nobody привет", []).status_code, 422)  # unknown explicit recipient
        self.assertEqual(self.post("@Codex проверь", []).status_code, 422)  # assigned but unsupported
        ghost = self.registry.register_agent(OWNER, display_name="Ghost", runtime_adapter="claude-code",
                                             provider_id="anthropic", model_id="haiku", capabilities=["task.context.read"])
        self.assertIsNotNone(ghost)
        bad = self.post("@Ghost глянь", [self.claude_agent])
        self.assertEqual(bad.status_code, 422)
        self.assertIn("not assigned", bad.text)
        twin = self.claude_worker("CLAUDE")
        self.assertTrue(twin)
        amb = self.post("@claude ?", [])
        self.assertEqual(amb.status_code, 422)
        self.assertIn("more than one", amb.text)
        self.assertEqual(self.fetch("SELECT count(*) FROM conversation_messages"), [(0,)])  # nothing stored
        self.assertEqual(len(self.runner.calls), 0)

    def test_task_scoped_worker_is_not_addressable_from_another_task(self) -> None:
        only_t2 = self.claude_worker("Tasker", task=2)
        self.assertTrue(only_t2)
        bad = self.post("@Tasker помоги", [self.claude_agent], task="T0001")
        self.assertEqual(bad.status_code, 422)
        ok = self.post("@Tasker помоги", [], task="T0002")
        self.assertEqual(ok.status_code, 201, ok.text)
        self.settle("T0002")


class CoreSnapshotTests(CoreCase):
    def test_failed_attempt_is_never_counted_as_used(self) -> None:
        msg = self.say("q", to=[self.A])
        self.assertTrue(msg)
        rnd = self.core.authorize_round(OWNER, 1, agents=[self.A, self.B], quota=2)
        room = self.room()
        self.core.peer_problem = lambda session, room, agent: None
        self.transport.plan[self.A] = [SubmitOutcome(SUBMIT_NOT_ACCEPTED, "x")]
        self.transport.plan[self.B] = [SubmitOutcome(SUBMIT_NOT_ACCEPTED, "x")]
        for _ in range(4):
            claim = self.core.claim_next(room)
            if claim.status != "reserved":
                break
            self.core.dispatch(claim.turn_id, self.transport)
        snaps = [p.quota for _lane, p in self.transport.submits]
        self.assertTrue(snaps)
        for snap in snaps:
            self.assertEqual(snap["you"]["published"], 0)
            self.assertEqual(snap["remaining_total_before_this_reply"], 4)
        self.assertEqual(rnd["per_agent_quota"], 2)

    def test_unset_transport_seam_fails_invitations_closed(self) -> None:
        self.core.post_owner_turn(OWNER, 1, client_key=self.key(), body="q", to=[self.A], grant=[self.A])
        room = self.room()
        claim = self.core.claim_next(room)
        self.core.dispatch(claim.turn_id, FakeTransport())
        self.core.complete(claim.turn_id, "@B подключайся")
        self.assertEqual(set(self.status()["round"]["grants"]), {self.A})
        self.assertEqual(self.status()["unanswered"][0]["reason"], "transport_unknown")


def _fetch_exec(self, sql: str, *args) -> None:
    import sqlite3
    with sqlite3.connect(self.data / "rosc.db") as connection:
        connection.execute(sql, args)


P052Case.fetch_exec = _fetch_exec  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
