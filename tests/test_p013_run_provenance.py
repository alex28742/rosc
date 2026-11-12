from __future__ import annotations

import sqlite3
import threading
import unittest
import uuid

from alembic import command

import test_slice01 as base
from rosc import authorization as authz
from rosc.agent_registry import (CAPABILITY_ARCHITECT_DOCUMENT_READ, CAPABILITY_ARCHITECT_DOCUMENT_WRITE,
                                 CAPABILITY_PROJECT_CONTEXT_READ, CAPABILITY_TASK_CONTEXT_READ, AgentRegistryService)
from rosc.authorization import ActorContext, AuthorizationDecision, LocalOwnerPolicy, LocalOwnerProvider, ResourceScope, evaluate
from rosc.db import make_session_factory
from rosc.run_provenance import (InvalidRunInput, RunConflict, RunNotFound, RunProvenanceDenied, RunProvenanceService,
                                 UnsafeRunProjectIdentity)

OWNER = LocalOwnerProvider().actor()
TASK = {"title": "T", "description": "D", "acceptance_criteria": "A"}
WORKER_CAPS = [CAPABILITY_PROJECT_CONTEXT_READ, CAPABILITY_TASK_CONTEXT_READ]
ARCHITECT_CAPS = [CAPABILITY_PROJECT_CONTEXT_READ, CAPABILITY_ARCHITECT_DOCUMENT_READ,
                  CAPABILITY_ARCHITECT_DOCUMENT_WRITE]
DIGEST = "a" * 64
OWNED_TABLES = ("run_records", "run_agent_links", "report_records", "evidence_records")
LEGACY_TABLES = ("projects", "project_root_bindings", "tasks", "agent_definitions", "agent_capabilities",
                 "role_assignments", "assignment_capabilities")


class DenyAll:
    def decide(self, actor, action, resource):
        return AuthorizationDecision(False, "test_denied")


class ProvenanceCase(base.StageCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.register(self.new_root()).status_code, 201)
        self.assertEqual(self.client.post("/api/projects/P0001/tasks", json=TASK).status_code, 201)
        self.svc = self.service()
        self.agents = AgentRegistryService(make_session_factory(self.data / "rosc.db"), LocalOwnerPolicy())

    def service(self, policy=None) -> RunProvenanceService:
        return RunProvenanceService(make_session_factory(self.data / "rosc.db"), policy or LocalOwnerPolicy())

    def second_project(self) -> None:
        path = self.base / "second"
        path.mkdir()
        self.assertEqual(self.register(path).status_code, 201)
        self.assertEqual(self.client.post("/api/projects/P0002/tasks", json=TASK).status_code, 201)

    def agent(self, name: str = "Worker One", caps=WORKER_CAPS) -> dict:
        return self.agents.register_agent(OWNER, display_name=name, runtime_adapter="manual", provider_id="local",
                                          model_id="model-a", capabilities=caps)

    def mkrun(self, key="k1", **kw) -> dict:
        kw.setdefault("source_kind", "manual-report")
        return self.svc.register_external_run(OWNER, 1, 1, source_key=key, **kw)

    def fetch(self, statement: str, *args) -> list[tuple]:
        with sqlite3.connect(self.data / "rosc.db") as connection:
            return connection.execute(statement, args).fetchall()

    def sql(self, statement: str, *args) -> None:
        with sqlite3.connect(self.data / "rosc.db") as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(statement, args)

    def state(self) -> dict:
        workspace = self.data / "workspace"
        tree = {str(p.relative_to(workspace)): p.read_bytes() for p in sorted(workspace.rglob("*")) if p.is_file()}
        with sqlite3.connect(self.data / "rosc.db") as connection:
            rows = {t: connection.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid").fetchall()
                    for t in OWNED_TABLES + LEGACY_TABLES}
        return {"tree": tree, "rows": rows}


