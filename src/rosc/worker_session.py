"""P042A: Project + Worker CLI session bridge (external, independently running Claude Code in a ROSC-owned tmux).

What this module can and cannot prove (read before relying on any state):

* ``tmux alive``      - a pane of the stored ROSC-owned tmux session exists and is not dead.
* ``CLI observable``  - that pane's process is the very incarnation ROSC started: same pid, same /proc start time and
  the random incarnation nonce in its environment.  A different/unknown process under the same name is ``unknown``.
* ``acknowledged``    - the CLI's own ``SessionStart`` hook (installed per launch through ``--settings``; no global
  configuration is touched) wrote a local file echoing that nonce from a process whose ancestry contains the pane
  process.  It proves the interactive CLI *initialised a session*.  It does NOT prove the model or Worker accepted
  any task: that is a separate acknowledgement that belongs to a later Run slice.  The ack is never reused across
  incarnations (new nonce per start) and is re-validated against the live process on every read.

Only ``activate`` may create a tmux session / CLI process, and only for an Owner actor.  Status/list are read-only
observations: no process start, no keystroke, no stop, no DB write.  Nothing here stops or kills anything: the CLI
outlives the Engine and is re-discovered from the persisted binding.  Foreign or ambiguous sessions are never adopted,
written to or replaced.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .agent_registry import (AgentNotFound, InvalidRegistryInput, ProjectNotFound, RegistryDenied, RegistryStoreError,
                             canonical_agent_id)
from .authorization import (WORKER_SESSION_ACTIVATE, WORKER_SESSION_STATUS, ActorContext, AuthorizationPolicy,
                            ResourceScope, evaluate)
from .models import (ASSIGNMENT_ACTIVE, ROLE_WORKER, AgentDefinition, Project, ProjectIdentityError, RoleAssignment,
                     WorkerSession, validate_project_identity)

STATE_NOT_ASSIGNED = "not_assigned"
STATE_NOT_CONNECTED = "not_connected"
STATE_CONNECTING = "connecting"
STATE_ACTIVE = "active"          # "CLI-сессия активна": initialised + verified live; NOT "task accepted"
STATE_UNAVAILABLE = "unavailable"
STATE_UNKNOWN = "unknown"

# Closed set of runtime adapters the CLI bridge supports (compared case-insensitively). Anything else is unavailable.
SUPPORTED_ADAPTERS = frozenset({"claude-code", "claude_code", "claude-code-cli", "claudecode"})
TMUX_SOCKET = "rosc"             # dedicated tmux server: Owner's own tmux sessions are never listed or touched
ACK_GRACE_SECONDS = 90.0
ACK_WAIT_SECONDS = 15.0
MAX_ACK_BYTES = 8192
NONCE_ENV = "ROSC_WORKER_INCARNATION"
FILE_ENV = "ROSC_WORKER_ACK_FILE"
ACK_SCRIPT = Path(__file__).with_name("worker_ack.py")


class SessionError(Exception):
    pass


class TmuxError(SessionError):
    """tmux ran but could not answer (timeout, unexpected failure): the observation is uncertain."""


class TmuxUnavailable(TmuxError):
    """The tmux binary is not installed/executable."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class PaneInfo:
    pane_id: str
    pane_pid: int
    dead: bool
    server_pid: int | None = None


