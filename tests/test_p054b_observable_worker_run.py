"""P054B: observable Worker execution - E0 -> execution continuation of the SAME Run, the run-pinned offline Worker
Artifact Tool (driven as a REAL subprocess), Core import/catch-up after a Core restart, ``reported`` lifecycle, the one
Worker -> Architect handoff and the scoped ``rosc_report_read`` Architect Tool.

Synthetic only: temporary SQLite migrated to head, fake tmux/proc, the REAL CLI hook functions in-process, the REAL
Artifact Tool file executed with ``python3 -I -S`` (no site-packages, so no ``rosc`` import is even possible).  No model
call, no real tmux send, no Owner data.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

from alembic import command

from rosc.app import DEFAULT_GLOBAL_LIBRARY
from rosc.authorization import LocalOwnerPolicy
from rosc.project_context import CAP_CONTEXT_READ, Caller, ProjectContextBroker, ToolError
from rosc.run_launch import RunLaunchConflict, RunLaunchService
from test_p042_worker_session import OWNER, WORKER_CAPS
from test_p054a_managed_run import RunCase

ARCHITECT_CAPS = ["project.context.read", "architect.document.read", "architect.document.write"]
TOOL_SOURCE = DEFAULT_GLOBAL_LIBRARY / "agents/workers/tools/rosc_run_tool.py"
STAGES = ["E0", "E1", "E2", "E3", "E4", "E5", "E6"]


class WorkerRunCase(RunCase):
    """A Run launched, delivered and ACKed through the real P054A path (no second Owner action afterwards)."""

    def acked(self, key: str = "launch-0001") -> tuple[dict, str]:
        incarnation, _pid = self.ready_worker()
        self.approve()
        run = self.launch(key)
        self.worker_accepts(incarnation, run)
        view = self.runs.reconcile(OWNER, 1, run["run_id"])  # Owner reconcile is evidence-only: it never types
        self.assertEqual(view["recorded_state"], "acknowledged")
        self.incarnation = incarnation
        return run, incarnation

    def exec_lines(self) -> list[str]:
        return [t for t in self.run_sends() if t.startswith("[ROSC run-exec ")]

    def running(self) -> dict:
        run, incarnation = self.acked()
        self.assertEqual(self.runs.tick(), 1)
        lines = self.exec_lines()
        self.assertEqual(len(lines), 1)
        self.cli_submit(incarnation, lines[0])  # the CLI's own UserPromptSubmit hook sees the execution line
        self.runs.tick()
        self.assertEqual(self.state_row(run["run_id"])["state"], "running")
        return run

    def run_dir(self, run_id: str) -> Path:
        return self.package_dir(run_id).parent

    def tool(self, run_id: str, *args: str, stdin: bytes | None = None, ok: bool = True) -> dict:
        """The REAL pinned tool copy, as a separate process from the Project root: no site-packages, no ROSC."""
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.base), "LANG": "C.UTF-8"}
        proc = subprocess.run([sys.executable, "-I", "-S", str(self.package_dir(run_id) / "rosc-run-tool.py"), *args],
                              cwd=self.root, env=env, input=stdin, capture_output=True, timeout=60)
        result = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual((proc.returncode == 0, result["ok"]), (ok, ok), (args, result, proc.stderr[-400:]))
        return result

    def journal(self, run_id: str) -> list[dict]:
        path = self.run_dir(run_id) / "journal/events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def plan(self, run_id: str, steps=("S01", "S02", "S03")) -> None:
        self.tool(run_id, "run", "start")
        previous = None
        for sid in steps:
            extra = ["--depends", previous] if previous else []
            self.tool(run_id, "plan", "add-step", "--id", sid, "--title", f"Step {sid}", "--description", "how",
                      "--expect", "works", "--test-intent", "unit", *extra)
            previous = sid
        self.tool(run_id, "plan", "publish", "--reason", "initial plan")

    def finish(self, run_id: str, outcome: str = "completed") -> None:
        body = self.base / f"report-{uuid.uuid4().hex}.md"
        body.write_text("Changed `feature.txt`. Tests T01/T02 recorded.\n")
        self.tool(run_id, "report", "finalize", "--outcome", outcome, "--body-file", str(body))
        self.tool(run_id, "run", "finish")

    def full_work(self, run_id: str) -> None:
        self.plan(run_id)
        for sid in ("S01", "S02"):
            self.tool(run_id, "step", "start", "--id", sid)
            (self.root / f"{sid}.txt").write_text(sid)  # a synthetic source-like edit AFTER the Plan
            self.tool(run_id, "step", "produced", "--id", sid, "--note", "done")
        log = self.base / "t01.log"
        log.write_text("2 passed\n")
        self.tool(run_id, "test", "record", "--id", "T01", "--steps", "S01,S02", "--command", "pytest -k s01_s02",
                  "--exit", "0", "--outcome", "passed", "--log-file", str(log))
        self.tool(run_id, "step", "start", "--id", "S03")
        self.tool(run_id, "step", "produced", "--id", "S03")
        self.tool(run_id, "test", "record", "--id", "T02", "--steps", "S03", "--command", "pytest -k s03",
                  "--exit", "1", "--outcome", "failed", "--log-stdin", stdin=b"1 failed\n")
        self.tool(run_id, "checkpoint", "save", "--current-step", "S03", "--note", "S03 test fails")

    def add_architect(self) -> str:
        architect = self.registry.register_agent(OWNER, display_name="Architect", runtime_adapter="opencode",
                                                 provider_id="anthropic", model_id="claude-x",
                                                 capabilities=ARCHITECT_CAPS)
        self.registry.create_assignment(OWNER, 1, agent_id=architect["agent_id"], role="architect",
                                        capabilities=ARCHITECT_CAPS)
        return architect["agent_id"]


class ContinuationTests(WorkerRunCase):
    def test_ack_leads_to_exactly_one_execution_line_without_a_second_owner_click(self) -> None:
        run, _incarnation = self.acked()
        rid = run["run_id"]
        self.assertEqual(self.exec_lines(), [])  # Owner reconcile/status never type the execution line
        self.runs.status(OWNER, 1, rid)
        self.assertEqual(self.exec_lines(), [])
        self.runs.tick()
        lines = self.exec_lines()
        state = self.state_row(rid)
        self.assertEqual((len(lines), state["state"]), (1, "exec_dispatching"))
        self.assertIn(state["exec_token"], lines[0])
        self.assertNotIn(state["run_token"], lines[0])  # never confusable with the E0 delivery
        self.assertIn(f"run_id={rid}", lines[0])
        self.assertIn("rosc-run-tool.py", lines[0])
        # retries, status refreshes, Owner reconcile, the same launch key and a Core restart never send again
        for _ in range(2):
            self.runs.tick()
            self.runs.reconcile(OWNER, 1, rid)
            self.assertEqual(self.launch()["run_id"], rid)
        self.build()
        self.runs.tick()
        self.client.get(f"/api/projects/P0001/runs/{rid}")
        self.assertEqual(len(self.exec_lines()), 1)
        events = [r[0] for r in self.rows(f"select to_state from managed_run_events where run_id='{rid}' order by seq")]
        self.assertEqual(events.count("exec_dispatching"), 1)

    def test_exec_submit_evidence_is_running_and_no_evidence_is_unknown_never_resent(self) -> None:
        run = self.running()
        view = self.runs.status(OWNER, 1, run["run_id"])
        self.assertEqual((view["state"], view["holding"], view["evidence"]["exec_submitted"]), ("running", True, True))
        self.assertFalse(view["work_started"])  # submission alone is not work: nothing is in the Journal yet

    def test_no_exec_submission_in_window_is_unknown_and_holds(self) -> None:
        run, _ = self.acked()
        self.runs.tick()
        self.clock_now[0] += 301
        self.runs.tick()
        row = self.state_row(run["run_id"])
        self.assertEqual((row["state"], row["reason"]), ("unknown", "no_exec_submit_within_timeout"))
        self.runs.tick()
        self.assertEqual(len(self.exec_lines()), 1)
        with self.assertRaises(RunLaunchConflict) as caught:  # UNKNOWN keeps the slot: no overlapping writer
            self.launch("launch-0002")
        self.assertEqual(caught.exception.code, "run_in_progress")

    def test_crash_after_execution_intent_is_never_resent(self) -> None:
        run, _ = self.acked()
        with patch.object(type(self.service), "deliver_run_line", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.runs.advance(run["run_id"])
        self.assertEqual(self.state_row(run["run_id"])["state"], "exec_dispatching")
        self.build()  # Core restart
        self.runs.tick()
        self.assertEqual(self.exec_lines(), [])
        self.clock_now[0] += 301
        self.runs.tick()
        self.assertEqual(self.state_row(run["run_id"])["state"], "unknown")
        self.assertEqual(self.exec_lines(), [])

    def test_crash_before_execution_intent_sends_once_with_the_same_token(self) -> None:
        run, _ = self.acked()
        original = RunLaunchService._transition

        def crash(self_, run_id, expected, to_state, reason, **kw):
            if to_state == "exec_dispatching":
                raise KeyboardInterrupt
            return original(self_, run_id, expected, to_state, reason, **kw)
        with patch.object(RunLaunchService, "_transition", crash):
            with self.assertRaises(KeyboardInterrupt):
                self.runs.tick()
        row = self.state_row(run["run_id"])
        self.assertEqual((row["state"], row["exec_intent_at"]), ("acknowledged", None))
        token = row["exec_token"]
        self.assertIsNotNone(token)
        self.build()
        self.runs.tick()
        self.assertEqual(len(self.exec_lines()), 1)
        self.assertIn(token, self.exec_lines()[0])

    def refused(self, act, reason: str) -> None:
        run, _ = self.acked()
        act(run["run_id"])
        self.runs.tick()
        row = self.state_row(run["run_id"])
        self.assertEqual((row["state"], row["reason"], row["exec_intent_at"]), ("not_executed", reason, None))
        self.assertEqual(self.exec_lines(), [])
        self.assertFalse(self.runs.status(OWNER, 1, run["run_id"])["holding"])  # nothing sent: the slot is released

    def test_stale_ack_is_not_executed(self) -> None:
        self.refused(lambda rid: self.clock_now.__setitem__(0, self.clock_now[0] + 400), "continuation_stale")

    def test_tampered_package_is_not_executed(self) -> None:
        self.refused(self.tamper, "package_invalid")

    def test_changed_cli_incarnation_is_not_executed(self) -> None:
        def swap(rid: str) -> None:
            with sqlite3.connect(self.data / "rosc.db") as db:
                db.execute("update worker_sessions set incarnation=?", (str(uuid.uuid4()),))
        self.refused(swap, "worker_changed")

    def tamper(self, rid: str) -> None:
        package = self.package_dir(rid)
        os.chmod(package, 0o700)
        os.chmod(package / "spec.md", 0o600)
        (package / "spec.md").write_text("changed")

    def test_historical_e0_only_package_is_never_upgraded_or_executed(self) -> None:
        library = self.legacy_library()
        incarnation, _ = self.ready_worker()
        self.approve()
        legacy = self.make_runs(library=library)
        run = legacy.launch(OWNER, 1, 1, self.agent_id, launch_key="launch-0001")
        manifest = json.loads((self.package_dir(run["run_id"]) / "run.json").read_text())
        self.assertEqual((manifest["operative_stages"], "tool" in manifest), (["E0"], False))
        before = {p.name: p.read_bytes() for p in self.package_dir(run["run_id"]).iterdir()}
        self.worker_accepts(incarnation, run)
        self.runs.reconcile(OWNER, 1, run["run_id"])  # the CURRENT 0.2.0 service sees the 0.1.0 Run
        self.runs.tick()
        row = self.state_row(run["run_id"])
        self.assertEqual((row["state"], row["reason"], row["protocol_version"]), ("not_executed", "e0_only_package", "0.1.0"))
        self.assertEqual({p.name: p.read_bytes() for p in self.package_dir(run["run_id"]).iterdir()}, before)
        self.assertEqual(self.exec_lines(), [])

    def test_interrupted_historical_launches_after_the_ruleset_changed(self) -> None:
        library = self.legacy_library()
        self.ready_worker()
        self.approve()
        legacy = self.make_runs(library=library)
        with patch.object(RunLaunchService, "_continue", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                legacy.launch(OWNER, 1, 1, self.agent_id, launch_key="launch-0001")
        rid = self.rows("select run_id from managed_run_states")[0][0]
        self.assertFalse(self.runs.status(OWNER, 1, rid)["resumable_with_same_key"])  # truthful: rules changed
        view = self.launch()  # resumed by the current service: never published under substituted rules
        self.assertEqual((view["state"], view["reason"]), ("not_delivered", "package_publish_failed:protocol_changed"))
        self.assertEqual(self.run_sends(), [])
        # a package already published under 0.1.0 is verified against its OWN snapshot and delivered unchanged
        with patch.object(RunLaunchService, "_dispatch", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                legacy.launch(OWNER, 1, 1, self.agent_id, launch_key="launch-0002")
        view = self.launch("launch-0002")
        self.assertEqual(view["state"], "dispatching")
        self.assertEqual(len(self.run_sends()), 1)

    def legacy_library(self) -> Path:
        """A 0.1.0-style E0-only global library (exact pinned digests, no tool) to create historical Runs."""
        library = self.base / f"legacy-{uuid.uuid4().hex[:6]}"
        workers = library / "agents/workers"
        shutil.copytree(DEFAULT_GLOBAL_LIBRARY / "agents/workers", workers)
        pins = []
        for ident, rel in (("role", "rules/role.md"), ("execution-protocol", "rules/execution-protocol.md")):
            data = (workers / rel).read_bytes()
            pins.append(f"  - id: {ident}\n    path: agents/workers/{rel}\n    sha256: "
                        f"{hashlib.sha256(data).hexdigest()}\n    bytes: {len(data)}\n")
        (workers / "ruleset.yaml").write_text("format: rosc.worker.ruleset.v1\nruleset_id: rosc.worker.global\n"
                                              "version: 0.1.0\noperative_stages: [E0]\nfiles:\n" + "".join(pins))
        return library

    def test_package_pins_the_tool_and_operative_protocol(self) -> None:
        run, _ = self.acked()
        package = self.package_dir(run["run_id"])
        manifest = json.loads((package / "run.json").read_text())
        self.assertEqual(manifest["operative_stages"], STAGES)
        tool = (package / "rosc-run-tool.py").read_bytes()
        self.assertEqual(tool, TOOL_SOURCE.read_bytes())
        self.assertEqual(manifest["tool"]["sha256"], hashlib.sha256(tool).hexdigest())
        self.assertEqual(manifest["artifacts"]["format"], "rosc.run-artifacts.v1")
        self.assertNotIn(self.state_row(run["run_id"])["incarnation"], (package / "run.json").read_text())
        protocol = (package / "worker-execution-protocol.md").read_text()
        for command_text in ("TOOL run start", "TOOL plan publish", "TOOL step produced", "TOOL test record",
                             "TOOL checkpoint save", "TOOL report finalize", "TOOL run finish",
                             "python3 <package>/rosc-run-tool.py", "[ROSC run-exec"):
            self.assertIn(command_text, protocol)


class ArtifactToolTests(WorkerRunCase):
    def test_static_tool_has_no_core_db_network_or_provider_dependency(self) -> None:
        tree = ast.parse(TOOL_SOURCE.read_text())
        modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        modules |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertTrue(modules <= {"__future__", "argparse", "errno", "fcntl", "hashlib", "json", "os", "re", "stat",
                                    "subprocess", "sys", "time"}, modules)
        self.assertNotIn("sqlite", TOOL_SOURCE.read_text())
        no_rosc = subprocess.run([sys.executable, "-I", "-S", "-c", "import rosc"], capture_output=True)
        self.assertNotEqual(no_rosc.returncode, 0)  # the tool's environment cannot even import ROSC

    def test_help_and_usage(self) -> None:
        run = self.running()
        proc = subprocess.run([sys.executable, "-I", "-S", str(self.package_dir(run["run_id"]) / "rosc-run-tool.py"),
                               "--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        for word in ("status", "plan", "step", "test", "checkpoint", "report", "run", "journal"):
            self.assertIn(word, proc.stdout)

    def test_full_offline_flow_with_core_stopped_imports_exactly_once_after_restart(self) -> None:
        architect = self.add_architect()
        run = self.running()
        rid = run["run_id"]
        self.tool(rid, "run", "start")
        self.assertEqual(self.runs.tick(), 1)  # Core imports the first fresh fact
        self.assertEqual(self.count("managed_run_journal_events"), 1)
        # -- Core is stopped: no Core service object exists; the Worker keeps writing through the tool only --
        del self.runs, self.service
        before_db = (self.data / "rosc.db").read_bytes()
        self.plan(rid)
        self.assertTrue((self.run_dir(rid) / "plan/v001.json").exists())
        plan_seq = [e["seq"] for e in self.journal(rid) if e["type"] == "plan.published"][0]
        self.full_work(rid)
        first_edit = min((self.root / name).stat().st_mtime_ns for name in ("S01.txt", "S02.txt"))
        self.assertLess((self.run_dir(rid) / "plan/v001.json").stat().st_mtime_ns, first_edit)  # Plan before edits
        self.tool(rid, "blocker", "record", "--id", "B01", "--summary", "flaky env", "--step", "S03")
        self.tool(rid, "blocker", "resolve", "--id", "B01", "--note", "fixed")
        self.finish(rid)
        self.assertEqual((self.data / "rosc.db").read_bytes(), before_db)  # nothing touched the Core database
        layout = self.run_dir(rid)
        for rel in ("plan/v001.json", "journal/events.jsonl", "checkpoints/latest.json", "reports/final.md",
                    "terminal.json", "evidence/T01.log", "evidence/T02.log"):
            self.assertTrue((layout / rel).is_file(), rel)
        self.assertEqual(stat.S_IMODE((layout / "reports/final.md").stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE((layout / "plan/v001.json").stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE((layout / "journal").stat().st_mode), 0o700)
        self.assertGreater(plan_seq, 1)
        # -- Core restarts and catches up: the missing delta exactly once, one terminal import, one handoff --
        self.build()
        self.runs.tick()
        self.runs.tick()
        self.build()
        self.runs.tick()
        row = self.state_row(rid)
        self.assertEqual((row["state"], row["outcome"]), ("reported", "completed"))
        self.assertEqual(self.count("managed_run_journal_events"), len(self.journal(rid)))
        self.assertEqual(self.count("managed_run_reports"), 1)
        self.assertEqual(self.count("managed_run_handoffs"), 1)
        handoffs = self.rows("select m.body, p.kind, p.agent_id, e.sender_agent_id, e.round_id, e.turn_id, c.task_id "
                             "from managed_run_handoffs h join conversation_messages m on m.message_id = h.message_id "
                             "join conversation_participants p on p.participant_id = m.sender_participant_id "
                             "join conversation_message_envelopes e on e.message_id = m.message_id "
                             "join conversations c on c.conversation_id = m.conversation_id")
        body, kind, sender, envelope_sender, round_id, turn_id, task_id = handoffs[0]
        self.assertEqual((kind, sender, envelope_sender, round_id, turn_id, task_id),
                         ("worker", self.agent_id, self.agent_id, None, None, 1))
        self.assertIn(rid, body)
        self.assertIn("passed 1, failed 1", body)
        self.assertIn("не VERIFIED", body)
        to = self.rows("select party_ref, party_kind from conversation_message_parties where role='to'")
        self.assertEqual(to, [(architect, "architect")])
        self.assertEqual((self.count("group_rounds"), self.count("group_turns")), (0, 0))  # no group turn / quota
        view = self.runs.status(OWNER, 1, rid)
        self.assertEqual((view["holding"], view["review"]["state"], view["review"]["next_actor"]),
                         (False, "awaiting_architect_review", "Architect"))
        steps = {s["id"]: (s["implementation"], s["test"]) for s in view["plan"]["steps"]}
        self.assertEqual(steps, {"S01": ("PRODUCED", "PASSED"), "S02": ("PRODUCED", "PASSED"),
                                 "S03": ("PRODUCED", "FAILED")})
        self.assertEqual(view["plan"]["progress"]["produced"], 3)
        self.assertEqual(view["report"]["sha256"],
                         hashlib.sha256((layout / "reports/final.md").read_bytes()).hexdigest())
        self.assertEqual(len(self.run_sends()), 2)  # E0 delivery + one execution line; no handoff keystroke
        # no private Run artifact is inside the Git-tracked source tree
        tracked = subprocess.run(["git", "ls-files", "data"], cwd=DEFAULT_GLOBAL_LIBRARY.parent, capture_output=True,
                                 text=True).stdout
        self.assertEqual(tracked.strip(), "")

    def test_without_architect_the_handoff_is_stored_for_the_owner(self) -> None:
        run = self.running()
        self.plan(run["run_id"], steps=("S01",))
        self.finish(run["run_id"], outcome="partial")
        self.runs.tick()
        self.assertEqual(self.rows("select addressed_to from managed_run_handoffs"), [("owner",)])

    def test_claims_without_the_tool_record_nothing(self) -> None:
        run = self.running()
        self.cli_stop(self.incarnation, "Plan: S01, S02. S01 PRODUCED, tests PASSED. ROSC Run completed.")
        self.runs.tick()
        self.assertFalse((self.run_dir(run["run_id"]) / "journal").exists())
        self.assertEqual(self.count("managed_run_journal_events"), 0)
        self.assertEqual(self.state_row(run["run_id"])["state"], "running")

    def test_two_independent_axes_batch_test_and_stale(self) -> None:
        run = self.running()
        rid = run["run_id"]
        self.plan(rid, steps=("S01", "S02"))
        for sid in ("S01", "S02"):
            self.tool(rid, "step", "start", "--id", sid)
            self.tool(rid, "step", "produced", "--id", sid)
        self.tool(rid, "test", "begin", "--id", "T01", "--steps", "S01,S02")
        self.tool(rid, "test", "record", "--id", "T01", "--steps", "S01,S02", "--command", "make t", "--exit", "0",
                  "--outcome", "passed")
        result = self.tool(rid, "step", "start", "--id", "S01")  # rework after a passed test
        self.assertEqual((result["implementation"], result["test"]), ("IN_PROGRESS", "STALE"))
        self.tool(rid, "step", "produced", "--id", "S01")
        self.tool(rid, "test", "record", "--id", "T02", "--steps", "S01", "--command", "make t", "--exit", "2",
                  "--outcome", "inconclusive", "--limitations", "no network")
        self.runs.tick()
        steps = {s["id"]: s for s in self.runs.status(OWNER, 1, rid)["plan"]["steps"]}
        self.assertEqual((steps["S01"]["implementation"], steps["S01"]["test"], steps["S01"]["tests"]),
                         ("PRODUCED", "INCONCLUSIVE", ["T01", "T02"]))
        self.assertEqual((steps["S02"]["implementation"], steps["S02"]["test"]), ("PRODUCED", "PASSED"))
        tests = {t["test_id"]: t for t in self.runs.status(OWNER, 1, rid)["progress"]["tests"]}
        self.assertEqual(tests["T01"]["steps"], ["S01", "S02"])
        self.assertEqual(tests["T01"]["command"], "make t")

    def test_refusals_never_modify_prior_valid_evidence(self) -> None:
        run = self.running()
        rid = run["run_id"]
        self.assertEqual(self.tool(rid, "step", "start", "--id", "S01", ok=False)["error"], "plan_not_published")
        self.plan(rid, steps=("S01", "S02"))
        journal = self.run_dir(rid) / "journal/events.jsonl"
        snapshot = journal.read_bytes()
        refusals = [
            (("plan", "add-step", "--id", "bad id", "--title", "x"), "invalid_argument"),
            (("step", "start", "--id", "S09"), "unknown_step"),
            (("step", "produced", "--id", "S01"), "invalid_transition"),
            (("plan", "publish"), None),  # identical re-publish is a no-op
            (("test", "record", "--id", "T01", "--steps", "S01", "--command", "x", "--exit", "0", "--outcome",
              "passed"), "invalid_transition"),
            (("report", "finalize", "--outcome", "done", "--body-stdin"), "invalid_argument"),
            (("blocker", "resolve", "--id", "B09", "--note", "x"), "unknown_blocker"),
        ]
        for args, code in refusals:
            with self.subTest(args=args):
                result = self.tool(rid, *args, ok=code is None)
                if code:
                    self.assertEqual(result["error"], code)
                else:
                    self.assertTrue(result.get("unchanged"))
                self.assertEqual(journal.read_bytes(), snapshot)
        self.tool(rid, "step", "start", "--id", "S01")
        self.tool(rid, "step", "produced", "--id", "S01")
        args = ("test", "record", "--id", "T01", "--steps", "S01", "--command", "make", "--exit", "0", "--outcome",
                "passed")
        self.tool(rid, *args)
        snapshot = journal.read_bytes()
        self.assertTrue(self.tool(rid, *args)["unchanged"])  # an identical retry
        self.assertEqual(self.tool(rid, *args[:-1], "failed", ok=False)["error"], "test_conflict")
        self.assertEqual(self.tool(rid, "report", "finalize", "--outcome", "completed", "--body-stdin",
                                   stdin=b"x" * 262_145, ok=False)["error"], "too_large")
        self.assertEqual(journal.read_bytes(), snapshot)
        # interrupted append: a torn tail is detected, preserved and recorded; never silently dropped
        partial = b'{"schema": "rosc.run-event.v1", "seq": 9'
        with open(journal, "ab") as handle:
            handle.write(partial)
        self.assertEqual(self.tool(rid, "note", "--kind", "action", "--text", "x", ok=False)["error"], "journal_torn")
        self.assertEqual(self.tool(rid, "status")["journal"]["torn_tail_bytes"], len(partial))
        recovered = self.tool(rid, "journal", "recover")
        self.assertTrue((self.run_dir(rid) / recovered["preserved_as"]).read_bytes().startswith(b'{"schema"'))
        self.assertTrue(journal.read_bytes().startswith(snapshot))
        self.assertEqual(self.journal(rid)[-1]["type"], "journal.recovered")
        # a modified historical Plan revision and a symlinked artifact directory fail closed
        plan = self.run_dir(rid) / "plan/v001.json"
        original = plan.read_bytes()
        os.chmod(plan, 0o600)
        plan.write_bytes(original.replace(b"Step S01", b"Step XXX"))
        self.assertEqual(self.tool(rid, "status", ok=False)["error"], "plan_corrupt")
        plan.write_bytes(original)
        evidence = self.run_dir(rid) / "evidence"
        shutil.rmtree(evidence, ignore_errors=True)
        outside = self.base / "outside"
        outside.mkdir()
        evidence.symlink_to(outside)
        log = self.base / "x.log"
        log.write_text("x")
        self.assertEqual(self.tool(rid, "test", "record", "--id", "T05", "--steps", "S01", "--command", "y", "--exit",
                                   "0", "--outcome", "passed", "--log-file", str(log), ok=False)["error"], "unsafe_path")
        self.assertEqual(list(outside.iterdir()), [])

    def test_tool_hash_mismatch_is_refused(self) -> None:
        run = self.running()
        tool = self.package_dir(run["run_id"]) / "rosc-run-tool.py"
        os.chmod(tool.parent, 0o700)
        os.chmod(tool, 0o600)
        tool.write_text(tool.read_text() + "\n# changed\n")
        self.assertEqual(self.tool(run["run_id"], "status", ok=False)["error"], "tool_mismatch")

    def test_preflight_blocker_before_a_plan_is_an_honest_blocked_report(self) -> None:
        run = self.running()
        rid = run["run_id"]
        self.tool(rid, "run", "start")
        self.tool(rid, "blocker", "record", "--id", "B01", "--summary", "Spec contradicts AGENTS.md",
                  "--next-action", "Owner decides", "--owner-decision")
        self.assertEqual(self.tool(rid, "report", "finalize", "--outcome", "completed", "--body-stdin", stdin=b"x",
                                   ok=False)["error"], "plan_not_published")
        self.finish(rid, outcome="blocked")
        self.runs.tick()
        view = self.runs.status(OWNER, 1, rid)
        self.assertEqual((view["state"], view["execution"]["outcome"], view["plan"]["revision"]),
                         ("reported", "blocked", 0))
        self.assertEqual(view["progress"]["blockers"][0]["blocker_id"], "B01")
        self.assertIn("No Working Plan was published.", (self.run_dir(rid) / "reports/final.md").read_text())


CRASH_DRIVER = '''import importlib.util, os, sys
tool, kind, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
spec = importlib.util.spec_from_file_location("pinned_tool", tool)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
real = mod.append_event
def crashing(run, events, k, data, **kw):
    if k == kind:
        os._exit(137)
    return real(run, events, k, data, **kw)
mod.append_event = crashing
raise SystemExit(mod.main(argv))
'''


class CrashWindowTests(WorkerRunCase):
    """r02: a process crash AFTER publish_once(plan/vNNN.json | reports/final.md) and BEFORE the matching Journal
    append.  The REAL pinned tool is loaded from its package path by a driver that os._exit()s exactly at that append."""

    def crash(self, rid: str, kind: str, *args: str) -> None:
        before = [e["type"] for e in self.journal(rid)].count(kind)
        driver = self.base / "crash_driver.py"
        driver.write_text(CRASH_DRIVER)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.base), "LANG": "C.UTF-8"}
        proc = subprocess.run([sys.executable, "-I", "-S", str(driver), str(self.package_dir(rid) / "rosc-run-tool.py"),
                               kind, *args], cwd=self.root, env=env, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 137, proc.stdout + proc.stderr)
        self.assertEqual([e["type"] for e in self.journal(rid)].count(kind), before)  # file published, no event

    def add(self, rid: str, *sids: str) -> None:
        for sid in sids:
            self.tool(rid, "plan", "add-step", "--id", sid, "--title", f"Step {sid}", "--description", "how",
                      "--expect", "works", "--test-intent", "unit")

    def published(self, rid: str) -> list[dict]:
        return [e for e in self.journal(rid) if e["type"] == "plan.published"]

    def orphan_v001(self, rid: str) -> bytes:
        self.tool(rid, "run", "start")
        self.add(rid, "S01", "S02")
        self.crash(rid, "plan.published", "plan", "publish", "--reason", "initial plan")
        raw = (self.run_dir(rid) / "plan/v001.json").read_bytes()
        status = self.tool(rid, "status")
        self.assertEqual((status["plan_revision"], status["steps"]), (0, []))  # a file alone is NOT a published Plan
        self.assertEqual(self.tool(rid, "step", "start", "--id", "S01", ok=False)["error"], "plan_not_published")
        return raw

    def test_plan_publish_crash_is_repaired_by_one_event_for_the_existing_file(self) -> None:
        rid = self.running()["run_id"]
        raw = self.orphan_v001(rid)
        retry = self.tool(rid, "plan", "publish", "--reason", "initial plan")
        self.assertEqual((retry["published"], retry["recovered"], retry["sha256"]), (1, True, hashlib.sha256(raw).hexdigest()))
        self.assertEqual((self.run_dir(rid) / "plan/v001.json").read_bytes(), raw)  # never rewritten
        events = len(self.journal(rid))
        self.assertTrue(self.tool(rid, "plan", "publish", "--reason", "initial plan")["unchanged"])  # repeat: no dup
        self.assertEqual((len(self.journal(rid)), len(self.published(rid))), (events, 1))
        self.assertEqual(self.published(rid)[0]["data"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.tool(rid, "step", "start", "--id", "S01")  # ordinary progress continues
        self.assertEqual(sorted(p.name for p in (self.run_dir(rid) / "plan").glob("v*.json")), ["v001.json"])

    def test_plan_revise_crash_is_repaired_once_and_normal_path_is_unchanged(self) -> None:
        rid = self.running()["run_id"]
        self.tool(rid, "run", "start")
        self.add(rid, "S01")
        first = self.tool(rid, "plan", "publish", "--reason", "initial plan")
        self.assertNotIn("recovered", first)  # the ordinary path is untouched
        self.assertTrue(self.tool(rid, "plan", "publish", "--reason", "initial plan")["unchanged"])
        self.add(rid, "S02")
        self.crash(rid, "plan.published", "plan", "revise", "--reason", "add S02")
        raw = (self.run_dir(rid) / "plan/v002.json").read_bytes()
        self.assertEqual(self.tool(rid, "status")["plan_revision"], 1)
        self.assertEqual(self.tool(rid, "plan", "publish", ok=False)["error"], "plan_exists")  # wrong command
        retry = self.tool(rid, "plan", "revise", "--reason", "add S02")
        self.assertEqual((retry["published"], retry["recovered"], retry["plan_revision"]), (2, True, 2))
        self.assertTrue(self.tool(rid, "plan", "revise", "--reason", "add S02")["unchanged"])
        self.assertEqual([e["data"]["revision"] for e in self.published(rid)], [1, 2])
        self.assertEqual(self.published(rid)[1]["data"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual((self.run_dir(rid) / "plan/v002.json").read_bytes(), raw)
        self.tool(rid, "step", "start", "--id", "S02")

    def test_mismatched_or_tampered_orphan_plan_fails_closed_and_is_kept(self) -> None:
        rid = self.running()["run_id"]
        raw = self.orphan_v001(rid)
        events = self.journal(rid)
        self.assertEqual(self.tool(rid, "plan", "publish", "--reason", "other", ok=False)["error"], "plan_conflict")
        self.assertEqual(self.tool(rid, "plan", "revise", "--reason", "x", ok=False)["error"], "plan_not_published")
        self.add(rid, "S03")  # the draft no longer matches the orphan: intent cannot be proven
        self.assertEqual(self.tool(rid, "plan", "publish", "--reason", "initial plan", ok=False)["error"],
                         "plan_conflict")
        self.tool(rid, "plan", "remove-step", "--id", "S03")
        draft = self.run_dir(rid) / "plan/draft.json"
        kept = draft.read_bytes()
        draft.unlink()  # without the draft the file would only "prove itself": refused
        self.assertEqual(self.tool(rid, "plan", "publish", "--reason", "initial plan", ok=False)["error"],
                         "plan_conflict")
        draft.write_bytes(kept)
        orphan = self.run_dir(rid) / "plan/v001.json"
        os.chmod(orphan, 0o600)
        orphan.write_bytes(raw.replace(b'"Step S02"', b'"Step SX"'))  # tampered, still well-formed and linked
        self.assertEqual(self.tool(rid, "plan", "publish", "--reason", "initial plan", ok=False)["error"],
                         "plan_conflict")
        self.assertEqual(self.journal(rid), events)  # no event was appended by any refusal
        self.assertEqual(sorted(p.name for p in (self.run_dir(rid) / "plan").glob("v*.json")), ["v001.json"])
        orphan.write_bytes(raw)  # restored genuine bytes: the same retry now recovers
        self.assertTrue(self.tool(rid, "plan", "publish", "--reason", "initial plan")["recovered"])

    def orphan_report(self, rid: str, outcome: str = "partial") -> tuple[Path, bytes]:
        self.plan(rid, steps=("S01",))
        body = self.base / "body.md"
        body.write_text("Partial work.\n")
        self.crash(rid, "report.finalized", "report", "finalize", "--outcome", outcome, "--body-file", str(body))
        raw = (self.run_dir(rid) / "reports/final.md").read_bytes()
        self.assertEqual(self.tool(rid, "run", "finish", ok=False)["error"], "report_missing")
        return body, raw

    def test_report_finalize_crash_is_repaired_once_and_imports(self) -> None:
        rid = self.running()["run_id"]
        body, raw = self.orphan_report(rid)
        retry = self.tool(rid, "report", "finalize", "--outcome", "partial", "--body-file", str(body))
        self.assertEqual((retry["recovered"], retry["sha256"], retry["bytes"]),
                         (True, hashlib.sha256(raw).hexdigest(), len(raw)))
        self.assertTrue(self.tool(rid, "report", "finalize", "--outcome", "partial", "--body-file", str(body))["unchanged"])
        self.assertEqual([e["type"] for e in self.journal(rid)].count("report.finalized"), 1)
        self.assertEqual((self.run_dir(rid) / "reports/final.md").read_bytes(), raw)
        self.tool(rid, "run", "finish")
        self.runs.tick()
        self.assertEqual(self.state_row(rid)["state"], "reported")
        self.assertEqual(self.count("managed_run_handoffs"), 1)

    def test_mismatched_or_tampered_orphan_report_fails_closed_and_is_kept(self) -> None:
        rid = self.running()["run_id"]
        body, raw = self.orphan_report(rid)
        report = self.run_dir(rid) / "reports/final.md"
        events = self.journal(rid)
        other = self.base / "other.md"
        other.write_text("Different body.\n")
        for outcome, path in (("partial", other), ("failed", body)):
            self.assertEqual(self.tool(rid, "report", "finalize", "--outcome", outcome, "--body-file", str(path),
                                       ok=False)["error"], "report_conflict")
        os.chmod(report, 0o600)
        report.write_bytes(raw + b"forged\n")
        self.assertEqual(self.tool(rid, "report", "finalize", "--outcome", "partial", "--body-file", str(body),
                                   ok=False)["error"], "report_conflict")
        self.assertEqual((self.journal(rid), report.read_bytes()), (events, raw + b"forged\n"))  # kept, nothing added
        report.write_bytes(raw)
        self.tool(rid, "note", "--kind", "action", "--text", "later note")  # the Journal moved on: pre-finalization state no longer proven
        self.assertEqual(self.tool(rid, "report", "finalize", "--outcome", "partial", "--body-file", str(body),
                                   ok=False)["error"], "report_conflict")
        self.assertEqual(report.read_bytes(), raw)


class ImportTests(WorkerRunCase):
    def test_completion_requires_report_closed_journal_and_terminal(self) -> None:
        run = self.running()
        rid = run["run_id"]
        self.plan(rid, steps=("S01",))
        body = self.base / "r.md"
        body.write_text("report")
        self.tool(rid, "report", "finalize", "--outcome", "completed", "--body-file", str(body))
        self.runs.tick()
        self.assertEqual(self.state_row(rid)["state"], "running")  # finalized but the Journal is not closed
        self.tool(rid, "run", "finish")
        terminal = self.run_dir(rid) / "terminal.json"
        good = terminal.read_text()
        terminal.write_text(good.replace('"outcome": "completed"', '"outcome": "failed"'))  # forged outcome
        self.runs.tick()
        self.assertEqual((self.state_row(rid)["state"], self.problem(rid)), ("running", "terminal_invalid"))
        terminal.unlink()
        self.runs.tick()
        self.assertEqual((self.state_row(rid)["state"], self.problem(rid)), ("running", "terminal_missing"))
        self.assertTrue(self.tool(rid, "run", "finish")["repaired"])  # re-derived from the closing event
        self.runs.tick()
        self.assertEqual(self.state_row(rid)["state"], "reported")
        self.assertEqual(self.count("managed_run_handoffs"), 1)

    def problem(self, rid: str) -> str | None:
        return self.rows(f"select problem from managed_run_imports where run_id='{rid}'")[0][0]

    def test_tampered_report_before_import_never_completes(self) -> None:
        run = self.running()
        rid = run["run_id"]
        self.plan(rid, steps=("S01",))
        self.finish(rid)
        report = self.run_dir(rid) / "reports/final.md"
        os.chmod(report, 0o600)
        report.write_text("forged")
        self.runs.tick()
        self.assertEqual((self.state_row(rid)["state"], self.problem(rid)), ("running", "report_tampered"))
        self.assertEqual((self.count("managed_run_reports"), self.count("managed_run_handoffs")), (0, 0))

    def test_history_rewrite_and_wrong_run_files_are_refused_without_erasing(self) -> None:
        run = self.running()
        rid = run["run_id"]
        self.plan(rid, steps=("S01", "S02"))
        self.tool(rid, "step", "start", "--id", "S01")
        self.runs.tick()
        imported = self.count("managed_run_journal_events")
        journal = self.run_dir(rid) / "journal/events.jsonl"
        good = journal.read_bytes()
        lines = good.splitlines(keepends=True)
        forged = json.loads(lines[0])
        forged["at"] = "2020-01-01T00:00:00Z"
        forged["hash"] = hashlib.sha256(json.dumps({k: v for k, v in forged.items() if k != "hash"}, sort_keys=True,
                                                   separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        journal.write_bytes(json.dumps(forged).encode() + b"\n" + b"".join(lines[1:]))
        self.runs.tick()
        self.assertIn(self.problem(rid), ("journal_invalid", "journal_history_changed"))
        self.assertEqual(self.count("managed_run_journal_events"), imported)
        journal.write_bytes(good)
        # an event of another Run/identity appended by hand (not the tool) is refused, earlier facts stay
        alien = json.loads(lines[-1])
        alien.update({"seq": len(lines) + 1, "prev_hash": alien["hash"], "run_id": str(uuid.uuid4())})
        alien["hash"] = hashlib.sha256(json.dumps({k: v for k, v in alien.items() if k != "hash"}, sort_keys=True,
                                                  separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        with open(journal, "ab") as handle:
            handle.write(json.dumps(alien).encode() + b"\n")
        self.runs.tick()
        self.assertEqual((self.problem(rid), self.count("managed_run_journal_events")), ("journal_invalid", imported))
        journal.write_bytes(good)
        self.runs.tick()
        self.assertIsNone(self.problem(rid))
        # a torn tail imports the valid prefix and is reported, not hidden
        with open(journal, "ab") as handle:
            handle.write(b'{"partial')
        self.runs.tick()
        view = self.runs.status(OWNER, 1, rid)["progress"]["import"]
        self.assertEqual((view["problem"], view["torn_tail_bytes"]), ("journal_torn_tail", 9))
        # a symlinked journal is never followed
        journal.unlink()
        journal.symlink_to(self.base / "elsewhere.jsonl")
        self.runs.tick()
        self.assertEqual(self.problem(rid), "unsafe_path")
        self.assertEqual(self.count("managed_run_journal_events"), imported)

    def test_report_mutated_after_registration_fails_reads(self) -> None:
        run = self.running()
        rid = run["run_id"]
        self.plan(rid, steps=("S01",))
        self.finish(rid)
        self.runs.tick()
        self.assertIn("Run Report", self.runs.report(OWNER, 1, rid)["content"])
        report = self.run_dir(rid) / "reports/final.md"
        os.chmod(report, 0o600)
        report.write_text("changed after review started")
        with self.assertRaises(RunLaunchConflict) as caught:
            self.runs.report(OWNER, 1, rid)
        self.assertEqual(caught.exception.code, "report_integrity_failed")
        self.assertEqual(self.state_row(rid)["state"], "reported")  # an earlier valid fact is never rewritten

    def test_http_journal_report_and_reads_never_send(self) -> None:
        run = self.running()
        rid = run["run_id"]
        self.plan(rid, steps=("S01",))
        self.finish(rid)
        sends = len(self.tmux.sent)
        self.assertEqual(self.client.get(f"/api/projects/P0001/runs/{rid}/journal").json()["events"], [])
        self.runs.tick()
        page = self.client.get(f"/api/projects/P0001/runs/{rid}/journal?after=0&limit=2").json()
        self.assertEqual(([e["seq"] for e in page["events"]], page["next_after"]), ([1, 2], 2))
        report = self.client.get(f"/api/projects/P0001/runs/{rid}/report")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["status"], "worker_reported")
        self.assertEqual(self.client.get(f"/api/projects/P0002/runs/{rid}/report").status_code, 404)
        self.assertEqual(self.client.get(f"/api/projects/P0001/runs/{uuid.uuid4()}/journal").status_code, 404)
        authoring = self.client.get("/api/projects/P0001/tasks/T0001/authoring").json()
        self.assertEqual((authoring["next"]["action"], authoring["next"]["next_actor"], authoring["run"]["state"]),
                         ("awaiting_review", "Architect", "reported"))
        self.assertEqual(len(self.tmux.sent), sends)

    def test_http_launch_is_one_click_idempotent(self) -> None:
        self.ready_worker()
        self.approve()
        body = {"agent_id": self.agent_id, "launch_key": "ui-click-0001"}
        first = self.client.post("/api/projects/P0001/tasks/T0001/runs", json=body)
        second = self.client.post("/api/projects/P0001/tasks/T0001/runs", json=body)
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.json()["run_id"], second.json()["run_id"])
        other = self.client.post("/api/projects/P0001/tasks/T0001/runs", json={**body, "launch_key": "ui-click-0002"})
        self.assertEqual((other.status_code, other.json()["detail"]["code"]), (409, "run_in_progress"))
        self.assertEqual(len(self.run_sends()), 1)
        authoring = self.client.get("/api/projects/P0001/tasks/T0001/authoring").json()
        self.assertEqual((authoring["next"]["action"], authoring["next"]["enabled"], authoring["run"]["holding"]),
                         ("observe_run", False, True))


class ArchitectReportToolTests(WorkerRunCase):
    def broker(self) -> ProjectContextBroker:
        return ProjectContextBroker(self.sessions, self.data / "workspace", DEFAULT_GLOBAL_LIBRARY,
                                    run_reports=self.runs.report_for_tool)

    def caller(self, project: int = 1) -> Caller:
        return Caller(str(uuid.uuid4()), project, str(uuid.uuid4()), str(uuid.uuid4()), "p", "m",
                      frozenset({CAP_CONTEXT_READ}))

    def reported(self) -> str:
        run = self.running()
        self.plan(run["run_id"], steps=("S01",))
        self.finish(run["run_id"])
        self.runs.tick()
        return run["run_id"]

    def test_own_project_report_is_bounded_and_digest_checked(self) -> None:
        rid = self.reported()
        broker = self.broker()
        with self.sessions() as session:
            page = broker.tool_report_read(session, self.caller(), {"run_id": rid, "limit": 50})
            self.assertEqual((page["kind"], len(page["content"]), page["status"]), ("run_report", 50, "worker_reported"))
            self.assertIsNotNone(page["next_offset"])
            full = broker.tool_report_read(session, self.caller(), {"run_id": rid, "task_id": "T0001"})
            self.assertIn("## Worker report", full["content"])
            listed = broker.tool_run_read(session, self.caller(), {"run_id": rid})
            self.assertEqual(listed["managed_report"]["read_with"], "rosc_report_read")
            for caller, args in ((self.caller(2), {"run_id": rid}), (self.caller(), {"run_id": str(uuid.uuid4())}),
                                 (self.caller(), {"run_id": "../../etc/passwd"}),
                                 (self.caller(), {"run_id": rid, "task_id": "T0002"})):
                with self.subTest(args=args), self.assertRaises(ToolError) as caught:
                    broker.tool_report_read(session, caller, args)
                self.assertIn(caught.exception.code, ("not_found", "task_not_found"))
                self.assertNotIn(str(self.data), caught.exception.message)
        report = self.run_dir(rid) / "reports/final.md"
        os.chmod(report, 0o600)
        report.write_text("tampered")
        with self.sessions() as session, self.assertRaises(ToolError) as caught:
            broker.tool_report_read(session, self.caller(), {"run_id": rid})
        self.assertEqual(caught.exception.code, "report_integrity_failed")

    def test_inactive_session_is_denied(self) -> None:
        rid = self.reported()
        result = self.broker().call(str(uuid.uuid4()), "report_read", {"run_id": rid})
        self.assertEqual((result["ok"], result["error"]["code"]), (False, "session_not_active"))
        self.assertNotIn("content", json.dumps(result))


class MigrationTests(WorkerRunCase):
    def test_populated_p013_and_p054a_history_survives_upgrade_and_downgrade_refuses(self) -> None:
        from rosc.run_provenance import RunProvenanceService
        provenance = RunProvenanceService(self.sessions, LocalOwnerPolicy())
        external = provenance.register_external_run(OWNER, 1, 1, source_kind="manual-report", source_key="ext-1",
                                                    reported_executor="someone")
        provenance.register_report_reference(OWNER, 1, external["run_id"], report_kind="executor_report",
                                             source_ref="reports/r1.md", source_key="r1")
        run, _ = self.acked()
        tables = ("run_records", "report_records", "evidence_records", "managed_run_states", "managed_run_events")
        command.downgrade(self.alembic, "0019_managed_run_foundation")  # no P054B facts yet: allowed
        before = {t: self.rows(f"select * from {t}") for t in tables}
        self.assertEqual(len(before["managed_run_states"][0]), 29)
        with self.assertRaises(sqlite3.IntegrityError):  # 0019 CHECK: no P054B state
            with sqlite3.connect(self.data / "rosc.db") as db:
                db.execute("update managed_run_states set state='running' where run_id=?", (run["run_id"],))
        command.upgrade(self.alembic, "head")
        after = {t: self.rows(f"select * from {t}") for t in tables}
        self.assertEqual(after["managed_run_events"], before["managed_run_events"])
        self.assertEqual([r[:29] for r in after["managed_run_states"]], before["managed_run_states"])
        self.assertEqual({t: after[t] for t in tables[:3]}, {t: before[t] for t in tables[:3]})
        triggers = {r[0] for r in self.rows("select name from sqlite_master where type='trigger'")}
        self.assertTrue({"tr_run_records_immutable", "tr_run_records_managed_shape", "tr_managed_run_states_identity",
                         "tr_managed_run_events_immutable", "tr_managed_run_journal_events_immutable",
                         "tr_managed_run_handoffs_scope"} <= triggers)
        with self.assertRaises(sqlite3.IntegrityError):  # P013 immutability still in force
            with sqlite3.connect(self.data / "rosc.db") as db:
                db.execute("update run_records set reported_executor='x' where run_id=?", (external["run_id"],))
        self.runs.tick()  # now the Run continues: P054B history exists
        with self.assertRaises(sqlite3.IntegrityError):  # the execution intent is write-once; no way back to E0
            with sqlite3.connect(self.data / "rosc.db") as db:
                db.execute("update managed_run_states set state='acknowledged' where run_id=?", (run["run_id"],))
        with self.assertRaises(sqlite3.IntegrityError):  # an execution state always keeps its intent
            with sqlite3.connect(self.data / "rosc.db") as db:
                db.execute("update managed_run_states set exec_intent_at=NULL where run_id=?", (run["run_id"],))
        with self.assertRaises(RuntimeError):
            command.downgrade(self.alembic, "0019_managed_run_foundation")
        self.assertEqual(self.state_row(run["run_id"])["state"], "exec_dispatching")
