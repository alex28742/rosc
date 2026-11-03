from __future__ import annotations

import ipaddress
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from . import authorization as authz
from .agent_registry import (AgentNotFound, AgentRegistryService, AssignmentNotFound, InvalidRegistryInput, ProjectNotFound,
                             RegistryConflict, RegistryDenied, RegistryError, RegistryStoreError, UnsafeProjectIdentity,
                             canonical_agent_id)
from .architect_runtime import ArchitectRuntime, OpenCodeRuntime
from .architect_service import ArchitectService
from .config import data_dir
from .conversation import (DEFAULT_PAGE_SIZE, ConversationConflict, ConversationDenied, ConversationError,
                           ConversationNotFound, ConversationStoreError, InvalidConversationInput,
                           ProjectConversationService, UnsafeProjectIdentity as UnsafeConversationIdentity)
from .db import make_session_factory
from .models import (CURRENT_BINDING, Project, ProjectIdentityError, ProjectRootBinding, Task, reserved_roots,
                     validate_project_identity)
from .workspace import (MAX_MANIFEST_BYTES, READY, WorkspaceConflict, WorkspaceError, WorkspaceUncertain, inspect_project, inspect_task, provision_project,
                        provision_task, repair_project, repair_task)


log = logging.getLogger(__name__)


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1)
    root_path: str = Field(min_length=1)
    mode: str = Field(pattern="^(new|existing)$")
    docs_entrypoint: str | None = None
    no_external_agent_confirmed: bool = False

    @field_validator("name", "description", "root_path")
    @classmethod
    def nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Required text cannot be blank")
        return value


class TaskInput(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1)
    acceptance_criteria: str = Field(min_length=1)

    @field_validator("title", "description", "acceptance_criteria")
    @classmethod
    def nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Required text cannot be blank")
        return value


