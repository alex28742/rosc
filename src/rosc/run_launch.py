"""P054A: Owner-only formal Run launch, offline Run package, Run delivery (E0) and evidence-only reconciliation.

One canonical identity: a managed Run is a P013 ``RunRecord`` (``execution_origin='managed'``, namespaced source kind
``rosc.managed_run``, ``source_key`` = the launch idempotency key) plus exactly one ``ManagedRunState`` with the same
``run_id``, created in ONE ``BEGIN IMMEDIATE`` transaction.  The RunRecord carries no external claim or attribution
(``agent_link_status='unlinked'``, no ``reported_executor``); ``source_attribution_assurance='claimed'`` is the only
value the P013 schema admits and is not a statement about the Worker.  The authoritative Worker/package/incarnation
snapshot lives in ``ManagedRunState``.

Lifecycle (Core-trusted delivery facts only)::

    authorized -> dispatch_pending -> dispatching -> submitted -> acknowledged | declined
         \\______________/      \\__________________________/
        not_delivered            unknown (no proof in the ACK window, inconsistent evidence)

* Before the durable dispatch intent (``authorized``/``dispatch_pending``) nothing has reached the CLI: a failure is
  ``not_delivered`` (slot released) and an interrupted launch is resumed only by the exact same launch key.
* From ``dispatching`` on, delivery MAY have happened: nothing is ever re-sent, restarted, killed or re-created because
  of a timeout or a restart; only CLI-hook evidence moves the state (``reconcile``), and ``unknown`` keeps the slot.
* ``submitted`` = the CLI's own ``UserPromptSubmit`` hook saw the exact Run token.  ``acknowledged`` = a write-once
  receipt from the CLI's ``Stop`` hook with the correlated ``ROSC-RUN-ACK ... ACCEPT`` line from a process descending
  from the bound pane, plus native Read evidence of the package manifest and pinned protocol files.  It is the Worker's
  acceptance of the delivery, never progress, ``running``, Task verification or Owner acceptance (P054B / S2).

Status reads are pure; the CLI-side helper (``worker_ack.py``) never imports Core or opens ``rosc.db``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import logging

import yaml
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from . import authorization as authz
from .agent_registry import AgentNotFound, InvalidRegistryInput, canonical_agent_id
from .models import (ASSIGNMENT_ACTIVE, ATTRIBUTION_CLAIMED, LINK_UNLINKED, MANAGED_HOLDING_STATES, MANAGED_SOURCE_KIND,
                     ORIGIN_MANAGED, ROLE_WORKER, RUN_ACKNOWLEDGED, RUN_AUTHORIZED, RUN_DECLINED, RUN_DISPATCH_PENDING,
                     RUN_DISPATCHING, RUN_EXEC_DISPATCHING, RUN_NOT_DELIVERED, RUN_NOT_EXECUTED, RUN_REPORTED,
                     RUN_RUNNING, RUN_SUBMITTED, RUN_UNKNOWN, AgentDefinition, ExecutionPackage, ManagedRunEvent,
                     ManagedRunState, Project, ProjectIdentityError, PromptApproval, PromptRevision, RoleAssignment,
                     RunRecord, Task, TaskSpecRevision, WorkerSession, validate_project_identity)
from .run_artifacts import EvidenceUnavailable, ReportUnavailable, RunArtifactImporter, RunEvidenceReader
from .run_review import review_state
from .task_lifecycle import activate_task
from .models import TASK_COMPLETED
from .task_authoring import AuthoringError, TaskAuthoringService, prompt_digest, spec_digest
from .worker_session import B_CONFIRMED, STATE_ACTIVE, STATE_NOT_ASSIGNED, SessionError, WorkerSessionService
from .workspace import (READY, WorkspaceConflict, check_run_parents, create_run_directory, inspect_project, inspect_task,
                        run_directory)

log = logging.getLogger(__name__)
DEFAULT_ACK_TIMEOUT = 300
MIN_ACK_TIMEOUT, MAX_ACK_TIMEOUT = 30, 3600
SUBMIT_WAIT_SECONDS = 4.0
MAX_PACKAGE_BYTES = 1_048_576
MAX_EVIDENCE_BYTES = 65_536
MAX_RULES_BYTES = 65_536
RUN_MANIFEST_FORMAT = "rosc.run.v1"
WORKER_RULESET = "agents/workers/ruleset.yaml"
PACKAGE_DIRNAME = "package"
MANIFEST_NAME = "run.json"
# Package files a Worker must natively Read before an E0 ACCEPT counts (P054A r02: the task inputs, not just the rules).
E0_REQUIRED_READS = (MANIFEST_NAME, "worker-role.md", "worker-execution-protocol.md", "spec.md", "prompt.md")
E0_ONLY = ("E0",)
FULL_STAGES = ("E0", "E1", "E2", "E3", "E4", "E5", "E6")   # P054B: operative only with the pinned Artifact Tool
ARTIFACT_TOOL_ID = "artifact-tool"
ARTIFACT_TOOL_FILE = "rosc-run-tool.py"
ARTIFACT_FORMAT = "rosc.run-artifacts.v1"
ARTIFACT_LAYOUT = {"format": ARTIFACT_FORMAT, "plan_dir": "plan", "journal": "journal/events.jsonl",
                   "checkpoint": "checkpoints/latest.json", "evidence_dir": "evidence", "report": "reports/final.md",
                   "terminal": "terminal.json"}
EXEC_MARKER = "ROSC run-exec"
MONITOR_BATCH = 50
_LAUNCH_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,95}", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)
_ROLE_FILE = re.compile(r"[a-z][a-z0-9-]{0,40}", re.ASCII)


class RunLaunchError(Exception):
    code, http = "run_error", 503

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class RunLaunchDenied(RunLaunchError):
    code, http = "forbidden", 403


class RunLaunchNotFound(RunLaunchError):
    code, http = "not_found", 404


class RunLaunchInvalid(RunLaunchError):
    code, http = "invalid", 422


class RunLaunchConflict(RunLaunchError):
    """``code``: launch_key_conflict, run_in_progress, prompt_not_approved, prompt_stale, digest_mismatch,
    binding_changed, workspace_not_ready, worker_not_assigned, worker_not_active, bootstrap_not_confirmed,
    worker_changed, artifact_root_unsafe."""

    code, http = "conflict", 409


class RunLaunchUnavailable(RunLaunchError):
    code, http = "unavailable", 503


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _aware(value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# -- pinned Worker protocol ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class WorkerProtocol:
    ruleset_id: str
    version: str
    sha256: str                                   # composed digest of version + ordered (id, sha256) pairs
    operative_stages: tuple[str, ...]
    files: tuple[tuple[str, str, bytes], ...]     # (id, package file name, exact bytes)
    tools: tuple[tuple[str, str, str, bytes], ...] = ()  # (id, package file name, version, exact bytes); P054B

    @property
    def executable(self) -> bool:
        """E1-E6 are operative (and backed by the pinned Artifact Tool) for packages built from this protocol."""
        return self.operative_stages == FULL_STAGES and any(t[0] == ARTIFACT_TOOL_ID for t in self.tools)


def load_worker_protocol(library_root: Path) -> WorkerProtocol:
    """Load ``global/agents/workers/ruleset.yaml`` and verify every listed file (exact bytes + sha256), fail closed."""
    root = Path(library_root)
    try:
        manifest = root / WORKER_RULESET
        if manifest.is_symlink() or manifest.stat().st_size > MAX_RULES_BYTES:
            raise ValueError("ruleset")
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("format") != "rosc.worker.ruleset.v1"
                or not isinstance(data.get("version"), str) or not isinstance(data.get("ruleset_id"), str)
                or not isinstance(data.get("files"), list) or not data["files"]):
            raise ValueError("ruleset")
        stages = data.get("operative_stages")
        # E0 alone (P054A) or the full E0-E6 that P054B implements (requires the pinned Artifact Tool below);
        # any other set would overclaim what ROSC supports.
        if not isinstance(stages, list) or tuple(stages) not in (E0_ONLY, FULL_STAGES):
            raise ValueError("stages")
        base = (root / "agents" / "workers").resolve()
        files, pairs = [], []
        for item in data["files"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not _ROLE_FILE.fullmatch(item["id"]):
                raise ValueError("item")
            path = root / str(item.get("path", ""))
            if path.is_symlink() or not path.resolve().is_relative_to(base):
                raise ValueError("path")
            raw = path.read_bytes()
            if (len(raw) > MAX_RULES_BYTES or len(raw) != item.get("bytes")
                    or hashlib.sha256(raw).hexdigest() != item.get("sha256")):
                raise ValueError("digest")
            name = "worker-role.md" if item["id"] == "role" else f"worker-{item['id']}.md"
            files.append((item["id"], name, raw))
            pairs.append([item["id"], item["sha256"]])
        tools, tool_pairs = [], []
        for item in data.get("tools") or []:
            if (not isinstance(item, dict) or item.get("id") != ARTIFACT_TOOL_ID
                    or item.get("package_file") != ARTIFACT_TOOL_FILE or not isinstance(item.get("version"), str)):
                raise ValueError("tool")
            path = root / str(item.get("path", ""))
            if path.is_symlink() or not path.resolve().is_relative_to(base):
                raise ValueError("path")
            raw = path.read_bytes()
            if (len(raw) > MAX_PACKAGE_BYTES // 2 or len(raw) != item.get("bytes")
                    or hashlib.sha256(raw).hexdigest() != item.get("sha256")):
                raise ValueError("digest")
            tools.append((item["id"], ARTIFACT_TOOL_FILE, item["version"][:16], raw))
            tool_pairs.append([item["id"], item["version"], item["sha256"]])
        if tuple(stages) == FULL_STAGES and len(tools) != 1:
            raise ValueError("tool")  # E1-E6 are never operative without the pinned writer
        if tuple(stages) == E0_ONLY and tools:
            raise ValueError("tool")
    except (OSError, ValueError, yaml.YAMLError, UnicodeDecodeError):
        raise RunLaunchUnavailable("The pinned Worker protocol is missing or does not match its digests",
                                   "protocol_unavailable") from None
    names = [name for _id, name, _raw in files]
    if len(set(names)) != len(names) or "worker-execution-protocol.md" not in names or "worker-role.md" not in names:
        raise RunLaunchUnavailable("The pinned Worker protocol is incomplete", "protocol_unavailable")
    material = [data["ruleset_id"], data["version"], pairs] + ([tool_pairs] if tool_pairs else [])
    composed = hashlib.sha256(json.dumps(material).encode()).hexdigest()
    return WorkerProtocol(data["ruleset_id"], data["version"][:32], composed, tuple(stages), tuple(files), tuple(tools))


# -- artifact roots ------------------------------------------------------------------------------------------------------
def validate_artifact_root(path: object, *, project_roots: list[str], private_paths: tuple[Path, ...] = ()) -> Path:
    """An explicit per-Project artifact root: absolute, an existing real directory owned by this user, not group/world
    writable, and disjoint from every registered Project source root and from ROSC private paths."""
    if not isinstance(path, (str, Path)) or not Path(path).is_absolute():
        raise RunLaunchConflict("Artifact root must be an absolute path", "artifact_root_unsafe")
    candidate = Path(path)
    try:
        info = os.lstat(candidate)
    except OSError:
        raise RunLaunchConflict("Artifact root does not exist", "artifact_root_unsafe") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise RunLaunchConflict("Artifact root must be a private directory of this user", "artifact_root_unsafe")
    resolved = candidate.resolve()
    if resolved != candidate.absolute():
        raise RunLaunchConflict("Artifact root must not traverse a symlink", "artifact_root_unsafe")
    for other in [*(Path(r).resolve() for r in project_roots), *(Path(p).resolve() for p in private_paths)]:
        if resolved == other or resolved.is_relative_to(other) or other.is_relative_to(resolved):
            raise RunLaunchConflict("Artifact root overlaps a Project source root or ROSC private data",
                                    "artifact_root_unsafe")
    return resolved


# -- package files -------------------------------------------------------------------------------------------------------
def _write_exclusive(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o400)


def _read_nofollow(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise OSError("file too large")
    return data


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _verify_package(package_dir: Path, expected: dict[str, bytes]) -> bool:
    """True when the published package holds exactly the expected files with exactly the expected bytes."""
    try:
        if package_dir.is_symlink() or not package_dir.is_dir():
            return False
        if sorted(entry.name for entry in package_dir.iterdir()) != sorted(expected):
            return False
        return all(_read_nofollow(package_dir / name, MAX_PACKAGE_BYTES) == data for name, data in expected.items())
    except OSError:
        return False


def incarnation_ref(incarnation: str) -> str:
    """Stable, non-reversible correlation of the CLI incarnation for Run artifacts (the raw hook nonce never leaves
    ROSC's private state)."""
    return hashlib.sha256(f"rosc.incarnation:{incarnation}".encode()).hexdigest()[:32]


def verify_published_package(package_dir: Path, manifest_sha256: str | None, protocol_sha256: str) -> dict | None:
    """The published package's own ``run.json`` (digest == the recorded manifest digest, pinned protocol digest) when
    every listed file is present with exactly its recorded bytes and nothing else is there; ``None`` otherwise.

    It never consults the live ``global/`` rules: a package published under an older ruleset stays verifiable after
    the ruleset changed, and it is never re-derived, rewritten or upgraded."""
    try:
        if package_dir.is_symlink() or not package_dir.is_dir() or manifest_sha256 is None:
            return None
        raw = _read_nofollow(package_dir / MANIFEST_NAME, MAX_PACKAGE_BYTES)
        if hashlib.sha256(raw).hexdigest() != manifest_sha256:
            return None
        manifest = json.loads(raw.decode("utf-8"))
        contents = manifest.get("contents") if isinstance(manifest, dict) else None
        if (not isinstance(contents, list) or manifest.get("protocol", {}).get("sha256") != protocol_sha256):
            return None
        listed = {item.get("file"): item for item in contents if isinstance(item, dict)}
        if sorted(entry.name for entry in package_dir.iterdir()) != sorted([*listed, MANIFEST_NAME]):
            return None
        for name, item in listed.items():
            if not isinstance(name, str) or "/" in name or name == MANIFEST_NAME:
                return None
            data = _read_nofollow(package_dir / name, MAX_PACKAGE_BYTES)
            if hashlib.sha256(data).hexdigest() != item.get("sha256") or len(data) != item.get("bytes"):
                return None
        return manifest
    except (OSError, ValueError, UnicodeDecodeError, AttributeError):
        return None


class RunLaunchService:
    """Owner-only formal Run start/status/reconcile over the P041 package, the P042 Worker CLI and the P013 Run identity."""

    def __init__(self, session_factory: sessionmaker[Session], policy: authz.AuthorizationPolicy, workspace_root: Path,
                 workers: WorkerSessionService, authoring: TaskAuthoringService, library_root: Path, *,
                 artifact_roots: Mapping[int, Path] | None = None, private_paths: tuple[Path, ...] = (),
                 clock=time.time, submit_wait: float = SUBMIT_WAIT_SECONDS, handoff=None) -> None:
        self._sessions = session_factory
        self._policy = policy
        self._workspace = Path(workspace_root)
        self._workers = workers
        self._authoring = authoring
        self._library = Path(library_root)
        # Python-level composition seam: an explicit, validated per-Project artifact root with the SAME hierarchy as the
        # Workspace.  No request can select or configure it.
        self._artifact_roots = dict(artifact_roots or {})
        self._private = tuple(private_paths)
        self._clock = clock
        self._submit_wait = submit_wait
        self._locks: dict[tuple[int, str], threading.Lock] = {}
        self._guard = threading.Lock()
        self._advance_lock = threading.Lock()  # one monitor/reconcile pass at a time (in-process; DB guards the rest)
        # P054B: Core-side reader of the Worker's local Run artifacts; ``handoff`` is the P052 seam that stores the ONE
        # Worker -> Architect Task Conversation message atomically with the Run's ``reported`` transition.
        if handoff is None:
            from .worker_conversation import WorkerConversationCore  # the P052 attribution/envelope layer
            handoff = WorkerConversationCore(session_factory, policy).post_run_handoff
        self.artifacts = RunArtifactImporter(session_factory, self._artifact_root, handoff=handoff)
        # P055: read-only Architect Evidence access over the same validated Run directory (never writes anything).
        self.evidence = RunEvidenceReader(self.artifacts._run_dir)  # noqa: SLF001 - same validated root resolution

    # -- guards ---------------------------------------------------------------------------------------------------------
    def _authorize(self, actor: object, action: str, **scope) -> None:
        if not authz.evaluate(self._policy, actor, action, authz.ResourceScope(**scope)).allowed:
            raise RunLaunchDenied("Not authorized for this operation")

    def _lock(self, key: tuple[int, str]) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    @staticmethod
    def _project(session: Session, project_id: int) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise RunLaunchNotFound("Project not found")
        try:
            validate_project_identity(project)
        except ProjectIdentityError:
            raise RunLaunchUnavailable("Project identity records are inconsistent", "project_unavailable") from None
        return project

    def _artifact_root(self, session: Session, project: Project) -> Path:
        custom = self._artifact_roots.get(project.id)
        if custom is None:
            return self._workspace
        try:
            roots = [validate_project_identity(p).root_path for p in session.scalars(select(Project))]
        except ProjectIdentityError:
            raise RunLaunchUnavailable("Project identity records are inconsistent", "project_unavailable") from None
        return validate_artifact_root(custom, project_roots=roots, private_paths=self._private)

    # -- launch ---------------------------------------------------------------------------------------------------------
    def launch(self, actor: object, project_id: int, task_id: int, agent_id: object, *, launch_key: object,
               ack_timeout_seconds: object = None) -> dict:
        """Explicit Owner start of ONE formal Run.  Idempotent per launch key; never a second writer."""
        try:
            agent_key = canonical_agent_id(agent_id)
        except InvalidRegistryInput:
            raise RunLaunchInvalid("agent_id must be a canonical UUIDv4 string") from None
        self._authorize(actor, authz.RUN_LAUNCH, project_id=project_id, task_id=task_id, agent_id=agent_key)
        if not isinstance(launch_key, str) or not _LAUNCH_KEY.fullmatch(launch_key):
            raise RunLaunchInvalid("launch_key must be 8-96 characters of letters, digits, '.', '_', ':' or '-'")
        timeout = DEFAULT_ACK_TIMEOUT if ack_timeout_seconds is None else ack_timeout_seconds
        if (not isinstance(timeout, int) or isinstance(timeout, bool)
                or not MIN_ACK_TIMEOUT <= timeout <= MAX_ACK_TIMEOUT):
            raise RunLaunchInvalid(f"ack_timeout_seconds must be an integer between {MIN_ACK_TIMEOUT} and {MAX_ACK_TIMEOUT}")
        with self._lock((project_id, agent_key)):
            with self._sessions() as session:
                existing = self._by_key(session, project_id, launch_key)
                if existing is not None:
                    if (existing.task_id, existing.agent_id) != (task_id, agent_key):
                        raise RunLaunchConflict("This launch key belongs to another Run", "launch_key_conflict")
                    run_id, state = existing.run_id, existing.state
                else:
                    run_id = state = None
            if run_id is not None:
                if state in (RUN_AUTHORIZED, RUN_DISPATCH_PENDING):
                    return self._continue(run_id, created=False)  # interrupted before any CLI side effect
                return self._view_by_id(project_id, run_id, created=False)
            context = self._preflight(project_id, task_id, agent_key)
            try:
                package = self._authoring.build_execution_package(project_id, task_id)
            except AuthoringError as exc:
                code = {"not_approved": "prompt_not_approved"}.get(exc.code, exc.code)
                if exc.http == 404:
                    raise RunLaunchConflict("No approved Prompt revision exists for this Task", "prompt_not_approved") from None
                raise RunLaunchConflict(str(exc), code) from None
            run_id, created = self._create(actor, context, package["package_id"], launch_key, timeout)
            if not created:
                return self._view_by_id(project_id, run_id, created=False)
            return self._continue(run_id, created=True)

    @staticmethod
    def _by_key(session: Session, project_id: int, key: str) -> ManagedRunState | None:
        run = session.scalars(select(RunRecord).where(
            RunRecord.project_id == project_id, RunRecord.source_kind == MANAGED_SOURCE_KIND,
            RunRecord.source_key == key)).first()
        return None if run is None else session.get(ManagedRunState, run.run_id)

    def _preflight(self, project_id: int, task_id: int, agent_id: str) -> dict:
        """Read-only gate: nothing is written and nothing is sent when any input is missing or invalid."""
        protocol = load_worker_protocol(self._library)
        with self._sessions() as session:
            project = self._project(session, project_id)
            binding = validate_project_identity(project)
            task = session.get(Task, task_id)
            if task is None or task.project_id != project_id:
                raise RunLaunchNotFound("Task not found in this Project")
            if task.status == TASK_COMPLETED:
                raise RunLaunchConflict("The Task is Completed: no new Run is started", "task_completed")
            if (inspect_project(self._workspace, project).problem != READY
                    or inspect_task(self._workspace, task).problem != READY):
                raise RunLaunchConflict("Project/Task identity manifests are not ready", "workspace_not_ready")
            root = self._artifact_root(session, project)
            try:
                check_run_parents(root, project_id, task_id)
            except WorkspaceConflict:
                raise RunLaunchConflict("The Run directory location is unsafe", "artifact_root_unsafe") from None
            try:
                obs, row = self._workers.run_target(session, project, agent_id)
            except AgentNotFound:
                raise RunLaunchNotFound("Agent not found") from None
            if obs.state == STATE_NOT_ASSIGNED:
                raise RunLaunchConflict("The Agent has no active Worker assignment in this Project",
                                        "worker_not_assigned")
            holding = self._holding(session, project_id, task_id, agent_id)
            if holding is not None:
                raise RunLaunchConflict(f"Run {holding.run_id} of this Task or Worker is still holding "
                                        f"({holding.state}); no second writer is started", "run_in_progress")
            if row is None or obs.state != STATE_ACTIVE:
                raise RunLaunchConflict(f"The Worker CLI is not active ({obs.reason})", "worker_not_active")
            if not obs.bootstrap or obs.bootstrap.get("state") != B_CONFIRMED:
                raise RunLaunchConflict("The Worker project bootstrap is not confirmed for the current Root Binding and "
                                        "CLI incarnation", "bootstrap_not_confirmed")
            return {"project_id": project_id, "task_id": task_id, "agent_id": agent_id,
                    "binding_version": binding.version, "session_id": row.session_id, "incarnation": row.incarnation,
                    "pane_pid": row.pane_pid, "proc_start": row.proc_start, "protocol": protocol}

    def _create(self, actor: authz.ActorContext, ctx: dict, package_id: int, key: str, timeout: int) -> tuple[str, bool]:
        """ONE transaction: re-verify every frozen input, then create the RunRecord + managed state + first event."""
        protocol: WorkerProtocol = ctx["protocol"]
        pid, tid, agent_id = ctx["project_id"], ctx["task_id"], ctx["agent_id"]
        with self._sessions() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                existing = self._by_key(session, pid, key)
                if existing is not None:  # a concurrent launch with the same key won the race
                    session.rollback()
                    if (existing.task_id, existing.agent_id) != (tid, agent_id):
                        raise RunLaunchConflict("This launch key belongs to another Run", "launch_key_conflict")
                    return existing.run_id, False
                project = self._project(session, pid)
                binding = validate_project_identity(project)
                if binding.version != ctx["binding_version"]:
                    raise RunLaunchConflict("The Root Binding changed during launch", "binding_changed")
                package, approval, spec, prompt = self._frozen_inputs(session, pid, tid, package_id)
                if package.root_binding_version != binding.version:
                    raise RunLaunchConflict("The approved Execution Package was built for another Root Binding version",
                                            "binding_changed")
                assignment = session.scalars(select(RoleAssignment).where(
                    RoleAssignment.project_id == pid, RoleAssignment.agent_id == agent_id,
                    RoleAssignment.role == ROLE_WORKER, RoleAssignment.state == ASSIGNMENT_ACTIVE,
                    RoleAssignment.task_id.is_(None))).first()
                row = session.get(WorkerSession, ctx["session_id"])
                if (assignment is None or row is None or row.project_id != pid or row.agent_id != agent_id
                        or row.incarnation != ctx["incarnation"] or row.pane_pid != ctx["pane_pid"]):
                    raise RunLaunchConflict("The Worker CLI binding changed during launch", "worker_changed")
                holding = self._holding(session, pid, tid, agent_id)
                if holding is not None:
                    raise RunLaunchConflict(f"Run {holding.run_id} of this Task or Worker is still holding "
                                            f"({holding.state}); no second writer is started", "run_in_progress")
                now = _now()
                run_id = str(uuid.uuid4())
                session.add(RunRecord(run_id=run_id, project_id=pid, task_id=tid, execution_origin=ORIGIN_MANAGED,
                                      source_kind=MANAGED_SOURCE_KIND, source_key=key,
                                      source_attribution_assurance=ATTRIBUTION_CLAIMED, reported_executor=None,
                                      agent_id=None, assignment_id=None, agent_link_status=LINK_UNLINKED,
                                      attribution_revision=1, registered_by=actor.actor_id,
                                      registered_by_assurance=actor.assurance, created_at=now))
                session.flush()
                session.add(ManagedRunState(
                    run_id=run_id, project_id=pid, task_id=tid, package_id=package.id, prompt_approval_id=approval.id,
                    spec_revision=spec.revision, spec_digest=spec.digest, prompt_revision=prompt.revision,
                    prompt_digest=prompt.digest, root_binding_version=binding.version, agent_id=agent_id,
                    worker_session_id=row.session_id, incarnation=row.incarnation, pane_pid=row.pane_pid,
                    proc_start=row.proc_start, protocol_version=protocol.version, protocol_sha256=protocol.sha256,
                    launch_key=key, run_token=str(uuid.uuid4()), ack_timeout_seconds=timeout, state=RUN_AUTHORIZED,
                    reason="owner_launch", manifest_sha256=None, state_revision=1, created_at=now, updated_at=now))
                session.flush()
                self._event(session, run_id, None, RUN_AUTHORIZED, "owner_launch", actor.actor_id, now)
                activate_task(session, tid)  # P056: the Owner's formal Run launch is Task collaboration
                session.commit()
                return run_id, True
            except OperationalError:
                session.rollback()
                raise RunLaunchUnavailable("Another change is in progress; retry with the same launch key", "busy") from None
            except IntegrityError:
                session.rollback()
                raise RunLaunchConflict("A Run of this Task or Worker is still holding; no second writer is started",
                                        "run_in_progress") from None
            except RunLaunchError:
                session.rollback()
                raise

    @staticmethod
    def _holding(session: Session, pid: int, tid: int, agent_id: str) -> ManagedRunState | None:
        """A holding managed Run of the same Task, or of the same Worker in this Project (no Project-wide lock)."""
        return session.scalars(select(ManagedRunState).where(
            ManagedRunState.state.in_(MANAGED_HOLDING_STATES),
            or_(ManagedRunState.task_id == tid,
                (ManagedRunState.project_id == pid) & (ManagedRunState.agent_id == agent_id)))).first()

    @staticmethod
    def _frozen_inputs(session: Session, pid: int, tid: int, package_id: int):
        task = session.get(Task, tid)
        if task is None or task.project_id != pid:
            raise RunLaunchNotFound("Task not found in this Project")
        if task.status == TASK_COMPLETED:
            raise RunLaunchConflict("The Task is Completed: no new Run is started", "task_completed")
        spec_head = session.scalars(select(TaskSpecRevision).where(TaskSpecRevision.task_id == tid)
                                    .order_by(TaskSpecRevision.revision.desc()).limit(1)).first()
        prompt = session.scalars(select(PromptRevision).where(PromptRevision.task_id == tid)
                                 .order_by(PromptRevision.revision.desc()).limit(1)).first()
        if spec_head is None or prompt is None:
            raise RunLaunchConflict("No approved Prompt revision exists for this Task", "prompt_not_approved")
        if prompt.source_spec_revision != spec_head.revision:
            raise RunLaunchConflict("The Spec changed after the approved Prompt", "prompt_stale")
        approval = session.scalars(select(PromptApproval).where(PromptApproval.prompt_revision_id == prompt.id)).first()
        if approval is None:
            raise RunLaunchConflict("The current Prompt revision is not approved by the Owner", "prompt_not_approved")
        package = session.get(ExecutionPackage, package_id)
        if (package is None or package.prompt_approval_id != approval.id or package.project_id != pid
                or package.task_id != tid):
            raise RunLaunchConflict("The Execution Package is not the one of the current approval", "prompt_stale")
        if (approval.digest != prompt.digest or approval.prompt_revision != prompt.revision
                or approval.spec_revision != spec_head.revision
                or prompt.digest != prompt_digest(prompt.source_spec_revision, prompt.summary, prompt.body)
                or spec_head.digest != spec_digest(spec_head.title, spec_head.scope, spec_head.description_html,
                                                   spec_head.criteria_html)):
            raise RunLaunchConflict("Stored revisions do not match the approved digests", "digest_mismatch")
        return package, approval, spec_head, prompt

    @staticmethod
    def _event(session: Session, run_id: str, from_state: str | None, to_state: str, reason: str, actor: str,
               when: datetime, evidence: dict | None = None) -> None:
        last = session.scalars(select(ManagedRunEvent.seq).where(ManagedRunEvent.run_id == run_id)
                               .order_by(ManagedRunEvent.seq.desc()).limit(1)).first() or 0
        session.add(ManagedRunEvent(event_id=str(uuid.uuid4()), run_id=run_id, seq=last + 1, from_state=from_state,
                                    to_state=to_state, reason=reason[:48], actor=actor[:64], created_at=when,
                                    evidence=None if evidence is None else json.dumps(evidence, sort_keys=True)[:2000]))

    def _transition(self, run_id: str, expected: tuple[str, ...], to_state: str, reason: str, *, actor: str = "rosc-core",
                    evidence: dict | None = None, **fields) -> bool:
        """Guarded state change + its audit event in one transaction.  False when the state moved meanwhile."""
        with self._sessions() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                row = session.get(ManagedRunState, run_id)
                if row is None or row.state not in expected:
                    session.rollback()
                    return False
                now = _now()
                previous = row.state
                row.state, row.reason, row.updated_at = to_state, reason[:48], now
                row.state_revision += 1
                for name, value in fields.items():
                    setattr(row, name, now if value == "now" else value)
                self._event(session, run_id, previous, to_state, reason, actor, now, evidence)
                session.commit()
                return True
            except (OperationalError, IntegrityError):
                session.rollback()
                raise RunLaunchUnavailable("The Run state could not be recorded; retry with the same launch key",
                                           "busy") from None

    # -- package + dispatch ---------------------------------------------------------------------------------------------
    def _package_files(self, row: ManagedRunState, protocol: WorkerProtocol) -> tuple[dict[str, bytes], Path]:
        """Exact bytes of the offline package (deterministic: an interrupted publish of the same Run re-creates them)."""
        view = self._authoring.execution_package_view(row.project_id, row.task_id, row.package_id)
        spec, prompt = view["spec"], view["prompt"]
        if (spec["revision"], spec["digest"], prompt["revision"], prompt["digest"]) != (
                row.spec_revision, row.spec_digest, row.prompt_revision, row.prompt_digest):
            raise RunLaunchConflict("The Execution Package no longer matches the Run snapshot", "digest_mismatch")
        if protocol.sha256 != row.protocol_sha256 or protocol.version != row.protocol_version:
            # Never publish a Run under rules other than the ones pinned at launch (nothing was sent yet).
            raise RunLaunchUnavailable("The pinned Worker protocol changed after launch", "protocol_changed")
        with self._sessions() as session:
            project = self._project(session, row.project_id)
            binding = validate_project_identity(project)
            if binding.version != row.root_binding_version:
                raise RunLaunchConflict("The Root Binding changed after launch", "binding_changed")
            agent = session.get(AgentDefinition, row.agent_id)
            root = self._artifact_root(session, project)
            files: dict[str, bytes] = {
                "spec.md": (f"# Task T{row.task_id:04d} — Spec revision {spec['revision']}\n\n"
                            f"## Title\n\n{spec['title']}\n\n## Scope\n\n{spec['scope'] or '(none)'}\n\n"
                            f"## Description\n\n{spec['description_text']}\n\n"
                            f"## Acceptance criteria\n\n{spec['acceptance_criteria_text']}\n").encode("utf-8"),
                "prompt.md": (f"# Approved Prompt revision {prompt['revision']} (Spec revision "
                              f"{prompt['source_spec_revision']})\n\n## Summary\n\n{prompt['summary']}\n\n"
                              f"## Prompt\n\n{prompt['body']}\n").encode("utf-8"),
            }
            for _id, name, raw in protocol.files:
                files[name] = raw
            for _id, name, _version, raw in protocol.tools:
                files[name] = raw
            contents = [{"file": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                        for name, data in sorted(files.items())]
            manifest = {
                "format": RUN_MANIFEST_FORMAT, "run_id": row.run_id,
                "project": {"id": f"P{project.id:04d}", "uuid": project.project_uuid, "name": project.name[:160]},
                "task": {"id": f"T{row.task_id:04d}"},
                "binding": {"version": binding.version, "root": binding.root_path},
                "worker": {"agent_id": row.agent_id, "display_name": agent.display_name if agent else None,
                           "worker_session_id": row.worker_session_id,
                           **({"incarnation_ref": incarnation_ref(row.incarnation)} if protocol.executable else {})},
                "package": {"package_id": row.package_id, "prompt_approval_id": row.prompt_approval_id,
                            "spec_revision": row.spec_revision, "spec_digest": row.spec_digest,
                            "prompt_revision": row.prompt_revision, "prompt_digest": row.prompt_digest},
                "protocol": {"ruleset_id": protocol.ruleset_id, "version": protocol.version,
                             "sha256": protocol.sha256,
                             "files": [name for _id, name, _raw in protocol.files]},
                "operative_stages": list(protocol.operative_stages),
                "contents": contents, "created_at": _iso(row.created_at),
            }
            if protocol.executable:  # P054B: the run-pinned offline writer and the fixed artifact layout of this Run
                _tid, tool_name, tool_version, tool_raw = protocol.tools[0]
                manifest["tool"] = {"file": tool_name, "version": tool_version,
                                    "sha256": hashlib.sha256(tool_raw).hexdigest(),
                                    "invoke": f"python3 <package>/{tool_name}"}
                manifest["artifacts"] = dict(ARTIFACT_LAYOUT)
        files[MANIFEST_NAME] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if sum(len(data) for data in files.values()) > MAX_PACKAGE_BYTES:
            raise RunLaunchConflict("The Run package exceeds its size bound", "package_too_large")
        return files, root

    def _publish(self, row: ManagedRunState, files: dict[str, bytes], root: Path) -> Path:
        """Atomically publish the package directory once; an existing one must be byte-identical (never overwritten)."""
        run_dir = create_run_directory(root, row.project_id, row.task_id, row.run_id)
        package_dir = run_dir / PACKAGE_DIRNAME
        if package_dir.exists() or package_dir.is_symlink():
            if not _verify_package(package_dir, files):
                raise WorkspaceConflict("foreign", "An existing Run package differs from the frozen contents")
            return package_dir
        staging = run_dir / f".package-staging-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        try:
            for name, data in files.items():
                _write_exclusive(staging / name, data)
            _fsync_dir(staging)
            os.chmod(staging, 0o500)
            os.rename(staging, package_dir)  # fails when the target appeared meanwhile; never replaces it
            _fsync_dir(run_dir)
        finally:
            if staging.exists():
                os.chmod(staging, 0o700)
                for child in staging.iterdir():
                    child.unlink()
                staging.rmdir()
        if not _verify_package(package_dir, files):
            raise WorkspaceConflict("corrupt", "The published Run package failed verification")
        return package_dir

    def _continue(self, run_id: str, *, created: bool) -> dict:
        with self._sessions() as session:
            row = session.get(ManagedRunState, run_id)
            project_id, state = row.project_id, row.state
        if state == RUN_AUTHORIZED:
            try:
                protocol = load_worker_protocol(self._library)
                with self._sessions() as session:
                    row = session.get(ManagedRunState, run_id)
                    files, root = self._package_files(row, protocol)
                    self._publish(row, files, root)
            except (RunLaunchError, WorkspaceConflict, AuthoringError, OSError) as exc:
                reason = getattr(exc, "code", None) or getattr(exc, "problem", None) or "package_publish_failed"
                self._transition(run_id, (RUN_AUTHORIZED,), RUN_NOT_DELIVERED, f"package_publish_failed:{reason}"[:48])
                return self._view_by_id(project_id, run_id, created=created)
            manifest = hashlib.sha256(files[MANIFEST_NAME]).hexdigest()
            self._transition(run_id, (RUN_AUTHORIZED,), RUN_DISPATCH_PENDING, "package_published",
                             manifest_sha256=manifest)
        with self._sessions() as session:
            row = session.get(ManagedRunState, run_id)
            state = row.state
        if state == RUN_DISPATCH_PENDING:
            self._dispatch(run_id)
        return self._view_by_id(project_id, run_id, created=created)

    def _dispatch(self, run_id: str) -> None:
        """Re-verify the live target, record the durable intent, then type exactly one pointer line."""
        with self._sessions() as session:
            row = session.get(ManagedRunState, run_id)
            project = self._project(session, row.project_id)
            problem = None
            try:
                binding = validate_project_identity(project)
                root = self._artifact_root(session, project)
                obs, worker = self._workers.run_target(session, project, row.agent_id)
            except (RunLaunchError, ProjectIdentityError, AgentNotFound) as exc:
                problem, binding, worker, obs, root = getattr(exc, "code", "target_unavailable"), None, None, None, None
            if problem is None:
                if binding.version != row.root_binding_version:
                    problem = "binding_changed"
                elif worker is None or worker.incarnation != row.incarnation or worker.pane_pid != row.pane_pid:
                    problem = "worker_changed"
                elif obs.state != STATE_ACTIVE:
                    problem = "worker_not_active"
                elif not obs.bootstrap or obs.bootstrap.get("state") != B_CONFIRMED:
                    problem = "bootstrap_not_confirmed"
            if problem is None:
                package_dir = run_directory(root, row.project_id, row.task_id, row.run_id) / PACKAGE_DIRNAME
                # P054B: the published package is verified against its OWN pinned snapshot (recorded manifest digest +
                # every listed file), never re-derived from the live global/ rules that may have changed since launch.
                if verify_published_package(package_dir, row.manifest_sha256, row.protocol_sha256) is None:
                    problem = "package_invalid"
            token, incarnation, manifest = row.run_token, row.incarnation, row.manifest_sha256
        if problem is not None:
            self._transition(run_id, (RUN_DISPATCH_PENDING,), RUN_NOT_DELIVERED, problem)
            return
        base = self._workers.run_evidence_base(incarnation)
        request = Path(str(base) + ".run-request.json")
        try:
            request.parent.mkdir(parents=True, exist_ok=True)
            temp = request.with_name(f"{request.name}.{uuid.uuid4().hex}.tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"incarnation": incarnation, "run_id": run_id, "token": token,
                           "package_dir": str(package_dir), "manifest_sha256": manifest}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, request)
        except OSError:
            self._transition(run_id, (RUN_DISPATCH_PENDING,), RUN_NOT_DELIVERED, "hook_request_failed")
            return
        # DURABLE DISPATCH INTENT: from this commit on, delivery may happen and is never repeated.
        if not self._transition(run_id, (RUN_DISPATCH_PENDING,), RUN_DISPATCHING, "dispatch_intent",
                                dispatch_intent_at="now"):
            return
        text = (f"[ROSC run {token}] Формальный ROSC Run (не чат и не bootstrap): run_id={run_id} "
                f"package={package_dir} manifest_sha256={manifest}. Выполни этап E0 Worker Execution Protocol: "
                f"прочитай инструментом Read {package_dir / MANIFEST_NAME} и затем файлы из его protocol.files, "
                f"проверь их и закончи ответ одной последней строкой ROSC-RUN-ACK {token} {run_id} {manifest} ACCEPT "
                f"(или DECLINE с причиной выше). Не начинай реализацию: этапы, не перечисленные в operative_stages, "
                f"не поддерживаются.")
        try:
            self._workers.deliver_run_line(worker, text)
        except SessionError:
            self._note(run_id, "send_uncertain")  # stays dispatching -> unknown after the window; never re-sent
            return
        deadline = self._clock() + self._submit_wait
        while self._clock() < deadline and not self._submitted(row_snapshot(run_id, self._sessions), base):
            time.sleep(0.25)
        self.reconcile_internal(run_id)

    def _protocol_unchanged(self, row: ManagedRunState) -> bool:
        try:
            protocol = load_worker_protocol(self._library)
        except RunLaunchError:
            return False
        return (protocol.sha256, protocol.version) == (row.protocol_sha256, row.protocol_version)

    # -- P054B Owner reads of the imported facts (DB only + the registered report file) --------------------------------
    def _own_run(self, actor: object, project_id: int, run_id: object) -> None:
        if not authz.is_canonical_uuid4(run_id):
            raise RunLaunchNotFound("Run not found")
        self._authorize(actor, authz.RUN_READ, project_id=project_id, run_id=run_id)

    def journal(self, actor: object, project_id: int, run_id: object, *, after: object = 0, limit: object = 100) -> dict:
        self._own_run(actor, project_id, run_id)
        after = after if isinstance(after, int) and not isinstance(after, bool) and after >= 0 else 0
        limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 500 else 100
        with self._sessions() as session:
            row = session.get(ManagedRunState, run_id)
            if row is None or row.project_id != project_id:
                raise RunLaunchNotFound("Run not found")
            return self.artifacts.journal_page(session, run_id, after, limit)

    def report(self, actor: object, project_id: int, run_id: object, *, offset: object = 0, limit: object = 65536) -> dict:
        self._own_run(actor, project_id, run_id)
        offset = offset if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0 else 0
        limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 1 else 65536
        with self._sessions() as session:
            row = session.get(ManagedRunState, run_id)
            if row is None or row.project_id != project_id:
                raise RunLaunchNotFound("Run not found")
            try:
                return self.artifacts.read_report(session, row, offset, limit)
            except ReportUnavailable as exc:
                raise RunLaunchConflict("The Run Report is not available", exc.code) from None

    def report_for_tool(self, session: Session, project_id: int, run_id: str, task_id: int | None,
                        offset: int, limit: int) -> dict:
        """Narrow seam for the Architect ``rosc_report_read`` Tool (the caller is already verified by the broker)."""
        row = session.get(ManagedRunState, run_id)
        if row is None or row.project_id != project_id or (task_id is not None and row.task_id != task_id):
            raise ReportUnavailable("not_found")
        return self.artifacts.read_report(session, row, offset, limit)

    def evidence_for_tool(self, session: Session, project_id: int, run_id: str, task_id: int | None, *,
                          path: object = None, offset: int = 0, limit: int = 16000) -> dict:
        """P055 seam for the Architect ``rosc_evidence_list``/``rosc_evidence_read`` Tools (caller verified by the broker).
        The Run is named by its canonical id only; ``path`` is Run-relative (``evidence/<name>``), never a host path."""
        row = session.get(ManagedRunState, run_id)
        if row is None or row.project_id != project_id or (task_id is not None and row.task_id != task_id):
            raise EvidenceUnavailable("not_found", "No such managed Run in the current Project")
        if path is None:
            return self.evidence.list(session, row, offset, limit)
        return self.evidence.read(session, row, path, offset, limit)

    def _note(self, run_id: str, reason: str) -> None:
        with self._sessions() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                row = session.get(ManagedRunState, run_id)
                now = _now()
                row.reason, row.updated_at = reason, now
                row.state_revision += 1
                self._event(session, run_id, row.state, row.state, reason, "rosc-core", now)
                session.commit()
            except (OperationalError, IntegrityError):
                session.rollback()

    # -- evidence evaluation (pure) ------------------------------------------------------------------------------------
    @staticmethod
    def _load_evidence(path: Path) -> dict | None:
        try:
            if path.is_symlink() or path.stat().st_size > MAX_EVIDENCE_BYTES:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _valid_hook_record(self, data: dict | None, snap: dict) -> bool:
        if data is None or data.get("incarnation") != snap["incarnation"] or data.get("token") != snap["run_token"]:
            return False
        if data.get("run_id") != snap["run_id"]:
            return False
        ancestors, stamp = data.get("ancestors"), data.get("acked_at")
        if not isinstance(ancestors, list) or snap["pane_pid"] not in ancestors:
            return False
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or snap["dispatch_intent_at"] is None:
            return False
        return stamp >= _aware(snap["dispatch_intent_at"]).timestamp() - 5

    def _submitted(self, snap: dict, base: Path) -> bool:
        return self._valid_hook_record(self._load_evidence(Path(f"{base}.run-{snap['run_token']}.submitted.json")), snap)

    def _evaluate(self, session: Session, snap: dict) -> tuple[str, str, dict]:
        """Derived state from the recorded state + CLI-hook evidence + clock.  Never writes, sends or deletes anything."""
        state, reason = snap["state"], snap["reason"]
        evidence = {"submitted": False, "receipt": None, "required_reads": False, "missing_reads": []}
        if state not in (RUN_DISPATCHING, RUN_SUBMITTED, RUN_UNKNOWN, RUN_ACKNOWLEDGED, RUN_DECLINED,
                         RUN_EXEC_DISPATCHING, RUN_RUNNING, RUN_REPORTED, RUN_NOT_EXECUTED):
            return state, reason, evidence
        # recorded outcome stands; evidence is shown, not re-decided.  P054B: past the execution intent (or once the
        # ACK led to a post-E0 state) the E0 outcome is final history, never re-derived.
        final = (state in (RUN_ACKNOWLEDGED, RUN_DECLINED, RUN_EXEC_DISPATCHING, RUN_RUNNING, RUN_REPORTED,
                           RUN_NOT_EXECUTED) or snap.get("exec_intent_at") is not None)
        base = self._workers.run_evidence_base(snap["incarnation"])
        evidence["submitted"] = self._submitted(snap, base)
        receipt = self._load_evidence(Path(f"{base}.run-{snap['run_token']}.receipt.json"))
        project = session.get(Project, snap["project_id"])
        try:
            binding = validate_project_identity(project)
            root = self._artifact_root(session, project)
        except (ProjectIdentityError, RunLaunchError):
            return RUN_UNKNOWN, "project_identity", evidence
        if self._valid_hook_record(receipt, snap) and receipt.get("manifest_sha256") == snap["manifest_sha256"]:
            decision = receipt.get("decision")
            evidence["receipt"] = {"ACCEPT": "accept", "DECLINE": "decline"}.get(decision, "invalid")
            package_dir = (run_directory(root, snap["project_id"], snap["task_id"], snap["run_id"])
                           / PACKAGE_DIRNAME).resolve()
            reads = receipt.get("observed_reads")
            seen = {os.path.realpath(p) for p in reads if isinstance(p, str)} if isinstance(reads, list) else set()
            # E0: the manifest, the pinned rules AND the task inputs of THIS Run's package, each a native Read recorded
            # by the CLI's own PostToolUse hook; the Worker's reply alone never proves a read.
            missing = [name for name in E0_REQUIRED_READS if str(package_dir / name) not in seen]
            evidence["required_reads"], evidence["missing_reads"] = not missing, missing
            if final:
                return state, reason, evidence
            if binding.version != snap["root_binding_version"]:
                return RUN_UNKNOWN, "binding_changed", evidence
            if decision == "DECLINE":
                return RUN_DECLINED, "worker_declined", evidence
            if decision == "ACCEPT":
                if not evidence["required_reads"]:
                    return RUN_UNKNOWN, "ack_without_required_reads", evidence
                return RUN_ACKNOWLEDGED, "late_receipt" if state == RUN_UNKNOWN else "worker_acknowledged", evidence
            return RUN_UNKNOWN, "invalid_receipt", evidence
        if final or state == RUN_UNKNOWN:
            return state, reason, evidence
        started = _aware(snap["dispatch_intent_at"]).timestamp()
        if self._clock() - started > snap["ack_timeout_seconds"]:
            return RUN_UNKNOWN, "no_ack_within_timeout", evidence
        if evidence["submitted"]:
            return RUN_SUBMITTED, "awaiting_worker_ack", evidence
        return state, reason, evidence


    # -- P054B: execution continuation of the SAME Run (no second Owner click) ------------------------------------------
    def _exec_files(self, snap: dict) -> tuple[Path, Path]:
        base = self._workers.run_evidence_base(snap["incarnation"])
        return Path(f"{base}.run-request.json"), Path(f"{base}.run-{snap['run_token']}.exec-submitted.json")

    def _exec_submitted(self, snap: dict) -> bool:
        """The CLI's own UserPromptSubmit hook saw THIS Run's execution line (correlated by the exec token)."""
        _request, path = self._exec_files(snap)
        data = self._load_evidence(path)
        if (data is None or data.get("incarnation") != snap["incarnation"] or data.get("run_id") != snap["run_id"]
                or data.get("exec_token") != snap["exec_token"] or snap["exec_intent_at"] is None):
            return False
        ancestors, stamp = data.get("ancestors"), data.get("acked_at")
        if not isinstance(ancestors, list) or snap["pane_pid"] not in ancestors:
            return False
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            return False
        return stamp >= _aware(snap["exec_intent_at"]).timestamp() - 5

    def _execution_problem(self, session: Session, row: ManagedRunState) -> tuple[str | None, Path | None, object]:
        """Why the execution line must NOT be sent now (``None`` = it may be sent once).  Read-only."""
        if row.reason != "worker_acknowledged" or row.acknowledged_at is None:
            return "ack_not_current", None, None  # a late/irregular ACK is not a grant to start work
        if self._clock() - _aware(row.acknowledged_at).timestamp() > row.ack_timeout_seconds:
            return "continuation_stale", None, None
        project = self._project(session, row.project_id)
        try:
            binding = validate_project_identity(project)
            root = self._artifact_root(session, project)
            obs, worker = self._workers.run_target(session, project, row.agent_id)
        except (RunLaunchError, ProjectIdentityError, AgentNotFound) as exc:
            return getattr(exc, "code", "target_unavailable")[:40], None, None
        if binding.version != row.root_binding_version:
            return "binding_changed", None, None
        if worker is None or worker.incarnation != row.incarnation or worker.pane_pid != row.pane_pid:
            return "worker_changed", None, None
        if obs.state != STATE_ACTIVE:
            return "worker_not_active", None, None
        package_dir = run_directory(root, row.project_id, row.task_id, row.run_id) / PACKAGE_DIRNAME
        manifest = verify_published_package(package_dir, row.manifest_sha256, row.protocol_sha256)
        if manifest is None:
            return "package_invalid", None, None
        if tuple(manifest.get("operative_stages") or ()) != FULL_STAGES or manifest.get("artifacts") != ARTIFACT_LAYOUT \
                or not isinstance(manifest.get("tool"), dict):
            return "e0_only_package", None, None  # historical P054A package: never upgraded or executed
        return None, package_dir, worker

    def _start_execution(self, run_id: str) -> None:
        """``acknowledged`` -> one execution line, at most once.

        Order (each crash window has one outcome):
          1. persist the exec token (state stays ``acknowledged``; nothing sent)       crash -> step 1 again, same token
          2. extend the CLI hook request with the exec token (file, nothing sent)       crash -> steps 2-3 again
          3. DURABLE INTENT ``acknowledged -> exec_dispatching``                         crash -> never re-sent; UNKNOWN
          4. type ONE pointer line into the bound pane                                  crash -> hook evidence decides
          5. the CLI's own UserPromptSubmit hook proves submission -> ``running``       none in window -> UNKNOWN
        A refused precondition (stale/late ACK, changed binding/CLI, invalid or E0-only package) is ``not_executed``:
        provably nothing was sent after the ACK, the slot is released and no second writer can be overlapping."""
        with self._sessions() as session:
            row = session.get(ManagedRunState, run_id)
            if row is None or row.state != RUN_ACKNOWLEDGED:
                return
            problem, package_dir, worker = self._execution_problem(session, row)
            token = row.exec_token
        if problem is not None:
            self._transition(run_id, (RUN_ACKNOWLEDGED,), RUN_NOT_EXECUTED, problem)
            return
        if token is None:
            token = str(uuid.uuid4())
            with self._sessions() as session:
                try:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    row = session.get(ManagedRunState, run_id)
                    if row.state != RUN_ACKNOWLEDGED:
                        session.rollback()
                        return
                    if row.exec_token is None:
                        row.exec_token, row.updated_at = token, _now()
                        row.state_revision += 1
                    token = row.exec_token
                    session.commit()
                except (OperationalError, IntegrityError):
                    session.rollback()
                    return
        snap = row_snapshot(run_id, self._sessions)
        request, _submitted = self._exec_files(snap)
        try:
            temp = request.with_name(f"{request.name}.{uuid.uuid4().hex}.tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"incarnation": snap["incarnation"], "run_id": run_id, "token": snap["run_token"],
                           "package_dir": str(package_dir), "manifest_sha256": snap["manifest_sha256"],
                           "exec_token": token}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, request)
        except OSError:
            return  # nothing sent; the next pass retries steps 2-3 with the same token
        # DURABLE EXECUTION INTENT: from this commit on, the line may be delivered and is never repeated.
        if not self._transition(run_id, (RUN_ACKNOWLEDGED,), RUN_EXEC_DISPATCHING, "exec_intent",
                                exec_intent_at="now"):
            return
        text = (f"[{EXEC_MARKER} {token}] ROSC Core принял E0 ACK этого же формального Run: run_id={run_id} "
                f"package={package_dir} manifest_sha256={snap['manifest_sha256']}. Продолжай ЭТОТ Run: выполни этапы "
                f"E1-E6 по {package_dir / 'worker-execution-protocol.md'} строго в рамках утверждённых spec.md/prompt.md. "
                f"Все артефакты Run записывай только командой python3 {package_dir / ARTIFACT_TOOL_FILE} "
                f"(начни с: run start, затем plan add-step/plan publish до правок); вручную файлы Run не создавай.")
        try:
            self._workers.deliver_run_line(worker, text)
        except SessionError:
            self._note(run_id, "exec_send_uncertain")  # stays exec_dispatching -> unknown after the window
            return
        deadline = self._clock() + self._submit_wait
        while self._clock() < deadline and not self._exec_submitted(row_snapshot(run_id, self._sessions)):
            time.sleep(0.25)
        self._reconcile_execution(run_id)

    def _reconcile_execution(self, run_id: str, *, actor: str = "rosc-core") -> None:
        """Evidence-only: CLI-hook submission proof and imported Journal facts move the state; nothing is ever sent."""
        snap = row_snapshot(run_id, self._sessions)
        if snap["state"] == RUN_EXEC_DISPATCHING:
            if self._exec_submitted(snap):
                self._transition(run_id, (RUN_EXEC_DISPATCHING,), RUN_RUNNING, "exec_submitted", actor=actor,
                                 exec_submitted_at="now")
        elif snap["state"] == RUN_UNKNOWN and snap["exec_intent_at"] is not None and snap["exec_submitted_at"] is None:
            if self._exec_submitted(snap):
                self._transition(run_id, (RUN_UNKNOWN,), RUN_RUNNING, "late_exec_submitted", actor=actor,
                                 exec_submitted_at="now")
        self.artifacts.import_run(run_id)
        snap = row_snapshot(run_id, self._sessions)
        if (snap["state"] == RUN_EXEC_DISPATCHING
                and self._clock() - _aware(snap["exec_intent_at"]).timestamp() > snap["ack_timeout_seconds"]):
            self._transition(run_id, (RUN_EXEC_DISPATCHING,), RUN_UNKNOWN, "no_exec_submit_within_timeout", actor=actor)

    def advance(self, run_id: str, *, actor: str = "rosc-core", send: bool = True) -> None:
        """One bounded, idempotent step of a holding managed Run, routed by its recorded state.  At-most-once sends
        are guaranteed by the durable intents in the database, not by this call.  ``send=False`` (the Owner's
        evidence-only reconcile) records evidence and imports artifacts but never types anything."""
        with self._advance_lock:
            with self._sessions() as session:
                row = session.get(ManagedRunState, run_id)
                if row is None:
                    return
                state, exec_intent = row.state, row.exec_intent_at
            if state in (RUN_DISPATCHING, RUN_SUBMITTED) or (state == RUN_UNKNOWN and exec_intent is None):
                self.reconcile_internal(run_id, actor=actor)
                with self._sessions() as session:
                    state = session.get(ManagedRunState, run_id).state
            if state == RUN_ACKNOWLEDGED:
                if send:
                    self._start_execution(run_id)
            elif state in (RUN_EXEC_DISPATCHING, RUN_RUNNING) or (state == RUN_UNKNOWN and exec_intent is not None):
                self._reconcile_execution(run_id, actor=actor)

    def tick(self) -> int:
        """Startup catch-up and periodic in-process pass over holding managed Runs only (bounded batch).  Never
        launches a Run, restarts/kills a CLI, re-sends an uncertain line or invokes a model."""
        with self._sessions() as session:
            ids = list(session.scalars(select(ManagedRunState.run_id).where(
                ManagedRunState.state.in_((RUN_DISPATCHING, RUN_SUBMITTED, RUN_ACKNOWLEDGED, RUN_UNKNOWN,
                                           RUN_EXEC_DISPATCHING, RUN_RUNNING)))
                .order_by(ManagedRunState.updated_at).limit(MONITOR_BATCH)))
        for run_id in ids:
            try:
                self.advance(run_id)
            except Exception as exc:  # one broken Run never stops the others
                log.warning("Managed Run %s could not be advanced: %s", run_id, type(exc).__name__)
        return len(ids)

    # -- read / reconcile -----------------------------------------------------------------------------------------------
    def status(self, actor: object, project_id: int, run_id: object) -> dict:
        """Read-only: derived state; no DB write, no keystroke, no file change."""
        if not authz.is_canonical_uuid4(run_id):
            raise RunLaunchNotFound("Run not found")
        self._authorize(actor, authz.RUN_READ, project_id=project_id, run_id=run_id)
        return self._view_by_id(project_id, run_id)

    def list_task_runs(self, actor: object, project_id: int, task_id: int) -> list[dict]:
        self._authorize(actor, authz.RUN_LIST, project_id=project_id, task_id=task_id)
        with self._sessions() as session:
            self._project(session, project_id)
            ids = list(session.scalars(select(ManagedRunState.run_id).where(
                ManagedRunState.project_id == project_id, ManagedRunState.task_id == task_id)
                .order_by(ManagedRunState.created_at, ManagedRunState.run_id)))
        return [self._view_by_id(project_id, rid, events=False) for rid in ids]

    def reconcile(self, actor: object, project_id: int, run_id: object) -> dict:
        """Owner-requested, idempotent: record what the evidence already proves.  Never sends, restarts or kills."""
        if not authz.is_canonical_uuid4(run_id):
            raise RunLaunchNotFound("Run not found")
        self._authorize(actor, authz.RUN_RECONCILE, project_id=project_id, run_id=run_id)
        with self._sessions() as session:
            self._project(session, project_id)
            row = session.get(ManagedRunState, run_id)
            if row is None or row.project_id != project_id:
                raise RunLaunchNotFound("Run not found")
        self.advance(run_id, actor=getattr(actor, "actor_id", "rosc-core"), send=False)
        return self._view_by_id(project_id, run_id)

    def reconcile_internal(self, run_id: str, *, actor: str = "rosc-core") -> None:
        with self._sessions() as session:
            snap = row_snapshot(run_id, self._sessions, session=session)
            derived, reason, evidence = self._evaluate(session, snap)
        if derived == snap["state"]:
            if reason != snap["reason"]:
                self._note(run_id, reason)
            return
        fields: dict = {}
        if evidence["submitted"] and snap["submitted_at"] is None:
            fields["submitted_at"] = "now"
        if derived == RUN_ACKNOWLEDGED:
            fields["acknowledged_at"] = "now"
        self._transition(run_id, (snap["state"],), derived, reason, actor=actor, evidence=evidence, **fields)

    def _view_by_id(self, project_id: int, run_id: str, *, created: bool | None = None, events: bool = True) -> dict:
        with self._sessions() as session:
            row = session.get(ManagedRunState, run_id)
            if row is None or row.project_id != project_id:
                raise RunLaunchNotFound("Run not found")
            snap = row_snapshot(run_id, self._sessions, session=session)
            derived, reason, evidence = self._evaluate(session, snap)
            progress = self.artifacts.progress(session, row)
            review = review_state(session, run_id, derived)
            if snap["exec_intent_at"] is not None:
                evidence = {**evidence, "exec_submitted": self._exec_submitted(snap)}
            history = [] if not events else [
                {"seq": e.seq, "from": e.from_state, "to": e.to_state, "reason": e.reason, "actor": e.actor,
                 "at": _iso(e.created_at)}
                for e in session.scalars(select(ManagedRunEvent).where(ManagedRunEvent.run_id == run_id)
                                         .order_by(ManagedRunEvent.seq))]
        view = {
            "run_id": run_id, "execution_origin": ORIGIN_MANAGED, "project_id": f"P{row.project_id:04d}",
            "task_id": f"T{row.task_id:04d}", "agent_id": row.agent_id, "launch_key": row.launch_key,
            "state": derived, "reason": reason, "recorded_state": row.state, "recorded_reason": row.reason,
            "holding": derived in MANAGED_HOLDING_STATES,
            "resumable_with_same_key": row.state == RUN_DISPATCH_PENDING or (
                row.state == RUN_AUTHORIZED and self._protocol_unchanged(row)),
            "package": {"package_id": row.package_id, "prompt_approval_id": row.prompt_approval_id,
                        "spec_revision": row.spec_revision, "spec_digest": row.spec_digest,
                        "prompt_revision": row.prompt_revision, "prompt_digest": row.prompt_digest,
                        "root_binding_version": row.root_binding_version, "manifest_sha256": row.manifest_sha256},
            "worker": {"agent_id": row.agent_id, "worker_session_id": row.worker_session_id},
            "protocol": {"version": row.protocol_version, "sha256": row.protocol_sha256},
            "run_directory": f"projects/P{row.project_id:04d}/tasks/T{row.task_id:04d}/runs/{run_id}",
            "ack_timeout_seconds": row.ack_timeout_seconds,
            "evidence": evidence,
            "created_at": _iso(row.created_at), "dispatch_intent_at": _iso(row.dispatch_intent_at),
            "submitted_at": _iso(row.submitted_at), "acknowledged_at": _iso(row.acknowledged_at),
            # P054B: execution continuation of this same Run and the imported Worker artifacts (observed claims).
            "execution": {"intent_at": _iso(row.exec_intent_at), "submitted_at": _iso(row.exec_submitted_at),
                          "reported_at": _iso(row.reported_at), "outcome": row.outcome},
            "work_started": bool(progress and (progress["started"] or progress["journal"]["events"])),
            "plan": None if not progress else {"revision": progress["plan_revision"], "steps": progress["steps"],
                                               "progress": progress["progress"]},
            "progress": progress,
            "report": progress["report"] if progress else None,
            # P055: derived ONLY from a recorded ``rosc_review_submit`` (never from chat text); the Run stays reported.
            "review": review,
            "checked_at": _iso(_now()),
        }
        if events:
            view["events"] = history
        if created is not None:
            view["created"] = created
        return view


def row_snapshot(run_id: str, sessions: sessionmaker[Session], *, session: Session | None = None) -> dict:
    """Plain-dict copy of the fields evidence evaluation needs (detached from any ORM session)."""
    def take(s: Session) -> dict:
        row = s.get(ManagedRunState, run_id)
        return {name: getattr(row, name) for name in (
            "run_id", "project_id", "task_id", "agent_id", "incarnation", "pane_pid", "run_token", "state", "reason",
            "manifest_sha256", "dispatch_intent_at", "submitted_at", "ack_timeout_seconds", "root_binding_version",
            "exec_token", "exec_intent_at", "exec_submitted_at", "acknowledged_at", "protocol_sha256")}
    if session is not None:
        return take(session)
    with sessions() as fresh:
        return take(fresh)
