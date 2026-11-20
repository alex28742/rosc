"""P060: guaranteed Architect assessment of Worker messages addressed to the Project Architect (Variant A).

Synthetic only: temporary SQLite migrated to head, the P043B Core with its deterministic fake transport, and the P059
wake fixture (test-only fake ``opencode serve`` with scripted Tool calls through the real MCP transport).  No model,
provider, CLI, Owner data, live database or second ROSC instance is touched.
"""
from __future__ import annotations

import json
import random
import uuid

from alembic import command

from rosc import architect_maintenance as maintenance
from rosc.agent_registry import AgentRegistryService
from rosc.authorization import LocalOwnerPolicy
from rosc.conversation import group_metadata
from rosc.db import make_session_factory
from rosc.worker_conversation import (CONTROL_EXPLICIT, CONTROL_INVALID, INTENT_CONTINUE, INTENT_FINISH,
                                      WorkerConversationCore, parse_reply_control, parse_reply_expectation)
from test_p043b_worker_conversation_core import (ARCHITECT_CAPS, OWNER, TASK, WORKER_CAPS, CoreCase, FakeTransport)
from test_p059_architect_context_maintenance import WakeCase
from test_p052_r04_agent_conversation import R04Case

import unittest


def control(nxt: str = "finish", expect: str | None = None) -> str:
    return f'\n<rosc-control next="{nxt}"' + (f' expect="{expect}"' if expect else "") + "/>"


class ControlLineTests(unittest.TestCase):
    def test_expect_is_parsed_beside_next_and_removed_from_the_body(self) -> None:
        text = "FYI: done." + control("finish", "informational")
        self.assertEqual(parse_reply_control(text), ("FYI: done.", INTENT_FINISH, CONTROL_EXPLICIT))
        self.assertEqual(parse_reply_expectation(text), "informational")
        text = 'Q?\n<rosc-control expect="reply_requested" next="continue"/>'
        self.assertEqual((parse_reply_control(text).intent, parse_reply_expectation(text)),
                         (INTENT_CONTINUE, "reply_requested"))
        self.assertIsNone(parse_reply_expectation("x" + control()))  # absent = unspecified, next unchanged

    def test_malformed_expect_fails_safe_as_finish_and_is_removed(self) -> None:
        for bad in ('<rosc-control next="continue" expect="urgent"/>', '<rosc-control next="continue" expect="a" '
                    'expect="b"/>', '<rosc-control expect="informational"/>'):
            with self.subTest(bad):
                self.assertEqual(parse_reply_control("text\n" + bad), ("text", INTENT_FINISH, CONTROL_INVALID))
                self.assertIsNone(parse_reply_expectation("text\n" + bad))


class AssessmentCoreCase(CoreCase):
    def setUp(self) -> None:
        super().setUp()
        arch = self.registry.register_agent(OWNER, display_name="Arch", runtime_adapter="fake-cli", provider_id="x",
                                            model_id="m", capabilities=ARCHITECT_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=arch["agent_id"], role="architect",
                                        capabilities=ARCHITECT_CAPS)
        self.arch = arch["agent_id"]
        self.sessions = make_session_factory(self.data / "rosc.db")
        self.session_id = str(uuid.uuid4())

    def worker_says(self, text: str, *, task_id=None, to=None, grant_arch: bool = False) -> tuple[str, dict]:
        """Owner -> Worker A (Architect addressed too, so it is a room member); A publishes ``text``."""
        grant = [self.A, self.arch] if grant_arch else [self.A]
        sent = self.core.post_owner_turn(OWNER, 1, client_key=self.key(), body="Please report.",
                                         to=[self.A, self.arch], grant=grant, task_id=task_id)
        room = sent["message"]["conversation_id"]
        while True:
            claim = self.core.claim_next(room)
            self.assertEqual(claim.status, "reserved")
            if claim.agent_id == self.A:
                break
            self.core.dispatch(claim.turn_id, self.transport)
            self.core.complete(claim.turn_id, "Architect first." + control("continue"))
        self.assertEqual(self.core.dispatch(claim.turn_id, self.transport)["state"], "accepted")
        view = self.core.complete(claim.turn_id, text, to=[self.arch] if to is None else to)
        return room, view

    def events(self) -> list[tuple]:
        return self.fetch("SELECT kind, source_kind, source_id, task_id FROM project_events ORDER BY seq")

    def obligation(self) -> str:
        return self.fetch("SELECT obligation_id FROM architect_obligations o JOIN project_events e "
                          "ON e.event_id = o.event_id WHERE e.kind = 'worker_message' ORDER BY o.event_seq DESC")[0][0]

    def ack(self, oid: str, outcome: str, agent: str | None = None, project: int = 1, **extra) -> dict:
        with self.sessions() as session:
            return maintenance.acknowledge_message(session, project_id=project, agent_id=agent or self.arch,
                                                   session_id=self.session_id,
                                                   args={"obligation_id": oid, "outcome": outcome,
                                                         "rationale": "Assessed the Worker message.", **extra})

    def refused(self, code: str, *args, **kw) -> None:
        with self.assertRaises(maintenance.MaintenanceError) as caught:
            self.ack(*args, **kw)
        self.assertEqual(caught.exception.code, code)

    def meta(self, message_id: str) -> dict:
        with self.sessions() as session:
            return group_metadata(session, [message_id])[message_id]

    def ready(self) -> list:
        with self.sessions() as session:
            return maintenance.wake_candidates(session, 1, self.session_id, owner_retry=True, chat=True)


