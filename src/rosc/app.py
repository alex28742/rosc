from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import data_dir
from .db import make_session_factory
from .models import Project, Task
from .workspace import manifest_matches, provision_project, provision_task, task_manifest_matches, task_manifest_path


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


def project_view(project: Project, workspace_root: Path) -> dict:
    return {
        "id": f"P{project.id:04d}", "name": project.name, "description": project.description,
        "root_path": project.root_path, "mode": project.mode, "docs_entrypoint": project.docs_entrypoint,
        "workspace_state": "ready" if project.workspace_state == "ready" and manifest_matches(workspace_root, project) else "needs_reconciliation",
        "architect": "not_active", "active_workers": 0, "created_at": project.created_at,
    }


def task_view(task: Task, workspace_root: Path) -> dict:
    return {"id": f"T{task.id:04d}", "project_id": f"P{task.project_id:04d}", "title": task.title,
            "description": task.description, "acceptance_criteria": task.acceptance_criteria,
            "status": task.status, "workspace_state": "ready" if task.workspace_state == "ready" and task_manifest_matches(workspace_root, task) else "needs_reconciliation",
            "worker": "not_assigned", "created_at": task.created_at}


def numeric_id(value: str, prefix: str) -> int:
    if not value.startswith(prefix) or not value[1:].isdigit():
        raise HTTPException(404, "Not found")
    return int(value[1:])


def validate_root(data: ProjectInput, session) -> str:
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
    for other in session.scalars(select(Project.root_path)):
        path = Path(other)
        if path == root or path in root.parents or root in path.parents:
            raise HTTPException(409, "Project root overlaps another registered Project")
    return str(root)


def create_app(db_path: Path | None = None, workspace_root: Path | None = None) -> FastAPI:
    base = data_dir() if db_path is None or workspace_root is None else None
    db_path = db_path or base / "rosc.db"
    workspace_root = workspace_root or base / "workspace"
    sessions = make_session_factory(db_path)
    app = FastAPI(title="ROSC Core", version="0.1.0")

    @app.get("/api/health")
    def health() -> dict:
        return {"service": "rosc", "engine_slice": 1, "agent_runtime": "not_started"}

    @app.get("/api/projects")
    def list_projects() -> list[dict]:
        with sessions() as session:
            return [project_view(p, workspace_root) for p in session.scalars(select(Project).order_by(Project.id))]

    @app.post("/api/projects", status_code=201)
    def create_project(data: ProjectInput) -> dict:
        with sessions() as session:
            root = validate_root(data, session)
            project = Project(name=data.name, description=data.description, root_path=root,
                              mode=data.mode, docs_entrypoint=data.docs_entrypoint, workspace_state="provisioning")
            session.add(project)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise HTTPException(409, "Project root already registered") from None
            try:
                provision_project(workspace_root, project)
            except OSError as exc:
                raise HTTPException(503, f"Project P{project.id:04d} registered; Workspace provisioning failed. Retry reconciliation.") from exc
            project.workspace_state = "ready"
            session.commit()
            return project_view(project, workspace_root)

    @app.post("/api/projects/{project_id}/reconcile-workspace")
    def reconcile_project(project_id: str) -> dict:
        with sessions() as session:
            project = session.get(Project, numeric_id(project_id, "P"))
            if not project:
                raise HTTPException(404, "Project not found")
            if not manifest_matches(workspace_root, project):
                # A mismatch is a conflict; a missing manifest can be rebuilt from canonical DB.
                path = workspace_root / "projects" / f"P{project.id:04d}" / "project.yaml"
                if path.exists():
                    raise HTTPException(409, "Workspace manifest conflicts with canonical Project record")
                provision_project(workspace_root, project)
            project.workspace_state = "ready"
            session.commit()
            return project_view(project, workspace_root)

    @app.get("/api/projects/{project_id}/tasks")
    def list_tasks(project_id: str) -> list[dict]:
        pid = numeric_id(project_id, "P")
        with sessions() as session:
            if not session.get(Project, pid):
                raise HTTPException(404, "Project not found")
            return [task_view(t, workspace_root) for t in session.scalars(select(Task).where(Task.project_id == pid).order_by(Task.id))]

    @app.post("/api/projects/{project_id}/tasks", status_code=201)
    def create_task(project_id: str, data: TaskInput) -> dict:
        pid = numeric_id(project_id, "P")
        with sessions() as session:
            project = session.get(Project, pid)
            if not project:
                raise HTTPException(404, "Project not found")
            if project.workspace_state != "ready" or not manifest_matches(workspace_root, project):
                raise HTTPException(409, "Project Workspace requires reconciliation")
            task = Task(project_id=pid, title=data.title, description=data.description,
                        acceptance_criteria=data.acceptance_criteria, status="Draft", workspace_state="provisioning")
            session.add(task)
            session.commit()
            try:
                provision_task(workspace_root, task)
            except OSError as exc:
                raise HTTPException(503, f"Task T{task.id:04d} saved; Task Workspace provisioning failed") from exc
            task.workspace_state = "ready"
            session.commit()
            return task_view(task, workspace_root)

    @app.post("/api/projects/{project_id}/tasks/{task_id}/reconcile-workspace")
    def reconcile_task(project_id: str, task_id: str) -> dict:
        pid, tid = numeric_id(project_id, "P"), numeric_id(task_id, "T")
        with sessions() as session:
            task = session.get(Task, tid)
            if not task or task.project_id != pid:
                raise HTTPException(404, "Task not found")
            if not task_manifest_matches(workspace_root, task):
                if task_manifest_path(workspace_root, task).exists():
                    raise HTTPException(409, "Task manifest conflicts with canonical Task record")
                provision_task(workspace_root, task)
            task.workspace_state = "ready"
            session.commit()
            return task_view(task, workspace_root)

    return app


app = create_app()
