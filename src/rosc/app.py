from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from . import authorization as authz
from .config import data_dir
from .db import make_session_factory
from .models import (CURRENT_BINDING, Project, ProjectIdentityError, ProjectRootBinding, Task, reserved_roots,
                     validate_project_identity)
from .workspace import (MAX_MANIFEST_BYTES, READY, WorkspaceConflict, WorkspaceError, WorkspaceUncertain, inspect_project, inspect_task, provision_project,
                        provision_task, repair_project, repair_task)


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


def conflict_detail(exc: WorkspaceConflict) -> str:
    if exc.problem in ("corrupt", "drift"):
        return f"Workspace manifest is {exc.problem}; use the Owner-confirmed repair action. {exc.message}"
    if exc.problem == "oversized":
        return f"Workspace manifest is larger than the {MAX_MANIFEST_BYTES} byte limit and cannot be backed up automatically; manual resolution is required. It was left untouched."
    return f"Workspace manifest conflicts with the canonical record ({exc.problem}); manual resolution is required. {exc.message}"


class RepairInput(BaseModel):
    confirm_repair: bool = False


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


def host_name(header: str) -> str:
    """Hostname of a Host header value without port; IPv6 literals keep their brackets stripped."""
    value = header.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else ""
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def checked_hosts(extra: frozenset[str]) -> frozenset[str]:
    if any(not isinstance(h, str) or not h or "*" in h or "/" in h for h in extra):
        raise ValueError("Test host overrides must be explicit host names, never wildcards")
    return LOOPBACK_HOSTS | frozenset(h.lower() for h in extra)


def checked_origins(extra: frozenset[str]) -> frozenset[str]:
    if any(not isinstance(o, str) or "*" in o or o == "null" or not o.startswith("http://") for o in extra):
        raise ValueError("Test origin overrides must be explicit http:// origins, never wildcards or null")
    return DEFAULT_ALLOWED_ORIGINS | frozenset(o.lower() for o in extra)


def create_app(db_path: Path | None = None, workspace_root: Path | None = None, busy_timeout_ms: int = 5000, *,
               auth_provider: authz.AuthenticationProvider | None = None,
               authorization_policy: authz.AuthorizationPolicy | None = None,
               extra_allowed_hosts: frozenset[str] = frozenset(),
               extra_allowed_origins: frozenset[str] = frozenset()) -> FastAPI:
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
    app = FastAPI(title="ROSC Core", version="0.1.0")

    @app.middleware("http")
    async def local_request_guard(request: Request, call_next):
        """Loopback-oriented defence against DNS rebinding and cross-site browser requests. It is NOT authentication:
        a direct non-browser local caller sends no Origin and is accepted, and it still only ever receives the
        ``local_unverified`` Owner context. Forwarded/X-Forwarded-* headers are never consulted."""
        problem = None
        if host_name(request.headers.get("host", "")) not in allowed_hosts:
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

    def authorize(action: str, **scope) -> None:
        """Application-owned actor resolution + policy check; runs before any lookup, SQL write or Workspace access."""
        decision = authz.evaluate(policy, provider.actor(), action, authz.ResourceScope(**scope))
        if not decision.allowed:
            raise HTTPException(403, "Not authorized for this operation")

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

    return app


app = create_app()