class IdentityTests(ProvenanceCase):
    def test_many_runs_per_task_survive_restart_and_touch_nothing_else(self) -> None:
        before = self.state()
        first, second = self.mkrun("a"), self.mkrun("b", reported_executor="Some Tool 1")
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertTrue(authz.is_canonical_uuid4(first["run_id"]))
        after = self.state()
        for table in LEGACY_TABLES:
            self.assertEqual(after["rows"][table], before["rows"][table], table)
        self.assertEqual(after["tree"], before["tree"])
        self.assertEqual(self.fetch("SELECT status FROM tasks"), [("Draft",)])
        restarted = self.service()
        listed = restarted.list_runs(OWNER, 1, 1)
        self.assertEqual([r["run_id"] for r in listed], [first["run_id"], second["run_id"]])
        self.assertEqual(listed[0]["execution_origin"], "external")
        self.assertEqual(listed[0]["source_attribution_assurance"], "claimed")
        self.assertEqual(listed[0]["registered_by_assurance"], "local_unverified")
        for forbidden in ("verified", "accepted", "status", "success"):
            self.assertNotIn(forbidden, listed[0])
        self.assertEqual(self.client.get("/api/health").json()["agent_runtime"], "not_started")
        self.assertEqual(self.client.get("/api/projects").json()[0]["active_workers"], 0)

    def test_run_requires_existing_task_of_same_project(self) -> None:
        self.second_project()
        before = self.state()
        with self.assertRaises(RunNotFound):
            self.svc.register_external_run(OWNER, 1, 2, source_kind="manual-report", source_key="x")
        with self.assertRaises(RunNotFound):
            self.svc.register_external_run(OWNER, 1, 99, source_kind="manual-report")
        with self.assertRaises(RunNotFound):
            self.svc.register_external_run(OWNER, 99, 1, source_kind="manual-report")
        self.assertEqual(self.state(), before)
        # The database also refuses a mismatching Run/Task/Project row written around the service.
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO run_records VALUES (?, 1, 2, 'external', 'k', NULL, 'claimed', NULL, NULL, NULL, "
                     "'unlinked', 1, 'local-owner', 'local_unverified', '2026-01-01 00:00:00')", str(uuid.uuid4()))

    def test_same_source_key_in_different_projects_does_not_collide(self) -> None:
        self.second_project()
        one = self.svc.register_external_run(OWNER, 1, 1, source_kind="manual-report", source_key="same")
        two = self.svc.register_external_run(OWNER, 2, 2, source_kind="manual-report", source_key="same")
        self.assertNotEqual(one["run_id"], two["run_id"])
        self.assertTrue(one["created"] and two["created"])

    def test_managed_origin_is_denied_without_side_effects(self) -> None:
        before = self.state()
        with self.assertRaises(RunProvenanceDenied):
            self.svc.register_external_run(OWNER, 1, 1, source_kind="manual-report", execution_origin="managed")
        with self.assertRaises(InvalidRunInput):
            self.svc.register_external_run(OWNER, 1, 1, source_kind="manual-report", execution_origin="other")
        self.assertEqual(self.state(), before)
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO run_records VALUES (?, 1, 1, 'bogus', 'k', NULL, 'claimed', NULL, NULL, NULL, "
                     "'unlinked', 1, 'local-owner', 'local_unverified', '2026-01-01 00:00:00')", str(uuid.uuid4()))