class PublicationAndAckTests(AssessmentCoreCase):
    def test_informational_finish_one_event_one_ack_and_no_worker_turn(self) -> None:
        self.client.post("/api/projects/P0001/tasks", json=TASK)
        room, view = self.worker_says("Tests are green." + control("finish", "informational"), task_id=1)
        message_id = view["reply_message_id"]
        self.assertEqual(self.events(), [("worker_message", "conversation_message", message_id, 1)])
        body = self.fetch("SELECT body FROM conversation_messages WHERE message_id = ?", message_id)[0][0]
        self.assertEqual(body, "Tests are green.")  # control metadata is never visible text
        meta = self.meta(message_id)
        self.assertEqual((meta["next"], meta["expectation"], meta["assessment"]["state"]),
                         ("finish", "informational", "awaiting"))
        self.assertEqual(self.core.claim_next(room).status, "awaiting_owner")  # finish: nobody is called
        self.assertEqual(len(self.ready()), 1)  # the stopped round still leaves an independent assessment due
        turns = self.fetch("SELECT count(*) FROM group_turns")[0][0]
        result = self.ack(self.obligation(), "no_reply_needed")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.fetch("SELECT count(*) FROM group_turns")[0][0], turns)  # no Worker/Architect turn
        meta = self.meta(message_id)
        self.assertEqual((meta["next"], meta["assessment"]["state"], meta["assessment"]["outcome"]),
                         ("finish", "assessed", "no_reply_needed"))
        self.assertEqual(self.ready(), [])
        self.assertTrue(self.ack(self.obligation(), "no_reply_needed")["replayed"])  # identical ACK: idempotent
        self.refused("already_completed", self.obligation(), "escalated", escalation_reason="other")

    def test_reply_requested_finish_without_permitted_reply_escalates_to_owner(self) -> None:
        room, view = self.worker_says("Which port should I use?" + control("finish", "reply_requested"))
        oid = self.obligation()
        self.refused("outcome_not_allowed", oid, "no_reply_needed")
        self.refused("reply_not_found", oid, "replied")  # no published or in-flight Architect reply exists
        self.refused("invalid_argument", oid, "escalated")  # a reason is required
        self.assertEqual(self.core.claim_next(room).status, "awaiting_owner")
        result = self.ack(oid, "escalated", escalation_reason="no_reply_permission")
        self.assertEqual((result["outcome"]["next_actor"], result["outcome"]["escalation_reason"]),
                         ("Owner", "no_reply_permission"))
        assessment = self.meta(view["reply_message_id"])["assessment"]
        self.assertEqual((assessment["state"], assessment["escalation_reason"], assessment["next_actor"]),
                         ("owner_action", "no_reply_permission", "Owner"))
        agents = self.fetch("SELECT count(*) FROM conversation_messages m JOIN conversation_participants p "
                            "ON p.participant_id = m.sender_participant_id WHERE p.kind = 'architect'")[0][0]
        self.assertEqual(agents, 0)  # no reply was produced and no round was opened by the escalation
        self.assertEqual(self.fetch("SELECT count(*) FROM group_rounds")[0][0], 1)

    def test_typed_to_is_the_authority_and_republish_never_duplicates(self) -> None:
        _room, view = self.worker_says("@Arch FYI only for the Owner." + control(), to=["owner"])
        self.assertEqual(self.events(), [])  # an @name in the body is not the addressee
        self.assertIsNone(self.meta(view["reply_message_id"])["assessment"])  # no event: nothing claimed
        _room, view = self.worker_says("Done." + control("finish", "informational"))
        self.core.publish(view["turn_id"])  # idempotent replay of the same publication
        self.assertEqual(len(self.events()), 1)
        owner = self.say("owner note", to=[self.arch])
        self.assertIsNone(self.meta(owner["message_id"])["assessment"])
        self.assertIsNone(self.meta(owner["message_id"])["expectation"])

    def test_obligation_types_scope_and_addressee_are_checked(self) -> None:
        self.worker_says("Done." + control("finish", "informational"))
        oid = self.obligation()
        with self.sessions() as session:
            event = maintenance.emit(session, project_id=1, kind="run_reported", source_kind="managed_run_report",
                                     source_id=str(uuid.uuid4()), task_id=None, run_id=str(uuid.uuid4()),
                                     digest="c" * 64, facts={"outcome": "completed"})
            session.commit()
            p059 = session.scalars(maintenance.select(maintenance.ArchitectObligation).where(
                maintenance.ArchitectObligation.event_id == event.event_id)).one().obligation_id
        store = None
        with self.sessions() as session, self.assertRaises(maintenance.MaintenanceError) as caught:
            maintenance.acknowledge(session, store, project_id=1, agent_id=self.arch, session_id=self.session_id,
                                    args={"obligation_ids": [oid], "assessment": "x",
                                          "no_change_needed": "nothing to change here"})
        self.assertEqual(caught.exception.code, "wrong_obligation_type")
        self.refused("wrong_obligation_type", p059, "no_reply_needed")
        self.refused("not_addressee", oid, "no_reply_needed", agent=self.A)
        self.refused("not_found", oid, "no_reply_needed", project=2)
        with self.sessions() as session:
            with self.assertRaises(maintenance.MaintenanceError):
                maintenance.acknowledge_message(session, project_id=1, agent_id=self.arch, session_id="s",
                                                args={"obligation_id": oid, "outcome": "no_reply_needed",
                                                      "rationale": "short"})  # rationale too short
        self.assertEqual(self.fetch("SELECT state FROM architect_obligations WHERE obligation_id = ?", oid),
                         [("pending",)])

    def test_continue_group_turn_ack_binds_the_published_reply_and_needs_no_wake(self) -> None:
        room, view = self.worker_says("Should I split the PR?" + control("continue", "reply_requested"),
                                      grant_arch=True)
        oid = self.obligation()
        self.assertEqual(self.ready(), [])  # the round still runs: the ordinary Architect turn may ACK first
        claim = self.core.claim_next(room)
        self.assertEqual(claim.agent_id, self.arch)
        self.transport.plan[self.arch] = []
        self.assertEqual(self.core.dispatch(claim.turn_id, self.transport)["state"], "accepted")
        pending = self.ack(oid, "replied")  # during its own turn: bound to the reply ROSC will publish
        self.assertEqual(pending["state"], "awaiting_reply_publication")
        self.assertEqual(self.meta(view["reply_message_id"])["assessment"]["state"], "assessing")
        self.assertTrue(self.ack(oid, "replied")["replayed"])
        published = self.core.complete(claim.turn_id, "Yes, split it." + control("finish"))
        assessment = self.meta(view["reply_message_id"])["assessment"]
        self.assertEqual((assessment["state"], assessment["outcome"], assessment["reply_message_id"]),
                         ("assessed", "replied", published["reply_message_id"]))
        self.assertEqual(self.core.claim_next(room).status, "awaiting_owner")
        self.assertEqual(self.ready(), [])  # completed: no separate wake

    def test_explicit_reply_message_must_be_the_architects_later_reply(self) -> None:
        room, view = self.worker_says("Port?" + control("continue", "reply_requested"), grant_arch=True)
        oid = self.obligation()
        self.refused("reply_not_found", oid, "replied", reply_message_id=view["reply_message_id"])
        claim = self.core.claim_next(room)
        self.core.dispatch(claim.turn_id, self.transport)
        published = self.core.complete(claim.turn_id, "Use 8010." + control("finish"))
        result = self.ack(oid, "replied", reply_message_id=published["reply_message_id"])
        self.assertEqual(result["outcome"]["reply_message_id"], published["reply_message_id"])