def utc_iso(value: datetime) -> str:
    """Naive values written by this slice are UTC (SQLite drops the offset)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def workspace_status(db_state: str, problem: str) -> tuple[str, str | None]:
    if problem == READY and db_state == "ready":
        return "ready", None
    return "needs_reconciliation", "pending" if problem == READY else problem


def binding_view(binding: ProjectRootBinding | None) -> dict | None:
    if binding is None:
        return None
    return {"binding_id": binding.id, "version": binding.version, "root_path": binding.root_path,
            "docs_entrypoint": binding.docs_entrypoint, "mode": binding.mode, "state": binding.state,
            "created_at": utc_iso(binding.created_at)}


def project_identity_problem() -> HTTPException:
    return HTTPException(503, "Project identity records are inconsistent; manual reconciliation is required")


def require_project_identity(project: Project) -> ProjectRootBinding:
    try:
        return validate_project_identity(project)
    except ProjectIdentityError as exc:
        raise project_identity_problem() from exc


def project_view(project: Project, workspace_root: Path) -> dict:
    binding = require_project_identity(project)
    state, problem = workspace_status(project.workspace_state, inspect_project(workspace_root, project).problem)
    return {
        "id": f"P{project.id:04d}", "name": project.name, "description": project.description,
        # Transitional compatibility field: the original registration root, unchanged from earlier slices.
        "root_path": project.root_path,
        "registration_root_path": project.registration_root_path, "project_uuid": project.project_uuid,
        "root_binding": binding_view(binding),
        "mode": project.mode, "docs_entrypoint": project.docs_entrypoint,
        "workspace_state": state, "workspace_problem": problem,
        "architect": "not_active", "active_workers": 0, "created_at": utc_iso(project.created_at),
    }


def task_view(task: Task, workspace_root: Path) -> dict:
    state, problem = workspace_status(task.workspace_state, inspect_task(workspace_root, task).problem)
    return {"id": f"T{task.id:04d}", "project_id": f"P{task.project_id:04d}", "title": task.title,
            "description": task.description, "acceptance_criteria": task.acceptance_criteria,
            "status": task.status, "workspace_state": state, "workspace_problem": problem,
            "worker": "not_assigned", "created_at": utc_iso(task.created_at)}


CANONICAL_ID = {"P": re.compile(r"P(?:[0-9]{4}|[1-9][0-9]{4,})", re.ASCII), "T": re.compile(r"T(?:[0-9]{4}|[1-9][0-9]{4,})", re.ASCII)}


def numeric_id(value: str, prefix: str) -> int:
    """Accept only IDs exactly as the API emits them (P0001, P10000); never an alias."""
    if not CANONICAL_ID[prefix].fullmatch(value):
        raise HTTPException(404, "Not found")
    return int(value[1:])


def agent_path_id(value: str) -> str:
    """Accept only canonical UUIDv4 Agent/assignment ids exactly as the API emits them; anything else is 404."""
    try:
        return canonical_agent_id(value)
    except InvalidRegistryInput:
        raise HTTPException(404, "Not found") from None


def registry_error(exc: RegistryError) -> HTTPException:
    """Map the administrative boundary onto the established HTTP status contract.

    Messages stay generic and never include a path, credential or provider detail.
    """
    if isinstance(exc, RegistryDenied):
        return HTTPException(403, "Not authorized for this operation")
    if isinstance(exc, AgentNotFound):
        return HTTPException(404, "Agent not found")
    if isinstance(exc, ProjectNotFound):
        return HTTPException(404, "Project not found")
    if isinstance(exc, AssignmentNotFound):
        return HTTPException(404, "Assignment not found")
    if isinstance(exc, RegistryConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, InvalidRegistryInput):
        return HTTPException(422, str(exc))
    if isinstance(exc, UnsafeProjectIdentity):
        return HTTPException(503, "Project identity records are inconsistent; manual reconciliation is required")
    if isinstance(exc, RegistryStoreError):
        return HTTPException(503, "Another registry change is in progress; retry shortly")
    return HTTPException(503, "Agent registry operation failed")


def conflict_detail(exc: WorkspaceConflict) -> str:
    if exc.problem in ("corrupt", "drift"):
        return f"Workspace manifest is {exc.problem}; use the Owner-confirmed repair action. {exc.message}"
    if exc.problem == "oversized":
        return f"Workspace manifest is larger than the {MAX_MANIFEST_BYTES} byte limit and cannot be backed up automatically; manual resolution is required. It was left untouched."
    return f"Workspace manifest conflicts with the canonical record ({exc.problem}); manual resolution is required. {exc.message}"


class RepairInput(BaseModel):
    confirm_repair: bool = False


class AgentInput(BaseModel):
    """Descriptive metadata only: no credential, path, command or provider session is accepted here."""

    display_name: str = Field(min_length=1, max_length=80)
    icon_label: str | None = Field(default=None, max_length=32)
    runtime_adapter: str = Field(min_length=1, max_length=64)
    provider_id: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=96)
    capabilities: list[str] = Field(default_factory=list, max_length=8)


class AgentUpdateInput(AgentInput):
    """Full replacement of editable metadata with an optimistic version check."""

    expected_version: int = Field(ge=1)


class AssignmentInput(BaseModel):
    agent_id: str = Field(min_length=36, max_length=36)
    role: str = Field(min_length=1, max_length=16)
    task_id: str | None = None
    capabilities: list[str] = Field(default_factory=list, max_length=8)


class RevokeInput(BaseModel):
    expected_version: int = Field(ge=1)


class ConversationMessageInput(BaseModel):
    """Owner message text plus an opaque client idempotency key; no author, participant or session is accepted."""

    client_key: str = Field(min_length=8, max_length=128)
    body: str = Field(min_length=1, max_length=4000)


CONVERSATION_PATH = re.compile(r"^/api/projects/[^/]+/(conversation(/messages(/[^/]+/(reconcile|retry))?)?|architect/(activate|deactivate)|architect)$")


def sanitized_validation_response(exc: RequestValidationError) -> JSONResponse:
    """422 for conversation requests: only type, location and a fixed message, never the framework's ``input``,
    ``ctx`` or ``url`` fields, which echo the rejected body, idempotency key or decode details."""
    detail = [{"type": str(error.get("type", "invalid")), "loc": [part for part in error.get("loc", ())],
               "msg": str(error.get("msg", "Invalid request"))} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": detail})


def conversation_error(exc: ConversationError) -> HTTPException:
    """Map the conversation boundary onto the established HTTP status contract without echoing message content."""
    if isinstance(exc, ConversationDenied):
        return HTTPException(403, "Not authorized for this operation")
    if isinstance(exc, ConversationNotFound):
        return HTTPException(404, "Project not found")
    if isinstance(exc, ConversationConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, InvalidConversationInput):
        return HTTPException(422, str(exc))
    if isinstance(exc, UnsafeConversationIdentity):
        return HTTPException(503, "Project identity records are inconsistent; manual reconciliation is required")
    if isinstance(exc, ConversationStoreError):
        return HTTPException(503, str(exc))
    return HTTPException(503, "Conversation operation failed")


def require_confirmation(data: RepairInput) -> None:
    if not data.confirm_repair:
        raise HTTPException(422, "Owner confirmation is required to repair a Workspace manifest")


def repair_response(view: dict, record: dict | None) -> dict:
    return {**view, "repair": record}


def validate_root(data: ProjectInput) -> str:
    root = Path(data.root_path).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(422, "Project root must be an existing directory")
    if data.mode == "new" and any(root.iterdir()):
        raise HTTPException(422, "New Project root must be empty")
    if data.mode == "existing":
        if not any(root.iterdir()):
            raise HTTPException(422, "Existing Project root has no files")
        if not data.no_external_agent_confirmed:
            raise HTTPException(422, "Confirm that no external agent is writing in this checkout")
    if data.docs_entrypoint:
        docs = Path(data.docs_entrypoint)
        if docs.is_absolute() or ".." in docs.parts or not (root / docs).resolve().is_relative_to(root) or not (root / docs).is_file():
            raise HTTPException(422, "Documentation entrypoint must be an existing file within Project root")
    return str(root)


def check_overlap(root: str, session) -> None:
    """Reject a root that duplicates or overlaps any *current* Root Binding.

    Historical bindings never reserve a root, and contradictory identity records fail closed instead of making a
    root look free.
    """
    candidate = Path(root)
    try:
        reservations = reserved_roots(session)
    except ProjectIdentityError as exc:
        raise project_identity_problem() from exc
    for other in reservations:
        path = Path(other)
        if path == candidate or path in candidate.parents or candidate in path.parents:
            raise HTTPException(409, "Project root overlaps another registered Project")


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Vite dev server (5173), Vite preview (4173) and uvicorn itself (8000).
LOOPBACK_PORTS = (5173, 4173, 8000)
DEFAULT_ALLOWED_ORIGINS = frozenset(f"http://{host}:{port}" for host in ("localhost", "127.0.0.1", "[::1]")
                                    for port in LOOPBACK_PORTS)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


HOST_AUTHORITY = re.compile(r"(?:(?P<v6>\[[0-9A-Fa-f:]+\])|(?P<name>[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?))(?::(?P<port>[0-9]{1,5}))?")
HOST_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")


def host_name(header: str | None) -> str | None:
    """Lower-cased host of ONE well-formed HTTP Host authority (``host[:port]``), or None when any part is invalid.

    The whole value must match: a hostname or a bracketed IPv6 literal, optionally followed by a decimal port in
    1..65535. Userinfo, paths, whitespace, suffix text, empty/odd ports and bare IPv6 are all invalid."""
    if not isinstance(header, str):
        return None
    match = HOST_AUTHORITY.fullmatch(header)
    if match is None:
        return None
    if match["port"] is not None and not 1 <= int(match["port"]) <= 65535:
        return None
    if match["v6"] is not None:
        try:
            return ipaddress.IPv6Address(match["v6"][1:-1]).compressed
        except ValueError:
            return None
    return match["name"].lower()


def checked_hosts(extra: frozenset[str]) -> frozenset[str]:
    if any(not isinstance(h, str) or HOST_NAME.fullmatch(h) is None for h in extra):
        raise ValueError("Test host overrides must be explicit plain host names, never wildcards or authorities")
    return LOOPBACK_HOSTS | frozenset(h.lower() for h in extra)


def checked_origins(extra: frozenset[str]) -> frozenset[str]:
    if any(not isinstance(o, str) or "*" in o or o == "null" or not o.startswith("http://") for o in extra):
        raise ValueError("Test origin overrides must be explicit http:// origins, never wildcards or null")
    return DEFAULT_ALLOWED_ORIGINS | frozenset(o.lower() for o in extra)


def create_app(db_path: Path | None = None, workspace_root: Path | None = None, busy_timeout_ms: int = 5000, *,
               auth_provider: authz.AuthenticationProvider | None = None,
               authorization_policy: authz.AuthorizationPolicy | None = None,
               extra_allowed_hosts: frozenset[str] = frozenset(),
               extra_allowed_origins: frozenset[str] = frozenset(),
               architect_runtime: ArchitectRuntime | None = None) -> FastAPI:
    """``auth_provider``/``authorization_policy`` and the ``extra_allowed_*`` sets are Python-level composition and
    test seams only. Nothing in a request (body, query, header, cookie) can select an actor, a role or these values,
    and there is deliberately no environment knob."""
    provider = auth_provider or authz.LocalOwnerProvider()
    policy = authorization_policy or authz.LocalOwnerPolicy()
    allowed_hosts = checked_hosts(extra_allowed_hosts)
    allowed_origins = checked_origins(extra_allowed_origins)
    base = data_dir() if db_path is None or workspace_root is None else None
    db_path = db_path or base / "rosc.db"
    workspace_root = workspace_root or base / "workspace"
    sessions = make_session_factory(db_path, busy_timeout_ms)
    registry = AgentRegistryService(sessions, policy)
    conversation = ProjectConversationService(sessions, policy)
    # The runtime is a Python-level composition seam like the policy: no request can select or configure it.
    architect = ArchitectService(sessions, policy, architect_runtime or OpenCodeRuntime(db_path.parent))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            architect.recover()  # database-only: pending work of a dead process becomes unknown, nothing is re-sent
        except OperationalError:
            log.warning("Architect recovery skipped: database is not migrated")
        yield

    app = FastAPI(title="ROSC Core", version="0.1.0", lifespan=lifespan)
    app.state.architect = architect

    @app.exception_handler(RequestValidationError)
    async def validation_failed(request: Request, exc: RequestValidationError):
        # Scoped to the conversation routes so other APIs keep the framework's default 422 contract.
        if CONVERSATION_PATH.match(request.url.path):
            return sanitized_validation_response(exc)
        return await request_validation_exception_handler(request, exc)

    @app.middleware("http")
    async def local_request_guard(request: Request, call_next):
        """Loopback-oriented defence against DNS rebinding and cross-site browser requests. It is NOT authentication:
        a direct non-browser local caller sends no Origin and is accepted, and it still only ever receives the
        ``local_unverified`` Owner context. Forwarded/X-Forwarded-* headers are never consulted."""
        problem = None
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or host_name(hosts[0]) not in allowed_hosts:
            problem = "Host is not allowed"
        elif request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            problem = "Cross-site requests are not allowed"
        else:
            origin = request.headers.get("origin")
            if origin is not None and origin.lower() not in allowed_origins:
                problem = "Origin is not allowed"
        if problem:
            return JSONResponse({"detail": problem}, status_code=403)
        return await call_next(request)

    def current_actor() -> authz.ActorContext:
        """Resolve the trusted actor from the application-selected provider. Never from request data."""
        try:
            return provider.actor()
        except Exception as exc:  # fail closed: no policy allow, no Core read or mutation
            log.warning("Authentication provider failed: %s", type(exc).__name__)
            raise HTTPException(503, "Actor context unavailable") from None

    def authorize(action: str, **scope) -> None:
        """Application-owned actor resolution + policy check; runs before any lookup, SQL write or Workspace access."""
        decision = authz.evaluate(policy, current_actor(), action, authz.ResourceScope(**scope))
        if not decision.allowed:
            raise HTTPException(403, "Not authorized for this operation")

    def registry_call(call, *args, **kwargs):
        """Run one administrative service call and translate its boundary errors into HTTP contract errors."""
        try:
            return call(*args, **kwargs)
        except RegistryError as exc:
            raise registry_error(exc) from None

    def conversation_call(call, *args, **kwargs):
        """Run one conversation service call and translate its boundary errors into HTTP contract errors."""
        try:
            return call(*args, **kwargs)
        except ConversationError as exc:
            raise conversation_error(exc) from None

    @app.get("/api/health")
    def health() -> dict:
        return {"service": "rosc", "engine_slice": 1, "agent_runtime": "not_started"}

    @app.get("/api/projects")
    def list_projects() -> list[dict]:
        authorize(authz.PROJECT_LIST)
        with sessions() as session:
            return [project_view(p, workspace_root) for p in session.scalars(select(Project).order_by(Project.id))]

    @app.post("/api/projects", status_code=201)
    def create_project(data: ProjectInput) -> dict:
        authorize(authz.PROJECT_REGISTER)
        root = validate_root(data)
        with sessions() as session:
            try:
                # Single-writer critical section: take the write lock BEFORE reading existing roots so that
                # parent/child/same-path registrations from independent processes cannot interleave.
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                check_overlap(root, session)
                project = Project(name=data.name, description=data.description, root_path=root,
                                  registration_root_path=root, project_uuid=str(uuid.uuid4()),
                                  mode=data.mode, docs_entrypoint=data.docs_entrypoint, workspace_state="provisioning")
                # The initial version 1 current binding is committed with the Project or not at all.
                project.bindings.append(ProjectRootBinding(version=1, root_path=root, docs_entrypoint=data.docs_entrypoint,
                                                           mode=data.mode, state=CURRENT_BINDING))
                session.add(project)
                session.commit()
            except OperationalError:
                session.rollback()
                raise HTTPException(503, "Another registration is in progress; retry shortly") from None
            except IntegrityError:
                session.rollback()
                raise HTTPException(409, "Project root already registered") from None
            try:
                provision_project(workspace_root, project)
            except WorkspaceConflict as exc:
                raise HTTPException(409, f"Project P{project.id:04d} registered, but its Workspace manifest collides: {conflict_detail(exc)}") from exc
            except OSError as exc:
                raise HTTPException(503, f"Project P{project.id:04d} registered; Workspace provisioning failed. Retry reconciliation.") from exc
            project.workspace_state = "ready"
            session.commit()
            return project_view(project, workspace_root)

    def load_project(session, project_id: str) -> Project:
        project = session.get(Project, numeric_id(project_id, "P"))
        if not project:
            raise HTTPException(404, "Project not found")
        return project

    @app.post("/api/projects/{project_id}/reconcile-workspace")
    def reconcile_project(project_id: str) -> dict:
        authorize(authz.PROJECT_RECONCILE, project_id=numeric_id(project_id, "P"))
        with sessions() as session:
            project = load_project(session, project_id)
            require_project_identity(project)
            try:
                provision_project(workspace_root, project)  # rebuilds a missing manifest only; never replaces one
            except WorkspaceConflict as exc:
                raise HTTPException(409, conflict_detail(exc)) from exc
            except OSError as exc:
                raise HTTPException(503, "Workspace is not writable; retry reconciliation") from exc
            project.workspace_state = "ready"
            session.commit()
            return project_view(project, workspace_root)

    @app.post("/api/projects/{project_id}/repair-workspace")
    def repair_project_workspace(project_id: str, data: RepairInput) -> dict:
        authorize(authz.PROJECT_REPAIR, project_id=numeric_id(project_id, "P"))
        require_confirmation(data)
        with sessions() as session:
            project = load_project(session, project_id)
            require_project_identity(project)
            try:
                record = repair_project(workspace_root, project)
            except WorkspaceConflict as exc:
                raise HTTPException(409, conflict_detail(exc)) from exc
            except WorkspaceUncertain as exc:
                raise HTTPException(503, f"Workspace repair outcome is uncertain: {exc}") from exc
            except (WorkspaceError, OSError) as exc:
                raise HTTPException(503, f"Workspace repair failed and the original manifest is preserved: {exc}") from exc
            project.workspace_state = "ready"
            session.commit()
            return repair_response(project_view(project, workspace_root), record)

    @app.get("/api/projects/{project_id}/tasks")
    def list_tasks(project_id: str) -> list[dict]:
        pid = numeric_id(project_id, "P")
        authorize(authz.TASK_LIST, project_id=pid)
        with sessions() as session:
            if not session.get(Project, pid):
                raise HTTPException(404, "Project not found")
            return [task_view(t, workspace_root) for t in session.scalars(select(Task).where(Task.project_id == pid).order_by(Task.id))]

    @app.post("/api/projects/{project_id}/tasks", status_code=201)
    def create_task(project_id: str, data: TaskInput) -> dict:
        pid = numeric_id(project_id, "P")
        authorize(authz.TASK_CREATE, project_id=pid)
        with sessions() as session:
            project = session.get(Project, pid)
            if not project:
                raise HTTPException(404, "Project not found")
            require_project_identity(project)
            if project.workspace_state != "ready" or inspect_project(workspace_root, project).problem != READY:
                raise HTTPException(409, "Project Workspace requires reconciliation")
            task = Task(project_id=pid, title=data.title, description=data.description,
                        acceptance_criteria=data.acceptance_criteria, status="Draft", workspace_state="provisioning")
            session.add(task)
            session.commit()
            try:
                provision_task(workspace_root, task)
            except WorkspaceConflict as exc:
                raise HTTPException(409, f"Task T{task.id:04d} saved, but its Workspace manifest collides: {conflict_detail(exc)}") from exc
            except OSError as exc:
                raise HTTPException(503, f"Task T{task.id:04d} saved; Task Workspace provisioning failed") from exc
            task.workspace_state = "ready"
            session.commit()
            return task_view(task, workspace_root)

    def load_task(session, project_id: str, task_id: str) -> Task:
        pid, tid = numeric_id(project_id, "P"), numeric_id(task_id, "T")
        task = session.get(Task, tid)
        if not task or task.project_id != pid:
            raise HTTPException(404, "Task not found")
        return task

    @app.post("/api/projects/{project_id}/tasks/{task_id}/reconcile-workspace")
    def reconcile_task(project_id: str, task_id: str) -> dict:
        authorize(authz.TASK_RECONCILE, project_id=numeric_id(project_id, "P"), task_id=numeric_id(task_id, "T"))
        with sessions() as session:
            task = load_task(session, project_id, task_id)
            require_project_identity(session.get(Project, task.project_id))
            try:
                provision_task(workspace_root, task)
            except WorkspaceConflict as exc:
                raise HTTPException(409, conflict_detail(exc)) from exc
            except OSError as exc:
                raise HTTPException(503, "Workspace is not writable; retry reconciliation") from exc
            task.workspace_state = "ready"
            session.commit()
            return task_view(task, workspace_root)

    @app.post("/api/projects/{project_id}/tasks/{task_id}/repair-workspace")
    def repair_task_workspace(project_id: str, task_id: str, data: RepairInput) -> dict:
        authorize(authz.TASK_REPAIR, project_id=numeric_id(project_id, "P"), task_id=numeric_id(task_id, "T"))
        require_confirmation(data)
        with sessions() as session:
            task = load_task(session, project_id, task_id)
            require_project_identity(session.get(Project, task.project_id))
            try:
                record = repair_task(workspace_root, task)
            except WorkspaceConflict as exc:
                raise HTTPException(409, conflict_detail(exc)) from exc
            except WorkspaceUncertain as exc:
                raise HTTPException(503, f"Workspace repair outcome is uncertain: {exc}") from exc
            except (WorkspaceError, OSError) as exc:
                raise HTTPException(503, f"Workspace repair failed and the original manifest is preserved: {exc}") from exc
            task.workspace_state = "ready"
            session.commit()
            return repair_response(task_view(task, workspace_root), record)

    # -- M2-C Owner-only Agent registry and scoped assignments (no agent-facing surface) ----------------------
    @app.get("/api/agents")
    def list_agents() -> list[dict]:
        return registry_call(registry.list_agents, current_actor())

    @app.post("/api/agents", status_code=201)
    def register_agent(data: AgentInput) -> dict:
        return registry_call(registry.register_agent, current_actor(), display_name=data.display_name,
                             icon_label=data.icon_label, runtime_adapter=data.runtime_adapter,
                             provider_id=data.provider_id, model_id=data.model_id,
                             capabilities=data.capabilities)

    @app.get("/api/agents/{agent_id}")
    def get_agent(agent_id: str) -> dict:
        return registry_call(registry.get_agent, current_actor(), agent_path_id(agent_id))

    @app.patch("/api/agents/{agent_id}")
    def update_agent(agent_id: str, data: AgentUpdateInput) -> dict:
        return registry_call(registry.update_agent, current_actor(), agent_path_id(agent_id),
                             expected_version=data.expected_version, display_name=data.display_name,
                             icon_label=data.icon_label, runtime_adapter=data.runtime_adapter,
                             provider_id=data.provider_id, model_id=data.model_id,
                             capabilities=data.capabilities)

    @app.get("/api/projects/{project_id}/assignments")
    def list_assignments(project_id: str, include_revoked: bool = False) -> list[dict]:
        pid = numeric_id(project_id, "P")
        return registry_call(registry.list_assignments, current_actor(), pid, include_revoked=include_revoked)

    @app.post("/api/projects/{project_id}/assignments", status_code=201)
    def create_assignment(project_id: str, data: AssignmentInput) -> dict:
        pid = numeric_id(project_id, "P")
        task_id = None
        if data.task_id is not None:
            try:
                task_id = numeric_id(data.task_id, "T")
            except HTTPException:
                raise HTTPException(422, "Task id must be canonical (T0001 or T10000)") from None
        return registry_call(registry.create_assignment, current_actor(), pid, agent_id=data.agent_id,
                             role=data.role, task_id=task_id, capabilities=data.capabilities)

    @app.post("/api/projects/{project_id}/assignments/{assignment_id}/revoke")
    def revoke_assignment(project_id: str, assignment_id: str, data: RevokeInput) -> dict:
        pid = numeric_id(project_id, "P")
        return registry_call(registry.revoke_assignment, current_actor(), pid, agent_path_id(assignment_id),
                             expected_version=data.expected_version)

    # -- M3-A durable Owner-only Project Conversation (no agent, delivery or activation surface) ---------------
    @app.get("/api/projects/{project_id}/conversation")
    def read_project_conversation(project_id: str, before: int | None = None,
                                  limit: int = DEFAULT_PAGE_SIZE) -> dict:
        pid = numeric_id(project_id, "P")
        return conversation_call(conversation.read_conversation, current_actor(), pid, before=before, limit=limit)

    @app.post("/api/projects/{project_id}/conversation/messages", status_code=201)
    def create_project_conversation_message(project_id: str, data: ConversationMessageInput,
                                            response: Response) -> dict:
        pid = numeric_id(project_id, "P")
        # The sender is the trusted local Owner context, never a request-supplied id, role or session.
        result = conversation_call(conversation.create_message, current_actor(), pid,
                                   client_key=data.client_key, body=data.body)
        if result["replayed"]:
            response.status_code = 200
            return result
        # Only a NEW message may be delivered, and only to a live verified session; history is never replayed.
        result["message"]["delivery"] = architect.submit(current_actor(), pid, result["message"]["message_id"])
        return result

    # -- M3-B explicit Project Architect activation, liveness and delivery recovery ----------------------------
    @app.get("/api/projects/{project_id}/architect")
    def architect_status(project_id: str) -> dict:
        return conversation_call(architect.status, current_actor(), numeric_id(project_id, "P"))

    @app.post("/api/projects/{project_id}/architect/activate")
    def architect_activate(project_id: str) -> dict:
        return conversation_call(architect.activate, current_actor(), numeric_id(project_id, "P"))

    @app.post("/api/projects/{project_id}/architect/deactivate")
    def architect_deactivate(project_id: str) -> dict:
        return conversation_call(architect.deactivate, current_actor(), numeric_id(project_id, "P"))

    @app.post("/api/projects/{project_id}/conversation/messages/{message_id}/reconcile")
    def reconcile_delivery(project_id: str, message_id: str) -> dict:
        return {"delivery": conversation_call(architect.reconcile, current_actor(),
                                              numeric_id(project_id, "P"), message_id)}

    @app.post("/api/projects/{project_id}/conversation/messages/{message_id}/retry")
    def retry_delivery(project_id: str, message_id: str) -> dict:
        return {"delivery": conversation_call(architect.retry, current_actor(), numeric_id(project_id, "P"),
                                              message_id)}

    return app


app = create_app()