class AttributionTests(ProvenanceCase):
    def test_unlinked_run_then_owner_link_keeps_original_claim_and_history(self) -> None:
        run = self.mkrun(reported_executor="Claude Code 1")
        self.assertIsNone(run["agent_id"])
        self.assertEqual(run["agent_link_status"], "unlinked")
        self.assertEqual(self.agents.list_agents(OWNER), [])
        agent = self.agent("Claude Code 1")  # same label as the claim: still no automatic matching
        self.assertIsNone(self.svc.get_run(OWNER, 1, run["run_id"])["agent_id"])
        linked = self.svc.link_external_agent(OWNER, 1, run["run_id"], agent_id=agent["agent_id"], expected_revision=1)
        self.assertTrue(linked["changed"])
        self.assertEqual(linked["agent_link_status"], "owner_curated_correspondence")
        self.assertEqual((linked["reported_executor"], linked["source_key"]), ("Claude Code 1", "k1"))
        self.assertEqual(linked["attribution_revision"], 2)
        history = self.svc.get_run(OWNER, 1, run["run_id"])["agent_link_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]["from_agent_id"], history[0]["agent_id"], history[0]["original_claim"],
                          history[0]["expected_revision"], history[0]["resulting_revision"], history[0]["linked_by"]),
                         (None, agent["agent_id"], "Claude Code 1", 1, 2, "local-owner"))
        self.assertEqual(self.fetch("SELECT count(*) FROM role_assignments"), [(0,)])

    def test_repeat_is_idempotent_other_target_and_stale_revision_conflict(self) -> None:
        run = self.mkrun()
        one, two = self.agent("One"), self.agent("Two")
        with self.assertRaises(RunConflict):
            self.svc.link_external_agent(OWNER, 1, run["run_id"], agent_id=one["agent_id"], expected_revision=7)
        self.svc.link_external_agent(OWNER, 1, run["run_id"], agent_id=one["agent_id"], expected_revision=1)
        again = self.svc.link_external_agent(OWNER, 1, run["run_id"], agent_id=one["agent_id"], expected_revision=1)
        self.assertFalse(again["changed"])
        before = self.state()
        with self.assertRaises(RunConflict):
            self.svc.link_external_agent(OWNER, 1, run["run_id"], agent_id=two["agent_id"], expected_revision=2)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.fetch("SELECT count(*) FROM run_agent_links"), [(1,)])
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("UPDATE run_records SET agent_id = ? WHERE run_id = ?", two["agent_id"], run["run_id"])

    def test_invalid_targets_and_assignments_fail_without_side_effects(self) -> None:
        run = self.mkrun()
        agent, other = self.agent("One"), self.agent("Two")
        self.second_project()
        before = self.state()
        for bad in (str(uuid.uuid4()),):
            with self.assertRaises(RunNotFound):
                self.svc.link_external_agent(OWNER, 1, run["run_id"], agent_id=bad, expected_revision=1)
        for bad in ("nope", agent["agent_id"].upper(), 5, None):
            with self.assertRaises(RunProvenanceDenied):  # malformed scope is denied before any lookup
                self.svc.link_external_agent(OWNER, 1, run["run_id"], agent_id=bad, expected_revision=1)
        with self.assertRaises(RunProvenanceDenied):
            self.svc.link_external_agent(OWNER, 1, "nope", agent_id=agent["agent_id"], expected_revision=1)
        with self.assertRaises(RunNotFound):  # Run exists, but in another Project
            self.svc.link_external_agent(OWNER, 2, run["run_id"], agent_id=agent["agent_id"], expected_revision=1)
        with self.assertRaises(InvalidRunInput):
            self.svc.link_external_agent(OWNER, 1, run["run_id"], agent_id=agent["agent_id"], expected_revision=0)
        with self.assertRaises(InvalidRunInput):  # unknown assignment
            self.svc.link_external_agent(OWNER, 1, run["run_id"], agent_id=agent["agent_id"], expected_revision=1,
                                         assignment_id=str(uuid.uuid4()))
        mine = self.client.post("/api/projects/P0001/assignments", json={
            "agent_id": other["agent_id"], "role": "worker", "capabilities": WORKER_CAPS}).json()
        with self.assertRaises(InvalidRunInput):  # assignment of a different Agent
            self.svc.link_external_agent(OWNER, 1, run["run_id"], agent_id=agent["agent_id"], expected_revision=1,
                                         assignment_id=mine["assignment_id"])
        revoked = self.client.post("/api/projects/P0001/assignments", json={
            "agent_id": agent["agent_id"], "role": "worker", "capabilities": WORKER_CAPS}).json()
        self.client.post(f"/api/projects/P0001/assignments/{revoked['assignment_id']}/revoke",
                         json={"expected_version": revoked["version"]})
        before = self.state()
        with self.assertRaises(InvalidRunInput):  # revoked assignment
            self.svc.link_external_agent(OWNER, 1, run["run_id"], agent_id=agent["agent_id"], expected_revision=1,
                                         assignment_id=revoked["assignment_id"])
        self.assertEqual(self.state(), before)

    def test_active_architect_assignment_is_not_valid_run_attribution(self) -> None:
        run = self.mkrun()
        agent = self.agent("Architect", ARCHITECT_CAPS)
        assignment = self.client.post("/api/projects/P0001/assignments", json={
            "agent_id": agent["agent_id"], "role": "architect", "capabilities": ARCHITECT_CAPS}).json()
        before = self.state()
        with self.assertRaises(InvalidRunInput):
            self.svc.link_external_agent(OWNER, 1, run["run_id"], agent_id=agent["agent_id"], expected_revision=1,
                                         assignment_id=assignment["assignment_id"])
        self.assertEqual(self.state(), before)

    def test_active_assignment_may_be_referenced_but_grants_nothing(self) -> None:
        run = self.mkrun()
        agent = self.agent()
        assignment = self.client.post("/api/projects/P0001/assignments", json={
            "agent_id": agent["agent_id"], "role": "worker", "capabilities": WORKER_CAPS}).json()
        self.assertEqual(self.fetch("SELECT count(*) FROM run_records"), [(1,)], "an assignment alone creates no Run")
        linked = self.svc.link_external_agent(OWNER, 1, run["run_id"], agent_id=agent["agent_id"],
                                              expected_revision=1, assignment_id=assignment["assignment_id"])
        self.assertEqual(linked["assignment_id"], assignment["assignment_id"])
        self.assertEqual(self.client.get("/api/projects/P0001/assignments").json()[0]["runtime_effect"], "none")
        agent_actor = ActorContext(authz.ACTOR_AGENT, agent["agent_id"], authz.ASSURANCE_LOCAL_UNVERIFIED)
        with self.assertRaises(RunProvenanceDenied):
            self.svc.get_run(agent_actor, 1, run["run_id"])

    def test_failed_history_insert_rolls_back_run_link(self) -> None:
        run = self.mkrun(reported_executor="External Worker")
        agent = self.agent()
        workspace_before = self.state()["tree"]
        self.sql("CREATE TRIGGER fail_run_agent_link BEFORE INSERT ON run_agent_links "
                 "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        try:
            with self.assertRaises(RunConflict):
                self.svc.link_external_agent(OWNER, 1, run["run_id"], agent_id=agent["agent_id"], expected_revision=1)
        finally:
            self.sql("DROP TRIGGER fail_run_agent_link")
        self.assertEqual(self.fetch("SELECT agent_id, assignment_id, agent_link_status, attribution_revision "
                                    "FROM run_records WHERE run_id = ?", run["run_id"]),
                         [(None, None, "unlinked", 1)])
        self.assertEqual(self.fetch("SELECT count(*) FROM run_agent_links"), [(0,)])
        self.assertEqual(self.state()["tree"], workspace_before)

    def test_concurrent_links_produce_one_canonical_result(self) -> None:
        run = self.mkrun()
        one, two = self.agent("One"), self.agent("Two")
        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def attempt(agent_id: str) -> None:
            service = self.service()
            barrier.wait()
            try:
                service.link_external_agent(OWNER, 1, run["run_id"], agent_id=agent_id, expected_revision=1)
                outcomes.append("ok")
            except RunConflict:
                outcomes.append("conflict")

        threads = [threading.Thread(target=attempt, args=(a["agent_id"],)) for a in (one, two)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(outcomes), ["conflict", "ok"])
        self.assertEqual(self.fetch("SELECT count(*) FROM run_agent_links"), [(1,)])
        self.assertEqual(self.fetch("SELECT attribution_revision FROM run_records"), [(2,)])


class ReplayAndRaceTests(ProvenanceCase):
    def test_run_replay_idempotent_and_conflict(self) -> None:
        first = self.mkrun("same", reported_executor="Tool")
        again = self.mkrun("same", reported_executor="Tool")
        self.assertEqual((again["run_id"], again["created"], first["created"]), (first["run_id"], False, True))
        before = self.state()
        with self.assertRaises(RunConflict):
            self.mkrun("same", reported_executor="Other")
        with self.assertRaises(RunConflict):
            self.svc.register_external_run(OWNER, 1, 1, source_kind="manual-report", source_key="same")
        self.assertEqual(self.state(), before)
        # Different source kind is a different identity; no key means explicit non-idempotent manual creation.
        self.assertTrue(self.mkrun("same", source_kind="other-kind")["created"])
        a = self.svc.register_external_run(OWNER, 1, 1, source_kind="manual-report")
        b = self.svc.register_external_run(OWNER, 1, 1, source_kind="manual-report")
        self.assertNotEqual(a["run_id"], b["run_id"])

    def test_report_and_evidence_replay_and_revisions(self) -> None:
        run = self.mkrun()["run_id"]
        r1 = self.svc.register_report_reference(OWNER, 1, run, report_kind="executor_report", source_ref="reports/a",
                                                source_key="r", report_revision=1, declared_digest=DIGEST)
        r1b = self.svc.register_report_reference(OWNER, 1, run, report_kind="executor_report", source_ref="reports/a",
                                                 source_key="r", report_revision=1, declared_digest=DIGEST)
        self.assertEqual((r1b["report_id"], r1b["created"]), (r1["report_id"], False))
        self.assertEqual(r1["provenance"], "claimed")
        with self.assertRaises(RunConflict):
            self.svc.register_report_reference(OWNER, 1, run, report_kind="executor_report", source_ref="reports/b",
                                               source_key="r", report_revision=1)
        r2 = self.svc.register_report_reference(OWNER, 1, run, report_kind="executor_report", source_ref="reports/a",
                                                source_key="r2", report_revision=2)
        self.assertNotEqual(r1["report_id"], r2["report_id"])
        e1 = self.svc.register_evidence_reference(OWNER, 1, run, evidence_kind="test_result_reference",
                                                  reference="checks/unit", source_key="e", report_id=r1["report_id"],
                                                  reported_outcome="reported_pass")
        e1b = self.svc.register_evidence_reference(OWNER, 1, run, evidence_kind="test_result_reference",
                                                   reference="checks/unit", source_key="e", report_id=r1["report_id"],
                                                   reported_outcome="reported_pass")
        self.assertEqual((e1b["evidence_id"], e1b["created"]), (e1["evidence_id"], False))
        with self.assertRaises(RunConflict):
            self.svc.register_evidence_reference(OWNER, 1, run, evidence_kind="test_result_reference",
                                                 reference="checks/unit", source_key="e", reported_outcome="reported_fail")
        got = self.svc.get_run(OWNER, 1, run)
        self.assertEqual(len(got["reports"]), 2)
        self.assertEqual(len(got["evidence"]), 1)

    def test_racing_registrations_create_one_record(self) -> None:
        results: list[dict] = []
        barrier = threading.Barrier(4)

        def attempt() -> None:
            service = self.service()
            barrier.wait()
            results.append(service.register_external_run(OWNER, 1, 1, source_kind="manual-report", source_key="race"))

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(results), 4)
        self.assertEqual(len({r["run_id"] for r in results}), 1)
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertEqual(self.fetch("SELECT count(*) FROM run_records"), [(1,)])


