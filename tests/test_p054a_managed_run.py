"""P054A: managed Run foundation - canonical identity, Owner-only launch, offline package, Run ACK (E0) and UNKNOWN.

Everything runs against a temporary SQLite database and isolated directories with a fake tmux, fake /proc and the REAL
CLI hook script functions (``worker_ack``) driven in-process.  No Claude/OpenCode call, no real tmux, no Owner data.
The fake pane pid is this test process's parent, so the hook's genuine /proc ancestry check is exercised.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import yaml
from alembic import command
from fastapi.testclient import TestClient

from rosc import worker_ack
from rosc.app import DEFAULT_GLOBAL_LIBRARY, create_app
from rosc.authorization import ActorContext, LocalOwnerPolicy
from rosc.config import resolve_data_dir
from rosc.run_launch import (RunLaunchConflict, RunLaunchDenied, RunLaunchService, RunLaunchUnavailable,
                             load_worker_protocol)
from rosc.task_authoring import TaskAuthoringService
from rosc.workspace import WorkspaceConflict, run_directory
from rosc.worker_session import TmuxError
from test_p042_worker_session import OWNER, WORKER_CAPS, SessionCase

TASK = {"title": "Run task", "description": "Line one", "acceptance_criteria": "Criterion A"}
AGENT_ACTOR = ActorContext("agent", "local-owner", "local_unverified")


class RunCase(SessionCase):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "AGENTS.md").write_text("# entry point\n")
        for title in ("Run task", "Second task"):
            self.assertEqual(self.client.post("/api/projects/P0001/tasks", json={**TASK, "title": title}).status_code, 201)
        self.assertEqual(self.client.post("/api/projects/P0002/tasks", json=TASK).status_code, 201)  # T0003 of P0002
        self.authoring = TaskAuthoringService(self.sessions, LocalOwnerPolicy(), self.data / "workspace")
        self.sent_before = len(self.tmux.sent)

    # -- composition ----------------------------------------------------------------------------------------------
    def build(self, **kwargs):
        service = super().build(**kwargs)
        self.runs = self.make_runs()
        self.client.close()
        self.client = TestClient(create_app(self.data / "rosc.db", self.data / "workspace",
                                            worker_session_service=service, run_launch_service=self.runs),
                                 base_url="http://localhost")
        return service

    def make_runs(self, **kwargs) -> RunLaunchService:
        options = {"clock": lambda: self.clock_now[0], "submit_wait": 0.0, **kwargs}
        library = options.pop("library", DEFAULT_GLOBAL_LIBRARY)
        return RunLaunchService(self.sessions, LocalOwnerPolicy(), self.data / "workspace", self.service,
                                TaskAuthoringService(self.sessions, LocalOwnerPolicy(), self.data / "workspace"),
                                library, private_paths=(self.data,), **options)

    # -- fixtures -------------------------------------------------------------------------------------------------
    def approve(self, task: str = "T0001", body: str = "Do the work") -> dict:
        prefix = f"/api/projects/P0001/tasks/{task}"
        head = self.client.get(f"{prefix}/authoring").json()
        rev = head["prompt"]["revision"] if head["prompt"] else None
        row = self.client.post(f"{prefix}/prompt/revisions", json={
            "expected_revision": rev, "source_spec_revision": head["spec"]["revision"], "body": body,
            "summary": "1. Scope"}).json()["prompt"]
        response = self.client.post(f"{prefix}/prompt/revisions/{row['revision']}/approve", json={"digest": row["digest"]})
        self.assertEqual(response.status_code, 200, response.text)
        return row

    def ready_worker(self) -> tuple[str, int]:
        """Active CLI (fake) with a confirmed project bootstrap for the current binding + incarnation."""
        self.tmux.next_pid = os.getppid() - 1  # pane pid == our parent: real hook ancestry contains it
        self.activate()
        incarnation, pid, _ = self.current()
        self.write_ack(incarnation, [pid, 1])
        self.client.post(self.url("session/bootstrap"))
        token = self.rows("select bootstrap_token from worker_sessions")[0][0]
        (self.state_dir / f"{incarnation}.bootstrap.json").write_text(json.dumps({
            "incarnation": incarnation, "token": token, "claimed": "OK", "observed_reads": [str(self.root / "AGENTS.md")],
            "ancestors": [pid, 1], "acked_at": time.time()}))
        self.assertEqual(self.status()["bootstrap"]["state"], "confirmed")
        self.sent_before = len(self.tmux.sent)
        return incarnation, pid

    def launch(self, key: str = "launch-0001", task: int = 1, agent: str | None = None, **kw) -> dict:
        return self.runs.launch(OWNER, 1, task, agent or self.agent_id, launch_key=key, **kw)

    def run_sends(self) -> list[str]:
        return [text for _pane, text in self.tmux.sent[self.sent_before:]]

    def count(self, table: str) -> int:
        return self.rows(f"select count(*) from {table}")[0][0]

    def state_row(self, run_id: str) -> dict:
        with sqlite3.connect(self.data / "rosc.db") as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("select * from managed_run_states where run_id=?", (run_id,)).fetchone())

    def package_dir(self, run_id: str, task: int = 1) -> Path:
        return run_directory(self.data / "workspace", 1, task, run_id) / "package"

    # -- simulated CLI turn through the REAL hook functions ---------------------------------------------------------
    def target(self, incarnation: str) -> str:
        return str(self.state_dir / f"{incarnation}.ack.json")

    def cli_submit(self, incarnation: str, text: str) -> None:
        worker_ack._run_submit(self.target(incarnation), incarnation, {"prompt": text, "session_id": "cli-1"})
        worker_ack._submit(self.target(incarnation), incarnation, {"prompt": text})

    def cli_read(self, incarnation: str, path: Path) -> None:
        event = {"tool_name": "Read", "tool_input": {"file_path": str(path)}}
        worker_ack._run_read(self.target(incarnation), incarnation, event)
        worker_ack._read(self.target(incarnation), incarnation, event)

    def cli_stop(self, incarnation: str, message: str) -> None:
        event = {"last_assistant_message": message}
        worker_ack._stop(self.target(incarnation), incarnation, event)
        worker_ack._run_stop(self.target(incarnation), incarnation, event)

    def worker_accepts(self, incarnation: str, run: dict, decision: str = "ACCEPT", reads: bool = True,
                       skip: tuple[str, ...] = ()) -> None:
        text = self.run_sends()[-1]
        self.cli_submit(incarnation, text)
        package = self.package_dir(run["run_id"])
        if reads:
            manifest = json.loads((package / "run.json").read_text())
            for name in ["run.json", *manifest["protocol"]["files"], "spec.md", "prompt.md"]:
                if name not in skip:
                    self.cli_read(incarnation, package / name)
        state = self.state_row(run["run_id"])
        self.cli_stop(incarnation, f"Checked.\nROSC-RUN-ACK {state['run_token']} {run['run_id']} "
                                   f"{state['manifest_sha256']} {decision}")


class MigrationTests(RunCase):
    def sql(self, statement: str, *args) -> None:
        with sqlite3.connect(self.data / "rosc.db") as db:
            db.execute(statement, args)

    def test_fresh_head_and_downgrade_upgrade_preserve_p013_rows_and_guards(self) -> None:
        from rosc.run_provenance import RunProvenanceService
        provenance = RunProvenanceService(self.sessions, LocalOwnerPolicy())
        external = provenance.register_external_run(OWNER, 1, 1, source_kind="manual-report", source_key="ext-1",
                                                    reported_executor="someone")
        provenance.register_report_reference(OWNER, 1, external["run_id"], report_kind="executor_report",
                                             source_ref="reports/r1.md", source_key="r1")
        before = {t: self.rows(f"select * from {t}") for t in ("run_records", "report_records", "evidence_records")}
        command.downgrade(self.alembic, "0018_architect_onboarding_attempt")
        tables = {r[0] for r in self.rows("select name from sqlite_master where type='table'")}
        self.assertNotIn("managed_run_states", tables)
        command.upgrade(self.alembic, "head")
        self.assertEqual({t: self.rows(f"select * from {t}") for t in before}, before)
        with self.assertRaises(sqlite3.IntegrityError):  # P013 immutability trigger still in force
            self.sql("update run_records set reported_executor='x' where run_id=?", external["run_id"])
        triggers = {r[0] for r in self.rows("select name from sqlite_master where type='trigger'")}
        self.assertTrue({"tr_run_records_immutable", "tr_managed_run_states_origin", "tr_managed_run_events_immutable",
                         "tr_run_records_managed_shape"} <= triggers)

    def test_state_cannot_attach_to_external_run_and_shape_is_guarded(self) -> None:
        from rosc.run_provenance import RunConflict, RunProvenanceService
        provenance = RunProvenanceService(self.sessions, LocalOwnerPolicy())
        external = provenance.register_external_run(OWNER, 1, 1, source_kind="manual-report", source_key="ext-1")
        with self.assertRaises(RunConflict):  # the namespaced kind is reserved for managed Runs
            provenance.register_external_run(OWNER, 1, 1, source_kind="rosc.managed_run", source_key="x-1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("insert into managed_run_events (event_id, run_id, seq, to_state, reason, actor, created_at) "
                     "values ('e', ?, 1, 'authorized', 'x', 'y', '2026-01-01')", external["run_id"])
        with self.assertRaises(sqlite3.IntegrityError):  # managed record with an attribution claim
            self.sql("insert into run_records values (?, 1, 1, 'managed', 'rosc.managed_run', 'k-0001', 'claimed', "
                     "'claimed-executor', NULL, NULL, 'unlinked', 1, 'local-owner', 'local_unverified', '2026-01-01')",
                     str(uuid.uuid4()))
        self.assertEqual(self.count("managed_run_states"), 0)

    def test_managed_rows_are_immutable_and_events_append_only(self) -> None:
        incarnation, _pid = self.ready_worker()
        self.approve()
        run = self.launch()
        rid = run["run_id"]
        for statement in ("update run_records set source_key='other-key' where run_id=?",
                          "delete from run_records where run_id=?",
                          "update managed_run_states set incarnation='x' where run_id=?",
                          "update managed_run_states set run_token='x' where run_id=?",
                          "update managed_run_states set state='authorized' where run_id=?",  # back before intent
                          "delete from managed_run_states where run_id=?",
                          "update managed_run_events set reason='x' where run_id=?",
                          "delete from managed_run_events where run_id=?"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.sql(statement, rid)
        with self.assertRaises(sqlite3.IntegrityError):  # a second holding state for the same Task
            other = str(uuid.uuid4())
            with sqlite3.connect(self.data / "rosc.db") as db:
                db.execute("insert into run_records values (?, 1, 1, 'managed', 'rosc.managed_run', 'k-0002', "
                           "'claimed', NULL, NULL, NULL, 'unlinked', 1, 'local-owner', 'local_unverified', "
                           "'2026-01-01')", (other,))
                db.execute("insert into managed_run_states select ?, project_id, task_id, package_id, "
                           "prompt_approval_id, spec_revision, spec_digest, prompt_revision, prompt_digest, "
                           "root_binding_version, agent_id, worker_session_id, incarnation, pane_pid, proc_start, "
                           "protocol_version, protocol_sha256, 'k-0002', ?, ack_timeout_seconds, 'authorized', 'x', "
                           "NULL, 1, created_at, updated_at, NULL, NULL, NULL from managed_run_states where run_id=?",
                           (other, str(uuid.uuid4()), rid))
        self.assertEqual(self.count("run_records"), 1)
        # P013 never links a managed Run to an Agent as an owner-curated claim
        from rosc.run_provenance import InvalidRunInput, RunProvenanceService
        with self.assertRaises(InvalidRunInput):
            RunProvenanceService(self.sessions, LocalOwnerPolicy()).link_external_agent(
                OWNER, 1, rid, agent_id=self.agent_id, expected_revision=1)
        record = self.rows(f"select execution_origin, source_kind, agent_id, agent_link_status, reported_executor "
                           f"from run_records where run_id='{rid}'")[0]
        self.assertEqual(record, ("managed", "rosc.managed_run", None, "unlinked", None))


class AuthorizationTests(RunCase):
    def assert_nothing(self) -> None:
        self.assertEqual((self.count("run_records"), self.count("managed_run_states")), (0, 0))
        self.assertEqual(self.run_sends(), [])
        self.assertFalse((self.data / "workspace/projects/P0001/tasks/T0001/runs").exists())

    def expect(self, code: str, **kw) -> None:
        with self.assertRaises(RunLaunchConflict) as caught:
            self.launch(**kw)
        self.assertEqual(caught.exception.code, code)
        self.assert_nothing()

    def test_missing_unapproved_and_stale_prompt_are_refused(self) -> None:
        self.ready_worker()
        self.expect("prompt_not_approved")
        self.client.post("/api/projects/P0001/tasks/T0001/prompt/revisions", json={
            "expected_revision": None, "source_spec_revision": 1, "body": "b", "summary": "s"})
        self.expect("prompt_not_approved")
        self.approve()
        self.client.post("/api/projects/P0001/tasks/T0001/spec/revisions", json={
            "expected_revision": 1, "title": "Changed", "description": "<p>x</p>", "acceptance_criteria": "<p>y</p>"})
        self.expect("prompt_stale")

    def test_worker_preconditions_are_refused_without_send(self) -> None:
        self.approve()
        self.expect("worker_not_active")  # never activated
        other = self.registry.register_agent(OWNER, display_name="Unassigned", runtime_adapter="claude-code",
                                             provider_id="anthropic", model_id="m", capabilities=WORKER_CAPS)
        self.expect("worker_not_assigned", agent=other["agent_id"])
        self.tmux.next_pid = os.getppid() - 1
        self.activate()
        incarnation, pid, _ = self.current()
        self.expect("worker_not_active")  # no SessionStart ack yet
        self.write_ack(incarnation, [pid, 1])
        self.expect("bootstrap_not_confirmed")
        self.client.post(self.url("session/bootstrap"))
        self.sent_before = len(self.tmux.sent)
        self.expect("bootstrap_not_confirmed")  # pending bootstrap is not enough
        self.write_ack(incarnation, [999999, 1])  # foreign process ancestry: not our CLI
        self.expect("worker_not_active")

    def test_wrong_scope_and_non_owner_are_refused(self) -> None:
        self.ready_worker()
        self.approve()
        from rosc.run_launch import RunLaunchNotFound
        with self.assertRaises(RunLaunchNotFound):
            self.runs.launch(OWNER, 1, 3, self.agent_id, launch_key="launch-0001")  # T0003 belongs to P0002
        with self.assertRaises(RunLaunchNotFound):
            self.runs.launch(OWNER, 99, 1, self.agent_id, launch_key="launch-0001")
        for actor in (AGENT_ACTOR, ActorContext("owner", "someone", "local_unverified"), None):
            with self.assertRaises(RunLaunchDenied):
                self.runs.launch(actor, 1, 1, self.agent_id, launch_key="launch-0001")
        self.assert_nothing()

    def test_no_implicit_run_from_approval_activation_bootstrap_reads_or_mentions(self) -> None:
        self.ready_worker()
        self.approve()
        self.client.get("/api/projects/P0001/tasks/T0001/authoring")
        self.client.get("/api/projects/P0001/tasks/T0001/runs")
        self.status()
        self.activate()
        self.client.post(self.url("session/bootstrap"))
        self.client.get("/api/projects/P0001/workers")
        self.assertEqual(self.client.get("/api/projects/P0001/tasks/T0001/authoring").json()["next"]["enabled"], False)
        self.assert_nothing()

    def test_unsafe_run_location_is_refused_before_any_write(self) -> None:
        self.ready_worker()
        self.approve()
        outside = self.base / "outside"
        outside.mkdir()
        (self.data / "workspace/projects/P0001/tasks/T0001/runs").symlink_to(outside)
        with self.assertRaises(RunLaunchConflict) as caught:
            self.launch()
        self.assertEqual(caught.exception.code, "artifact_root_unsafe")
        self.assertEqual((self.count("run_records"), self.count("managed_run_states"), self.run_sends()), (0, 0, []))
        self.assertEqual(list(outside.iterdir()), [])

    def test_tampered_protocol_fails_closed(self) -> None:
        self.ready_worker()
        self.approve()
        library = self.base / "library"
        shutil.copytree(DEFAULT_GLOBAL_LIBRARY / "agents" / "workers", library / "agents" / "workers")
        (library / "agents/workers/rules/execution-protocol.md").write_text("tampered")
        with self.assertRaises(RunLaunchUnavailable):
            self.make_runs(library=library).launch(OWNER, 1, 1, self.agent_id, launch_key="launch-0001")
        self.assert_nothing()


    def test_tracked_ruleset_has_no_approval_marker_and_pins_stay_authoritative(self) -> None:
        workers = DEFAULT_GLOBAL_LIBRARY / "agents" / "workers"
        ruleset = yaml.safe_load((workers / "ruleset.yaml").read_text())
        self.assertNotIn("status", ruleset)
        for rel in ("ruleset.yaml", "README.md", "rules/role.md", "rules/execution-protocol.md"):
            self.assertNotIn("pending_owner_review", (workers / rel).read_text(), rel)
        protocol = load_worker_protocol(DEFAULT_GLOBAL_LIBRARY)
        self.assertEqual((protocol.version, protocol.operative_stages), ("0.1.0", ("E0",)))
        e0 = (workers / "rules/execution-protocol.md").read_text().split("## E1")[0]
        for name in ("run.json", "spec.md", "prompt.md", "DECLINE"):
            self.assertIn(name, e0)
        library = self.base / "library"
        shutil.copytree(workers, library / "agents" / "workers")
        target = library / "agents/workers/ruleset.yaml"
        for bad in (("bytes: 2763", "bytes: 2764"), ("operative_stages: [E0]", "operative_stages: [E0, E1]")):
            with self.subTest(bad=bad):
                text = (workers / "ruleset.yaml").read_text()
                self.assertIn(bad[0], text)
                target.write_text(text.replace(bad[0], bad[1]))
                with self.assertRaises(RunLaunchUnavailable):
                    load_worker_protocol(library)

class IdempotencyTests(RunCase):
    def test_exact_retry_returns_the_same_run_without_a_second_send(self) -> None:
        incarnation, _pid = self.ready_worker()
        self.approve()
        first = self.client.post("/api/projects/P0001/tasks/T0001/runs",
                                 json={"agent_id": self.agent_id, "launch_key": "launch-0001"})
        self.assertEqual(first.status_code, 201, first.text)
        again = self.client.post("/api/projects/P0001/tasks/T0001/runs",
                                 json={"agent_id": self.agent_id, "launch_key": "launch-0001"})
        self.assertEqual((again.status_code, again.json()["run_id"]), (200, first.json()["run_id"]))
        self.assertEqual(len(self.run_sends()), 1)
        self.assertEqual((self.count("run_records"), self.count("managed_run_states"), self.count("execution_packages")),
                         (1, 1, 1))
        view = first.json()
        self.assertEqual((view["state"], view["execution_origin"], view["work_started"]),
                         ("dispatching", "managed", False))
        self.assertNotIn(self.state_row(view["run_id"])["run_token"], json.dumps(view))  # token never exposed

    def test_other_key_while_holding_is_an_explicit_conflict(self) -> None:
        self.ready_worker()
        self.approve()
        run = self.launch()
        with self.assertRaises(RunLaunchConflict) as caught:
            self.launch("launch-0002")
        self.assertEqual(caught.exception.code, "run_in_progress")
        self.approve(task="T0002")
        with self.assertRaises(RunLaunchConflict) as caught:  # same Worker, other Task: still one writer per Worker
            self.launch("launch-0003", task=2)
        self.assertEqual(caught.exception.code, "run_in_progress")
        with self.assertRaises(RunLaunchConflict) as caught:  # the key names another Task
            self.launch(run["launch_key"], task=2)
        self.assertEqual(caught.exception.code, "launch_key_conflict")
        self.assertEqual((self.count("run_records"), len(self.run_sends())), (1, 1))
        response = self.client.post("/api/projects/P0001/tasks/T0001/runs",
                                    json={"agent_id": self.agent_id, "launch_key": "launch-0009"})
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "run_in_progress"))

    def test_unrelated_task_on_another_worker_is_not_locked(self) -> None:
        self.ready_worker()
        self.approve()
        self.launch()
        second = self.registry.register_agent(OWNER, display_name="Second", runtime_adapter="claude-code",
                                              provider_id="anthropic", model_id="m", capabilities=WORKER_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=second["agent_id"], role="worker", capabilities=WORKER_CAPS)
        url = f"/api/projects/P0001/workers/{second['agent_id']}/session"
        self.client.post(url + "/activate")
        incarnation, pid = self.rows(f"select incarnation, pane_pid from worker_sessions where agent_id='{second['agent_id']}'")[0]
        self.write_ack(incarnation, [pid, 1])
        self.client.post(url + "/bootstrap")
        token = self.rows(f"select bootstrap_token from worker_sessions where agent_id='{second['agent_id']}'")[0][0]
        (self.state_dir / f"{incarnation}.bootstrap.json").write_text(json.dumps({
            "incarnation": incarnation, "token": token, "claimed": "OK", "observed_reads": [str(self.root / "AGENTS.md")],
            "ancestors": [pid, 1], "acked_at": time.time()}))
        self.approve(task="T0002")
        other = self.launch("launch-0002", task=2, agent=second["agent_id"])
        self.assertEqual(other["state"], "dispatching")
        self.assertEqual(self.count("managed_run_states"), 2)

    def test_concurrent_identical_launches_create_one_run(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        self.ready_worker()
        self.approve()
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _i: self.launch()["run_id"], range(4)))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual((self.count("run_records"), len(self.run_sends())), (1, 1))


class AtomicityTests(RunCase):
    def test_failure_inside_the_launch_transaction_leaves_nothing(self) -> None:
        self.ready_worker()
        self.approve()
        with patch.object(RunLaunchService, "_event", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.launch()
        self.assertEqual((self.count("run_records"), self.count("managed_run_states"), self.count("managed_run_events")),
                         (0, 0, 0))
        self.assertEqual(self.run_sends(), [])
        self.assertEqual(self.launch()["state"], "dispatching")  # the same key works afterwards

    def test_package_publish_failure_is_not_delivered_and_releases_the_slot(self) -> None:
        self.ready_worker()
        self.approve()
        with patch.object(RunLaunchService, "_publish", side_effect=OSError("disk full")):
            failed = self.launch()
        self.assertEqual((failed["state"], failed["holding"]), ("not_delivered", False))
        self.assertTrue(failed["reason"].startswith("package_publish_failed"))
        self.assertEqual(self.run_sends(), [])
        self.assertEqual(self.launch()["run_id"], failed["run_id"])  # exact retry: same terminal Run, nothing sent
        self.assertEqual(self.run_sends(), [])
        fresh = self.launch("launch-0002")
        self.assertEqual((fresh["state"], len(self.run_sends())), ("dispatching", 1))

    def test_target_change_before_intent_is_not_delivered(self) -> None:
        self.ready_worker()
        self.approve()
        with patch.object(RunLaunchService, "_dispatch", side_effect=KeyboardInterrupt):  # Core dies before intent
            with self.assertRaises(KeyboardInterrupt):
                self.launch()
        rid = self.rows("select run_id from managed_run_states")[0][0]
        self.assertEqual(self.state_row(rid)["state"], "dispatch_pending")
        self.tmux.sessions.clear()  # the CLI vanished meanwhile
        self.assertEqual(self.launch()["state"], "not_delivered")
        self.assertEqual(self.run_sends(), [])


class AckEvidenceTests(RunCase):
    def setUp(self) -> None:
        super().setUp()
        self.incarnation, self.pid = self.ready_worker()
        self.approve()
        self.run = self.launch()
        self.rid = self.run["run_id"]

    def status_of(self) -> dict:
        response = self.client.get(f"/api/projects/P0001/runs/{self.rid}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def reconcile(self) -> dict:
        return self.client.post(f"/api/projects/P0001/runs/{self.rid}/reconcile").json()

    def test_delivery_line_is_a_single_pointer(self) -> None:
        [text] = self.run_sends()
        state = self.state_row(self.rid)
        self.assertNotIn("\n", text)
        for part in (state["run_token"], self.rid, state["manifest_sha256"], str(self.package_dir(self.rid))):
            self.assertIn(part, text)
        self.assertNotIn("Do the work", text)  # the task text lives in the package, not in the pane
        request = json.loads((self.state_dir / f"{self.incarnation}.run-request.json").read_text())
        self.assertEqual((request["run_id"], request["token"]), (self.rid, state["run_token"]))
        self.assertIsNone(json.loads((self.state_dir / f"{self.incarnation}.request.json").read_text()).get("run_id"))

    def test_session_start_bootstrap_and_submit_are_not_run_ack(self) -> None:
        self.assertEqual(self.status_of()["state"], "dispatching")  # SessionStart ack + confirmed bootstrap exist
        self.cli_submit(self.incarnation, self.run_sends()[-1])
        view = self.status_of()
        self.assertEqual((view["state"], view["recorded_state"], view["evidence"]["submitted"]),
                         ("submitted", "dispatching", True))  # GET derives, never writes
        self.assertEqual(self.reconcile()["recorded_state"], "submitted")
        self.cli_stop(self.incarnation, "I will look at it later.")
        self.assertEqual(self.reconcile()["state"], "submitted")

    def test_valid_correlated_receipt_acknowledges_exactly_once(self) -> None:
        self.worker_accepts(self.incarnation, self.run)
        view = self.reconcile()
        self.assertEqual((view["state"], view["recorded_state"], view["evidence"]["required_reads"]),
                         ("acknowledged", "acknowledged", True))
        self.assertEqual((view["work_started"], view["report"]), (False, None))
        events = self.count("managed_run_events")
        self.reconcile()
        self.cli_stop(self.incarnation, "replay")  # receipt is write-once
        self.assertEqual((self.count("managed_run_events"), self.status_of()["state"]), (events, "acknowledged"))
        self.assertEqual([e["to"] for e in view["events"]],
                         ["authorized", "dispatch_pending", "dispatching", "acknowledged"])
        self.assertEqual(len(self.run_sends()), 1)

    def test_accept_without_package_reads_is_not_acknowledged(self) -> None:
        self.worker_accepts(self.incarnation, self.run, reads=False)
        view = self.reconcile()
        self.assertEqual((view["state"], view["reason"]), ("unknown", "ack_without_required_reads"))

    def assert_unread_task_input_blocks_ack(self, name: str) -> None:
        # r02: the rules alone are not enough; each task input must be a hook-recorded Read of THIS package
        self.worker_accepts(self.incarnation, self.run, skip=(name,))
        view = self.reconcile()
        self.assertEqual((view["state"], view["reason"], view["evidence"]["required_reads"],
                          view["evidence"]["missing_reads"]), ("unknown", "ack_without_required_reads", False, [name]))
        self.cli_read(self.incarnation, self.package_dir(self.rid) / name)  # reading later cannot repair the receipt
        self.assertEqual(self.reconcile()["state"], "unknown")
        self.assertEqual(len(self.run_sends()), 1)  # UNKNOWN is never resent

    def test_accept_without_reading_spec_is_not_acknowledged(self) -> None:
        self.assert_unread_task_input_blocks_ack("spec.md")

    def test_accept_without_reading_prompt_is_not_acknowledged(self) -> None:
        self.assert_unread_task_input_blocks_ack("prompt.md")

    def test_task_reads_outside_the_package_or_by_other_hooks_do_not_count(self) -> None:
        state = self.state_row(self.rid)
        package = self.package_dir(self.rid)
        self.cli_submit(self.incarnation, self.run_sends()[-1])
        for name in ("run.json", "worker-role.md", "worker-execution-protocol.md"):
            self.cli_read(self.incarnation, package / name)
        copy = self.base / "elsewhere" / "spec.md"  # same name, wrong path
        copy.parent.mkdir()
        copy.write_bytes((package / "spec.md").read_bytes())
        self.cli_read(self.incarnation, copy)
        stale = str(uuid.uuid4())  # an old/other incarnation's hook records nothing for this Run
        worker_ack._run_read(self.target(stale), stale, {"tool_name": "Read",
                                                         "tool_input": {"file_path": str(package / "prompt.md")}})
        event = {"tool_name": "Read", "tool_input": {"file_path": str(package / "prompt.md")}}
        worker_ack._read(self.target(self.incarnation), self.incarnation, event)  # bootstrap hook only
        worker_ack._run_read(self.target(self.incarnation), self.incarnation,
                             {"tool_name": "Grep", "tool_input": {"file_path": str(package / "prompt.md")}})
        self.cli_stop(self.incarnation, "I read spec.md and prompt.md.\nROSC-RUN-ACK "
                                        f"{state['run_token']} {self.rid} {state['manifest_sha256']} ACCEPT")
        view = self.reconcile()
        self.assertEqual((view["state"], view["evidence"]["missing_reads"]), ("unknown", ["spec.md", "prompt.md"]))

    def test_decline_without_task_reads_is_legitimate(self) -> None:
        self.worker_accepts(self.incarnation, self.run, decision="DECLINE", skip=("spec.md", "prompt.md"))
        view = self.reconcile()
        self.assertEqual((view["state"], view["reason"]), ("declined", "worker_declined"))

    def test_decline_releases_the_slot(self) -> None:
        self.worker_accepts(self.incarnation, self.run, decision="DECLINE")
        self.assertEqual(self.reconcile()["state"], "declined")
        self.assertEqual(self.launch("launch-0002")["state"], "dispatching")

    def test_forged_stale_or_foreign_evidence_never_acknowledges(self) -> None:
        state = self.state_row(self.rid)
        good = {"incarnation": self.incarnation, "token": state["run_token"], "run_id": self.rid,
                "manifest_sha256": state["manifest_sha256"], "decision": "ACCEPT",
                "observed_reads": [str(self.package_dir(self.rid) / n) for n in
                                   ("run.json", "worker-role.md", "worker-execution-protocol.md", "spec.md",
                                    "prompt.md")],
                "ancestors": [self.pid, 1], "acked_at": time.time()}
        receipt = self.state_dir / f"{self.incarnation}.run-{state['run_token']}.receipt.json"
        for bad in ({"token": str(uuid.uuid4())}, {"incarnation": str(uuid.uuid4())}, {"run_id": str(uuid.uuid4())},
                    {"manifest_sha256": "0" * 64}, {"ancestors": [999999, 1]}, {"acked_at": time.time() - 3600}):
            with self.subTest(bad=bad):
                receipt.write_text(json.dumps({**good, **bad}))
                self.assertNotEqual(self.reconcile()["state"], "acknowledged")
        # a hook marker for another Run/hash is ignored by the hook itself
        receipt.unlink()
        self.cli_stop(self.incarnation, f"ROSC-RUN-ACK {state['run_token']} {uuid.uuid4()} {state['manifest_sha256']} ACCEPT")
        self.cli_stop(self.incarnation, f"ROSC-RUN-ACK {uuid.uuid4()} {self.rid} {state['manifest_sha256']} ACCEPT")
        self.assertFalse(receipt.exists())
        receipt.write_text(json.dumps(good))
        self.assertEqual(self.reconcile()["state"], "acknowledged")

    def test_bootstrap_hook_files_are_untouched_by_run_evidence(self) -> None:
        names = ("bootstrap.json", "request.json", "submitted.json", "reads.jsonl")
        before = {n: (self.state_dir / f"{self.incarnation}.{n}").read_bytes()
                  for n in names if (self.state_dir / f"{self.incarnation}.{n}").exists()}
        self.worker_accepts(self.incarnation, self.run)
        after = {n: (self.state_dir / f"{self.incarnation}.{n}").read_bytes()
                 for n in names if (self.state_dir / f"{self.incarnation}.{n}").exists()}
        # Run evidence uses its own files; the bootstrap acknowledgement and request are byte-identical.  (The
        # pre-existing P042 ``read`` hook keeps appending any Read to the bootstrap reads log while its request exists;
        # that log is consulted only before the write-once bootstrap.json, so it cannot change the confirmed state.)
        for name in ("bootstrap.json", "request.json"):
            self.assertEqual(before[name], after[name])
        self.assertFalse((self.state_dir / f"{self.incarnation}.submitted.json").exists() and
                         "run_id" in (self.state_dir / f"{self.incarnation}.submitted.json").read_text())
        self.assertEqual(self.status()["bootstrap"]["state"], "confirmed")


class UnknownAndCrashTests(RunCase):
    def test_timeout_without_ack_is_unknown_and_never_resent(self) -> None:
        self.ready_worker()
        self.approve()
        run = self.launch()
        self.clock_now[0] += 301
        view = self.runs.status(OWNER, 1, run["run_id"])
        self.assertEqual((view["state"], view["recorded_state"], view["holding"]), ("unknown", "dispatching", True))
        view = self.runs.reconcile(OWNER, 1, run["run_id"])
        self.assertEqual((view["recorded_state"], view["reason"]), ("unknown", "no_ack_within_timeout"))
        self.assertEqual(self.launch()["state"], "unknown")  # exact retry: no resend
        with self.assertRaises(RunLaunchConflict):
            self.launch("launch-0002")
        self.assertEqual((len(self.run_sends()), self.tmux.killed), (1, []))

    def test_configurable_ack_window(self) -> None:
        self.ready_worker()
        self.approve()
        run = self.launch(ack_timeout_seconds=60)
        self.clock_now[0] += 61
        self.assertEqual(self.runs.status(OWNER, 1, run["run_id"])["state"], "unknown")
        from rosc.run_launch import RunLaunchInvalid
        for bad in (0, 29, 3601, True, "300"):
            with self.assertRaises(RunLaunchInvalid):
                self.launch("launch-0002", ack_timeout_seconds=bad)

    def test_crash_after_intent_is_never_blindly_resent(self) -> None:
        self.ready_worker()
        self.approve()
        with patch.object(type(self.service), "deliver_run_line", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.launch()
        rid = self.rows("select run_id from managed_run_states")[0][0]
        self.assertEqual(self.state_row(rid)["state"], "dispatching")
        self.build()  # Engine restart: new service objects, same DB/files
        self.assertEqual(self.launch()["state"], "dispatching")
        self.assertEqual(self.run_sends(), [])
        self.clock_now[0] += 301
        self.assertEqual(self.runs.reconcile(OWNER, 1, rid)["state"], "unknown")
        self.assertEqual((self.run_sends(), self.tmux.killed), ([], []))

    def test_crash_before_intent_resumes_the_same_run_once(self) -> None:
        self.ready_worker()
        self.approve()
        with patch.object(RunLaunchService, "_dispatch", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.launch()
        rid = self.rows("select run_id from managed_run_states")[0][0]
        self.build()
        resumed = self.launch()
        self.assertEqual((resumed["run_id"], resumed["state"], len(self.run_sends())), (rid, "dispatching", 1))
        self.assertEqual(self.launch()["state"], "dispatching")
        self.assertEqual(len(self.run_sends()), 1)

    def test_uncertain_send_stays_dispatching_then_unknown(self) -> None:
        self.ready_worker()
        self.approve()

        def broken(_pane, _text):
            raise TmuxError("send-keys failed")
        self.tmux.on_send = broken
        enters = len(self.tmux.enters)
        run = self.launch()
        self.assertEqual((run["state"], run["reason"]), ("dispatching", "send_uncertain"))
        self.clock_now[0] += 301
        self.assertEqual(self.runs.reconcile(OWNER, 1, run["run_id"])["state"], "unknown")
        self.assertEqual(len(self.tmux.sent), self.sent_before + 1)  # the one attempt, never repeated
        self.assertEqual(len(self.tmux.enters), enters)  # no Enter repetition for a Run delivery

    def test_vanished_cli_is_not_killed_or_adopted(self) -> None:
        self.ready_worker()
        self.approve()
        run = self.launch()
        self.tmux.sessions.clear()
        self.clock_now[0] += 301
        view = self.runs.reconcile(OWNER, 1, run["run_id"])
        self.assertEqual(view["state"], "unknown")
        self.assertEqual((self.tmux.killed, self.tmux.new_calls[1:]), ([], []))


class OfflinePackageTests(RunCase):
    def test_package_is_offline_readable_private_and_free_of_secrets(self) -> None:
        self.ready_worker()
        self.approve(body="Do the work")
        run = self.launch()
        state = self.state_row(run["run_id"])
        package = self.package_dir(run["run_id"])
        del self.runs, self.service  # no Core object needed to read the package
        manifest_bytes = (package / "run.json").read_bytes()
        self.assertEqual(hashlib.sha256(manifest_bytes).hexdigest(), state["manifest_sha256"])
        manifest = json.loads(manifest_bytes)
        self.assertEqual((manifest["format"], manifest["run_id"], manifest["operative_stages"]),
                         ("rosc.run.v1", run["run_id"], ["E0"]))
        for item in manifest["contents"]:
            data = (package / item["file"]).read_bytes()
            self.assertEqual((hashlib.sha256(data).hexdigest(), len(data)), (item["sha256"], item["bytes"]))
        self.assertIn("Do the work", (package / "prompt.md").read_text())
        self.assertIn("Criterion A", (package / "spec.md").read_text())
        blob = b"".join(p.read_bytes() for p in package.iterdir())
        for secret in (state["run_token"].encode(), b"rosc.db", b"bootstrap_token", state["incarnation"].encode()):
            self.assertNotIn(secret, blob)
        self.assertEqual(stat.S_IMODE(package.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(package.stat().st_mode), 0o500)
        self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o400 for p in package.iterdir()))
        self.assertEqual(list(self.root.iterdir()), [self.root / "AGENTS.md"])  # project source untouched
        protocol = load_worker_protocol(DEFAULT_GLOBAL_LIBRARY)
        self.assertEqual(manifest["protocol"]["sha256"], protocol.sha256)

    def test_cli_side_helper_has_no_core_or_database_dependency(self) -> None:
        tree = ast.parse(Path(worker_ack.__file__).read_text())
        modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        modules |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertTrue(modules <= {"__future__", "json", "os", "sys", "time"}, modules)

    def test_existing_package_is_never_overwritten(self) -> None:
        self.ready_worker()
        self.approve()
        run = self.launch()
        package = self.package_dir(run["run_id"])
        os.chmod(package, 0o700)
        os.chmod(package / "prompt.md", 0o600)
        (package / "prompt.md").write_text("tampered")
        with sqlite3.connect(self.data / "rosc.db") as db:
            row_data = db.execute("select project_id, task_id from managed_run_states").fetchone()
        from rosc.models import ManagedRunState
        with self.sessions() as session:
            row = session.get(ManagedRunState, run["run_id"])
            files, root = self.runs._package_files(row, load_worker_protocol(DEFAULT_GLOBAL_LIBRARY))
            with self.assertRaises(WorkspaceConflict):
                self.runs._publish(row, files, root)
        self.assertEqual((package / "prompt.md").read_text(), "tampered")
        self.assertEqual(row_data, (1, 1))

    def test_custom_artifact_root_is_validated(self) -> None:
        self.ready_worker()
        self.approve()
        inside = self.root / "artifacts"
        inside.mkdir()
        self.runs = self.make_runs(artifact_roots={1: inside})
        with self.assertRaises(RunLaunchConflict) as caught:
            self.launch()
        self.assertEqual(caught.exception.code, "artifact_root_unsafe")
        loose = self.base / "loose"
        loose.mkdir()
        os.chmod(loose, 0o777)
        self.runs = self.make_runs(artifact_roots={1: loose})
        with self.assertRaises(RunLaunchConflict):
            self.launch()
        private = self.base / "private-artifacts"
        private.mkdir(mode=0o700)
        self.runs = self.make_runs(artifact_roots={1: private})
        run = self.launch()
        self.assertTrue((run_directory(private, 1, 1, run["run_id"]) / "package" / "run.json").is_file())
        self.assertFalse((self.data / "workspace/projects/P0001/tasks/T0001/runs").exists())
        self.assertEqual(self.count("run_records"), 1)

    def test_default_data_root_is_the_checkout_data_tree(self) -> None:
        with tempfile.TemporaryDirectory() as fixture:
            checkout = Path(fixture)
            data = resolve_data_dir({}, checkout=checkout)
            rid = str(uuid.uuid4())
            self.assertEqual(run_directory(data / "workspace", 1, 1, rid),
                             checkout / "data/workspace/projects/P0001/tasks/T0001/runs" / rid)
            configured = resolve_data_dir({"ROSC_DATA_DIR": str(checkout / "elsewhere")}, checkout=checkout)
            self.assertEqual(configured, (checkout / "elsewhere").resolve())
        with self.assertRaises(WorkspaceConflict):
            run_directory(self.data, 1, 1, "../escape")


if __name__ == "__main__":
    unittest.main()