class TmuxClient:
    """Thin wrapper over the dedicated ROSC tmux server (``tmux -L rosc``).  Never touches another socket."""

    def __init__(self, binary: str = "tmux", socket: str = TMUX_SOCKET, timeout: float = 5.0) -> None:
        self.binary, self.socket, self.timeout = binary, socket, timeout

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([self.binary, "-L", self.socket, *args], capture_output=True, text=True,
                                  timeout=self.timeout, start_new_session=True, stdin=subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise TmuxUnavailable("tmux is not installed") from exc
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise TmuxError(type(exc).__name__) from exc

    def panes(self, session: str) -> list[PaneInfo] | None:
        """Panes of exactly this session; ``None`` when the server or the session does not exist."""
        result = self._run("list-panes", "-t", f"={session}", "-F", "#{pane_id}\t#{pane_pid}\t#{pane_dead}\t#{pid}")
        if result.returncode != 0:
            text = (result.stderr or "").lower()
            if "no server running" in text or "can't find" in text or "no such file" in text or "error connecting" in text:
                return None
            raise TmuxError("list-panes failed")
        panes = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 4 or not parts[1].isdigit():
                raise TmuxError("unexpected list-panes output")
            panes.append(PaneInfo(parts[0], int(parts[1]), parts[2] == "1", int(parts[3]) if parts[3].isdigit() else None))
        return panes

    def new_session(self, name: str, cwd: str, env: dict[str, str], argv: list[str]) -> None:
        args = ["new-session", "-d", "-s", name, "-c", cwd, "-x", "200", "-y", "50"]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        result = self._run(*args, "--", *argv)
        if result.returncode != 0:
            raise TmuxError("new-session failed")

    def kill_session(self, name: str) -> None:
        self._run("kill-session", "-t", f"={name}")


class ProcInspector:
    """Linux /proc identity checks (pid reuse and incarnation correlation)."""

    @staticmethod
    def start_time(pid: int) -> int | None:
        try:
            raw = Path(f"/proc/{pid}/stat").read_bytes().decode("utf-8", "replace")
            return int(raw.rsplit(")", 1)[1].split()[19])
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def env_value(pid: int, key: str) -> str | None:
        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            return None
        prefix = f"{key}=".encode()
        for item in raw.split(b"\0"):
            if item.startswith(prefix):
                return item[len(prefix):].decode("utf-8", "replace")
        return None


def default_cli_resolver(adapter: str) -> str | None:
    """Absolute path of the ordinary Claude Code CLI, or ``None``.  No flag, model or config is chosen here."""
    return shutil.which("claude")


@dataclass
class Observation:
    state: str
    reason: str
    tmux_alive: bool | None = None
    cli_observable: bool | None = None
    acked: bool = False
    acked_at: datetime | None = None
    can_activate: bool = False
    reused: bool = False


def _safe_name(project_id: int, agent_id: str) -> str:
    return f"rosc-p{project_id:04d}-{agent_id[:8]}"


class WorkerSessionService:
    def __init__(self, session_factory: sessionmaker[Session], policy: AuthorizationPolicy, state_dir: Path, *,
                 tmux: TmuxClient | None = None, proc: ProcInspector | None = None, cli_resolver=default_cli_resolver,
                 ack_grace: float = ACK_GRACE_SECONDS, ack_wait: float = ACK_WAIT_SECONDS,
                 python: str | None = None, clock=time.time) -> None:
        self._sessions = session_factory
        self._policy = policy
        self._dir = Path(state_dir)
        self._tmux = tmux or TmuxClient()
        self._proc = proc or ProcInspector()
        self._resolve_cli = cli_resolver
        self._grace, self._wait = ack_grace, ack_wait
        self._python = python or sys.executable
        self._clock = clock
        self._locks: dict[tuple[int, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- authorization / scope ---------------------------------------------------------------------------------
    def _authorize(self, actor: ActorContext, action: str, project_id: int, agent_id: str) -> None:
        if not evaluate(self._policy, actor, action, ResourceScope(project_id=project_id, agent_id=agent_id)).allowed:
            raise RegistryDenied("Not authorized for this operation")

    def _lock(self, key: tuple[int, str]) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def _context(self, session: Session, project_id: int, agent_id: str):
        project = session.get(Project, project_id)
        if project is None:
            raise ProjectNotFound("Project not found")
        agent = session.get(AgentDefinition, agent_id)
        if agent is None:
            raise AgentNotFound("Agent not found")
        assignment = session.scalars(select(RoleAssignment).where(
            RoleAssignment.project_id == project_id, RoleAssignment.agent_id == agent_id,
            RoleAssignment.role == ROLE_WORKER, RoleAssignment.state == ASSIGNMENT_ACTIVE,
            RoleAssignment.task_id.is_(None))).first()
        return project, agent, assignment

    # -- observation (read-only) -------------------------------------------------------------------------------
    def _ack_path(self, incarnation: str) -> Path:
        return self._dir / f"{incarnation}.ack.json"

    def _read_ack(self, row: WorkerSession) -> tuple[bool, datetime | None]:
        path = self._ack_path(row.incarnation)
        try:
            if path.stat().st_size > MAX_ACK_BYTES:
                return False, None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False, None
        if not isinstance(data, dict) or data.get("incarnation") != row.incarnation:
            return False, None
        ancestors = data.get("ancestors")
        if not isinstance(ancestors, list) or row.pane_pid not in ancestors:
            return False, None
        stamp = data.get("acked_at")
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            return False, None
        created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
        if stamp < created.timestamp() - 5:
            return False, None
        return True, datetime.fromtimestamp(stamp, timezone.utc)

    def _prerequisites(self, agent: AgentDefinition, project: Project) -> str | None:
        if agent.runtime_adapter.lower() not in SUPPORTED_ADAPTERS:
            return "unsupported_adapter"
        if not self._tmux.available():
            return "tmux_missing"
        if not self._resolve_cli(agent.runtime_adapter):
            return "cli_missing"
        try:
            root = Path(validate_project_identity(project).root_path)
        except ProjectIdentityError:
            return "project_identity"
        if not root.is_dir():
            return "root_missing"
        return None

    def _observe(self, session: Session, project: Project, agent: AgentDefinition,
                 assignment: RoleAssignment | None) -> tuple[Observation, WorkerSession | None]:
        if assignment is None:
            return Observation(STATE_NOT_ASSIGNED, "no_assignment"), None
        row = session.scalars(select(WorkerSession).where(WorkerSession.project_id == project.id,
                                                          WorkerSession.agent_id == agent.agent_id)).first()
        if row is None:
            problem = self._prerequisites(agent, project)
            if problem:
                return Observation(STATE_UNAVAILABLE, problem), None
            return Observation(STATE_NOT_CONNECTED, "never_activated", can_activate=True), None
        return self._observe_bound(row), row

    def _observe_bound(self, row: WorkerSession) -> Observation:
        try:
            panes = self._tmux.panes(row.tmux_session)
        except TmuxUnavailable:
            return Observation(STATE_UNKNOWN, "tmux_unobservable")
        except TmuxError:
            return Observation(STATE_UNKNOWN, "tmux_unobservable")
        same_process = (self._proc.start_time(row.pane_pid) is not None
                        and (row.proc_start is None or self._proc.start_time(row.pane_pid) == row.proc_start))
        if panes is None:
            if same_process:  # a prior writer may still be alive outside tmux: never claim it is gone
                return Observation(STATE_UNKNOWN, "session_missing_process_alive", tmux_alive=False)
            return Observation(STATE_NOT_CONNECTED, "vanished", tmux_alive=False, cli_observable=False, can_activate=True)
        pane = next((p for p in panes if p.pane_id == row.pane_id), None)
        if pane is None or pane.pane_pid != row.pane_pid:
            return Observation(STATE_UNKNOWN, "process_mismatch", tmux_alive=True, cli_observable=False)
        if pane.dead:
            if same_process:
                return Observation(STATE_UNKNOWN, "process_mismatch", tmux_alive=False)
            return Observation(STATE_NOT_CONNECTED, "vanished", tmux_alive=False, cli_observable=False, can_activate=True)
        if not same_process or self._proc.env_value(row.pane_pid, NONCE_ENV) != row.incarnation:
            return Observation(STATE_UNKNOWN, "process_mismatch", tmux_alive=True, cli_observable=False)
        acked, when = self._read_ack(row)
        if acked:
            return Observation(STATE_ACTIVE, "initialised", tmux_alive=True, cli_observable=True, acked=True, acked_at=when)
        created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
        if self._clock() - created.timestamp() <= self._grace:
            return Observation(STATE_CONNECTING, "awaiting_ack", tmux_alive=True, cli_observable=True)
        return Observation(STATE_UNKNOWN, "no_ack", tmux_alive=True, cli_observable=True)

    def _view(self, project_id: int, agent_id: str, obs: Observation) -> dict:
        """Public read model. No tmux names, pane/process ids, nonces or filesystem paths."""
        return {
            "project_id": f"P{project_id:04d}", "agent_id": agent_id, "state": obs.state, "reason": obs.reason,
            "can_activate": obs.can_activate, "can_check": obs.state != STATE_NOT_ASSIGNED,
            "reused": obs.reused, "initialised_at": _iso(obs.acked_at), "checked_at": _iso(_now()),
            "evidence": {"tmux_alive": obs.tmux_alive, "cli_observable": obs.cli_observable,
                         "cli_session_initialised": obs.acked},
        }

    # -- public API --------------------------------------------------------------------------------------------
    def status(self, actor: ActorContext, project_id: int, agent_id: object) -> dict:
        agent_key = canonical_agent_id(agent_id)
        self._authorize(actor, WORKER_SESSION_STATUS, project_id, agent_key)
        with self._sessions() as session:
            project, agent, assignment = self._context(session, project_id, agent_key)
            obs, _row = self._observe(session, project, agent, assignment)
        return self._view(project_id, agent_key, obs)

    def list_workers(self, actor: ActorContext, project_id: int) -> list[dict]:
        """Active Project-scoped Worker assignments with their observed session state (read-only)."""
        from .authorization import ASSIGNMENT_LIST
        if not evaluate(self._policy, actor, ASSIGNMENT_LIST, ResourceScope(project_id=project_id)).allowed:
            raise RegistryDenied("Not authorized for this operation")
        out: list[dict] = []
        with self._sessions() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise ProjectNotFound("Project not found")
            rows = session.scalars(select(RoleAssignment).where(
                RoleAssignment.project_id == project_id, RoleAssignment.role == ROLE_WORKER,
                RoleAssignment.state == ASSIGNMENT_ACTIVE, RoleAssignment.task_id.is_(None))
                .order_by(RoleAssignment.created_at, RoleAssignment.assignment_id)).all()
            for assignment in rows:
                agent = session.get(AgentDefinition, assignment.agent_id)
                if agent is None:
                    continue
                obs, _row = self._observe(session, project, agent, assignment)
                out.append({"agent": {"agent_id": agent.agent_id, "display_name": agent.display_name,
                                      "icon_label": agent.icon_label, "runtime_adapter": agent.runtime_adapter,
                                      "provider_id": agent.provider_id, "model_id": agent.model_id},
                            "session": self._view(project_id, agent.agent_id, obs)})
        return out

    def activate(self, actor: ActorContext, project_id: int, agent_id: object) -> dict:
        """The one explicit Owner action that may create a ROSC-owned tmux session with an ordinary CLI inside it."""
        agent_key = canonical_agent_id(agent_id)
        self._authorize(actor, WORKER_SESSION_ACTIVATE, project_id, agent_key)
        with self._lock((project_id, agent_key)):
            with self._sessions() as session:
                project, agent, assignment = self._context(session, project_id, agent_key)
                obs, row = self._observe(session, project, agent, assignment)
                if obs.state in (STATE_ACTIVE, STATE_CONNECTING):
                    obs.reused = True  # reconnect/reuse: never a second CLI
                    return self._view(project_id, agent_key, obs)
                if not obs.can_activate:
                    return self._view(project_id, agent_key, obs)  # not assigned / unavailable / unknown: fail closed
                name = _safe_name(project_id, agent_key)
                if row is None:
                    try:
                        foreign = self._tmux.panes(name)
                    except TmuxError:
                        return self._view(project_id, agent_key, Observation(STATE_UNKNOWN, "tmux_unobservable"))
                    if foreign is not None:  # a same-named session we never bound: not ours to adopt or replace
                        return self._view(project_id, agent_key, Observation(STATE_UNKNOWN, "foreign_session"))
                else:
                    try:
                        name = row.tmux_session
                        if self._tmux.panes(name) is not None:
                            return self._view(project_id, agent_key, Observation(STATE_UNKNOWN, "process_mismatch"))
                    except TmuxError:
                        return self._view(project_id, agent_key, Observation(STATE_UNKNOWN, "tmux_unobservable"))
                problem = self._prerequisites(agent, project)
                if problem:
                    return self._view(project_id, agent_key, Observation(STATE_UNAVAILABLE, problem))
                cli = self._resolve_cli(agent.runtime_adapter)
                root = validate_project_identity(project).root_path
                incarnation = str(uuid.uuid4())
                self._dir.mkdir(parents=True, exist_ok=True)
                ack_file = self._ack_path(incarnation)
                hook = f"{shlex.quote(self._python)} {shlex.quote(str(ACK_SCRIPT))}"
                settings = json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": hook}]}]}},
                                      separators=(",", ":"))
                created = False
                try:
                    self._tmux.new_session(name, root, {NONCE_ENV: incarnation, FILE_ENV: str(ack_file)},
                                           [cli, "--settings", settings])
                    created = True
                    panes = self._tmux.panes(name)
                except TmuxError:
                    if created:
                        self._cleanup(name)
                    return self._view(project_id, agent_key, Observation(STATE_UNAVAILABLE, "spawn_failed"))
                if not panes or len(panes) != 1:
                    self._cleanup(name)
                    return self._view(project_id, agent_key, Observation(STATE_UNAVAILABLE, "spawn_failed"))
                pane = panes[0]
                now = _now()
                old_ack = self._ack_path(row.incarnation) if row is not None else None
                try:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    if row is None:
                        row = WorkerSession(session_id=str(uuid.uuid4()), project_id=project_id, agent_id=agent_key)
                        session.add(row)
                    row.incarnation, row.tmux_socket, row.tmux_session = incarnation, self._tmux.socket, name
                    row.pane_id, row.pane_pid, row.workdir = pane.pane_id, pane.pane_pid, root
                    row.proc_start = self._proc.start_time(pane.pane_pid)
                    row.created_at, row.last_observed_at, row.last_state = now, now, STATE_CONNECTING
                    session.commit()
                except (OperationalError, IntegrityError):
                    session.rollback()
                    self._cleanup(name)  # we just created it: ROSC-owned, safe to remove; nothing was recorded
                    raise RegistryStoreError("Worker session could not be recorded; retry shortly") from None
                if old_ack is not None:
                    old_ack.unlink(missing_ok=True)
                deadline = self._clock() + self._wait
                obs = self._observe_bound(row)
                while obs.state == STATE_CONNECTING and self._clock() < deadline:
                    time.sleep(0.25)
                    obs = self._observe_bound(row)
                return self._view(project_id, agent_key, obs)

    def _cleanup(self, name: str) -> None:
        try:
            self._tmux.kill_session(name)
        except SessionError:
            pass