class MigrationTests(AssessmentCoreCase):
    def test_downgrade_is_refused_once_a_worker_message_event_exists_and_clean_round_trip_works(self) -> None:
        command.downgrade(self.alembic, "0024_architect_context_maintenance")
        self.assertEqual(self.fetch("SELECT version_num FROM alembic_version"), [("0024_architect_context_maintenance",)])
        command.upgrade(self.alembic, "head")
        self.restart()
        self.worker_says("Done." + control("finish", "informational"))
        self.assertEqual(len(self.events()), 1)  # nothing historical is backfilled; the new message has its event
        with self.assertRaises(RuntimeError):
            command.downgrade(self.alembic, "0024_architect_context_maintenance")
        self.assertEqual(self.fetch("SELECT count(*) FROM project_events"), [(1,)])
        triggers = {r[0] for r in self.fetch("SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertLessEqual({"tr_project_events_immutable", "tr_project_events_no_delete",
                              "tr_architect_obligations_scope"}, triggers)


class MessageWakeCase(WakeCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=TASK).status_code, 201)
        worker = self.registry.register_agent(OWNER, display_name="Wkr", runtime_adapter="fake-cli", provider_id="x",
                                              model_id="m", capabilities=WORKER_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=worker["agent_id"], role="worker", capabilities=WORKER_CAPS)
        self.worker = worker["agent_id"]
        self.core = WorkerConversationCore(self.sessions, LocalOwnerPolicy(), rng=random.Random(3))
        self.fake = FakeTransport()
        self.keys = 0

    def worker_says(self, text: str, task_id=None, stop: bool = True) -> tuple[str, str]:
        self.keys += 1
        arch = self.agent["agent_id"]
        sent = self.core.post_owner_turn(OWNER, 1, client_key=f"p060-key-{self.keys:04d}", body="Report please.",
                                         to=[self.worker, arch], grant=[self.worker], task_id=task_id)
        room = sent["message"]["conversation_id"]
        claim = self.core.claim_next(room)
        self.assertEqual(self.core.dispatch(claim.turn_id, self.fake)["state"], "accepted")
        view = self.core.complete(claim.turn_id, text, to=[arch])
        if stop:
            self.assertEqual(self.core.claim_next(room).status, "awaiting_owner")
        return room, view["reply_message_id"]

    def obligation(self) -> str:
        with self.sessions() as session:
            return session.connection().exec_driver_sql(
                "SELECT o.obligation_id FROM architect_obligations o JOIN project_events e ON e.event_id = o.event_id "
                "WHERE e.kind = 'worker_message' ORDER BY o.event_seq DESC").scalar()

    def chat_wakes(self) -> list[tuple]:
        with self.sessions() as session:
            return [tuple(r) for r in session.connection().exec_driver_sql(
                "SELECT conversation_id, state, detail FROM architect_maintenance_wakes ORDER BY attempt")]

    def assessment(self, message_id: str) -> dict:
        with self.sessions() as session:
            return group_metadata(session, [message_id])[message_id]["assessment"]