class ReportEvidenceTests(ProvenanceCase):
    def test_parent_run_and_report_must_match(self) -> None:
        self.second_project()
        run = self.mkrun()["run_id"]
        other = self.svc.register_external_run(OWNER, 2, 2, source_kind="manual-report")["run_id"]
        other_run_same_project = self.mkrun("second")["run_id"]
        report = self.svc.register_report_reference(OWNER, 1, run, report_kind="other_report", source_ref="r/1")
        foreign = self.svc.register_report_reference(OWNER, 2, other, report_kind="other_report", source_ref="r/1")
        before = self.state()
        with self.assertRaises(RunNotFound):
            self.svc.register_report_reference(OWNER, 1, other, report_kind="other_report", source_ref="r/1")
        with self.assertRaises(RunNotFound):
            self.svc.register_report_reference(OWNER, 1, str(uuid.uuid4()), report_kind="other_report", source_ref="r")
        with self.assertRaises(InvalidRunInput):
            self.svc.register_evidence_reference(OWNER, 1, other_run_same_project, evidence_kind="other_source_reference",
                                                 reference="x/y", report_id=report["report_id"])
        with self.assertRaises(InvalidRunInput):
            self.svc.register_evidence_reference(OWNER, 1, run, evidence_kind="other_source_reference",
                                                 reference="x/y", report_id=foreign["report_id"])
        with self.assertRaises(InvalidRunInput):
            self.svc.register_evidence_reference(OWNER, 1, run, evidence_kind="other_source_reference",
                                                 reference="x/y", report_id=str(uuid.uuid4()))
        self.assertEqual(self.state(), before)
        with self.assertRaises(sqlite3.IntegrityError):  # DB guard around the service
            self.sql("INSERT INTO evidence_records VALUES (?, ?, ?, NULL, 'other_source_reference', 'x', NULL, NULL, "
                     "'claimed', 'manual_owner_registration', 'local-owner', 'local_unverified', '2026-01-01 00:00:00')",
                     str(uuid.uuid4()), other_run_same_project, report["report_id"])

    def test_vocabulary_and_claim_only_semantics(self) -> None:
        run = self.mkrun()["run_id"]
        ev = self.svc.register_evidence_reference(OWNER, 1, run, evidence_kind="git_commit_reference",
                                                  reference="0e06cb3df125ff74a98453b142cd7ebfa3f55fc5",
                                                  declared_digest=DIGEST)
        self.assertEqual((ev["provenance"], ev["reported_outcome"]), ("claimed", None))
        self.svc.register_report_reference(OWNER, 1, run, report_kind="other_report", source_ref="r/1")
        for kind, ref, extra in (("verified_reference", "x/y", {}), ("git_commit_reference", "not-a-sha", {}),
                                 ("other_source_reference", "x/y", {"reported_outcome": "reported_pass"}),
                                 ("test_result_reference", "x/y", {"reported_outcome": "verified"})):
            with self.assertRaises(InvalidRunInput):
                self.svc.register_evidence_reference(OWNER, 1, run, evidence_kind=kind, reference=ref, **extra)
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("UPDATE evidence_records SET provenance = 'verified'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("UPDATE report_records SET declared_digest = NULL")
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("DELETE FROM evidence_records")
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("UPDATE run_records SET reported_executor = 'edited'")

    def test_no_source_file_is_read(self) -> None:
        # A reference to a file that does not exist (and a digest that matches nothing) is stored verbatim as a claim.
        run = self.mkrun()["run_id"]
        report = self.svc.register_report_reference(OWNER, 1, run, report_kind="executor_report",
                                                    source_ref="reports/missing-file.md", declared_digest="b" * 64)
        self.assertEqual((report["source_ref"], report["declared_digest"]), ("reports/missing-file.md", "b" * 64))


class InputSafetyTests(ProvenanceCase):
    SECRET = "ghp_" + "x" * 30

    def test_untrusted_strings_are_rejected_without_echo_or_writes(self) -> None:
        run = self.mkrun()["run_id"]
        before = self.state()
        bad_refs = ["../etc/passwd", "a/../b", "/abs/path", "C:/x", "C:\\x", "a\\b", "ftp://host/x", "a//b", "",
                    " lead", "x" * 201, "bad\x00byte", "line\nbreak", self.SECRET, "token=abc", "sp ace", 5, None]
        for bad in bad_refs:
            with self.assertRaises(InvalidRunInput) as caught:
                self.svc.register_report_reference(OWNER, 1, run, report_kind="other_report", source_ref=bad)
            self.assertNotIn(str(bad), str(caught.exception)) if isinstance(bad, str) and bad else None
            with self.assertRaises(InvalidRunInput):
                self.svc.register_external_run(OWNER, 1, 1, source_kind="manual-report", source_key=bad) \
                    if bad is not None else self.svc.register_external_run(OWNER, 1, 1, source_kind=bad)
        for bad in ["x\x07y", "https://u", self.SECRET, "a" * 97, "", " ", 7, "k" * 40 + "ABCDEFGHIJKLMNOP"]:
            with self.assertRaises(InvalidRunInput) as caught:
                self.svc.register_external_run(OWNER, 1, 1, source_kind="manual-report", reported_executor=bad)
            if isinstance(bad, str) and bad.strip():
                self.assertNotIn(bad, str(caught.exception))
        for bad in ("short", "A" * 64, "g" * 64, 5, "a" * 63):
            with self.assertRaises(InvalidRunInput):
                self.svc.register_report_reference(OWNER, 1, run, report_kind="other_report", source_ref="r", declared_digest=bad)
        for bad in (0, -1, True, "1", 1.5):
            with self.assertRaises(InvalidRunInput):
                self.svc.register_report_reference(OWNER, 1, run, report_kind="other_report", source_ref="r", report_revision=bad)
        for bad in ("Bad Kind", "UPPER", "", "x" * 65, None, 3):
            with self.assertRaises(InvalidRunInput):
                self.svc.register_external_run(OWNER, 1, 1, source_kind=bad)
        self.assertEqual(self.state(), before)

    def test_corrupt_project_identity_fails_closed(self) -> None:
        run = self.mkrun()["run_id"]
        self.sql("UPDATE projects SET project_uuid = 'corrupt'")
        before = self.state()
        with self.assertRaises(UnsafeRunProjectIdentity):
            self.mkrun("another")
        with self.assertRaises(UnsafeRunProjectIdentity):
            self.svc.register_report_reference(OWNER, 1, run, report_kind="other_report", source_ref="r")
        with self.assertRaises(UnsafeRunProjectIdentity):
            self.svc.list_runs(OWNER, 1)
        self.assertEqual(self.state(), before)
        self.sql("UPDATE projects SET project_uuid = ?", str(uuid.uuid4()))
        self.sql("DELETE FROM project_root_bindings")
        with self.assertRaises(UnsafeRunProjectIdentity):
            self.mkrun("again")
        self.assertEqual(self.fetch("SELECT count(*) FROM run_records"), [(1,)])


class AuthorizationTests(ProvenanceCase):
    ACTIONS = (authz.RUN_REGISTER_EXTERNAL, authz.RUN_LIST, authz.RUN_READ, authz.RUN_REPORT_REGISTER,
               authz.RUN_EVIDENCE_REGISTER, authz.RUN_LINK_AGENT)

    def calls(self, svc: RunProvenanceService, actor, run_id: str, agent_id: str):
        return [
            lambda: svc.register_external_run(actor, 1, 1, source_kind="manual-report", source_key="z"),
            lambda: svc.list_runs(actor, 1),
            lambda: svc.get_run(actor, 1, run_id),
            lambda: svc.register_report_reference(actor, 1, run_id, report_kind="other_report", source_ref="r"),
            lambda: svc.register_evidence_reference(actor, 1, run_id, evidence_kind="other_source_reference", reference="r"),
            lambda: svc.link_external_agent(actor, 1, run_id, agent_id=agent_id, expected_revision=1),
        ]

    def test_direct_core_calls_deny_untrusted_actors_and_policies_before_any_state_access(self) -> None:
        run_id = self.mkrun()["run_id"]
        agent_id = self.agent()["agent_id"]
        before = self.state()
        agent_actor = ActorContext(authz.ACTOR_AGENT, "agent:claim", authz.ASSURANCE_LOCAL_UNVERIFIED)
        forged = (agent_actor, ActorContext("owner", "someone-else", "local_unverified"),
                  ActorContext("owner", "local-owner", "authenticated"), ActorContext("admin", "local-owner", "local_unverified"),
                  {"actor_kind": "owner"}, None, "owner")
        for actor in forged:
            for call in self.calls(self.svc, actor, run_id, agent_id):
                with self.assertRaises(RunProvenanceDenied):
                    call()
        for call in self.calls(self.service(DenyAll()), OWNER, run_id, agent_id):
            with self.assertRaises(RunProvenanceDenied):
                call()
        self.assertEqual(self.state(), before)

    def test_scope_shapes_and_reserved_actions(self) -> None:
        policy = LocalOwnerPolicy()
        rid = str(uuid.uuid4())
        self.assertTrue(set(self.ACTIONS) <= authz.IMPLEMENTED_ACTIONS)
        self.assertFalse(evaluate(policy, OWNER, authz.RUN_READ, ResourceScope(project_id=1)).allowed)
        self.assertFalse(evaluate(policy, OWNER, authz.RUN_READ, ResourceScope(project_id=1, run_id="x")).allowed)
        self.assertFalse(evaluate(policy, OWNER, authz.RUN_READ, ResourceScope(project_id=1, run_id=rid, task_id=1)).allowed)
        self.assertFalse(evaluate(policy, OWNER, authz.PROJECT_LIST, ResourceScope(run_id=rid)).allowed)
        self.assertFalse(evaluate(policy, OWNER, authz.RUN_LINK_AGENT, ResourceScope(project_id=1, run_id=rid)).allowed)
        self.assertTrue(evaluate(policy, OWNER, authz.RUN_LIST, ResourceScope(project_id=1)).allowed)
        self.assertTrue(evaluate(policy, OWNER, authz.RUN_LIST, ResourceScope(project_id=1, task_id=1)).allowed)
        for malformed in (True, "1", 1.0, 0, -1):
            with self.subTest(optional_task_id=malformed):
                decision = evaluate(policy, OWNER, authz.RUN_LIST,
                                    ResourceScope(project_id=1, task_id=malformed))
                self.assertEqual((decision.allowed, decision.reason), (False, "invalid_resource"))
        self.assertNotIn("run.approve", authz.IMPLEMENTED_ACTIONS)
        self.assertFalse(evaluate(policy, OWNER, "run.approve", ResourceScope(project_id=1, task_id=1)).allowed)
        self.assertFalse(evaluate(policy, OWNER, "run.approve", ResourceScope(project_id=1, run_id=rid)).allowed)
        agent_actor = ActorContext(authz.ACTOR_AGENT, "agent:claim", authz.ASSURANCE_LOCAL_UNVERIFIED)
        for action in self.ACTIONS:
            required, _ = authz._SHAPES[action]
            scope = ResourceScope(project_id=1, task_id=1 if "task_id" in required else None,
                                  run_id=rid if "run_id" in required else None,
                                  agent_id=rid if "agent_id" in required else None)
            self.assertTrue(evaluate(policy, OWNER, action, scope).allowed, action)
            self.assertEqual(evaluate(policy, agent_actor, action, scope).reason, "agent_authority_not_implemented")

    def test_list_runs_rejects_malformed_optional_task_scope_without_side_effects(self) -> None:
        self.mkrun()
        before = self.state()
        for malformed in (True, "1", 1.0, 0, -1):
            with self.subTest(optional_task_id=malformed), self.assertRaises(RunProvenanceDenied):
                self.svc.list_runs(OWNER, 1, malformed)
            self.assertEqual(self.state(), before)

    def test_no_http_surface_exists_for_runs(self) -> None:
        for path in ("/api/runs", "/api/projects/P0001/runs", "/api/projects/P0001/tasks/T0001/runs"):
            self.assertIn(self.client.get(path).status_code, (404, 405))
            self.assertIn(self.client.post(path, json={}).status_code, (404, 405))


class MigrationTests(ProvenanceCase):
    def test_single_head_and_fresh_schema(self) -> None:
        from alembic.script import ScriptDirectory
        self.assertEqual(ScriptDirectory.from_config(self.alembic).get_heads(), ["0013_execution_package_core"])
        names = {r[0] for r in self.fetch("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(set(OWNED_TABLES) <= names)
        with sqlite3.connect(self.data / "rosc.db") as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_upgrade_from_populated_0003_preserves_prior_rows_and_workspace_bytes(self) -> None:
        agent = self.agent()
        self.client.post("/api/projects/P0001/assignments", json={
            "agent_id": agent["agent_id"], "role": "worker", "capabilities": WORKER_CAPS})
        command.downgrade(self.alembic, "0003_agent_registry_assignments")
        names = {r[0] for r in self.fetch("SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")}
        self.assertFalse(names & set(OWNED_TABLES))
        self.assertFalse([n for n in names if n.startswith("tr_")])
        workspace = self.data / "workspace"

        def snapshot():
            files = {str(p.relative_to(workspace)): p.read_bytes() for p in sorted(workspace.rglob("*")) if p.is_file()}
            rows = {t: self.fetch(f"SELECT rowid, * FROM {t} ORDER BY rowid") for t in LEGACY_TABLES}
            return files, rows

        before = snapshot()
        self.assertEqual(len(before[1]["role_assignments"]), 1)
        command.upgrade(self.alembic, "head")
        self.assertEqual(snapshot(), before)
        restarted = self.service()
        run = restarted.register_external_run(OWNER, 1, 1, source_kind="manual-report", source_key="after-migration")
        self.assertTrue(run["created"])
        self.assertEqual(self.service().get_run(OWNER, 1, run["run_id"])["run_id"], run["run_id"])

    def test_downgrade_removes_only_p013_schema(self) -> None:
        self.mkrun()
        before = {t: self.fetch(f"SELECT rowid, * FROM {t} ORDER BY rowid") for t in LEGACY_TABLES}
        command.downgrade(self.alembic, "0003_agent_registry_assignments")
        names = {r[0] for r in self.fetch("SELECT name FROM sqlite_master")}
        self.assertFalse(names & set(OWNED_TABLES))
        self.assertEqual({t: self.fetch(f"SELECT rowid, * FROM {t} ORDER BY rowid") for t in LEGACY_TABLES}, before)
        command.upgrade(self.alembic, "head")
        self.assertEqual(self.fetch("SELECT count(*) FROM run_records"), [(0,)])


if __name__ == "__main__":
    unittest.main()
