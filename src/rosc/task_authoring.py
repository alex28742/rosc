"""Guarded Core Authoring Service for Task Spec revisions and Prompt Draft revisions (P040).

One service is used by the Owner (UI/API, authorized through ``AuthorizationPolicy``) and by the Project Architect (scoped
Tools, identified by an ``ArchitectAuthor`` that only ``ProjectContextBroker`` can build from a verified live session).
Revisions are immutable rows; the Spec head, ``stale`` and ``approved`` are derived from them.  Approval of a Prompt
revision is Owner-only and is NOT a Run authorization: this module never creates a Run, Execution Package, lease or
Worker session.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from . import authorization as authz
from . import rich_text
from .models import (PROVENANCE_ARCHITECT, PROVENANCE_OWNER, Project, PromptApproval, PromptRevision, Task,
                     TaskSpecRevision, ProjectIdentityError, validate_project_identity)
from .workspace import READY, WorkspaceConflict, inspect_project, provision_task

MAX_PROMPT_CHARS = 100_000
MAX_SUMMARY_CHARS = 8_000
EDITABLE_STATUS = "Draft"


class AuthoringError(Exception):
    code = "authoring_error"
    http = 503


class AuthoringDenied(AuthoringError):
    code, http = "forbidden", 403


class AuthoringNotFound(AuthoringError):
    code, http = "not_found", 404


class AuthoringInvalid(AuthoringError):
    code, http = "invalid", 422


class AuthoringConflict(AuthoringError):
    """``code`` is one of: spec_conflict, prompt_conflict, source_not_current, prompt_stale, not_head, not_editable,
    digest_mismatch, project_unavailable."""

    http = 409

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AuthoringUnavailable(AuthoringError):
    """Project identity records are contradictory: fail closed with the established 503 contract."""

    code, http = "project_unavailable", 503


@dataclass(frozen=True)
class ArchitectAuthor:
    """Trusted identity of the live Project Architect host.  Not a TCA identity: the Tool token identifies the host."""

    project_id: int
    agent_id: str
    session_id: str


_ARCHITECT_ACTIONS = frozenset({authz.TASK_CREATE, authz.TASK_SPEC_READ, authz.TASK_SPEC_SAVE,
                                authz.PROMPT_DRAFT_READ, authz.PROMPT_DRAFT_SAVE})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def spec_digest(title: str, scope: str, description_html: str, criteria_html: str) -> str:
    return hashlib.sha256(json.dumps([title, scope, description_html, criteria_html], ensure_ascii=False).encode()).hexdigest()


def prompt_digest(source_spec_revision: int, summary: str, body: str) -> str:
    return hashlib.sha256(json.dumps([source_spec_revision, summary, body], ensure_ascii=False).encode()).hexdigest()


def _text(value: object, field: str, *, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise AuthoringInvalid(f"{field} must be text")
    value = value.replace("\r\n", "\n").strip()
    if required and not value:
        raise AuthoringInvalid(f"{field} is required")
    if len(value) > limit:
        raise AuthoringInvalid(f"{field} is longer than {limit} characters")
    return value


def _rich(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise AuthoringInvalid(f"{field} must be text")
    if len(value) > rich_text.MAX_FIELD_CHARS * 4:
        raise AuthoringInvalid(f"{field} is too long")
    canonical = rich_text.canonicalize(value)
    if not canonical:
        raise AuthoringInvalid(f"{field} is required")
    if len(rich_text.to_text(canonical)) > rich_text.MAX_FIELD_CHARS:
        raise AuthoringInvalid(f"{field} is longer than {rich_text.MAX_FIELD_CHARS} characters")
    return canonical


def _int(value: object, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AuthoringInvalid(f"{field} must be an integer >= {minimum}")
    return value


def _actor(author: object) -> tuple[str, str, str, str]:
    """(provenance, kind, id, assurance) of the writer."""
    if isinstance(author, ArchitectAuthor):
        return PROVENANCE_ARCHITECT, "architect", author.agent_id, "adapter_verified"
    assert isinstance(author, authz.ActorContext)
    return PROVENANCE_OWNER, author.actor_kind, author.actor_id, author.assurance


def spec_view(row: TaskSpecRevision, *, head: bool = False) -> dict:
    return {"revision": row.revision, "title": row.title, "scope": row.scope, "description_html": row.description_html,
            "criteria_html": row.criteria_html, "description_text": rich_text.to_text(row.description_html),
            "acceptance_criteria_text": rich_text.to_text(row.criteria_html), "digest": row.digest,
            "provenance": row.provenance, "actor": {"kind": row.actor_kind, "id": row.actor_id},
            "created_at": _iso(row.created_at), "current": head}


def prompt_view(row: PromptRevision, *, state: str, approval: PromptApproval | None, body: bool = True) -> dict:
    view = {"revision": row.revision, "source_spec_revision": row.source_spec_revision, "summary": row.summary,
            "digest": row.digest, "rules_ref": row.rules_ref, "state": state,
            "actor": {"kind": row.actor_kind, "id": row.actor_id}, "created_at": _iso(row.created_at),
            "approval": None if approval is None else {
                "spec_revision": approval.spec_revision, "prompt_revision": approval.prompt_revision,
                "digest": approval.digest, "actor": {"kind": approval.actor_kind, "id": approval.actor_id},
                "created_at": _iso(approval.created_at)}}
    if body:
        view["body"] = row.body
    return view


class TaskAuthoringService:
    def __init__(self, session_factory: sessionmaker[Session], policy: authz.AuthorizationPolicy,
                 workspace_root: Path) -> None:
        self._sessions = session_factory
        self._policy = policy
        self._workspace = workspace_root

    # -- guards -----------------------------------------------------------------------------------------------------
    def _authorize(self, author: object, action: str, project_id: int, task_id: int | None = None) -> None:
        if isinstance(author, ArchitectAuthor):
            if action not in _ARCHITECT_ACTIONS or author.project_id != project_id:
                raise AuthoringDenied("Not authorized for this operation")
            return
        scope = {"project_id": project_id}
        if action != authz.TASK_CREATE:
            scope["task_id"] = task_id  # type: ignore[assignment]
        decision = authz.evaluate(self._policy, author, action, authz.ResourceScope(**scope))
        if not decision.allowed:
            raise AuthoringDenied("Not authorized for this operation")

    @staticmethod
    def _project(session: Session, project_id: int) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise AuthoringNotFound("Project not found")
        try:
            validate_project_identity(project)
        except ProjectIdentityError:
            raise AuthoringUnavailable("Project identity records are inconsistent; manual reconciliation is required") from None
        return project

    def _task(self, session: Session, project_id: int, task_id: int, *, editable: bool = False) -> Task:
        task = session.get(Task, task_id)
        if task is None or task.project_id != project_id:
            raise AuthoringNotFound("Task not found")
        if editable and task.status != EDITABLE_STATUS:
            raise AuthoringConflict("not_editable", "The Task is no longer in the Draft stage")
        return task

    @staticmethod
    def _spec_head(session: Session, task_id: int) -> TaskSpecRevision | None:
        return session.scalars(select(TaskSpecRevision).where(TaskSpecRevision.task_id == task_id)
                               .order_by(TaskSpecRevision.revision.desc()).limit(1)).first()

    @staticmethod
    def _prompt_head(session: Session, task_id: int) -> PromptRevision | None:
        return session.scalars(select(PromptRevision).where(PromptRevision.task_id == task_id)
                               .order_by(PromptRevision.revision.desc()).limit(1)).first()

    @staticmethod
    def _approval(session: Session, prompt_id: int) -> PromptApproval | None:
        return session.scalars(select(PromptApproval).where(PromptApproval.prompt_revision_id == prompt_id)).first()

    @staticmethod
    def _state(row: PromptRevision, spec_head: TaskSpecRevision, approval: PromptApproval | None) -> str:
        if row.source_spec_revision != spec_head.revision:
            return "stale"
        return "approved" if approval is not None else "unapproved"

    # -- Task + Spec v1 ---------------------------------------------------------------------------------------------
    def create_task(self, author: object, project_id: int, *, title: object, description: object,
                    acceptance_criteria: object, scope: object = "") -> dict:
        """Create a Draft Task with Spec revision 1 (Owner API or Architect Tool).  Starts no Run and assigns no Worker."""
        self._authorize(author, authz.TASK_CREATE, project_id)
        title = _text(title, "title", limit=160)
        scope = _text(scope, "scope", limit=rich_text.MAX_FIELD_CHARS, required=False)
        description, criteria = _rich(description, "description"), _rich(acceptance_criteria, "acceptance_criteria")
        provenance, kind, actor_id, assurance = _actor(author)
        with self._sessions() as session:
            project = self._project(session, project_id)
            if project.workspace_state != "ready" or inspect_project(self._workspace, project).problem != READY:
                raise AuthoringConflict("project_unavailable", "Project Workspace requires reconciliation")
            task = Task(project_id=project_id, title=title, description=rich_text.to_text(description),
                        acceptance_criteria=rich_text.to_text(criteria), status=EDITABLE_STATUS,
                        workspace_state="provisioning")
            session.add(task)
            session.flush()
            session.add(TaskSpecRevision(
                project_id=project_id, task_id=task.id, revision=1, title=title, scope=scope, description_html=description,
                criteria_html=criteria, digest=spec_digest(title, scope, description, criteria), provenance=provenance,
                actor_kind=kind, actor_id=actor_id, actor_assurance=assurance))
            session.commit()
            try:
                provision_task(self._workspace, task)
            except WorkspaceConflict as exc:
                raise AuthoringConflict("project_unavailable", f"Task T{task.id:04d} saved, but its Workspace manifest collides") from exc
            except OSError as exc:
                raise AuthoringConflict("project_unavailable", f"Task T{task.id:04d} saved; Workspace provisioning failed") from exc
            task.workspace_state = "ready"
            session.commit()
            return {"task": task, "task_id": f"T{task.id:04d}", "spec_revision": 1}

    # -- Spec --------------------------------------------------------------------------------------------------------
    def save_spec(self, author: object, project_id: int, task_id: int, *, expected_revision: object, title: object,
                  description: object, acceptance_criteria: object, scope: object = None) -> dict:
        self._authorize(author, authz.TASK_SPEC_SAVE, project_id, task_id)
        expected = _int(expected_revision, "expected_revision")
        title = _text(title, "title", limit=160)
        description, criteria = _rich(description, "description"), _rich(acceptance_criteria, "acceptance_criteria")
        provenance, kind, actor_id, assurance = _actor(author)
        with self._sessions() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                self._project(session, project_id)
                task = self._task(session, project_id, task_id, editable=True)
                head = self._spec_head(session, task_id)
                if head is None:
                    raise AuthoringNotFound("Task Spec not found")
                scope = head.scope if scope is None else _text(scope, "scope", limit=rich_text.MAX_FIELD_CHARS, required=False)
                if head.revision != expected:
                    raise AuthoringConflict("spec_conflict", f"The Spec is at revision {head.revision}, not {expected}")
                digest = spec_digest(title, scope, description, criteria)
                if digest == head.digest:
                    return {"created": False, "spec": spec_view(head, head=True)}
                row = TaskSpecRevision(
                    project_id=project_id, task_id=task_id, revision=head.revision + 1, title=title, scope=scope,
                    description_html=description, criteria_html=criteria, digest=digest, provenance=provenance,
                    actor_kind=kind, actor_id=actor_id, actor_assurance=assurance)
                session.add(row)
                task.title, task.description = title, rich_text.to_text(description)
                task.acceptance_criteria = rich_text.to_text(criteria)
                session.commit()
                return {"created": True, "spec": spec_view(row, head=True)}
            except OperationalError:
                session.rollback()
                raise AuthoringConflict("spec_conflict", "Another Spec change is in progress; retry") from None
            except IntegrityError:
                session.rollback()
                raise AuthoringConflict("spec_conflict", "The Spec changed concurrently") from None

    def read_spec(self, author: object, project_id: int, task_id: int, revision: int | None = None) -> dict:
        self._authorize(author, authz.TASK_SPEC_READ, project_id, task_id)
        with self._sessions() as session:
            self._task(session, project_id, task_id)
            head = self._spec_head(session, task_id)
            if head is None:
                raise AuthoringNotFound("Task Spec not found")
            row = head if revision is None else session.scalars(select(TaskSpecRevision).where(
                TaskSpecRevision.task_id == task_id, TaskSpecRevision.revision == revision)).first()
            if row is None:
                raise AuthoringNotFound("Spec revision not found")
            return spec_view(row, head=row.revision == head.revision)

    def list_spec_revisions(self, author: object, project_id: int, task_id: int) -> list[dict]:
        self._authorize(author, authz.TASK_SPEC_READ, project_id, task_id)
        with self._sessions() as session:
            self._task(session, project_id, task_id)
            rows = session.scalars(select(TaskSpecRevision).where(TaskSpecRevision.task_id == task_id)
                                   .order_by(TaskSpecRevision.revision)).all()
            head = rows[-1].revision if rows else None
            return [{k: v for k, v in spec_view(r, head=r.revision == head).items()
                     if k not in ("description_html", "criteria_html", "description_text", "acceptance_criteria_text")}
                    for r in rows]

    # -- Prompt Draft ------------------------------------------------------------------------------------------------
    def save_prompt(self, author: object, project_id: int, task_id: int, *, expected_revision: object,
                    source_spec_revision: object, body: object, summary: object) -> dict:
        self._authorize(author, authz.PROMPT_DRAFT_SAVE, project_id, task_id)
        expected = None if expected_revision is None else _int(expected_revision, "expected_revision", minimum=0)
        source = _int(source_spec_revision, "source_spec_revision")
        body = _text(body, "body", limit=MAX_PROMPT_CHARS)
        summary = _text(summary, "summary", limit=MAX_SUMMARY_CHARS)
        _, kind, actor_id, assurance = _actor(author)
        with self._sessions() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                self._project(session, project_id)
                self._task(session, project_id, task_id, editable=True)
                spec_head = self._spec_head(session, task_id)
                if spec_head is None:
                    raise AuthoringNotFound("Task Spec not found")
                head = self._prompt_head(session, task_id)
                current = 0 if head is None else head.revision
                if (expected or 0) != current:
                    raise AuthoringConflict("prompt_conflict", f"The Prompt Draft is at revision {current}, not {expected or 0}")
                if source != spec_head.revision:
                    raise AuthoringConflict("source_not_current",
                                            f"The current Spec is revision {spec_head.revision}; the Draft must reference it")
                digest = prompt_digest(source, summary, body)
                if head is not None and head.digest == digest:
                    approval = self._approval(session, head.id)
                    return {"created": False, "prompt": prompt_view(head, state=self._state(head, spec_head, approval), approval=approval)}
                row = PromptRevision(project_id=project_id, task_id=task_id, revision=current + 1,
                                     source_spec_revision=source, body=body, summary=summary, digest=digest,
                                     actor_kind=kind, actor_id=actor_id, actor_assurance=assurance)
                session.add(row)
                session.commit()
                return {"created": True, "prompt": prompt_view(row, state="unapproved", approval=None)}
            except OperationalError:
                session.rollback()
                raise AuthoringConflict("prompt_conflict", "Another Prompt change is in progress; retry") from None
            except IntegrityError:
                session.rollback()
                raise AuthoringConflict("prompt_conflict", "The Prompt Draft changed concurrently") from None

    def read_prompt(self, author: object, project_id: int, task_id: int, revision: int | None = None) -> dict:
        self._authorize(author, authz.PROMPT_DRAFT_READ, project_id, task_id)
        with self._sessions() as session:
            self._task(session, project_id, task_id)
            head = self._prompt_head(session, task_id)
            spec_head = self._spec_head(session, task_id)
            if head is None or spec_head is None:
                raise AuthoringNotFound("Prompt Draft not found")
            row = head if revision is None else session.scalars(select(PromptRevision).where(
                PromptRevision.task_id == task_id, PromptRevision.revision == revision)).first()
            if row is None:
                raise AuthoringNotFound("Prompt revision not found")
            approval = self._approval(session, row.id)
            state = self._state(row, spec_head, approval) if row.revision == head.revision else "superseded"
            return {**prompt_view(row, state=state, approval=approval), "current": row.revision == head.revision}

    def list_prompt_revisions(self, author: object, project_id: int, task_id: int) -> list[dict]:
        self._authorize(author, authz.PROMPT_DRAFT_READ, project_id, task_id)
        with self._sessions() as session:
            self._task(session, project_id, task_id)
            spec_head = self._spec_head(session, task_id)
            rows = session.scalars(select(PromptRevision).where(PromptRevision.task_id == task_id)
                                   .order_by(PromptRevision.revision)).all()
            out = []
            for row in rows:
                approval = self._approval(session, row.id)
                state = self._state(row, spec_head, approval) if row is rows[-1] and spec_head else "superseded"
                out.append({**prompt_view(row, state=state, approval=approval, body=False), "current": row is rows[-1]})
            return out

    def approve_prompt(self, author: object, project_id: int, task_id: int, revision: int, *, digest: object) -> dict:
        """Owner approval of exactly one saved head revision.  Idempotent for the same immutable target.  Starts nothing."""
        self._authorize(author, authz.PROMPT_APPROVE, project_id, task_id)
        revision = _int(revision, "revision")
        if not isinstance(digest, str) or len(digest) != 64:
            raise AuthoringInvalid("digest must be the 64-character digest of the reviewed revision")
        _, kind, actor_id, assurance = _actor(author)
        with self._sessions() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                self._project(session, project_id)
                self._task(session, project_id, task_id, editable=True)
                spec_head = self._spec_head(session, task_id)
                head = self._prompt_head(session, task_id)
                row = session.scalars(select(PromptRevision).where(
                    PromptRevision.task_id == task_id, PromptRevision.revision == revision)).first()
                if row is None or spec_head is None:
                    raise AuthoringNotFound("Prompt revision not found")
                if row.digest != digest:
                    raise AuthoringConflict("digest_mismatch", "The Prompt revision differs from the reviewed one")
                if head is None or head.revision != row.revision:
                    raise AuthoringConflict("not_head", "Only the current Prompt Draft revision can be approved")
                if row.source_spec_revision != spec_head.revision:
                    raise AuthoringConflict("prompt_stale", "The Spec changed; review and re-save the Prompt Draft first")
                approval = self._approval(session, row.id)
                created = approval is None
                if created:
                    approval = PromptApproval(
                        prompt_revision_id=row.id, project_id=project_id, task_id=task_id,
                        spec_revision=row.source_spec_revision, prompt_revision=row.revision, digest=row.digest,
                        actor_kind=kind, actor_id=actor_id, actor_assurance=assurance)
                    session.add(approval)
                    session.commit()
                return {"created": created, "prompt": prompt_view(row, state="approved", approval=approval)}
            except OperationalError:
                session.rollback()
                raise AuthoringConflict("prompt_conflict", "Another change is in progress; retry") from None

    # -- projection --------------------------------------------------------------------------------------------------
    def projection(self, author: object, project_id: int, task_id: int) -> dict:
        """Owner-readable authoring state of one Task, including the single next primary action (derived, not stored)."""
        self._authorize(author, authz.TASK_SPEC_READ, project_id, task_id)
        with self._sessions() as session:
            self._task(session, project_id, task_id)
            spec_head = self._spec_head(session, task_id)
            if spec_head is None:
                raise AuthoringNotFound("Task Spec not found")
            head = self._prompt_head(session, task_id)
            approval = self._approval(session, head.id) if head else None
            state = self._state(head, spec_head, approval) if head else None
            prompt = prompt_view(head, state=state, approval=approval) if head else None
            task = session.get(Task, task_id)
            editable = task.status == EDITABLE_STATUS
            revisions = [r for r in session.scalars(select(PromptRevision).where(PromptRevision.task_id == task_id)
                                                    .order_by(PromptRevision.revision))]
            prompt_history = []
            for row in revisions:
                appr = self._approval(session, row.id)
                prompt_history.append({**prompt_view(row, state=self._state(row, spec_head, appr) if row is head else "superseded",
                                                     approval=appr, body=False), "current": row is head})
        return {
            "task_id": f"T{task_id:04d}", "editable": editable,
            "spec": spec_view(spec_head, head=True), "prompt": prompt, "prompt_history": prompt_history,
            "run": {"authorized": False, "label": "Run не авторизован", "worker": "Worker не запускался"},
            "next": self._next(state, head, editable),
        }

    @staticmethod
    def _next(state: str | None, head: PromptRevision | None, editable: bool) -> dict:
        if not editable:
            return {"action": "none", "label": "Task не в стадии Draft", "enabled": False, "next_actor": "Owner",
                    "hint": "Правка Spec и Prompt после стадии Draft недоступна."}
        if head is None:
            return {"action": "create_prompt", "label": "Создать Prompt Draft", "enabled": True, "next_actor": "Owner",
                    "hint": "Создание Draft не запускает Worker."}
        if state == "stale":
            return {"action": "update_prompt", "label": "Обновить Prompt Draft", "enabled": True, "next_actor": "Owner",
                    "hint": "Spec изменилась после этого Draft. Пересмотрите текст и резюме под текущую Spec."}
        if state == "unapproved":
            return {"action": "review_prompt", "label": "Просмотреть и утвердить Prompt", "enabled": True,
                    "next_actor": "Owner", "hint": "Утверждение Draft не даёт разрешения на запуск."}
        return {"action": "authorize_run", "label": "Проверить и разрешить Run", "enabled": False, "next_actor": "Owner",
                "hint": "Разрешение Run появится в P041", "secondary": "Просмотреть утверждённый Draft"}
