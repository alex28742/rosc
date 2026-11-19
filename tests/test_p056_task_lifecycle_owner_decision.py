"""P056: Task lifecycle (Draft -> Active -> Completed) and the Owner Decision on a reviewed managed Run.

Synthetic only: temporary SQLite migrated to head, fake tmux/proc and the P054B/P055 fixtures.  No model call, no real
CLI/tmux, no Owner data, no live migration and no HTTP server (FastAPI TestClient in-process).
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from alembic import command

from rosc.authorization import ActorContext
from rosc.task_authoring import ArchitectAuthor, AuthoringConflict, AuthoringDenied
from test_p042_worker_session import OWNER
from test_p055_live_qa_review import ReviewCase

T1 = "/api/projects/P0001/tasks/T0001"
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


class LifecycleCase(ReviewCase):
    def task_status(self, task: int = 1) -> str:
        return self.rows(f"select status from tasks where id={task}")[0][0]

    def submit(self, rid: str, verdict: str = "review_passed", **extra) -> dict:
        args = {"run_id": rid, "verdict": verdict, "summary": "Read report, evidence and source.",
                "report_sha256": self.report_sha(rid), **extra}
        return self.call("review_submit", args)

    def reviewed(self, verdict: str = "review_passed", **extra) -> tuple[str, dict]:
        rid = self.reported(tests=False)
        review = self.submit(rid, verdict, **extra)["review"]
        return rid, review

    def decide(self, rid: str, review: dict, decision: str = "accept", key: str = "decision-0001", **extra):
        body = {"decision": decision, "review_id": review["review_id"], "review_version": review["version"],
                "request_key": key, **extra}
        return self.client.post(f"{T1}/runs/{rid}/decision", json=body)

    def architect_author(self) -> ArchitectAuthor:
        return ArchitectAuthor(project_id=1, agent_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()))


# -- A. lifecycle ---------------------------------------------------------------------------------------------------
class DraftStaysDraftTests(LifecycleCase):
    def test_creation_reads_owner_authoring_and_notes_keep_draft(self) -> None:
        self.assertEqual(self.task_status(), "Draft")
        self.client.get(f"{T1}/authoring")
        self.client.get("/api/projects/P0001/tasks")
        self.client.get(f"{T1}/conversation")
        self.client.get(f"{T1}/conversation/group")
        head = self.client.get(f"{T1}/authoring").json()
        saved = self.client.post(f"{T1}/spec/revisions", json={
            "expected_revision": head["spec"]["revision"], "title": "Edited by Owner", "description": "<p>d</p>",
            "acceptance_criteria": "<p>c</p>"})
        self.assertEqual(saved.status_code, 201, saved.text)
        self.approve()  # Owner Prompt Draft + approval
        note = self.client.post(f"{T1}/conversation/messages", json={"client_key": "owner-note-1", "body": "note"})
        self.assertEqual(note.status_code, 201, note.text)
        group_note = self.client.post(f"{T1}/conversation/group/messages",
                                      json={"client_key": "owner-note-2", "body": "only for me", "to": ["owner"]})
        self.assertEqual(group_note.status_code, 201, group_note.text)
        project = self.client.post("/api/projects/P0001/conversation/messages",
                                   json={"client_key": "project-msg-1", "body": "Project-wide"})
        self.assertEqual(project.status_code, 201, project.text)
        self.assertEqual(self.task_status(), "Draft")
        authoring = self.client.get(f"{T1}/authoring").json()
        self.assertEqual((authoring["task_status"], authoring["step"], authoring["editable"]), ("Draft", "draft", True))

    def test_architect_creating_a_future_task_does_not_activate_it(self) -> None:
        created = self.authoring.create_task(self.architect_author(), 1, title="Idea", description="<p>d</p>",
                                             acceptance_criteria="<p>c</p>")
        self.assertEqual(self.task_status(int(created["task_id"][1:])), "Draft")


class ActivationTests(LifecycleCase):
    def test_owner_message_addressed_to_architect_activates_once(self) -> None:
        body = {"client_key": "to-architect-1", "body": "Please refine", "recipient": "architect"}
        self.assertEqual(self.client.post(f"{T1}/conversation/messages", json=body).status_code, 201)
        self.assertEqual(self.task_status(), "Active")
        self.assertEqual(self.task_status(2), "Draft")  # another Task of the same Project is untouched
        replay = self.client.post(f"{T1}/conversation/messages", json=body)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.task_status(), "Active")

    def test_architect_tool_edit_of_existing_task_activates_it(self) -> None:
        head = self.client.get(f"{T1}/authoring").json()["spec"]
        self.authoring.save_spec(self.architect_author(), 1, 1, expected_revision=head["revision"], title="Refined",
                                 description="<p>d2</p>", acceptance_criteria="<p>c2</p>")
        self.assertEqual(self.task_status(), "Active")

    def test_architect_prompt_revision_activates_it(self) -> None:
        head = self.client.get(f"{T1}/authoring").json()["spec"]
        self.authoring.save_prompt(self.architect_author(), 1, 1, expected_revision=None,
                                   source_spec_revision=head["revision"], body="Do it", summary="1. Scope")
        self.assertEqual(self.task_status(), "Active")

    def test_first_formal_run_launch_activates_and_active_stays_editable(self) -> None:
        self.add_architect()
        self.running()
        self.assertEqual(self.task_status(), "Active")
        authoring = self.client.get(f"{T1}/authoring").json()
        self.assertEqual((authoring["task_status"], authoring["editable"]), ("Active", True))
        # Active keeps normal versioned authoring; the frozen Run revisions stay untouched
        frozen = self.rows("select spec_revision, spec_digest, prompt_revision, prompt_digest from managed_run_states")
        saved = self.client.post(f"{T1}/spec/revisions", json={
            "expected_revision": authoring["spec"]["revision"], "title": "Next iteration", "description": "<p>d</p>",
            "acceptance_criteria": "<p>c</p>"})
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(self.rows("select spec_revision, spec_digest, prompt_revision, prompt_digest "
                                   "from managed_run_states"), frozen)


# -- B. Owner Decision ------------------------------------------------------------------------------------------------
class OwnerDecisionTests(LifecycleCase):
    def test_review_passed_offers_both_real_actions(self) -> None:
        self.reviewed()
        nxt = self.next_view()
        self.assertEqual((nxt["next"]["action"], nxt["next"]["enabled"], nxt["next"]["decisions"], nxt["step"]),
                         ("owner_decision", True, ["accept", "request_rework"], "awaiting_owner_decision"))
        self.assertIn("не VERIFIED", nxt["next"]["hint"])

    def test_accept_completes_task_durably_without_touching_run_or_review(self) -> None:
        rid, review = self.reviewed()
        reviews_before = self.rows("select * from managed_run_reviews")
        response = self.decide(rid, review)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual((body["recorded"], body["task_status"], body["decision"]["decision"]),
                         (True, "Completed", "accept"))
        self.assertEqual(body["decision"]["review_id"], review["review_id"])
        self.assertNotIn("VERIFIED", body["decision"].values())
        replay = self.decide(rid, review)
        self.assertEqual((replay.status_code, replay.json()["replayed"]), (200, True))
        self.build()  # Core restart / reload
        view = self.next_view()
        self.assertEqual((view["task_status"], view["step"], view["next"]["action"], view["editable"]),
                         ("Completed", "completed", "completed", False))
        self.assertNotEqual(view["next"]["label"], "Ожидает решения Owner")
        self.assertEqual(self.state_row(rid)["state"], "reported")
        self.assertEqual(self.rows("select * from managed_run_reviews"), reviews_before)
        self.assertEqual(self.count("task_owner_decisions"), 1)
        tasks = {t["id"]: t["status"] for t in self.client.get("/api/projects/P0001/tasks").json()}
        self.assertEqual((tasks["T0001"], tasks["T0002"]), ("Completed", "Draft"))
        # a Completed Task is closed: no silent edit and no new Run
        head = view["spec"]
        edit = self.client.post(f"{T1}/spec/revisions", json={
            "expected_revision": head["revision"], "title": "x", "description": "<p>d</p>", "acceptance_criteria": "<p>c</p>"})
        self.assertEqual((edit.status_code, edit.json()["detail"]["code"]), (409, "not_editable"))
        sends = len(self.run_sends())
        launch = self.client.post(f"{T1}/runs", json={"agent_id": self.agent_id, "launch_key": "launch-after-done"})
        self.assertEqual((launch.status_code, launch.json()["detail"]["code"]), (409, "task_completed"))
        self.assertEqual(len(self.run_sends()), sends)

    def test_request_rework_needs_reason_keeps_active_and_starts_nothing(self) -> None:
        rid, review = self.reviewed()
        sends, runs = len(self.run_sends()), self.count("managed_run_states")
        missing = self.decide(rid, review, "request_rework")
        self.assertEqual(missing.status_code, 422, missing.text)
        blank = self.decide(rid, review, "request_rework", reason="   ")
        self.assertEqual(blank.status_code, 422, blank.text)
        response = self.decide(rid, review, "request_rework", reason="S02 edge case is missing")
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["task_status"], "Active")
        view = self.next_view()
        self.assertEqual((view["task_status"], view["step"], view["editable"]), ("Active", "rework_needed", True))
        self.assertEqual(view["next"]["rework"]["reason"], "S02 edge case is missing")
        self.assertEqual(view["run"]["decision"]["decision"], "request_rework")
        self.assertEqual((len(self.run_sends()), self.count("managed_run_states")), (sends, runs))
        self.assertEqual(self.state_row(rid)["state"], "reported")
        # an incompatible second decision gets clear feedback instead of overwriting history
        late = self.decide(rid, review, "accept", key="decision-0002")
        self.assertEqual((late.status_code, late.json()["detail"]["code"]), (409, "decision_already_recorded"))
        self.assertEqual(self.count("task_owner_decisions"), 1)

    def test_changes_requested_offers_only_rework(self) -> None:
        rid, review = self.reviewed("changes_requested", blocking_findings=["S01 test fails"])
        nxt = self.next_view()["next"]
        self.assertEqual((nxt["action"], nxt["decisions"]), ("review_changes_requested", ["request_rework"]))
        refused = self.decide(rid, review, "accept")
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (409, "decision_not_allowed"))
        self.assertEqual(self.task_status(), "Active")
        self.assertEqual(self.decide(rid, review, "request_rework", reason="Fix S01").status_code, 201)

    def test_inconclusive_and_missing_review_offer_nothing(self) -> None:
        rid = self.reported(tests=False)
        self.assertNotIn("decisions", self.next_view()["next"])
        fake = {"review_id": str(uuid.uuid4()), "version": 1}
        missing = self.decide(rid, fake)
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (409, "review_missing"))
        review = self.submit(rid, "inconclusive", limitations=["log truncated"], continue_by="architect")["review"]
        self.assertNotIn("decisions", self.next_view()["next"])
        for decision, extra in (("accept", {}), ("request_rework", {"reason": "r"})):
            refused = self.decide(rid, review, decision, **extra)
            self.assertEqual(refused.json()["detail"]["code"], "decision_not_allowed")
        self.assertEqual(self.count("task_owner_decisions"), 0)

    def test_stale_wrong_scope_and_non_owner_fail_cleanly(self) -> None:
        rid, review = self.reviewed()
        stale = self.decide(rid, {**review, "version": 2})
        self.assertEqual((stale.status_code, stale.json()["detail"]["code"]), (409, "review_stale"))
        other = self.decide(rid, {**review, "review_id": str(uuid.uuid4())})
        self.assertEqual(other.json()["detail"]["code"], "review_stale")
        wrong_task = self.client.post(f"/api/projects/P0001/tasks/T0002/runs/{rid}/decision", json={
            "decision": "accept", "review_id": review["review_id"], "review_version": 1, "request_key": "decision-x1"})
        self.assertEqual(wrong_task.status_code, 404, wrong_task.text)
        wrong_project = self.client.post(f"/api/projects/P0002/tasks/T0003/runs/{rid}/decision", json={
            "decision": "accept", "review_id": review["review_id"], "review_version": 1, "request_key": "decision-x2"})
        self.assertEqual(wrong_project.status_code, 404, wrong_project.text)
        agent = ActorContext("agent", str(uuid.uuid4()), "adapter_verified")
        with self.assertRaises(AuthoringDenied):
            self.authoring.owner_decision(agent, 1, 1, rid, decision="accept", review_id=review["review_id"],
                                          review_version=1, request_key="decision-x3")
        with self.assertRaises(AuthoringDenied):
            self.authoring.owner_decision(self.architect_author(), 1, 1, rid, decision="accept",
                                          review_id=review["review_id"], review_version=1, request_key="decision-x4")
        self.assertEqual((self.count("task_owner_decisions"), self.task_status()), (0, "Active"))

    def test_quick_repeated_click_records_one_decision(self) -> None:
        rid, review = self.reviewed()
        results = [self.decide(rid, review).status_code for _ in range(3)]
        self.assertEqual(results, [201, 200, 200])
        self.assertEqual(self.count("task_owner_decisions"), 1)
        with self.assertRaises(AuthoringConflict) as caught:
            self.authoring.owner_decision(OWNER, 1, 1, rid, decision="request_rework", reason="changed my mind",
                                          review_id=review["review_id"], review_version=1, request_key="decision-0001")
        self.assertEqual(caught.exception.code, "decision_already_recorded")

    def test_database_rejects_mutation_and_out_of_scope_rows(self) -> None:
        rid, review = self.reviewed()
        self.assertEqual(self.decide(rid, review, "request_rework", reason="r").status_code, 201)
        with sqlite3.connect(self.data / "rosc.db") as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("update task_owner_decisions set decision='accept'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("delete from task_owner_decisions")
        with sqlite3.connect(self.data / "rosc.db") as db:
            with self.assertRaises(sqlite3.IntegrityError):  # accept against a non-latest/mismatching review
                db.execute("insert into task_owner_decisions values (?,1,1,?,?,1,'changes_requested',?,1,1,'accept',"
                           "NULL,'k-123456','owner','o','a','2026-01-01')",
                           (str(uuid.uuid4()), rid, review["review_id"], self.report_sha(rid)))


# -- Migration / backfill -------------------------------------------------------------------------------------------
class BackfillTests(LifecycleCase):
    def test_backfill_marks_only_grounded_tasks_active_and_is_idempotent(self) -> None:
        self.reported(tests=False)  # T0001 has a managed Run
        command.downgrade(self.alembic, "0021_managed_run_reviews")
        self.assertEqual(self.rows("select id, status from tasks order by id"), [(1, "Draft"), (2, "Draft"), (3, "Draft")])
        self.assertEqual(self.rows("select count(*) from sqlite_master where name='task_owner_decisions'"), [(0,)])
        command.upgrade(self.alembic, "head")
        self.assertEqual(self.rows("select id, status from tasks order by id"), [(1, "Active"), (2, "Draft"), (3, "Draft")])
        source = (MIGRATIONS / "0022_task_lifecycle_owner_decision.py").read_text()
        statement = source.split('BACKFILL_ACTIVE = """', 1)[1].split('"""', 1)[0]
        with sqlite3.connect(self.data / "rosc.db") as db:  # running the backfill again changes nothing
            db.execute(statement)
            self.assertEqual(db.execute("select id, status from tasks order by id").fetchall(),
                             [(1, "Active"), (2, "Draft"), (3, "Draft")])

    def test_backfill_uses_task_scoped_agent_messages_not_owner_notes(self) -> None:
        self.client.post(f"{T1}/conversation/messages", json={"client_key": "to-architect-1", "body": "x",
                                                                "recipient": "architect"})
        self.client.post("/api/projects/P0001/tasks/T0002/conversation/messages",
                         json={"client_key": "owner-note-1", "body": "note only"})
        command.downgrade(self.alembic, "0021_managed_run_reviews")
        self.assertEqual(self.rows("select status from tasks where id in (1, 2) order by id"), [("Draft",), ("Draft",)])
        command.upgrade(self.alembic, "head")
        self.assertEqual(self.rows("select status from tasks where id in (1, 2) order by id"), [("Active",), ("Draft",)])

    def test_downgrade_refuses_with_decision_history(self) -> None:
        rid, review = self.reviewed()
        self.assertEqual(self.decide(rid, review).status_code, 201)
        with self.assertRaises(RuntimeError):
            command.downgrade(self.alembic, "0021_managed_run_reviews")
        self.assertEqual((self.count("task_owner_decisions"), self.task_status()), (1, "Completed"))