class MessageWakeTests(MessageWakeCase):
    def test_task_room_finish_gets_one_wake_in_its_own_native_session_and_a_real_tool_ack(self) -> None:
        room, message_id = self.worker_says("Done, tests green." + control("finish", "informational"), task_id=1)
        oid = self.obligation()
        self.assertEqual(self.assessment(message_id)["state"], "awaiting")
        ack = {"tool": "message_ack", "args": {"obligation_id": oid, "outcome": "no_reply_needed",
                                               "rationale": "Informational report; nothing requested."}}
        self.script(["Assessed."], [ack])
        messages_before = self.conversation_count()
        self.tick()
        self.assertEqual(len(self.wakes()), 1)
        prompt = (self.bin / "last_prompt.txt").read_text()
        for needle in (oid, "rosc_message_ack", "Task Conversation of T0001", "Done, tests green."):
            self.assertIn(needle, prompt)
        self.assertEqual(self.chat_wakes(), [(room, "replied", None)])
        project_sessions = {s for s in self.sessions_used()[:-1]}
        self.assertNotIn(self.sessions_used()[-1], project_sessions)  # the Task's native session, not the PC one
        assessment = self.assessment(message_id)
        self.assertEqual((assessment["state"], assessment["outcome"]), ("assessed", "no_reply_needed"))
        self.assertEqual(self.conversation_count(), messages_before)  # the wake reply is never published
        self.tick()
        self.assertEqual(len(self.wakes()), 1)  # bounded: no second wake, no Worker turn

    def test_project_room_reply_requested_without_permission_escalates_via_the_wake(self) -> None:
        _room, message_id = self.worker_says("Which DB path?" + control("finish", "reply_requested"))
        oid = self.obligation()
        ack = {"tool": "message_ack", "args": {"obligation_id": oid, "outcome": "escalated",
                                               "escalation_reason": "no_reply_permission",
                                               "rationale": "The round stopped; I have no reply allowance."}}
        self.script(["Escalated."], [ack])
        self.tick()
        assessment = self.assessment(message_id)
        self.assertEqual((assessment["state"], assessment["next_actor"], assessment["escalation_reason"]),
                         ("owner_action", "Owner", "no_reply_permission"))
        self.assertEqual(self.chat_wakes()[0][0], _room)
        self.tick()
        self.assertEqual(len(self.wakes()), 1)

    def test_running_round_defers_the_wake_and_a_plain_reply_never_counts_as_assessment(self) -> None:
        room, message_id = self.worker_says("Question?" + control("continue", "reply_requested"), stop=False)
        self.tick()
        self.assertEqual(self.wakes(), [])  # the discussion still runs (the Architect may get its own turn)
        with self.sessions() as session:
            session.connection().exec_driver_sql("UPDATE group_rounds SET state='awaiting_owner'")
            session.commit()
        self.script(["I saw it."], [])  # reply without rosc_message_ack
        self.tick()
        self.assertEqual(len(self.wakes()), 1)
        self.assertEqual(self.assessment(message_id)["state"], "awaiting")
        self.tick()
        self.assertEqual(len(self.wakes()), 1)  # never loops automatically

    def test_restart_marks_a_room_wake_ambiguous_and_recovery_never_resends(self) -> None:
        self.worker_says("Done." + control("finish", "informational"), task_id=1)
        self.unscript()
        self.tick()
        with self.sessions() as session:
            session.connection().exec_driver_sql("UPDATE architect_maintenance_wakes SET state='sent', settled_at=NULL")
            session.connection().exec_driver_sql("UPDATE architect_obligations SET state='processing'")
            session.commit()
        self.architect.recover()
        self.assertEqual(self.chat_wakes()[0][1:], ("ambiguous", "interrupted"))
        self.architect.__dict__.pop("_wake_lookup_at", None)
        self.tick()  # resolved by lookup in the room's native session; the offered work is open again, not resent
        self.assertEqual(self.chat_wakes()[0][1], "replied")
        self.assertEqual(len(self.wakes()), 1)
        self.assertEqual(self.states(), ["pending"])


class GroupTurnNoteTests(R04Case):
    """The ordinary Architect group turn (real ArchitectGroupTransport) carries the trusted assessment note."""

    def test_continue_reply_requested_reaches_the_architect_turn_with_its_obligation(self) -> None:
        self.arch_runtime.replies = ["@Claude какая ширина колонки?", "Принято, 320px." + control("finish")]
        self.runner.replies = ["320px. Подтверди, пожалуйста." + control("continue", "reply_requested")]
        self.direct("Architect, выясни ширину у Claude")
        worker = [m for m in self.agent_messages() if m["sender"]["kind"] == "worker"][-1]
        self.assertEqual((worker["group"]["next"], worker["group"]["expectation"]), ("continue", "reply_requested"))
        oid = worker["group"]["assessment"]["obligation_id"]
        texts = self.arch_texts()
        self.assertEqual(len(texts), 2)
        self.assertIn("ROSC ASSESSMENT", texts[1])
        self.assertIn(oid, texts[1])
        self.assertNotIn("ROSC ASSESSMENT", texts[0])
        self.assertNotIn("rosc-control", worker["body"])


if __name__ == "__main__":
    unittest.main()
