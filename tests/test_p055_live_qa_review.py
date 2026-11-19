"""P055: Live QA corrections + Architect Evidence Tools + the recorded Architect Review.

Synthetic only: temporary SQLite migrated to head, fake tmux/proc, the REAL pinned Worker Artifact Tool run as a
subprocess (inherited from the P054B fixtures), synthetic Architect ``Caller`` objects for the broker.  No model call, no
real CLI/tmux, no Owner data, no live migration.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from unittest.mock import patch

from alembic import command

from rosc.app import DEFAULT_GLOBAL_LIBRARY
from rosc.architect_tools import TOOL_NAMES, TOOLS
from rosc.project_context import CAP_CONTEXT_READ, Caller, ProjectContextBroker, ToolError
from rosc.worker_conversation import WorkerConversationCore
from test_p042_worker_session import OWNER, SessionCase
from test_p054b_observable_worker_run import WorkerRunCase


class ReviewCase(WorkerRunCase):
    def broker(self) -> ProjectContextBroker:
        return ProjectContextBroker(self.sessions, self.data / "workspace", DEFAULT_GLOBAL_LIBRARY,
                                    run_reports=self.runs.report_for_tool, run_evidence=self.runs.evidence_for_tool)

    def caller(self, project: int = 1) -> Caller:
        if not hasattr(self, "_identity"):
            self._identity = (str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
        session_id, agent_id, assignment_id = self._identity
        return Caller(session_id, project, agent_id, assignment_id, "p", "m", frozenset({CAP_CONTEXT_READ}))

    def reported(self, *, tests: bool = True) -> str:
        self.add_architect()
        run = self.running()
        rid = run["run_id"]
        if tests:
            self.full_work(rid)
        else:
            self.plan(rid, steps=("S01",))
        self.finish(rid)
        self.runs.tick()
        self.assertEqual(self.state_row(rid)["state"], "reported")
        return rid

    def call(self, tool: str, args: dict, caller: Caller | None = None):
        with self.sessions() as session:
            return getattr(self.broker(), f"tool_{tool}")(session, caller or self.caller(), args)

    def refused(self, tool: str, args: dict, caller: Caller | None = None) -> ToolError:
        with self.assertRaises(ToolError) as caught:
            self.call(tool, args, caller)
        return caught.exception

    def report_sha(self, rid: str) -> str:
        return self.rows(f"select sha256 from managed_run_reports where run_id='{rid}'")[0][0]

    def next_view(self) -> dict:
        return self.client.get("/api/projects/P0001/tasks/T0001/authoring").json()

    def group(self) -> dict:
        response = self.client.get("/api/projects/P0001/tasks/T0001/conversation/group")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


# -- QA-RUN-HANDOFF-UNANSWERED ------------------------------------------------------------------------------------------
class HandoffClassificationTests(ReviewCase):
    def test_handoff_is_a_service_message_not_an_unanswered_peer_prompt(self) -> None:
        rid = self.reported(tests=False)
        status = self.group()
        self.assertEqual(status["unanswered"], [])
        self.assertIsNone(status["stop_reason"])
        self.assertNotEqual(status["engagement"], "group")
        self.assertFalse(status["round_active"])
        [handoff] = status["run_handoffs"]
        self.assertEqual((handoff["run_id"], handoff["review_state"], handoff["next_actor"]),
                         (rid, "awaiting_architect_review", "Architect"))
        # reload = a fresh Core/bridge: same truthful state; the history entry itself is unchanged and unique
        self.build()
        again = self.group()
        self.assertEqual((again["unanswered"], again["run_handoffs"]), ([], status["run_handoffs"]))
        self.assertEqual((self.count("managed_run_handoffs"), self.count("group_rounds"), self.count("group_turns")),
                         (1, 0, 0))
        messages = self.client.get("/api/projects/P0001/tasks/T0001/conversation").json()["messages"]
        self.assertEqual([m["message_id"] for m in messages if m["message_id"] == handoff["message_id"]],
                         [handoff["message_id"]])

    def test_reproducer_without_provenance_the_same_message_reads_as_unanswered(self) -> None:
        """The observed P054B symptom: only the durable receipt distinguishes it from a real @peer invitation, so the
        ordinary classifier still reports an Agent -> Agent message that nobody answered."""
        self.reported(tests=False)
        with patch.object(WorkerConversationCore, "is_run_handoff", staticmethod(lambda session, message_id: False)):
            status = self.group()
        self.assertEqual(len(status["unanswered"]), 1)
        self.assertEqual(status["engagement"], "group")


# -- Architect Evidence Tools -------------------------------------------------------------------------------------------
class EvidenceToolTests(ReviewCase):
    def test_list_and_read_match_the_journal_claims(self) -> None:
        rid = self.reported()
        listed = self.call("evidence_list", {"run_id": rid})
        self.assertEqual((listed["kind"], listed["evidence_directory"], listed["total"]),
                         ("run_evidence_list", "present", 2))
        items = {i["path"]: i for i in listed["items"]}
        self.assertEqual(set(items), {"evidence/T01.log", "evidence/T02.log"})
        log = (self.run_dir(rid) / "evidence/T01.log").read_bytes()
        t01 = items["evidence/T01.log"]
        self.assertEqual((t01["bytes"], t01["integrity"], t01["journal_claims"][0]["test_id"]),
                         (len(log), "matches_journal", "T01"))
        page = self.call("evidence_read", {"run_id": rid, "path": "evidence/T01.log", "limit": 3})
        self.assertEqual((page["content"], page["next_offset"], page["integrity"], page["verified_by_rosc"]),
                         (log.decode()[:3], 3, "matches_journal", False))
        rest = self.call("evidence_read", {"run_id": rid, "path": "evidence/T01.log", "offset": 3})
        self.assertEqual(page["content"] + rest["content"], log.decode())
        self.assertIsNone(rest["next_offset"])
        self.assertEqual(rest["sha256"], t01["sha256"])

    def test_bounded_listing_pages(self) -> None:
        rid = self.reported()
        first = self.call("evidence_list", {"run_id": rid, "limit": 1})
        second = self.call("evidence_list", {"run_id": rid, "offset": first["next_offset"], "limit": 1})
        self.assertEqual((len(first["items"]), first["next_offset"], second["next_offset"]), (1, 1, None))
        self.assertNotEqual(first["items"][0]["path"], second["items"][0]["path"])

    def test_no_current_run_is_ambiguous_and_never_the_latest(self) -> None:
        rid = self.reported()
        error = self.refused("evidence_list", {})
        self.assertEqual(error.code, "run_ambiguous")
        self.assertEqual([c["run_id"] for c in error.extra["candidates"]], [rid])
        by_task = self.call("evidence_list", {"task_id": "T0001"})  # exactly one managed Run of that Task
        self.assertEqual(by_task["run_id"], rid)
        self.assertEqual(self.refused("evidence_list", {"task_id": "T0002"}).code, "no_managed_run")

    def test_wrong_project_traversal_symlink_and_nonregular_are_refused(self) -> None:
        rid = self.reported()
        self.assertEqual(self.refused("evidence_list", {"run_id": rid}, self.caller(2)).code, "not_found")
        self.assertEqual(self.refused("evidence_read", {"run_id": rid, "path": "evidence/T01.log"},
                                      self.caller(2)).code, "not_found")
        self.assertIn(self.refused("evidence_list", {"run_id": rid, "task_id": "T0002"}).code,
                      ("not_found", "task_not_found"))
        for path in ("../package/run.json", "evidence/../reports/final.md", "/etc/passwd", "evidence/sub/x.log",
                     "reports/final.md", str(self.run_dir(rid) / "evidence/T01.log"), "evidence/.hidden"):
            with self.subTest(path=path):
                self.assertEqual(self.refused("evidence_read", {"run_id": rid, "path": path}).code, "invalid_path")
        evidence = self.run_dir(rid) / "evidence"
        os.chmod(evidence, 0o700)
        os.symlink(self.run_dir(rid) / "reports/final.md", evidence / "link.log")
        (evidence / "nested").mkdir()
        self.assertEqual(self.refused("evidence_read", {"run_id": rid, "path": "evidence/link.log"}).code,
                         "unsafe_path")
        self.assertEqual(self.refused("evidence_read", {"run_id": rid, "path": "evidence/nested"}).code,
                         "not_regular_file")
        items = {i["name"]: i for i in self.call("evidence_list", {"run_id": rid})["items"]}
        self.assertEqual((items["link.log"]["status"], items["nested"]["status"]),
                         ("refused_unsafe", "not_regular_file"))
        self.assertEqual(items["link.log"]["integrity"], "unreferenced")

    def test_modified_moved_and_binary_evidence_are_reported_honestly(self) -> None:
        rid = self.reported()
        evidence = self.run_dir(rid) / "evidence"
        os.chmod(evidence, 0o700)
        os.chmod(evidence / "T01.log", 0o600)
        (evidence / "T01.log").write_text("3 passed (edited)\n")
        page = self.call("evidence_read", {"run_id": rid, "path": "evidence/T01.log"})
        self.assertEqual((page["integrity"], page["content"]), ("mismatch_journal", "3 passed (edited)\n"))
        os.rename(evidence / "T02.log", evidence / "T02.moved")
        missing = self.refused("evidence_read", {"run_id": rid, "path": "evidence/T02.log"})
        self.assertEqual((missing.code, missing.extra["claimed_by_journal"]), ("not_found", True))
        items = {i["path"]: i for i in self.call("evidence_list", {"run_id": rid})["items"]}
        self.assertEqual((items["evidence/T02.log"]["status"], items["evidence/T02.moved"]["integrity"],
                          items["evidence/T01.log"]["integrity"]), ("missing", "unreferenced", "mismatch_journal"))
        (evidence / "blob.bin").write_bytes(b"\x00\x01")
        binary = self.refused("evidence_read", {"run_id": rid, "path": "evidence/blob.bin"})
        self.assertEqual((binary.code, binary.extra["integrity"], binary.extra["bytes"]), ("not_text", "unreferenced", 2))

    def test_missing_evidence_directory_and_no_side_effects(self) -> None:
        rid = self.reported(tests=False)
        before = self.rows("select count(*) from architect_tool_audit") + self.rows("select count(*) from managed_run_events")
        listed = self.call("evidence_list", {"run_id": rid})
        self.assertEqual((listed["evidence_directory"], listed["items"], listed["total"]), ("absent", [], 0))
        self.assertEqual(self.refused("evidence_read", {"run_id": rid, "path": "evidence/T01.log"}).code, "not_found")
        after = self.rows("select count(*) from architect_tool_audit") + self.rows("select count(*) from managed_run_events")
        self.assertEqual(after, before)
        self.assertEqual(self.state_row(rid)["state"], "reported")
        self.assertEqual(len(self.run_sends()), 2)

    def test_inactive_session_is_denied_and_tool_surface_registered(self) -> None:
        rid = self.reported(tests=False)
        result = self.broker().call(str(uuid.uuid4()), "evidence_list", {"run_id": rid})
        self.assertEqual((result["ok"], result["error"]["code"]), (False, "session_not_active"))
        self.assertTrue({"evidence_list", "evidence_read", "review_submit"} <= set(TOOL_NAMES))
        schemas = {t["name"]: t["inputSchema"] for t in TOOLS}
        self.assertNotIn("project", schemas["evidence_read"]["properties"])  # no cross-Project selection
        self.assertEqual(schemas["evidence_read"]["required"], ["path"])


# -- QA-RUN-REVIEW-NEXT-STATE -------------------------------------------------------------------------------------------
class ReviewSubmitTests(ReviewCase):
    def submit(self, rid: str, verdict: str = "review_passed", caller: Caller | None = None, **extra) -> dict:
        args = {"run_id": rid, "verdict": verdict, "summary": "Read report, evidence and source.",
                "report_sha256": self.report_sha(rid), **extra}
        return self.call("review_submit", args, caller)

    def test_reported_without_review_stays_awaiting_architect(self) -> None:
        rid = self.reported(tests=False)
        self.assertEqual((self.next_view()["next"]["action"], self.next_view()["next"]["next_actor"]),
                         ("awaiting_review", "Architect"))
        view = self.runs.status(OWNER, 1, rid)
        self.assertEqual((view["review"]["state"], view["review"]["latest"]), ("awaiting_architect_review", None))

    def test_review_passed_switches_next_actor_to_owner_and_survives_reload(self) -> None:
        rid = self.reported()
        result = self.submit(rid, limitations=["Worker logs only; nothing re-executed"])
        self.assertEqual((result["recorded"], result["review"]["verdict"], result["review"]["version"]),
                         (True, "review_passed", 1))
        self.build()  # reload / Core restart
        nxt = self.next_view()["next"]
        self.assertEqual((nxt["action"], nxt["label"], nxt["next_actor"]),
                         ("owner_decision", "Ожидает решения Owner", "Owner"))
        self.assertIn("не VERIFIED", nxt["hint"])
        run = self.client.get(f"/api/projects/P0001/runs/{rid}").json()
        self.assertEqual((run["state"], run["review"]["state"], run["review"]["next_actor"]),
                         ("reported", "review_passed", "Owner"))
        latest = run["review"]["latest"]
        self.assertEqual((latest["report_sha256"], latest["architect"]["agent_id"]),
                         (self.report_sha(rid), self.caller().agent_id))
        self.assertEqual(self.group()["run_handoffs"][0]["review_state"], "review_passed")
        audit = self.rows("select tool, outcome from architect_tool_audit where tool='review_record'")
        self.assertEqual(audit, [("review_record", "recorded:review_passed")])
        self.assertEqual((self.count("group_turns"), len(self.run_sends())), (0, 2))  # no invocation side effects

    def test_identical_replay_is_idempotent_and_contradiction_fails_closed(self) -> None:
        rid = self.reported()
        self.submit(rid)
        again = self.submit(rid)
        self.assertEqual((again["recorded"], again["replayed"]), (False, True))
        with self.assertRaises(ToolError) as caught:
            self.submit(rid, "changes_requested", blocking_findings=["S03 test fails"])
        self.assertEqual(caught.exception.code, "review_already_recorded")
        self.assertEqual(self.count("managed_run_reviews"), 1)

    def test_changes_requested_and_inconclusive_never_impersonate_approval(self) -> None:
        rid = self.reported()
        with self.assertRaises(ToolError) as caught:
            self.submit(rid, "changes_requested")
        self.assertEqual(caught.exception.code, "invalid_argument")
        with self.assertRaises(ToolError):
            self.submit(rid, "review_passed", blocking_findings=["x"])
        with self.assertRaises(ToolError):
            self.submit(rid, "inconclusive", limitations=["need CI log"])  # continue_by missing
        self.submit(rid, "inconclusive", limitations=["T02 log truncated"], continue_by="architect")
        nxt = self.next_view()["next"]
        self.assertEqual((nxt["action"], nxt["label"], nxt["next_actor"]),
                         ("review_incomplete", "Review incomplete / needs evidence", "Architect"))
        # explicit superseding of the inconclusive review needs the optimistic version check
        with self.assertRaises(ToolError) as caught:
            self.submit(rid, "changes_requested", blocking_findings=["S03 test fails"])
        self.assertEqual(caught.exception.code, "review_revision_conflict")
        result = self.submit(rid, "changes_requested", blocking_findings=["S03 test fails"], expected_version=1)
        self.assertEqual((result["review"]["version"], result["review"]["supersedes_review_id"] is not None), (2, True))
        nxt = self.next_view()["next"]
        self.assertEqual((nxt["action"], nxt["next_actor"]), ("review_changes_requested", "Owner"))
        self.assertIn("Блокирующих замечаний: 1", nxt["hint"])
        run = self.runs.status(OWNER, 1, rid)
        self.assertEqual((run["state"], run["review"]["latest"]["findings"]), ("reported", ["S03 test fails"]))
        self.assertEqual(self.count("managed_run_reviews"), 2)  # history kept, nothing replaced
        self.assertEqual(self.count("managed_run_states"), 1)   # no new Worker Run was created

    def test_inconclusive_owner_continuation_names_the_owner(self) -> None:
        rid = self.reported()
        self.submit(rid, "inconclusive", limitations=["needs Owner-run integration test"], continue_by="owner")
        self.assertEqual(self.next_view()["next"]["next_actor"], "Owner")

    def test_scope_digest_and_state_refusals(self) -> None:
        rid = self.reported()
        sha = self.report_sha(rid)
        cases = [({"run_id": str(uuid.uuid4())}, "not_found"), ({"run_id": "../x"}, "not_found"),
                 ({"run_id": rid, "report_sha256": "0" * 64}, "report_digest_mismatch"),
                 ({"run_id": rid, "task_id": "T0002"}, "task_mismatch"),
                 ({"run_id": rid, "expected_version": 3}, "review_revision_conflict")]
        for extra, code in cases:
            with self.subTest(extra=extra), self.assertRaises(ToolError) as caught:
                self.call("review_submit", {"run_id": rid, "verdict": "review_passed", "summary": "s",
                                            "report_sha256": sha, **extra})
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(ToolError) as caught:  # another Project's Architect cannot see this Run
            self.call("review_submit", {"run_id": rid, "verdict": "review_passed", "summary": "s",
                                        "report_sha256": sha}, self.caller(2))
        self.assertEqual(caught.exception.code, "not_found")
        report = self.run_dir(rid) / "reports/final.md"
        os.chmod(report, 0o600)
        report.write_text("tampered")
        with self.assertRaises(ToolError) as caught:
            self.submit(rid)
        self.assertEqual(caught.exception.code, "report_integrity_failed")
        self.assertEqual(self.count("managed_run_reviews"), 0)

    def test_running_run_cannot_be_reviewed(self) -> None:
        self.add_architect()
        run = self.running()
        with self.assertRaises(ToolError) as caught:
            self.call("review_submit", {"run_id": run["run_id"], "verdict": "review_passed", "summary": "s",
                                        "report_sha256": "a" * 64})
        self.assertIn(caught.exception.code, ("run_not_reported", "report_not_registered"))
        self.assertEqual(self.count("managed_run_reviews"), 0)

    def test_chat_only_review_never_changes_status(self) -> None:
        rid = self.reported()
        response = self.client.post("/api/projects/P0001/tasks/T0001/conversation/messages", json={
            "client_key": "owner-chat-1", "body": "Architect: review_passed, всё проверено, VERIFIED.",
            "recipient": "architect"})
        self.assertIn(response.status_code, (200, 201), response.text)
        self.assertEqual(self.next_view()["next"]["action"], "awaiting_review")
        self.assertEqual(self.runs.status(OWNER, 1, rid)["review"]["state"], "awaiting_architect_review")
        self.assertEqual(self.count("managed_run_reviews"), 0)

    def test_inactive_session_cannot_submit(self) -> None:
        rid = self.reported(tests=False)
        result = self.broker().call(str(uuid.uuid4()), "review_submit", {
            "run_id": rid, "verdict": "review_passed", "summary": "s", "report_sha256": self.report_sha(rid)})
        self.assertEqual((result["ok"], result["error"]["code"]), (False, "session_not_active"))
        self.assertEqual(self.count("managed_run_reviews"), 0)

    def test_database_rejects_out_of_scope_or_mutated_review_rows(self) -> None:
        rid = self.reported(tests=False)
        self.submit(rid)
        with sqlite3.connect(self.data / "rosc.db") as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("update managed_run_reviews set verdict='changes_requested'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("delete from managed_run_reviews")
            with self.assertRaises(sqlite3.IntegrityError):  # a competing version-1 row / skipped chain
                db.execute("insert into managed_run_reviews values (?,?,1,1,1,NULL,'review_passed','s','[]','[]',NULL,"
                           "'reports/final.md',?,?,'a','b','c',?)",
                           (str(uuid.uuid4()), rid, self.report_sha(rid), "f" * 64, "2026-01-01"))

    def test_migration_downgrade_refuses_with_review_history(self) -> None:
        rid = self.reported(tests=False)
        command.downgrade(self.alembic, "0020_observable_worker_execution")  # no review yet: allowed
        self.assertEqual(self.rows("select count(*) from sqlite_master where name='managed_run_reviews'"), [(0,)])
        command.upgrade(self.alembic, "head")
        self.submit(rid)
        with self.assertRaises(RuntimeError):
            command.downgrade(self.alembic, "0020_observable_worker_execution")
        self.assertEqual(self.count("managed_run_reviews"), 1)


# -- First-run bootstrap of a Project without documentation entry points ----------------------------------------------
class IdentityOnlyBootstrapTests(SessionCase):
    def make_active(self):
        self.activate()
        incarnation, pid, _ = self.current()
        self.write_ack(incarnation, [pid, 1])
        return incarnation, pid

    def bootstrap(self) -> dict:
        return self.client.post(self.url("session/bootstrap")).json()["bootstrap"]

    def ack(self, incarnation: str, pid: int, *, claimed: str = "OK", token: str | None = None) -> None:
        token = token or self.rows("select bootstrap_token from worker_sessions")[0][0]
        (self.state_dir / f"{incarnation}.bootstrap.json").write_text(json.dumps({
            "incarnation": incarnation, "token": token, "claimed": claimed, "observed_reads": [],
            "ancestors": [pid, 1], "acked_at": time.time()}))

    def test_empty_project_explicit_identity_only_bootstrap_confirms_with_native_ack(self) -> None:
        incarnation, pid = self.make_active()
        state = self.status()["bootstrap"]
        self.assertEqual((state["state"], state["reason"], state["can_bootstrap"], state["identity"]),
                         ("none", "no_sources", True, "provided_at_launch"))
        self.assertEqual(self.tmux.sent, [])                      # page open / status never sends
        self.assertEqual(self.bootstrap()["state"], "pending")
        self.assertEqual(len(self.tmux.sent), 1)
        self.assertIn("Читать файлы не нужно", self.tmux.sent[0][1])
        self.assertEqual(self.status()["bootstrap"]["state"], "pending")  # no fake confirmation without the ACK
        self.ack(incarnation, pid, token=str(uuid.uuid4()))       # foreign token: not ours
        self.assertEqual(self.status()["bootstrap"]["state"], "pending")
        self.ack(incarnation, pid)
        state = self.status()["bootstrap"]
        self.assertEqual((state["state"], state["reason"]), ("confirmed", "identity_acknowledged"))
        self.bootstrap()                                           # idempotent: confirmed is never resent
        self.activate()
        self.assertEqual(len(self.tmux.sent), 1)
        self.assertFalse((self.root / "AGENTS.md").exists())      # no documentation was created

    def test_failed_unknown_and_stale_identity_acknowledgements(self) -> None:
        incarnation, pid = self.make_active()
        self.bootstrap()
        self.ack(incarnation, pid, claimed="FAILED")
        state = self.status()["bootstrap"]
        self.assertEqual((state["state"], state["can_bootstrap"]), ("failed", True))
        self.activate()
        self.assertEqual(len(self.tmux.sent), 1)                  # a retry is only the explicit action
        self.bootstrap()
        self.assertEqual(len(self.tmux.sent), 2)
        self.clock_now[0] += 400
        state = self.status()["bootstrap"]
        self.assertEqual((state["state"], state["can_bootstrap"]), ("unknown", False))
        self.bootstrap()
        self.assertEqual(len(self.tmux.sent), 2)                  # unknown is never blindly resent

    def test_existing_entry_point_still_requires_reading_it(self) -> None:
        (self.root / "AGENTS.md").write_text("# rules\n")
        incarnation, pid = self.make_active()
        self.assertEqual(len(self.tmux.sent), 0)
        state = self.bootstrap()
        self.assertEqual((state["state"], state["sources"]), ("pending", ["AGENTS.md"]))
        self.ack(incarnation, pid)                                 # no observed Read of AGENTS.md
        self.assertEqual(self.status()["bootstrap"]["reason"], "reads_missing")

    def test_missing_cli_offers_nothing(self) -> None:
        self.activate()                                            # connecting: no ACK from the CLI yet
        state = self.status()["bootstrap"]
        self.assertEqual((state["reason"], state["can_bootstrap"]), ("cli_not_active", False))
        self.client.post(self.url("session/bootstrap"))
        self.assertEqual(self.tmux.sent, [])


# -- QA-ARCH-STATUS-UI (backend side): a zero-message room reports the same eligibility as a used one ----------------
from test_p015_architect_activation import CAPS as ARCH_CAPS, FakeRuntime  # noqa: E402
from test_p047_owner_qa_corrections import P047Case  # noqa: E402


class FreshRoomEligibilityTests(P047Case):
    def make_client(self, **kw):
        self.arch_runtime = FakeRuntime()
        return super().make_client(architect_runtime=self.arch_runtime, **kw)

    def setUp(self) -> None:
        super().setUp()
        architect = self.registry.register_agent(OWNER, display_name="Architect", runtime_adapter="opencode",
                                                 provider_id="prov", model_id="model-a", capabilities=ARCH_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=architect["agent_id"], role="architect",
                                        capabilities=ARCH_CAPS)
        self.arch = architect["agent_id"]

    def architect_entry(self, task: str | None = None) -> dict:
        status = self.api.get(self.url(task)).json()
        return next(e for e in status["roster"] if e["role"] == "architect"), status

    def test_zero_message_room_status_is_truthful_before_and_after_activation(self) -> None:
        for task in (None, "T0001"):
            entry, status = self.architect_entry(task)
            self.assertEqual((status["conversation_id"], status["has_owner_message"]), (None, False))
            self.assertEqual((entry["group_supported"], entry["unsupported_reason"]), (False, "architect_not_active"))
        self.assertEqual(self.api.post("/api/projects/P0001/architect/activate").json()["state"], "ready")
        self.app.state.architect.drain()
        for task in (None, "T0001"):  # still zero messages: eligibility does not depend on a prior message
            entry, status = self.architect_entry(task)
            self.assertEqual((status["conversation_id"], entry["group_supported"], entry["unsupported_reason"]),
                             (None, True, None))
        self.assertEqual(self.messages(), [])
