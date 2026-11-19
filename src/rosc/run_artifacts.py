"""P054B: bounded, idempotent Core import of a managed Run's local Worker artifacts + their canonical projection.

The Worker writes its Run directory ONLY through the run-pinned Worker Artifact Tool (``package/rosc-run-tool.py``) and
never talks to Core.  This module is the independent reader on the Core side:

* it re-validates every file it imports (exact package/manifest digest, Run/Project/Task/Worker/incarnation/protocol
  identity, contiguous hash-chained Journal sequence, linked Plan revisions, allowed Step transitions, bounded sizes,
  no symlinks/escapes) with its OWN code - nothing is taken from the Worker's tool on trust;
* it stores imported facts in Core-owned additive tables (``managed_run_journal_events``, ``managed_run_plan_revisions``,
  ``managed_run_reports``) behind its own cursor (``managed_run_imports``), so the Worker's history is independent of the
  import cursor and a replay never duplicates;
* a conflict (rewritten history, tampered Report, wrong-Run file, future Plan ref ...) is recorded as an import
  ``problem`` and stops further import WITHOUT erasing or rewriting earlier valid facts;
* a Run becomes ``reported`` only from a closed Journal + digest-matching finalized Report + correlated terminal record,
  in ONE transaction together with the single Worker -> Architect handoff message and its receipt.

Everything imported is an OBSERVED Worker claim: never an Owner authorization, an Architect verification or proof that a
code/test claim is true.  Nothing here sends a keystroke, starts a process or calls a model.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .models import (MANAGED_HOLDING_STATES, RUN_EXEC_DISPATCHING, RUN_OUTCOMES, RUN_REPORTED, RUN_RUNNING, RUN_UNKNOWN,
                     ManagedRunEvent, ManagedRunHandoff, ManagedRunImport, ManagedRunJournalEvent,
                     ManagedRunPlanRevision, ManagedRunReport, ManagedRunState, Project, RunRecord)
from .workspace import WorkspaceConflict, run_directory

ARTIFACT_FORMAT = "rosc.run-artifacts.v1"
PLAN_SCHEMA = "rosc.run-plan.v1"
EVENT_SCHEMA = "rosc.run-event.v1"
CHECKPOINT_SCHEMA = "rosc.run-checkpoint.v1"
TERMINAL_SCHEMA = "rosc.run-terminal.v1"
MAX_MANIFEST = 262_144
MAX_PLAN = 524_288
MAX_PLANS = 200
MAX_JOURNAL = 8_388_608
MAX_EVENTS = 5000
MAX_LINE = 65_536
MAX_CHECKPOINT = 262_144
MAX_TERMINAL = 65_536
MAX_REPORT = 1_048_576
MAX_REPORT_PAGE = 65_536
REPORT_PATH = "reports/final.md"
EVENT_TYPES = {"run.started", "plan.published", "step.status", "test.started", "test.recorded", "note.recorded",
               "blocker.recorded", "blocker.resolved", "checkpoint.saved", "report.finalized", "run.finished",
               "journal.recovered"}
ALLOWED = {"PENDING": {"IN_PROGRESS", "BLOCKED"},
           "IN_PROGRESS": {"PRODUCED", "BLOCKED", "FAILED", "UNCERTAIN"},
           "BLOCKED": {"IN_PROGRESS", "FAILED"}, "FAILED": {"IN_PROGRESS"}, "UNCERTAIN": {"IN_PROGRESS", "PRODUCED"},
           "PRODUCED": {"IN_PROGRESS"}}
TEST_OUTCOMES = {"passed": "PASSED", "failed": "FAILED", "inconclusive": "INCONCLUSIVE"}
_STEP_ID = re.compile(r"[A-Z][A-Z0-9._-]{0,31}")
_SHA = re.compile(r"[0-9a-f]{64}")
IMPORTABLE_STATES = (RUN_EXEC_DISPATCHING, RUN_RUNNING, RUN_UNKNOWN)


class ArtifactProblem(Exception):
    """One fail-closed import refusal: ``code`` is stored as the Run's import problem; nothing earlier is erased."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code, self.detail = code, detail


