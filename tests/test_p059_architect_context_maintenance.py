"""P059: event-driven Architect context maintenance (events, obligations, typed Handoff, canonical Pipeline, wake).

Synthetic only: temporary SQLite migrated to head, the P054B/P055/P056 managed-Run fixtures (fake tmux/proc) and the
test-only fake ``opencode serve`` (scripted Tool calls through the real MCP transport).  No model or provider call, no
real CLI/tmux, no Owner data, no live migration and no HTTP server (FastAPI TestClient in-process).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from unittest.mock import patch

from alembic import command

from rosc import architect_maintenance as maintenance
from rosc.architect_store import RevisionConflict
from rosc.project_context import CAP_CONTEXT_READ, CAP_DOC_READ, CAP_DOC_WRITE, Caller, ToolError
from test_p053_onboarding_readiness import READY, P053Case
from test_p029_architect_onboarding import ALL
from test_p056_task_lifecycle_owner_decision import LifecycleCase

MAINT = "/api/projects/P0001/architect/maintenance"
PIPE = "/api/projects/P0001/pipeline"


class CoreCase(LifecycleCase):
    def caller(self, project: int = 1) -> Caller:
        if not hasattr(self, "_identity"):
            self._identity = (str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
        session_id, agent_id, assignment_id = self._identity
        return Caller(session_id, project, agent_id, assignment_id, "p", "m",
                      frozenset({CAP_CONTEXT_READ, CAP_DOC_READ, CAP_DOC_WRITE}))

    def events(self) -> list[tuple]:
        return self.rows("select kind, task_id, run_id, review_id, decision_id from project_events order by seq")

    def obligations(self) -> list[tuple]:
        return self.rows("select o.state, e.kind from architect_obligations o join project_events e "
                         "on e.event_id = o.event_id order by o.event_seq")

    def pending(self, **args) -> dict:
        return self.call("maintenance_pending", args)

    def ack(self, ids, **args) -> dict:
        return self.call("maintenance_ack", {"obligation_ids": ids, "assessment": "Assessed the event.", **args})

    def first_open(self) -> str:
        return self.pending()["obligations"][0]["obligation_id"]

    def emit(self, n: int = 1, project: int = 1) -> list[str]:
        """Synthetic Core events for scope/paging scenarios (disposable DB only)."""
        out = []
        with self.sessions() as session:
            for _ in range(n):
                event = maintenance.emit(session, project_id=project, kind="run_reported",
                                         source_kind="managed_run_report", source_id=str(uuid.uuid4()), task_id=None,
                                         run_id=str(uuid.uuid4()), digest="a" * 64, facts={"outcome": "completed"})
                out.append(event.event_id)
            session.commit()
        return out


# -- P0-A: events exactly once, from authoritative rows only --------------------------------------------------------------
class EventEmissionTests(CoreCase):
    def test_reported_run_emits_one_event_and_one_pending_obligation(self) -> None:
        rid = self.reported(tests=False)
        self.assertEqual(self.events(), [("run_reported", 1, rid, None, None)])
        self.assertEqual(self.obligations(), [("pending", "run_reported")])
        for _ in range(3):  # replays of the import, a fresh Core (restart) and its catch-up pass
            self.runs.tick()
        self.build()
        self.runs.tick()
        self.assertEqual(self.count("project_events"), 1)
        event = self.pending()["obligations"][0]["event"]
        self.assertEqual((event["digest"], event["facts"]), (self.report_sha(rid), {"outcome": self.rows(
            f"select outcome from managed_run_reports where run_id='{rid}'")[0][0]}))
        self.assertNotIn("Worker-", json.dumps(event))  # ids, digest and compact facts only: no Report text

    def test_decisive_review_emits_inconclusive_does_not_and_replay_is_single(self) -> None:
        rid = self.reported(tests=False)
        self.submit(rid, "inconclusive", limitations=["missing log"], continue_by="architect")
        self.assertEqual([e[0] for e in self.events()], ["run_reported"])
        passed = self.submit(rid, expected_version=1)
        self.submit(rid, expected_version=1)  # identical replay
        self.assertEqual(self.events()[-1], ("review_submitted", 1, rid, passed["review"]["review_id"], None))
        self.assertEqual(self.count("project_events"), 2)

    def test_owner_accept_and_rework_events_keep_history_links(self) -> None:
        rid, review = self.reviewed()
        self.assertEqual(self.decide(rid, review).status_code, 201)
        self.assertEqual(self.decide(rid, review).status_code, 200)  # duplicate click: replay, no second event
        decision_id = self.rows("select decision_id from task_owner_decisions")[0][0]
        self.assertEqual(self.events()[-1], ("task_accepted", 1, rid, review["review_id"], decision_id))
        self.assertEqual(self.count("project_events"), 3)
        ids = [o["obligation_id"] for o in self.pending()["obligations"]]
        self.ack(ids, no_change_needed="Routine closure; Handoff and Pipeline unaffected.")
        # completing maintenance never touches the Task, the Run, the Review or the decision
        self.assertEqual(self.task_status(), "Completed")
        self.assertEqual(self.rows("select task_id, run_id, review_id from task_owner_decisions"),
                         [(1, rid, review["review_id"])])
        self.assertEqual(self.state_row(rid)["state"], "reported")

    def test_rework_request_emits_its_own_kind(self) -> None:
        rid, review = self.reviewed("changes_requested", blocking_findings=["tests missing"])
        self.assertEqual(self.decide(rid, review, "request_rework", reason="Add tests").status_code, 201)
        self.assertEqual(self.events()[-1][0], "task_rework_requested")
        self.assertEqual(self.task_status(), "Active")

    def test_migration_adds_no_backfill_and_downgrade_guards_outcomes(self) -> None:
        rid = self.reported(tests=False)
        command.downgrade(self.alembic, "0023_project_local_task_number")  # an untouched pending event only
        self.assertEqual(self.rows("select count(*) from sqlite_master where name='project_events'"), [(0,)])
        command.upgrade(self.alembic, "head")
        self.assertEqual(self.count("project_events"), 0)  # history is never backfilled
        self.assertEqual(self.state_row(rid)["state"], "reported")
        self.emit()
        self.ack([self.first_open()], no_change_needed="Synthetic event, nothing to change.")
        with self.assertRaises(RuntimeError):
            command.downgrade(self.alembic, "0023_project_local_task_number")
        self.assertEqual(self.count("architect_obligations"), 1)

    def test_events_and_completed_obligations_are_immutable_in_the_database(self) -> None:
        self.emit()
        self.ack([self.first_open()], no_change_needed="Synthetic event, nothing to change.")
        with sqlite3.connect(self.data / "rosc.db") as db:
            for statement in ("update project_events set kind='task_accepted'", "delete from project_events",
                              "update architect_obligations set state='pending'"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(statement)


# -- P0-A.5: acknowledge only with a verified outcome --------------------------------------------------------------------
class AcknowledgeTests(CoreCase):
    def test_bare_done_is_refused_and_no_change_needed_completes(self) -> None:
        self.emit()
        oid = self.first_open()
        for args in ({}, {"no_change_needed": "ok"}):
            with self.assertRaises(ToolError) as caught:
                self.ack([oid], **args)
            self.assertIn(caught.exception.code, ("outcome_required", "invalid_argument"))
        self.assertEqual(self.obligations(), [("pending", "run_reported")])
        done = self.ack([oid], no_change_needed="Routine report; nothing in context changed.",
                        pipeline_no_change_reason="No dependency or scope impact.",
                        adr_not_needed_reason="No architectural decision was made.")
        self.assertEqual((done["recorded"], done["processed_cursor"]), (True, 1))
        self.assertEqual(self.obligations(), [("completed", "run_reported")])
        self.assertTrue(self.ack([oid], no_change_needed="Routine report; nothing in context changed.",
                                 pipeline_no_change_reason="No dependency or scope impact.",
                                 adr_not_needed_reason="No architectural decision was made.")["replayed"])
        with self.assertRaises(ToolError) as caught:
            self.ack([oid], no_change_needed="A different story later on.")
        self.assertEqual(caught.exception.code, "already_completed")

    def test_unverifiable_revision_keeps_the_obligation_open_then_a_real_checkpoint_completes(self) -> None:
        self.emit()
        oid = self.first_open()
        with self.assertRaises(ToolError) as caught:
            self.ack([oid], handoff_revision=3)
        self.assertEqual(caught.exception.code, "handoff_revision_missing")
        self.assertEqual(self.rows("select state, last_error from architect_obligations"),
                         [("failed", "handoff_revision_missing")])
        self.assertEqual(self.pending()["counts"]["failed"], 1)  # still listed as open work
        self.call("handoff_init", {"current_focus": "Bootstrap"})
        saved = self.call("handoff_update_checkpoint", {"expected_revision": 1, "sections": {
            "last_handled": "#1 run_reported assessed"}, "append_sections": ["last_handled"]})
        done = self.ack([oid], handoff_revision=saved["revision"],
                        pipeline_no_change_reason="No milestone affected by this report.")
        self.assertEqual(done["outcome"]["handoff"]["revision"], 2)
        self.assertEqual(self.rows("select state, last_error from architect_obligations"), [("completed", None)])

    def test_pre_event_handoff_revision_is_stale(self) -> None:
        self.call("handoff_init", {})
        self.emit()
        with self.assertRaises(ToolError) as caught:
            self.ack([self.first_open()], handoff_revision=1)
        self.assertEqual(caught.exception.code, "handoff_revision_stale")

    def test_adr_outcome_through_private_docs_and_protected_handoff(self) -> None:
        self.emit()
        created = self.call("docs_create", {"doc_id": "adr-0001", "logical_path": "adr/0001-report-format.md",
                                            "content": "# ADR 0001\nStatus: accepted\nDecision: ..."})
        done = self.ack([self.first_open()], adr_doc_id="adr-0001", adr_revision=created["revision"])
        self.assertEqual(done["outcome"]["adr"]["doc_id"], "adr-0001")
        self.emit()
        with self.assertRaises(ToolError) as caught:  # an ADR reference must exist
            self.ack([self.first_open()], adr_doc_id="adr-0002", adr_revision=1)
        self.assertEqual(caught.exception.code, "adr_revision_missing")

    def test_scope_never_leaks_between_projects(self) -> None:
        self.emit(project=2)
        oid = self.rows("select obligation_id from architect_obligations")[0][0]
        self.assertEqual(self.pending()["obligations"], [])
        with self.assertRaises(ToolError) as caught:
            self.ack([oid], no_change_needed="Trying another Project's obligation.")
        self.assertEqual(caught.exception.code, "not_found")
        self.assertEqual(len(self.call("maintenance_pending", {}, self.caller(2))["obligations"]), 1)

    def test_offline_backlog_is_a_paged_delta_and_cursor_never_skips_an_open_event(self) -> None:
        self.emit(3)
        page = self.pending(limit=2)
        self.assertEqual(([o["event"]["seq"] for o in page["obligations"]], page["has_more"], page["next_cursor"]),
                         ([1, 2], True, 2))
        rest = self.pending(limit=2, cursor=page["next_cursor"])
        self.assertEqual(([o["event"]["seq"] for o in rest["obligations"]], rest["has_more"]), ([3], False))
        later = [page["obligations"][1]["obligation_id"], rest["obligations"][0]["obligation_id"]]
        done = self.ack(later, no_change_needed="Batch of routine reports assessed.")
        self.assertEqual(done["processed_cursor"], 0)  # event #1 is still open
        self.assertEqual(self.pending()["counts"], {"pending": 1, "processing": 0, "failed": 0, "completed": 2})


# -- P0-B: typed Handoff ----------------------------------------------------------------------------------------------
class HandoffTests(CoreCase):
    def test_absent_until_explicit_init_and_section_update_preserves_the_rest(self) -> None:
        self.assertFalse(self.call("handoff_get_snapshot", {})["exists"])
        self.assertFalse(self.call("docs_list", {})["documents"])  # reading created nothing
        self.call("handoff_init", {"current_focus": "Stage A"})
        with self.assertRaises(ToolError) as caught:
            self.call("handoff_init", {})
        self.assertEqual(caught.exception.code, "already_exists")
        before = self.call("docs_read", {"doc_id": "handoff"})["content"]
        saved = self.call("handoff_update_checkpoint", {"expected_revision": 1, "sections": {
            "next_action": "Owner: decide T0001", "blockers": "none"}, "source_event_ids": []})
        after = self.call("docs_read", {"doc_id": "handoff"})["content"]
        self.assertEqual(saved["revision"], 2)
        untouched = lambda text: [s for s in text.split("## ") if not s.startswith(("Next actor", "Blockers"))]
        self.assertEqual(untouched(before), untouched(after))
        self.assertIn("Owner: decide T0001", after)
        snapshot = self.call("handoff_get_snapshot", {"since_revision": 1})
        changed = {s["section"] for s in snapshot["sections"] if s["changed_since"]}
        self.assertEqual(changed, {"next_action", "blockers"})
        self.assertEqual(self.call("docs_read", {"doc_id": "handoff", "revision": 1})["revision"], 1)  # history kept

    def test_stale_write_is_refused_and_append_keeps_history(self) -> None:
        self.call("handoff_init", {})
        self.call("handoff_update_checkpoint", {"expected_revision": 1, "sections": {"last_handled": "#1 a"},
                                                "append_sections": ["last_handled"]})
        with self.assertRaises(RevisionConflict) as caught:  # stale CAS (call() maps it to revision_conflict)
            self.call("handoff_update_checkpoint", {"expected_revision": 1, "sections": {"notes": "x"}})
        self.assertEqual(caught.exception.current_revision, 2)
        self.call("handoff_update_checkpoint", {"expected_revision": 2, "sections": {"last_handled": "#2 b"},
                                                "append_sections": ["last_handled"]})
        section = self.call("handoff_get_section", {"section": "last_handled"})
        self.assertIn("#1 a", section["text"])
        self.assertIn("#2 b", section["text"])
        paged = self.call("handoff_get_section", {"section": "last_handled", "max_chars": 3})
        self.assertEqual((len(paged["text"]), paged["next_offset"]), (3, 3))

    def test_generic_docs_tools_cannot_bypass_the_typed_handoff(self) -> None:
        self.call("handoff_init", {})
        for tool, args in (("docs_update", {"doc_id": "handoff", "content": "x", "expected_revision": 1}),
                           ("docs_delete", {"doc_id": "handoff", "expected_revision": 1}),
                           ("docs_create", {"doc_id": "other", "logical_path": "handoff.md", "content": "x"})):
            with self.assertRaises(ToolError) as caught:
                self.call(tool, args)
            self.assertEqual(caught.exception.code, "protected_document", tool)
        self.assertEqual(self.call("docs_read", {"doc_id": "handoff"})["revision"], 1)


# -- P0-C: canonical Pipeline ---------------------------------------------------------------------------------------------
class PipelineTests(CoreCase):
    def propose(self, base: int, ops: list[dict]) -> dict:
        return self.call("pipeline_propose_change", {"base_revision": base, "ops": ops,
                                                     "rationale": "Plan derived from the Owner goals."})

    def decide(self, proposal_id: str, decision: str = "accept"):  # type: ignore[override]
        return self.client.post(f"{PIPE}/proposals/{proposal_id}/decision", json={"decision": decision})

    def test_uninitialized_is_honest_and_a_proposal_changes_nothing_until_owner_accepts(self) -> None:
        current = self.call("pipeline_get_current", {})
        self.assertEqual((current["state"], current["revision"]), ("uninitialized", 0))
        self.assertEqual(self.call("pipeline_list_items", {})["items"], [])
        proposal = self.propose(0, [{"op": "add_item", "kind": "milestone", "title": "M1", "ref": "m1"},
                                    {"op": "add_item", "kind": "step", "title": "S1", "parent_id": "m1"},
                                    {"op": "add_item", "kind": "step", "title": "S2", "parent_id": "m1",
                                     "status": "running"}])
        self.assertEqual(self.call("pipeline_get_current", {})["state"], "uninitialized")
        accepted = self.decide(proposal["proposal_id"])
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["revision"], 1)
        items = self.call("pipeline_list_items", {})["items"]
        self.assertEqual([(i["item_id"], i["parent_id"], i["status"]) for i in items],
                         [("PI-1", None, "backlog"), ("PI-2", "PI-1", "backlog"), ("PI-3", "PI-1", "running")])
        self.assertEqual([i["item_id"] for i in self.call("pipeline_list_items", {"status": ["running"]})["items"]],
                         ["PI-3"])
        page = self.call("pipeline_list_items", {"limit": 2})
        self.assertEqual((len(page["items"]), page["has_more"], page["next_cursor"]), (2, True, "PI-2"))
        self.assertEqual([i["item_id"] for i in self.call("pipeline_list_items", {"cursor": "PI-2"})["items"]],
                         ["PI-3"])
        self.assertEqual(self.call("pipeline_list_items", {"updated_after": "2999-01-01T00:00:00Z"})["items"], [])

    def test_changes_since_stale_proposal_and_reject(self) -> None:
        first = self.propose(0, [{"op": "add_item", "kind": "phase", "title": "P"}])
        stale = self.propose(0, [{"op": "add_item", "kind": "phase", "title": "Q"}])
        self.decide(first["proposal_id"])
        refused = self.decide(stale["proposal_id"])
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (409, "proposal_stale"))
        update = self.propose(1, [{"op": "update_item", "item_id": "PI-1", "status": "completed"}])
        self.decide(update["proposal_id"])
        changes = self.call("pipeline_changes_since", {"since_revision": 1})
        self.assertEqual([(c["revision"], c["op"], c["before"]["status"], c["after"]["status"])
                          for c in changes["changes"]], [(2, "update", "backlog", "completed")])
        self.assertEqual(changes["current_revision"], 2)
        all_changes = self.call("pipeline_changes_since", {"limit": 1})
        self.assertEqual((all_changes["has_more"], all_changes["next_cursor"]), (True, "1:1"))
        rejected = self.propose(2, [{"op": "update_item", "item_id": "PI-1", "status": "accepted"}])
        self.assertEqual(self.decide(rejected["proposal_id"], "reject").json()["proposal"]["state"], "rejected")
        self.assertEqual(self.call("pipeline_get_current", {})["revision"], 2)  # nothing applied
        view = self.client.get(PIPE).json()
        self.assertEqual([p["state"] for p in view["proposals"]], ["rejected", "accepted", "stale", "accepted"])

    def test_wrong_base_is_a_conflict_and_task_closure_never_closes_an_item(self) -> None:
        with self.assertRaises(ToolError) as caught:
            self.propose(4, [{"op": "add_item", "kind": "phase", "title": "P"}])
        self.assertEqual(caught.exception.code, "revision_conflict")
        self.decide(self.propose(0, [{"op": "add_item", "kind": "milestone", "title": "M"}])["proposal_id"])
        rid, review = self.reviewed()
        self.assertEqual(LifecycleCase.decide(self, rid, review).status_code, 201)  # Owner accepts the Task
        self.assertEqual([i["status"] for i in self.call("pipeline_list_items", {})["items"]], ["backlog"])
        self.assertEqual(self.call("pipeline_get_current", {})["revision"], 1)


# -- P0-E: Owner read model -------------------------------------------------------------------------------------------------
class OwnerStatusTests(CoreCase):
    def test_pending_work_without_a_live_architect_is_visible_and_names_the_owner(self) -> None:
        empty = self.client.get(MAINT).json()
        self.assertEqual((empty["next_actor"], empty["attention"], empty["blocked_reason"]), (None, False, None))
        self.reported(tests=False)
        view = self.client.get(MAINT).json()
        self.assertEqual(view["counts"]["pending"], 1)
        self.assertEqual((view["next_actor"], view["attention"], view["blocked_reason"]),
                         ("Owner", True, "architect_not_active"))
        self.assertEqual(view["obligations"][0]["event"]["kind"], "run_reported")
        self.assertEqual(view["pipeline"]["state"], "uninitialized")
        self.assertFalse(view["handoff"]["exists"])
        wake = self.client.post(f"{MAINT}/wake").json()
        self.assertEqual(wake, {"scheduled": False, "blocked_reason": "architect_not_active"})
        self.assertEqual(self.count("architect_maintenance_wakes"), 0)


# -- P0-A.4: bounded wake of a ready, familiarized Architect (real MCP transport, fake provider) -------------------------
class WakeCase(P053Case):
    def setUp(self) -> None:
        super().setUp()
        self.script([READY], ALL)
        self.activate_raw()
        self.architect.drain()
        self.assertEqual(self.view()["state"], "ready")

    def emit(self) -> str:
        with self.sessions() as session:
            event = maintenance.emit(session, project_id=1, kind="run_reported", source_kind="managed_run_report",
                                     source_id=str(uuid.uuid4()), task_id=None, run_id=str(uuid.uuid4()),
                                     digest="b" * 64, facts={"outcome": "completed"})
            session.commit()
            return session.scalars(maintenance.select(maintenance.ArchitectObligation).where(
                maintenance.ArchitectObligation.event_id == event.event_id)).one().obligation_id

    def wakes(self) -> list[str]:
        return [p for p in self.prompts() if p.startswith("msg_roscmw")]

    def tick(self) -> None:
        self.architect.maintenance_tick()
        self.architect.drain()

    def states(self) -> list[str]:
        with self.sessions() as session:
            return [r[0] for r in session.connection().exec_driver_sql(
                "select state from architect_obligations order by event_seq")]

    def wake_rows(self) -> list[tuple]:
        with self.sessions() as session:
            return [tuple(r) for r in session.connection().exec_driver_sql(
                "select trigger, state, detail from architect_maintenance_wakes order by attempt")]


class WakeTests(WakeCase):
    def test_one_wake_tool_review_and_ack_completes_without_any_owner_reminder(self) -> None:
        self.tick()
        self.assertEqual(self.wakes(), [])  # nothing pending: no turn
        oid = self.emit()
        ack = {"tool": "maintenance_ack", "args": {"obligation_ids": [oid], "assessment": "Report assessed.",
                                                     "no_change_needed": "Routine report, context unchanged."}}
        self.script(["Assessed."], [{"tool": "maintenance_pending", "args": {}}, ack])
        self.tick()
        self.assertEqual(len(self.wakes()), 1)
        prompt = (self.bin / "last_prompt.txt").read_text()
        self.assertIn(oid, prompt)
        self.assertIn("rosc_maintenance_ack", prompt)
        self.assertEqual(self.states(), ["completed"])
        self.assertEqual(self.wake_rows(), [("event", "replied", None)])
        self.tick()
        self.assertEqual(len(self.wakes()), 1)  # bounded: nothing new, no second turn
        self.assertEqual(self.conversation_count(), 0)  # nothing published to a conversation

    def test_reply_without_ack_stays_pending_and_never_loops_until_a_new_event(self) -> None:
        self.emit()
        self.script(["I will look later."], [])
        self.tick()
        self.assertEqual(self.states(), ["pending"])  # a reply is not a processed obligation
        self.tick()
        self.assertEqual(len(self.wakes()), 1)
        status = self.client.get(MAINT).json()
        self.assertEqual((status["next_actor"], status["latest_wake"]["state"]), ("Architect", "replied"))
        self.emit()
        self.tick()
        self.assertEqual(len(self.wakes()), 2)  # one more bounded turn for the new event
        prompt = (self.bin / "last_prompt.txt").read_text()
        self.assertIn("#1 run_reported", prompt)
        self.assertIn("#2 run_reported", prompt)

    def test_automatic_lookup_of_an_unresolved_turn_is_throttled(self) -> None:
        self.emit()
        self.late(["Done."], [])
        self.tick()
        calls = []
        original = self.architect._runtime.lookup
        with patch.object(self.architect._runtime, "lookup", lambda *a: calls.append(1) or original(*a)):
            for _ in range(3):
                self.tick()
        self.assertEqual(len(calls), 1)  # one provider lookup per interval, however often the monitor ticks

    @patch("rosc.architect_service.WAKE_LOOKUP_INTERVAL", 0.0)
    def test_ambiguous_turn_is_resolved_by_lookup_never_resent(self) -> None:
        self.emit()
        self.late(["Done."], [])
        self.tick()
        self.assertEqual(self.wake_rows()[0][1], "ambiguous")
        self.assertEqual(self.states(), ["processing"])
        status = self.client.get(MAINT).json()
        self.assertTrue(status["attention"])
        self.assertEqual(status["blocked_reason"], "wake_uncertain")
        for _ in range(2):
            self.tick()
            self.client.post(f"{MAINT}/wake")
            self.architect.drain()
        self.assertEqual(len(self.wakes()), 1)  # in progress: nothing sent over it
        self.release()
        self.unscript()
        self.tick()  # resolution only: the reply is found, the obligation is open again, no automatic resend
        self.assertEqual((self.wake_rows()[0][1], self.states(), len(self.wakes())), ("replied", ["pending"], 1))
        self.client.post(f"{MAINT}/wake")  # explicit Owner retry: one new turn
        self.architect.drain()
        self.assertEqual(len(self.wakes()), 2)
        self.assertEqual(self.wake_rows()[-1][0], "owner_retry")

    def test_restart_marks_an_interrupted_wake_ambiguous(self) -> None:
        self.emit()
        self.unscript()
        self.tick()
        with self.sessions() as session:
            session.connection().exec_driver_sql("update architect_maintenance_wakes set state='sent', settled_at=NULL")
            session.commit()
        self.architect.recover()
        self.assertEqual(self.wake_rows()[0][1:], ("ambiguous", "interrupted"))

    def test_not_familiarized_or_paused_leaves_work_pending_without_a_turn(self) -> None:
        self.send("pc-msg-0001", "hello")  # creates the Project conversation
        with self.sessions() as session:
            conversation = session.connection().exec_driver_sql(
                "select conversation_id from conversations where project_id=1").scalar()
            session.connection().exec_driver_sql(
                "insert into conversation_dispatch_controls values (?, 1, 0, 'owner', '2026-01-01')", (conversation,))
            session.commit()
        before = len(self.wakes())
        self.emit()
        self.tick()
        self.assertEqual((len(self.wakes()), self.states()), (before, ["pending"]))
        self.assertEqual(self.client.get(MAINT).json()["blocked_reason"], "paused")
        with self.sessions() as session:
            session.connection().exec_driver_sql("delete from conversation_dispatch_controls")
            session.connection().exec_driver_sql("update architect_sessions set onboarding_state='blocked'")
            session.commit()
        self.tick()
        self.assertEqual(len(self.wakes()), before)
        self.assertEqual(self.client.get(MAINT).json()["blocked_reason"], "architect_not_familiarized")


if __name__ == "__main__":
    import unittest

    unittest.main()