class ReportUnavailable(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- confined reads ------------------------------------------------------------------------------------------------------
def confined_read(run_dir: Path, parts: tuple[str, ...], limit: int) -> bytes | None:
    """Bytes of ``run_dir/<parts>`` or ``None`` when absent.  Every component must be a real (non-symlink) entry owned by
    this user; the file must be regular and within ``limit``.  Raises ``ArtifactProblem`` otherwise."""
    current = Path(run_dir)
    try:
        info = os.lstat(current)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ArtifactProblem("unsafe_path", "run directory")
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return None
        last = index == len(parts) - 1
        if stat.S_ISLNK(info.st_mode) or (not last and not stat.S_ISDIR(info.st_mode)) \
                or (last and not stat.S_ISREG(info.st_mode)) or info.st_uid != os.getuid():
            raise ArtifactProblem("unsafe_path", "/".join(parts[:index + 1]))
    try:
        fd = os.open(current, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ArtifactProblem("unsafe_path", "/".join(parts)) from None
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ArtifactProblem("too_large", "/".join(parts))
    return data


def _json(data: bytes, name: str) -> dict:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ArtifactProblem("invalid_json", name) from None
    if not isinstance(value, dict):
        raise ArtifactProblem("invalid_json", name)
    return value


def expected_identity(row: ManagedRunState) -> dict:
    return {"run_id": row.run_id, "project_id": f"P{row.project_id:04d}", "task_id": f"T{row.task_id:04d}",
            "agent_id": row.agent_id,
            "incarnation_ref": hashlib.sha256(f"rosc.incarnation:{row.incarnation}".encode()).hexdigest()[:32],
            "manifest_sha256": row.manifest_sha256,
            "protocol_sha256": row.protocol_sha256}


def _identity_ok(item: dict, identity: dict) -> bool:
    return all(item.get(key) == value for key, value in identity.items())


# -- independent Journal fold (Core's own rules) -------------------------------------------------------------------------
class Fold:
    """Step implementation/test axes, tests, blockers and terminal facts from validated events, in sequence order."""

    def __init__(self, plans: dict[int, dict]) -> None:
        self.plans = plans
        self.revision = 0
        self.steps: dict[str, dict] = {}
        self.order: list[str] = []
        self.tests: dict[str, dict] = {}
        self.started_tests: dict[str, list[str]] = {}
        self.blockers: dict[str, dict] = {}
        self.report: dict | None = None
        self.finished: dict | None = None
        self.started = False
        self.last_hash: str | None = None

    def _steps_of(self, values: object) -> list[str]:
        if (not isinstance(values, list) or not values or len(values) > 300
                or any(not isinstance(v, str) or v not in self.steps for v in values) or len(set(values)) != len(values)):
            raise ArtifactProblem("step_ref_invalid", "test steps")
        return values

    def apply(self, event: dict) -> None:
        kind, data, step, ref = event["type"], event["data"], event.get("step_id"), event.get("plan_revision")
        if self.finished is not None:
            raise ArtifactProblem("event_after_finish", str(event["seq"]))
        if ref is not None and (not isinstance(ref, int) or isinstance(ref, bool) or ref < 1 or ref > self.revision):
            if not (kind == "plan.published" and ref == self.revision + 1):
                raise ArtifactProblem("plan_ref_invalid", str(event["seq"]))
        if kind == "run.started":
            self.started = True
        elif kind == "plan.published":
            revision = data.get("revision")
            plan = self.plans.get(revision) if isinstance(revision, int) else None
            if revision != self.revision + 1 or plan is None or data.get("sha256") != plan["sha256"] or ref != revision:
                raise ArtifactProblem("plan_ref_invalid", f"plan.published {revision}")
            old = self.steps
            self.steps = {s["id"]: old.get(s["id"], {"impl": "PENDING", "test": "NOT_TESTED", "tests": []})
                          for s in plan["body"]["steps"]}
            self.order = [s["id"] for s in plan["body"]["steps"]]
            self.revision = revision
        elif kind == "step.status":
            current = self.steps.get(step) if isinstance(step, str) else None
            target = data.get("to")
            if current is None or ref != self.revision:
                raise ArtifactProblem("step_ref_invalid", str(event["seq"]))
            if data.get("from") != current["impl"] or target not in ALLOWED.get(current["impl"], ()):
                raise ArtifactProblem("invalid_transition", f"{step} {current['impl']}->{target}")
            current["impl"] = target
            if data.get("test_axis") is not None:
                if data["test_axis"] != "STALE":
                    raise ArtifactProblem("invalid_transition", "test axis")
                current["test"] = "STALE"
        elif kind == "test.started":
            test_id = data.get("test_id")
            if not isinstance(test_id, str) or test_id in self.tests:
                raise ArtifactProblem("test_conflict", str(test_id)[:32])
            steps = self._steps_of(data.get("steps"))
            if self.started_tests.setdefault(test_id, steps) != steps:
                raise ArtifactProblem("test_conflict", test_id[:32])
            for sid in steps:
                self.steps[sid]["test"] = "TESTING"
        elif kind == "test.recorded":
            test_id = data.get("test_id")
            if not isinstance(test_id, str) or test_id in self.tests or data.get("outcome") not in TEST_OUTCOMES:
                raise ArtifactProblem("test_conflict", str(test_id)[:32])
            if not isinstance(data.get("command"), str) or not isinstance(data.get("exit_code"), int):
                raise ArtifactProblem("invalid_event", "test.recorded")
            steps = self._steps_of(data.get("steps"))
            if any(self.steps[sid]["impl"] == "PENDING" for sid in steps):
                raise ArtifactProblem("invalid_transition", "test of an unstarted Step")
            self.tests[test_id] = {**data, "seq": event["seq"], "at": event["at"]}
            for sid in steps:
                self.steps[sid]["test"] = TEST_OUTCOMES[data["outcome"]]
                self.steps[sid]["tests"].append(test_id)
        elif kind == "blocker.recorded":
            blocker = data.get("blocker_id")
            if not isinstance(blocker, str) or blocker in self.blockers or not isinstance(data.get("summary"), str):
                raise ArtifactProblem("blocker_conflict", str(blocker)[:32])
            if step is not None and step not in self.steps:
                raise ArtifactProblem("step_ref_invalid", str(event["seq"]))
            self.blockers[blocker] = {**data, "open": True, "seq": event["seq"], "at": event["at"]}
        elif kind == "blocker.resolved":
            blocker = self.blockers.get(data.get("blocker_id"))
            if blocker is None or not blocker["open"]:
                raise ArtifactProblem("blocker_conflict", "resolve")
            blocker["open"] = False
        elif kind == "checkpoint.saved":
            if data.get("cursor_seq") != event["seq"] - 1 or data.get("cursor_hash") != event.get("prev_hash"):
                raise ArtifactProblem("checkpoint_invalid", str(event["seq"]))
        elif kind == "report.finalized":
            if (self.report is not None or data.get("path") != REPORT_PATH or data.get("outcome") not in RUN_OUTCOMES
                    or not isinstance(data.get("sha256"), str) or not _SHA.fullmatch(data["sha256"])
                    or not isinstance(data.get("bytes"), int)):
                raise ArtifactProblem("report_invalid", str(event["seq"]))
            if data["outcome"] in ("completed", "partial") and not self.revision:
                raise ArtifactProblem("report_invalid", "completed without a Plan")
            self.report = {**data, "seq": event["seq"]}
        elif kind == "run.finished":
            if (self.report is None or data.get("outcome") != self.report["outcome"]
                    or data.get("report_sha256") != self.report["sha256"]):
                raise ArtifactProblem("terminal_invalid", "run.finished does not match the finalized Report")
            self.finished = {**data, "seq": event["seq"], "hash": event["hash"], "at": event["at"]}
        self.last_hash = event["hash"]

    def view(self) -> dict:
        steps = []
        for sid in self.order:
            plan_step = next((s for s in self.plans[self.revision]["body"]["steps"] if s["id"] == sid), {})
            state = self.steps[sid]
            steps.append({"id": sid, "title": plan_step.get("title"), "description": plan_step.get("description"),
                          "depends_on": plan_step.get("depends_on", []),
                          "expected_outcome": plan_step.get("expected_outcome"),
                          "test_intent": plan_step.get("test_intent"),
                          "implementation": state["impl"], "test": state["test"], "tests": list(state["tests"])})
        produced = sum(1 for s in steps if s["implementation"] == "PRODUCED")
        passed = sum(1 for s in steps if s["test"] == "PASSED")
        return {"plan_revision": self.revision, "steps": steps,
                "progress": {"total": len(steps), "produced": produced, "test_passed": passed,
                             "note": "PRODUCED counts Worker-reported implementation only; not VERIFIED"},
                "tests": [{"test_id": tid, "steps": t["steps"], "command": t["command"], "exit_code": t["exit_code"],
                           "outcome": t["outcome"], "limitations": t.get("limitations"), "at": t["at"],
                           "evidence": t.get("evidence"), "source": t.get("source")} for tid, t in self.tests.items()],
                "blockers": [{"blocker_id": bid, "summary": b["summary"], "step_id": b.get("step_id"),
                              "next_action": b.get("next_action"), "owner_decision": bool(b.get("owner_decision")),
                              "open": b["open"], "at": b["at"]} for bid, b in self.blockers.items()],
                "started": self.started}


def _event_shape(event: object, seq: int, previous: str | None, identity: dict) -> dict:
    if (not isinstance(event, dict) or event.get("schema") != EVENT_SCHEMA or event.get("seq") != seq
            or event.get("prev_hash") != previous or not _identity_ok(event, identity)
            or event.get("type") not in EVENT_TYPES or not isinstance(event.get("data"), dict)
            or not isinstance(event.get("at"), str) or len(event["at"]) > 32
            or not isinstance(event.get("event_id"), str) or len(event["event_id"]) > 32
            or (event.get("step_id") is not None and (not isinstance(event["step_id"], str)
                                                      or not _STEP_ID.fullmatch(event["step_id"])))):
        raise ArtifactProblem("journal_invalid", f"event {seq}")
    if event.get("hash") != _sha(canonical({k: v for k, v in event.items() if k != "hash"})):
        raise ArtifactProblem("journal_hash_mismatch", f"event {seq}")
    return event


class RunArtifactImporter:
    """Core-side import/projection.  ``root_for(session, project)`` returns the validated artifact root of a Project."""

    def __init__(self, session_factory: sessionmaker[Session], root_for: Callable[[Session, Project], Path], *,
                 handoff: Callable[..., tuple[str, str, str]] | None = None) -> None:
        self._sessions = session_factory
        self._root_for = root_for
        self._handoff = handoff

    def _run_dir(self, session: Session, row: ManagedRunState) -> Path:
        project = session.get(Project, row.project_id)
        return run_directory(self._root_for(session, project), row.project_id, row.task_id, row.run_id)

    # -- reading ------------------------------------------------------------------------------------------------------
    def _read(self, run_dir: Path, row: ManagedRunState) -> dict:
        identity = expected_identity(row)
        manifest_raw = confined_read(run_dir, ("package", "run.json"), MAX_MANIFEST)
        if manifest_raw is None or _sha(manifest_raw) != row.manifest_sha256:
            raise ArtifactProblem("package_mismatch", "run.json")
        manifest = _json(manifest_raw, "run.json")
        if (manifest.get("run_id") != row.run_id or not isinstance(manifest.get("artifacts"), dict)
                or manifest["artifacts"].get("format") != ARTIFACT_FORMAT):
            raise ArtifactProblem("unsupported_package", "no artifact contract")
        plans: dict[int, dict] = {}
        previous = None
        for revision in range(1, MAX_PLANS + 1):
            raw = confined_read(run_dir, ("plan", f"v{revision:03d}.json"), MAX_PLAN)
            if raw is None:
                break
            body = _json(raw, f"plan v{revision:03d}")
            steps = body.get("steps")
            if (body.get("schema") != PLAN_SCHEMA or body.get("revision") != revision
                    or body.get("previous") != previous or not _identity_ok(body, identity)
                    or not isinstance(steps, list) or not steps or len(steps) > 300
                    or any(not isinstance(s, dict) or not isinstance(s.get("id"), str)
                           or not _STEP_ID.fullmatch(s["id"]) or not isinstance(s.get("title"), str)
                           or not isinstance(s.get("depends_on", []), list) for s in steps)
                    or len({s["id"] for s in steps}) != len(steps)
                    or any(d not in {s["id"] for s in steps} for s in steps for d in s.get("depends_on", []))):
                raise ArtifactProblem("plan_invalid", f"v{revision:03d}")
            digest = _sha(raw)
            plans[revision] = {"sha256": digest, "body": body, "raw": raw}
            previous = {"revision": revision, "sha256": digest}
        journal_raw = confined_read(run_dir, ("journal", "events.jsonl"), MAX_JOURNAL) or b""
        events: list[dict] = []
        offset, torn, previous_hash = 0, 0, None
        while offset < len(journal_raw):
            end = journal_raw.find(b"\n", offset)
            if end < 0:
                torn = len(journal_raw) - offset  # interrupted append: the valid prefix is imported, the tail is kept
                break
            line = journal_raw[offset:end]
            if len(line) > MAX_LINE or len(events) >= MAX_EVENTS:
                raise ArtifactProblem("too_large", "journal")
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise ArtifactProblem("journal_invalid", f"line {len(events) + 1}") from None
            events.append(_event_shape(event, len(events) + 1, previous_hash, identity))
            previous_hash = event["hash"]
            offset = end + 1
        try:
            observed = datetime.fromtimestamp(os.lstat(run_dir / "journal" / "events.jsonl").st_mtime, timezone.utc)
        except OSError:
            observed = None
        return {"identity": identity, "plans": plans, "events": events, "torn": torn, "observed": observed}

    # -- import -------------------------------------------------------------------------------------------------------
    def import_run(self, run_id: str) -> dict:
        """One bounded import pass of one managed Run.  Idempotent; returns a small summary."""
        with self._sessions() as session:
            row = session.get(ManagedRunState, run_id)
            if row is None or row.exec_intent_at is None:
                return {"imported": 0, "state": row.state if row else None}
            try:
                run_dir = self._run_dir(session, row)
            except Exception:  # an unsafe/unresolvable artifact root is a recorded problem, never a crash
                return self._record_problem(run_id, "artifact_root_unavailable", "")
            stored = {e.seq: e.hash for e in session.scalars(select(ManagedRunJournalEvent).where(
                ManagedRunJournalEvent.run_id == run_id))}
            stored_plans = {p.revision: p.sha256 for p in session.scalars(select(ManagedRunPlanRevision).where(
                ManagedRunPlanRevision.run_id == run_id))}
            registered = session.get(ManagedRunReport, run_id)
            registered_sha = registered.sha256 if registered else None
        try:
            found = self._read(run_dir, row)
        except ArtifactProblem as exc:
            return self._record_problem(run_id, exc.code, exc.detail)
        plans, events = found["plans"], found["events"]
        # history already imported must be byte-for-byte the same claim: a rewrite is a conflict, never a replacement
        for revision, digest in stored_plans.items():
            if revision not in plans or plans[revision]["sha256"] != digest:
                return self._record_problem(run_id, "plan_history_changed", f"v{revision:03d}")
        for seq, digest in stored.items():
            if seq > len(events) or events[seq - 1]["hash"] != digest:
                return self._record_problem(run_id, "journal_history_changed", f"event {seq}")
        fold = Fold(plans)
        accepted: list[dict] = []
        problem: ArtifactProblem | None = None
        for event in events:
            try:
                if event["type"] == "report.finalized":  # the file must match BEFORE the claim enters the fold
                    report_bytes = confined_read(run_dir, ("reports", "final.md"), MAX_REPORT)
                    if report_bytes is None:
                        raise ArtifactProblem("report_missing", REPORT_PATH)
                    if (_sha(report_bytes) != event["data"].get("sha256")
                            or len(report_bytes) != event["data"].get("bytes")):
                        raise ArtifactProblem("report_tampered", REPORT_PATH)
                fold.apply(event)
            except ArtifactProblem as exc:
                problem = exc
                if event["seq"] in stored:  # an already imported event became invalid only by a changed reference
                    return self._record_problem(run_id, exc.code, exc.detail)
                break
            accepted.append(event)
        if registered_sha is not None:
            current = None
            try:
                current = confined_read(run_dir, ("reports", "final.md"), MAX_REPORT)
            except ArtifactProblem:
                pass
            if current is None or _sha(current) != registered_sha:
                problem = problem or ArtifactProblem("report_integrity_failed", REPORT_PATH)
        terminal = None
        if fold.finished is not None and problem is None and len(accepted) == len(events):
            try:
                terminal = self._terminal(run_dir, found["identity"], fold)
            except ArtifactProblem as exc:
                problem = exc
        if problem is None and found["torn"]:
            problem = ArtifactProblem("journal_torn_tail", f"{found['torn']} bytes after event {len(events)}")
        checkpoint = self._checkpoint(run_dir, found["identity"], accepted)
        return self._commit(run_id, found, accepted, fold, problem, terminal, checkpoint, registered_sha)

    @staticmethod
    def _terminal(run_dir: Path, identity: dict, fold: Fold) -> dict:
        raw = confined_read(run_dir, ("terminal.json",), MAX_TERMINAL)
        if raw is None:
            raise ArtifactProblem("terminal_missing", "terminal.json")
        terminal = _json(raw, "terminal.json")
        finished, report = fold.finished, fold.report
        if (terminal.get("schema") != TERMINAL_SCHEMA or not _identity_ok(terminal, identity)
                or terminal.get("outcome") != report["outcome"]
                or terminal.get("report") != {"path": REPORT_PATH, "sha256": report["sha256"], "bytes": report["bytes"]}
                or terminal.get("journal") != {"last_seq": finished["seq"], "last_hash": finished["hash"]}):
            raise ArtifactProblem("terminal_invalid", "terminal.json does not match the Journal")
        tests = terminal.get("tests") if isinstance(terminal.get("tests"), dict) else {}
        return {"outcome": report["outcome"], "tests": {k: int(tests.get(k, 0)) for k in TEST_OUTCOMES
                                                        if isinstance(tests.get(k, 0), int)}}

    @staticmethod
    def _checkpoint(run_dir: Path, identity: dict, accepted: list[dict]) -> dict | None:
        try:
            raw = confined_read(run_dir, ("checkpoints", "latest.json"), MAX_CHECKPOINT)
        except ArtifactProblem:
            return {"valid": False, "problem": "unsafe_or_oversized"}
        if raw is None:
            return None
        try:
            body = _json(raw, "checkpoint")
        except ArtifactProblem:
            return {"valid": False, "problem": "invalid_json"}
        cursor = body.get("cursor") if isinstance(body.get("cursor"), dict) else {}
        seq = cursor.get("seq")
        valid = (body.get("schema") == CHECKPOINT_SCHEMA and _identity_ok(body, identity) and isinstance(seq, int)
                 and 1 <= seq <= len(accepted) and accepted[seq - 1]["hash"] == cursor.get("hash")
                 and any(e["type"] == "checkpoint.saved" and e["data"].get("sha256") == _sha(raw) for e in accepted))
        if not valid:
            return {"valid": False, "problem": "cursor_or_identity_mismatch"}
        return {"valid": True, "sha256": _sha(raw), "seq": seq,
                "summary": {"current_step": body.get("current_step"), "note": (body.get("note") or "")[:2000],
                            "open_blockers": body.get("open_blockers", [])[:50],
                            "uncertain": body.get("uncertain", [])[:50], "git": body.get("git"),
                            "saved_at": body.get("saved_at"), "plan": body.get("plan")}}

    def _record_problem(self, run_id: str, code: str, detail: str) -> dict:
        with self._sessions() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                imp = self._import_row(session, run_id)
                imp.problem, imp.problem_detail, imp.checked_at = code[:48], detail[:200], _now()
                session.commit()
            except (OperationalError, IntegrityError):
                session.rollback()
        return {"imported": 0, "problem": code}

    @staticmethod
    def _import_row(session: Session, run_id: str) -> ManagedRunImport:
        imp = session.get(ManagedRunImport, run_id)
        if imp is None:
            imp = ManagedRunImport(run_id=run_id, journal_seq=0, plan_revision=0, torn_tail_bytes=0)
            session.add(imp)
            session.flush()
        return imp

    def _commit(self, run_id: str, found: dict, accepted: list[dict], fold: Fold, problem: ArtifactProblem | None,
                terminal: dict | None, checkpoint: dict | None, registered_sha: str | None) -> dict:
        now = _now()
        with self._sessions() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                row = session.get(ManagedRunState, run_id)
                imp = self._import_row(session, run_id)
                have = set(session.scalars(select(ManagedRunJournalEvent.seq).where(
                    ManagedRunJournalEvent.run_id == run_id)))
                have_plans = set(session.scalars(select(ManagedRunPlanRevision.revision).where(
                    ManagedRunPlanRevision.run_id == run_id)))
                new_events = [e for e in accepted if e["seq"] not in have]
                for revision in range(1, fold.revision + 1):
                    if revision not in have_plans:
                        plan = found["plans"][revision]
                        session.add(ManagedRunPlanRevision(run_id=run_id, revision=revision, sha256=plan["sha256"],
                                                           body=plan["raw"].decode("utf-8"), imported_at=now))
                for event in new_events:
                    session.add(ManagedRunJournalEvent(
                        run_id=run_id, seq=event["seq"], event_id=event["event_id"], type=event["type"],
                        step_id=event.get("step_id"), plan_revision=event.get("plan_revision"), at=event["at"],
                        hash=event["hash"], data=canonical(event["data"]).decode("utf-8"), imported_at=now))
                if fold.report is not None and registered_sha is None:
                    session.add(ManagedRunReport(run_id=run_id, report_path=REPORT_PATH, sha256=fold.report["sha256"],
                                                 bytes=fold.report["bytes"], outcome=fold.report["outcome"],
                                                 journal_seq=fold.report["seq"], registered_at=now))
                session.flush()
                if accepted:
                    imp.journal_seq, imp.journal_hash = accepted[-1]["seq"], accepted[-1]["hash"]
                    imp.last_event_at = accepted[-1]["at"]
                imp.plan_revision = fold.revision
                imp.torn_tail_bytes = found["torn"]
                imp.observed_at = found["observed"]
                imp.checked_at = now
                if new_events:
                    imp.imported_at = now
                imp.problem = problem.code[:48] if problem else None
                imp.problem_detail = problem.detail[:200] if problem else None
                if checkpoint is not None and checkpoint.get("valid"):
                    imp.checkpoint_sha256, imp.checkpoint_seq = checkpoint["sha256"], checkpoint["seq"]
                    imp.checkpoint_body = json.dumps(checkpoint["summary"], ensure_ascii=False, sort_keys=True)[:8000]
                transitioned = None
                if row.state in IMPORTABLE_STATES and accepted:
                    if terminal is not None and problem is None:
                        transitioned = self._report(session, row, fold, terminal, now)
                    elif row.state in (RUN_EXEC_DISPATCHING, RUN_UNKNOWN):
                        self._move(session, row, RUN_RUNNING, "journal_observed", now)
                        transitioned = RUN_RUNNING
                session.commit()
            except (OperationalError, IntegrityError):
                session.rollback()
                return {"imported": 0, "problem": "busy"}
        return {"imported": len(new_events), "problem": problem.code if problem else None, "state": transitioned}

    @staticmethod
    def _move(session: Session, row: ManagedRunState, to_state: str, reason: str, now: datetime,
              evidence: dict | None = None, **fields) -> None:
        last = session.scalars(select(ManagedRunEvent.seq).where(ManagedRunEvent.run_id == row.run_id)
                               .order_by(ManagedRunEvent.seq.desc()).limit(1)).first() or 0
        previous = row.state
        row.state, row.reason, row.updated_at = to_state, reason[:48], now
        row.state_revision += 1
        for name, value in fields.items():
            setattr(row, name, value)
        session.add(ManagedRunEvent(event_id=str(uuid.uuid4()), run_id=row.run_id, seq=last + 1, from_state=previous,
                                    to_state=to_state, reason=reason[:48], actor="rosc-core:import", created_at=now,
                                    evidence=None if evidence is None else json.dumps(evidence, sort_keys=True)[:2000]))

    def _report(self, session: Session, row: ManagedRunState, fold: Fold, terminal: dict, now: datetime) -> str:
        """Closed Journal + registered Report + terminal record: ``reported`` + the ONE handoff, same transaction."""
        self._move(session, row, RUN_REPORTED, "worker_reported", now,
                   {"outcome": terminal["outcome"], "report_sha256": fold.report["sha256"],
                    "journal_seq": fold.finished["seq"]}, outcome=terminal["outcome"], reported_at=now)
        session.flush()
        if self._handoff is not None and session.get(ManagedRunHandoff, row.run_id) is None:
            tests = terminal["tests"]
            text = (f"Worker-отчёт по Run {row.run_id} (Task T{row.task_id:04d}): outcome={terminal['outcome']}. "
                    f"Тесты по Journal: passed {tests.get('passed', 0)}, failed {tests.get('failed', 0)}, "
                    f"inconclusive {tests.get('inconclusive', 0)}. Report: {REPORT_PATH} "
                    f"sha256={fold.report['sha256'][:16]}… (прочитать: rosc_report_read run_id={row.run_id}). "
                    f"Это заявление Worker, не VERIFIED и не ACCEPTED: нужен Architect Review.")
            run = session.get(RunRecord, row.run_id)
            message_id, conversation_id, addressed = self._handoff(
                session, project_id=row.project_id, task_id=row.task_id, agent_id=row.agent_id, body=text,
                client_key=f"rosc-run-handoff:{row.run_id}", owner_actor=(run.registered_by, run.registered_by_assurance))
            session.add(ManagedRunHandoff(run_id=row.run_id, message_id=message_id, conversation_id=conversation_id,
                                          addressed_to=addressed, created_at=now))
        return RUN_REPORTED

    # -- projection ---------------------------------------------------------------------------------------------------
    @staticmethod
    def _stored_fold(session: Session, run_id: str) -> tuple[Fold, list[ManagedRunJournalEvent]]:
        plans = {p.revision: {"sha256": p.sha256, "body": json.loads(p.body)} for p in session.scalars(
            select(ManagedRunPlanRevision).where(ManagedRunPlanRevision.run_id == run_id))}
        rows = list(session.scalars(select(ManagedRunJournalEvent).where(ManagedRunJournalEvent.run_id == run_id)
                                    .order_by(ManagedRunJournalEvent.seq)))
        fold = Fold(plans)
        previous = None
        for item in rows:
            fold.apply({"seq": item.seq, "type": item.type, "data": json.loads(item.data), "step_id": item.step_id,
                        "plan_revision": item.plan_revision, "at": item.at, "hash": item.hash, "prev_hash": previous})
            previous = item.hash
        return fold, rows

    def progress(self, session: Session, row: ManagedRunState) -> dict | None:
        """Projection of the imported facts (DB only; no file read).  ``None`` before any execution continuation."""
        if row.exec_intent_at is None:
            return None
        imp = session.get(ManagedRunImport, row.run_id)
        fold, rows = self._stored_fold(session, row.run_id)
        report = session.get(ManagedRunReport, row.run_id)
        handoff = session.get(ManagedRunHandoff, row.run_id)
        view = fold.view()
        checkpoint = None
        if imp is not None and imp.checkpoint_body:
            checkpoint = {"sha256": imp.checkpoint_sha256, "seq": imp.checkpoint_seq, **json.loads(imp.checkpoint_body)}
        view.update({
            "journal": {"events": len(rows), "last_event_at": imp.last_event_at if imp else None,
                        "last_seq": imp.journal_seq if imp else 0},
            "import": {"imported_at": _iso(imp.imported_at) if imp else None,
                       "checked_at": _iso(imp.checked_at) if imp else None,
                       "journal_observed_at": _iso(imp.observed_at) if imp else None,
                       "problem": imp.problem if imp else None, "problem_detail": imp.problem_detail if imp else None,
                       "torn_tail_bytes": imp.torn_tail_bytes if imp else 0},
            "checkpoint": checkpoint,
            "report": None if report is None else {
                "path": report.report_path, "sha256": report.sha256, "bytes": report.bytes, "outcome": report.outcome,
                "registered_at": _iso(report.registered_at),
                "note": "Digest and path identity verified at import; the content is the Worker's unverified claim"},
            "handoff": None if handoff is None else {"message_id": handoff.message_id,
                                                     "addressed_to": handoff.addressed_to,
                                                     "created_at": _iso(handoff.created_at)},
        })
        return view

    @staticmethod
    def journal_page(session: Session, run_id: str, after: int, limit: int) -> dict:
        rows = list(session.scalars(select(ManagedRunJournalEvent).where(
            ManagedRunJournalEvent.run_id == run_id, ManagedRunJournalEvent.seq > after)
            .order_by(ManagedRunJournalEvent.seq).limit(limit + 1)))
        page = rows[:limit]
        return {"run_id": run_id, "events": [{
            "seq": e.seq, "event_id": e.event_id, "type": e.type, "step_id": e.step_id,
            "plan_revision": e.plan_revision, "at": e.at, "data": json.loads(e.data),
            "imported_at": _iso(e.imported_at)} for e in page],
            "next_after": page[-1].seq if len(rows) > limit else None,
            "note": "Imported Worker Journal events: observed claims, not verification"}

    def read_report(self, session: Session, row: ManagedRunState, offset: int, limit: int) -> dict:
        """Bounded page of the REGISTERED report; re-verified against its registered digest on every read."""
        report = session.get(ManagedRunReport, row.run_id)
        if report is None:
            raise ReportUnavailable("report_not_registered")
        try:
            data = confined_read(self._run_dir(session, row), ("reports", "final.md"), MAX_REPORT)
        except (ArtifactProblem, WorkspaceConflict):
            raise ReportUnavailable("report_unavailable") from None
        if data is None:
            raise ReportUnavailable("report_unavailable")
        if _sha(data) != report.sha256 or len(data) != report.bytes:
            raise ReportUnavailable("report_integrity_failed")
        text = data.decode("utf-8", "replace")
        offset = max(0, min(offset, len(text)))
        limit = max(1, min(limit, MAX_REPORT_PAGE))
        chunk = text[offset:offset + limit]
        return {"run_id": row.run_id, "task_id": f"T{row.task_id:04d}", "project_id": f"P{row.project_id:04d}",
                "path": report.report_path, "sha256": report.sha256, "bytes": report.bytes, "outcome": report.outcome,
                "run_state": row.state, "status": "worker_reported",
                "offset": offset, "content": chunk, "chars": len(text),
                "next_offset": offset + len(chunk) if offset + len(chunk) < len(text) else None,
                "note": "Worker-produced Run Report (digest verified on read). Unverified claims: review, do not trust."}


# -- P055: read-only Architect Evidence access (no DB writes; Journal claims are compared, never trusted) ---------------
MAX_EVIDENCE_FILE = 1_048_576   # Core read/hash bound per evidence file (the Worker tool keeps <= 64 KiB per log)
MAX_EVIDENCE_SCAN = 2_000       # directory entries examined by one listing
MAX_EVIDENCE_PAGE = 65_536
DEFAULT_EVIDENCE_PAGE = 16_000
_EVIDENCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class EvidenceUnavailable(Exception):
    def __init__(self, code: str, message: str = "", **extra) -> None:
        super().__init__(code)
        self.code, self.message, self.extra = code, message or code.replace("_", " "), extra


def _evidence_claims(session: Session, run_id: str) -> dict[str, list[dict]]:
    """``evidence/<name>`` -> the imported ``test.recorded`` claims that reference it (Worker claims, not proof)."""
    claims: dict[str, list[dict]] = {}
    for event in session.scalars(select(ManagedRunJournalEvent).where(
            ManagedRunJournalEvent.run_id == run_id, ManagedRunJournalEvent.type == "test.recorded")
            .order_by(ManagedRunJournalEvent.seq)):
        data = json.loads(event.data)
        evidence = data.get("evidence")
        if not isinstance(evidence, dict) or not isinstance(evidence.get("path"), str):
            continue
        claims.setdefault(evidence["path"], []).append({
            "journal_seq": event.seq, "test_id": data.get("test_id"), "outcome": data.get("outcome"),
            "exit_code": data.get("exit_code"), "command": str(data.get("command", ""))[:300],
            "sha256": evidence.get("sha256"), "bytes": evidence.get("bytes"),
            "truncated": evidence.get("truncated"), "original_bytes": evidence.get("original_bytes")})
    return claims


def _integrity(claims: list[dict], sha: str | None, size: int | None) -> str:
    if not claims:
        return "unreferenced"
    if sha is None:
        return "not_hashed"
    ok = [c for c in claims if c.get("sha256") == sha and (c.get("bytes") in (None, size))]
    return "matches_journal" if len(ok) == len(claims) else "mismatch_journal"


def _open_real_dir(path: Path) -> int | None:
    """An owned, non-symlink directory fd, ``None`` when absent; ``EvidenceUnavailable`` when unsafe."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise EvidenceUnavailable("unsafe_path", "The Run evidence location is a symlink or not a directory")
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise EvidenceUnavailable("unsafe_path", "The Run evidence location cannot be opened safely") from None


def _read_evidence_file(dir_fd: int, name: str, limit: int) -> tuple[bytes, os.stat_result]:
    """Bytes of one regular evidence file below ``dir_fd`` (no symlink, special file or change while reading)."""
    for attempt in (0, 1):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
        except FileNotFoundError:
            raise EvidenceUnavailable("not_found", "Evidence file not found") from None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENXIO):
                raise EvidenceUnavailable("unsafe_path", "Evidence path is a symlink or special file") from None
            raise EvidenceUnavailable("not_accessible", "Evidence file is not accessible") from None
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise EvidenceUnavailable("not_regular_file", "Only regular evidence files can be read")
            if before.st_size > limit:
                raise EvidenceUnavailable("file_too_large", "Evidence file exceeds the read bound",
                                          bytes=before.st_size, limit=limit)
            data = os.read(fd, limit + 1)
            while len(data) <= limit:
                more = os.read(fd, limit + 1 - len(data))
                if not more:
                    break
                data += more
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if len(data) > limit:
            raise EvidenceUnavailable("file_too_large", "Evidence file exceeds the read bound", limit=limit)
        if (before.st_mtime_ns, before.st_size, before.st_ino) == (after.st_mtime_ns, after.st_size, after.st_ino) \
                and len(data) == after.st_size:
            return data, after
        if attempt == 1:
            break
    raise EvidenceUnavailable("changed_during_read", "The evidence file kept changing while it was read")


class RunEvidenceReader:
    """Bounded, read-only listing/reading of ``<run dir>/evidence/`` of one managed Run, compared with the imported
    Journal ``test.recorded`` claims.  It never writes (DB or files) and never claims independent verification."""

    NOTE = ("Worker-authored evidence (files written by the Worker's own tool). A digest match only shows the file "
            "is the one the Worker's Journal claims; it is not independent execution and not VERIFIED.")

    def __init__(self, run_dir_for: Callable[[Session, ManagedRunState], Path]) -> None:
        self._run_dir_for = run_dir_for

    def _dirs(self, session: Session, row: ManagedRunState) -> tuple[int | None, int | None]:
        try:
            run_dir = self._run_dir_for(session, row)
        except (WorkspaceConflict, OSError, ValueError):
            raise EvidenceUnavailable("run_directory_unavailable", "The Run directory is not available") from None
        run_fd = _open_real_dir(run_dir)
        if run_fd is None:
            return None, None
        try:
            try:
                info = os.lstat("evidence", dir_fd=run_fd)
            except FileNotFoundError:
                return run_fd, None
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise EvidenceUnavailable("unsafe_path", "evidence/ is a symlink or not a directory")
            try:
                ev_fd = os.open("evidence", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=run_fd)
            except OSError:
                raise EvidenceUnavailable("unsafe_path", "evidence/ cannot be opened safely") from None
            return run_fd, ev_fd
        except BaseException:
            os.close(run_fd)
            raise

    def list(self, session: Session, row: ManagedRunState, offset: int, limit: int) -> dict:
        claims = _evidence_claims(session, row.run_id)
        run_fd, ev_fd = self._dirs(session, row)
        items: dict[str, dict] = {}
        scanned, complete = 0, True
        try:
            if ev_fd is not None:
                for name in sorted(os.listdir(ev_fd)):
                    scanned += 1
                    if scanned > MAX_EVIDENCE_SCAN:
                        complete = False
                        break
                    path = f"evidence/{name}"
                    entry: dict = {"path": path, "name": name}
                    try:
                        info = os.lstat(name, dir_fd=ev_fd)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISLNK(info.st_mode):
                        entry.update(kind="symlink", status="refused_unsafe", sha256=None, bytes=None)
                    elif not stat.S_ISREG(info.st_mode):
                        entry.update(kind="directory" if stat.S_ISDIR(info.st_mode) else "special",
                                     status="not_regular_file", sha256=None, bytes=None)
                    elif not _EVIDENCE_NAME.match(name):
                        entry.update(kind="file", status="unreadable_name", sha256=None, bytes=info.st_size)
                    elif info.st_size > MAX_EVIDENCE_FILE:
                        entry.update(kind="file", status="too_large_to_hash", sha256=None, bytes=info.st_size)
                    else:
                        try:
                            data, after = _read_evidence_file(ev_fd, name, MAX_EVIDENCE_FILE)
                            entry.update(kind="file", status="present", sha256=_sha(data), bytes=len(data),
                                         hard_linked=after.st_nlink > 1)
                        except EvidenceUnavailable as exc:
                            entry.update(kind="file", status=exc.code, sha256=None, bytes=info.st_size)
                    refs = claims.get(path, [])
                    entry["journal_claims"] = refs
                    entry["integrity"] = _integrity(refs, entry.get("sha256"), entry.get("bytes"))
                    items[path] = entry
        finally:
            for fd in (ev_fd, run_fd):
                if fd is not None:
                    os.close(fd)
        for path, refs in claims.items():  # claimed by the Journal but not present (moved/deleted/never written)
            if path not in items and complete:
                items[path] = {"path": path, "name": path.split("/")[-1], "kind": None, "status": "missing",
                               "sha256": None, "bytes": None, "journal_claims": refs, "integrity": "missing"}
        ordered = [items[k] for k in sorted(items)]
        page = ordered[offset:offset + limit]
        return {"run_id": row.run_id, "project_id": f"P{row.project_id:04d}", "task_id": f"T{row.task_id:04d}",
                "run_state": row.state,
                "evidence_directory": "absent" if ev_fd is None else "present",
                "items": page, "total": len(ordered), "complete": complete,
                "next_offset": offset + len(page) if offset + len(page) < len(ordered) else None,
                "read_with": "rosc_evidence_read path=<path>", "note": self.NOTE}

    def read(self, session: Session, row: ManagedRunState, path: object, offset: int, limit: int) -> dict:
        if not isinstance(path, str) or "\x00" in path or "\\" in path or len(path) > 160:
            raise EvidenceUnavailable("invalid_path", "path is a Run-relative evidence path such as evidence/T01.log")
        parts = path.split("/")
        if len(parts) != 2 or parts[0] != "evidence" or not _EVIDENCE_NAME.match(parts[1]) or parts[1] in (".", ".."):
            raise EvidenceUnavailable("invalid_path", "path must be evidence/<file name> relative to the Run directory "
                                      "(no host paths, '..' or sub-directories)")
        claims = _evidence_claims(session, row.run_id).get(path, [])
        run_fd, ev_fd = self._dirs(session, row)
        try:
            if ev_fd is None:
                raise EvidenceUnavailable("not_found", "This Run has no evidence directory",
                                          claimed_by_journal=bool(claims))
            try:
                data, after = _read_evidence_file(ev_fd, parts[1], MAX_EVIDENCE_FILE)
            except EvidenceUnavailable as exc:
                if exc.code == "not_found":
                    exc.extra["claimed_by_journal"] = bool(claims)
                raise
        finally:
            for fd in (ev_fd, run_fd):
                if fd is not None:
                    os.close(fd)
        digest = _sha(data)
        integrity = _integrity(claims, digest, len(data))
        facts = {"run_id": row.run_id, "project_id": f"P{row.project_id:04d}", "task_id": f"T{row.task_id:04d}",
                 "path": path, "sha256": digest, "bytes": len(data), "hard_linked": after.st_nlink > 1,
                 "journal_claims": claims, "integrity": integrity}
        if b"\x00" in data:
            raise EvidenceUnavailable("not_text", "Binary evidence is not returned as text", **facts)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise EvidenceUnavailable("not_text", "Evidence is not valid UTF-8 text", **facts) from None
        offset = max(0, min(offset, len(text)))
        limit = max(1, min(limit, MAX_EVIDENCE_PAGE))
        chunk = text[offset:offset + limit]
        end = offset + len(chunk)
        return {**facts, "offset": offset, "content": chunk, "returned_chars": len(chunk), "total_chars": len(text),
                "next_offset": end if end < len(text) else None, "verified_by_rosc": False, "note": self.NOTE}


def holding(state: str) -> bool:
    return state in MANAGED_HOLDING_STATES
