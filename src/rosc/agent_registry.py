"""Owner-only Agent registry and scoped role assignment service (P011 / M2-C).

Governing chain implemented here: ``AgentDefinition (stable logical identity) -> RoleAssignment (bounded
Project/Task intent)``. Everything after that (authorized Run, RuntimeBinding, Session/Process, Events/Evidence)
belongs to M2-D/M3/M4 and is deliberately absent.

Trust limits (read before relying on anything here):

* Registration and assignment are administrative facts recorded by the local Owner context. Neither launches a
  provider, creates a session, writes into a connected source checkout, changes a Workspace manifest or grants an
  Agent any ROSC Core authority. ``LocalOwnerPolicy`` still denies ``actor_kind="agent"`` for every Project, Task and
  Architect-document action, whatever assignments exist.
* A capability is a *declaration* of intent. There is no ``verified`` readiness value: the maximum an assignment can
  reach is ``unverified`` (all requested capabilities declared), and ``blocked`` when one is no longer declared.
  Nothing here proves a provider is installed, authenticated or reachable.
* Registered identifiers are random canonical UUIDv4 values, independent of Project ids, model names, display labels
  and provider sessions. Editing metadata never re-identifies an Agent and never deletes its assignments.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .authorization import (AGENT_LIST, AGENT_READ, AGENT_REGISTER, AGENT_UPDATE, ASSIGNMENT_CREATE, ASSIGNMENT_LIST,
                            ASSIGNMENT_REVOKE, ActorContext, AuthorizationPolicy, ResourceScope, evaluate, is_canonical_uuid4)
from .models import (AGENT_CAPABILITY_DECLARED, ASSIGNMENT_ACTIVE, ASSIGNMENT_REVOKED, READINESS_BLOCKED,
                     READINESS_UNVERIFIED, ROLE_ARCHITECT, ROLE_WORKER, AgentCapability, AgentDefinition,
                     AssignmentCapability, Project, ProjectIdentityError, RoleAssignment, Task, validate_project_identity)

# ------------------------------------------------------------------------------------------------------------------
# Closed, provider-neutral capability vocabulary. These identifiers describe ROSC-owned operations; they are not
# native tool names, shell permissions, filesystem paths or provider-specific permission prompts.
# ------------------------------------------------------------------------------------------------------------------
CAPABILITY_PROJECT_CONTEXT_READ = "project.context.read"
CAPABILITY_TASK_CONTEXT_READ = "task.context.read"
CAPABILITY_ARCHITECT_DOCUMENT_READ = "architect.document.read"
CAPABILITY_ARCHITECT_DOCUMENT_WRITE = "architect.document.write"
# Declared vocabulary for a later Task artifact slice. No Task artifact/Prompt/Run operation is implemented by P011.
CAPABILITY_TASK_ARTIFACT_PUBLISH = "task.artifact.publish"

CAPABILITY_CATALOG = frozenset({
    CAPABILITY_PROJECT_CONTEXT_READ,
    CAPABILITY_TASK_CONTEXT_READ,
    CAPABILITY_ARCHITECT_DOCUMENT_READ,
    CAPABILITY_ARCHITECT_DOCUMENT_WRITE,
    CAPABILITY_TASK_ARTIFACT_PUBLISH,
})

# Capabilities an assignment of a role may request, and the subset that role always needs.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    ROLE_ARCHITECT: frozenset({CAPABILITY_PROJECT_CONTEXT_READ, CAPABILITY_ARCHITECT_DOCUMENT_READ,
                               CAPABILITY_ARCHITECT_DOCUMENT_WRITE}),
    ROLE_WORKER: frozenset({CAPABILITY_PROJECT_CONTEXT_READ, CAPABILITY_TASK_CONTEXT_READ,
                            CAPABILITY_TASK_ARTIFACT_PUBLISH}),
}
ROLE_REQUIRED_CAPABILITIES: dict[str, frozenset[str]] = {
    ROLE_ARCHITECT: frozenset({CAPABILITY_PROJECT_CONTEXT_READ, CAPABILITY_ARCHITECT_DOCUMENT_READ,
                               CAPABILITY_ARCHITECT_DOCUMENT_WRITE}),
    ROLE_WORKER: frozenset({CAPABILITY_PROJECT_CONTEXT_READ, CAPABILITY_TASK_CONTEXT_READ}),
}
ROLES = frozenset(ROLE_CAPABILITIES)

MAX_CAPABILITIES = 8
MAX_DISPLAY_NAME = 80
MAX_ICON_LABEL = 32
MAX_RUNTIME_ADAPTER = 64
MAX_PROVIDER_ID = 64
MAX_MODEL_ID = 96

_IDENTIFIER = {
    "runtime_adapter": re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", re.ASCII),
    "provider_id": re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", re.ASCII),
    "model_id": re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,95}", re.ASCII),
}
_ICON_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Conservative denylist for descriptive metadata: a credential pasted into a "model" or "display name" field is
# refused instead of being persisted as a harmless-looking label.
_SECRET_MARKERS = ("secret", "password", "passwd", "token", "api_key", "apikey", "private_key", "private key",
                   "bearer ", "authorization:", "-----begin", "credential")
_SECRET_PREFIXES = ("sk-", "pk-", "ghp_", "gho_", "ghs_", "github_pat_", "xoxb-", "xoxp-", "akia", "asia")


class RegistryError(Exception):
    """Base class for the administrative registry boundary."""


class RegistryDenied(RegistryError):
    """The actor, action or scope was refused before any lookup or write (HTTP 403)."""


class InvalidRegistryInput(RegistryError):
    """Malformed, unsupported or contradictory input (HTTP 422)."""


class RegistryNotFound(RegistryError):
    """The named Agent, Project or assignment does not exist (HTTP 404)."""


class AgentNotFound(RegistryNotFound):
    pass


class ProjectNotFound(RegistryNotFound):
    pass


class AssignmentNotFound(RegistryNotFound):
    pass


class RegistryConflict(RegistryError):
    """Uniqueness, lifecycle or stale-revision conflict (HTTP 409)."""


class UnsafeProjectIdentity(RegistryError):
    """Canonical Project identity records are unsafe to use; fail closed (HTTP 503)."""


class RegistryStoreError(RegistryError):
    """The database could not complete the administrative write (HTTP 503)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime) -> str:
    """SQLite drops the offset of the values this slice writes, which are UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_agent_id(value: object) -> str:
    """Return a canonical UUIDv4 Agent/assignment identifier, or reject before any lookup."""
    if not is_canonical_uuid4(value):
        raise InvalidRegistryInput("Identifier must be a canonical UUIDv4 string")
    return value  # type: ignore[return-value]


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise InvalidRegistryInput(f"{field} must be text")
    value = value.strip()
    if not value or len(value) > limit:
        raise InvalidRegistryInput(f"{field} must be 1-{limit} characters")
    if _CONTROL.search(value):
        raise InvalidRegistryInput(f"{field} must not contain control characters")
    lowered = value.lower()
    if "://" in value or any(marker in lowered for marker in _SECRET_MARKERS) or lowered.startswith(_SECRET_PREFIXES):
        raise InvalidRegistryInput(f"{field} must be descriptive metadata, not a URL or credential")
    return value


def _identifier(value: object, field: str, limit: int) -> str:
    """Structured but descriptive runtime/provider/model name: never a path, command or credential."""
    value = _text(value, field, limit)
    if "/" in value or "\\" in value or ".." in value or not _IDENTIFIER[field].fullmatch(value):
        raise InvalidRegistryInput(f"{field} must be a short identifier, not a path or command")
    return value


def _icon_label(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidRegistryInput("icon_label must be text or null")
    value = value.strip()
    if not value:
        return None
    if not _ICON_LABEL.fullmatch(value) or ".." in value:
        raise InvalidRegistryInput("icon_label must be a short reference, not a path")
    return value


def _capability_list(value: object) -> tuple[str, ...]:
    """Normalize a requested capability list into a sorted, duplicate-free tuple of catalog identifiers."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise InvalidRegistryInput("capabilities must be a list")
    items = list(value)
    if len(items) > MAX_CAPABILITIES:
        raise InvalidRegistryInput(f"At most {MAX_CAPABILITIES} capabilities may be requested")
    seen: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise InvalidRegistryInput("Capability identifiers must be strings")
        if item not in CAPABILITY_CATALOG:
            # Never echo the submitted text: it may be a pasted credential or an unbounded tool string.
            raise InvalidRegistryInput("Unsupported capability identifier")
        if item in seen:
            raise InvalidRegistryInput("Duplicate capability identifier")
        seen.append(item)
    return tuple(sorted(seen))


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidRegistryInput(f"{field} must be a positive integer")
    return value


def _project_label(project_id: int) -> str:
    return f"P{project_id:04d}"


def _task_label(task_id: int) -> str:
    return f"T{task_id:04d}"


def agent_view(agent: AgentDefinition) -> dict:
    """Public read model. ``runtime_state`` is always unverified: registration is not runtime proof."""
    return {
        "agent_id": agent.agent_id,
        "display_name": agent.display_name,
        "icon_label": agent.icon_label,
        "runtime_adapter": agent.runtime_adapter,
        "provider_id": agent.provider_id,
        "model_id": agent.model_id,
        "capabilities": [{"capability": item.capability, "status": item.status} for item in agent.capabilities],
        "runtime_state": READINESS_UNVERIFIED,
        "version": agent.version,
        "created_at": _utc_iso(agent.created_at),
        "updated_at": _utc_iso(agent.updated_at),
    }


def assignment_view(assignment: RoleAssignment) -> dict:
    """Public read model. ``runtime_effect`` stays ``none`` until a future Run slice exists."""
    return {
        "assignment_id": assignment.assignment_id,
        "agent_id": assignment.agent_id,
        "project_id": _project_label(assignment.project_id),
        "task_id": _task_label(assignment.task_id) if assignment.task_id is not None else None,
        "role": assignment.role,
        "scope": "task" if assignment.task_id is not None else "project",
        "state": assignment.state,
        "readiness": assignment.readiness,
        "capabilities": [item.capability for item in assignment.capabilities],
        "runtime_effect": "none",
        "version": assignment.version,
        "created_at": _utc_iso(assignment.created_at),
        "updated_at": _utc_iso(assignment.updated_at),
        "revoked_at": _utc_iso(assignment.revoked_at) if assignment.revoked_at is not None else None,
        "created_by": assignment.created_by,
        "created_by_assurance": assignment.created_by_assurance,
        "revoked_by": assignment.revoked_by,
        "revoked_by_assurance": assignment.revoked_by_assurance,
    }


class AgentRegistryService:
    """Canonical business boundary for AgentDefinitions and RoleAssignments.

    Every method authorizes through the injected ``AuthorizationPolicy`` before it reads or writes anything, so a
    direct Core call has exactly the same guard as the HTTP handler and no caller can select its own actor.
    """

    def __init__(self, session_factory: sessionmaker[Session], policy: AuthorizationPolicy) -> None:
        self._sessions = session_factory
        self._policy = policy

    def _authorize(self, actor: ActorContext, action: str, **scope) -> None:
        if not evaluate(self._policy, actor, action, ResourceScope(**scope)).allowed:
            raise RegistryDenied("Not authorized for this operation")

    # -- Agent registry --------------------------------------------------------------------------------------
    def register_agent(self, actor: ActorContext, *, display_name: object, runtime_adapter: object,
                       provider_id: object, model_id: object, icon_label: object = None,
                       capabilities: object = ()) -> dict:
        self._authorize(actor, AGENT_REGISTER)
        name = _text(display_name, "display_name", MAX_DISPLAY_NAME)
        icon = _icon_label(icon_label)
        adapter = _identifier(runtime_adapter, "runtime_adapter", MAX_RUNTIME_ADAPTER)
        provider = _identifier(provider_id, "provider_id", MAX_PROVIDER_ID)
        model = _identifier(model_id, "model_id", MAX_MODEL_ID)
        declared = _capability_list(capabilities)
        now = _now()
        agent = AgentDefinition(agent_id=str(uuid.uuid4()), display_name=name, icon_label=icon, runtime_adapter=adapter,
                                provider_id=provider, model_id=model, version=1, created_at=now, updated_at=now)
        agent.capabilities = [AgentCapability(capability=capability, status=AGENT_CAPABILITY_DECLARED, declared_at=now)
                              for capability in declared]
        with self._sessions() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                session.add(agent)
                session.commit()
            except OperationalError:
                session.rollback()
                raise RegistryStoreError("Another registry change is in progress; retry shortly") from None
            except IntegrityError:
                session.rollback()
                raise RegistryConflict("Agent could not be registered") from None
        return agent_view(agent)

    def list_agents(self, actor: ActorContext) -> list[dict]:
        """Global read path: independent of any Project, including one with damaged identity records."""
        self._authorize(actor, AGENT_LIST)
        with self._sessions() as session:
            agents = session.scalars(select(AgentDefinition).order_by(AgentDefinition.created_at,
                                                                      AgentDefinition.agent_id)).all()
            return [agent_view(agent) for agent in agents]

    def get_agent(self, actor: ActorContext, agent_id: object) -> dict:
        self._authorize(actor, AGENT_READ, agent_id=agent_id)
        with self._sessions() as session:
            agent = session.get(AgentDefinition, agent_id)
            if agent is None:
                raise AgentNotFound("Agent not found")
            return agent_view(agent)

    def update_agent(self, actor: ActorContext, agent_id: object, *, expected_version: object, display_name: object,
                     runtime_adapter: object, provider_id: object, model_id: object, icon_label: object = None,
                     capabilities: object = ()) -> dict:
        """Full metadata replacement with optimistic version check; identity and assignments are preserved."""
        self._authorize(actor, AGENT_UPDATE, agent_id=agent_id)
        version = _positive_int(expected_version, "expected_version")
        name = _text(display_name, "display_name", MAX_DISPLAY_NAME)
        icon = _icon_label(icon_label)
        adapter = _identifier(runtime_adapter, "runtime_adapter", MAX_RUNTIME_ADAPTER)
        provider = _identifier(provider_id, "provider_id", MAX_PROVIDER_ID)
        model = _identifier(model_id, "model_id", MAX_MODEL_ID)
        declared = _capability_list(capabilities)
        with self._sessions() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                agent = session.get(AgentDefinition, agent_id)
                if agent is None:
                    session.rollback()
                    raise AgentNotFound("Agent not found")
                if version != agent.version:
                    session.rollback()
                    raise RegistryConflict("Agent was changed by another operation; reload and retry")
                now = _now()
                agent.display_name = name
                agent.icon_label = icon
                agent.runtime_adapter = adapter
                agent.provider_id = provider
                agent.model_id = model
                agent.version += 1
                agent.updated_at = now
                self._replace_capabilities(agent, declared, now)
                self._recompute_readiness(session, agent, frozenset(declared), now)
                session.commit()
            except OperationalError:
                session.rollback()
                raise RegistryStoreError("Another registry change is in progress; retry shortly") from None
            except IntegrityError:
                session.rollback()
                raise RegistryConflict("Agent could not be updated") from None
        return agent_view(agent)

    @staticmethod
    def _replace_capabilities(agent: AgentDefinition, declared: tuple[str, ...], now: datetime) -> None:
        """Incremental replacement: rewrite statuses in place so a re-declared key is never delete+insert raced."""
        existing = {item.capability: item for item in list(agent.capabilities)}
        for capability in declared:
            if capability in existing:
                existing[capability].status = AGENT_CAPABILITY_DECLARED
                existing[capability].declared_at = now
            else:
                agent.capabilities.append(AgentCapability(capability=capability, status=AGENT_CAPABILITY_DECLARED,
                                                          declared_at=now))
        for capability, item in existing.items():
            if capability not in declared:
                agent.capabilities.remove(item)

    def _recompute_readiness(self, session: Session, agent: AgentDefinition, declared: frozenset[str],
                             now: datetime) -> None:
        """A descriptor/capability edit can only make an existing assignment unverified or blocked, never verified."""
        assignments = session.scalars(select(RoleAssignment).where(RoleAssignment.agent_id == agent.agent_id,
                                                                   RoleAssignment.state == ASSIGNMENT_ACTIVE)).all()
        for assignment in assignments:
            required = ROLE_REQUIRED_CAPABILITIES[assignment.role] | {
                item.capability for item in assignment.capabilities}
            readiness = READINESS_UNVERIFIED if required <= declared else READINESS_BLOCKED
            if readiness != assignment.readiness:
                assignment.readiness = readiness
                assignment.version += 1
                assignment.updated_at = now

    # -- Assignments -----------------------------------------------------------------------------------------
    def list_assignments(self, actor: ActorContext, project_id: object, *, include_revoked: bool = False) -> list[dict]:
        self._authorize(actor, ASSIGNMENT_LIST, project_id=project_id)
        with self._sessions() as session:
            if session.get(Project, project_id) is None:
                raise ProjectNotFound("Project not found")
            statement = select(RoleAssignment).where(RoleAssignment.project_id == project_id)
            if not include_revoked:
                statement = statement.where(RoleAssignment.state == ASSIGNMENT_ACTIVE)
            rows = session.scalars(statement.order_by(RoleAssignment.created_at, RoleAssignment.assignment_id)).all()
            return [assignment_view(row) for row in rows]

    def create_assignment(self, actor: ActorContext, project_id: object, *, agent_id: object, role: object,
                          capabilities: object, task_id: object = None) -> dict:
        self._authorize(actor, ASSIGNMENT_CREATE, project_id=project_id)
        if not isinstance(role, str) or role not in ROLES:
            raise InvalidRegistryInput("Unsupported role")
        requested = _capability_list(capabilities)
        if not set(requested) <= ROLE_CAPABILITIES[role]:
            raise InvalidRegistryInput("A requested capability is not available to this role")
        if not ROLE_REQUIRED_CAPABILITIES[role] <= set(requested):
            raise InvalidRegistryInput("A role assignment must request every capability its role requires")
        agent_key = canonical_agent_id(agent_id)
        task_key: int | None
        if task_id is None:
            task_key = None
        else:
            task_key = _positive_int(task_id, "task_id")
        if role == ROLE_ARCHITECT and task_key is not None:
            raise InvalidRegistryInput("An architect assignment is Project-scoped and cannot name a Task")

        with self._sessions() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                project = session.get(Project, project_id)
                if project is None:
                    session.rollback()
                    raise ProjectNotFound("Project not found")
                try:
                    validate_project_identity(project)
                except ProjectIdentityError:
                    session.rollback()
                    raise UnsafeProjectIdentity("Project identity records are inconsistent") from None
                agent = session.get(AgentDefinition, agent_key)
                if agent is None:
                    session.rollback()
                    raise AgentNotFound("Agent not found")
                if task_key is not None:
                    task = session.get(Task, task_key)
                    if task is None or task.project_id != project_id:
                        session.rollback()
                        raise InvalidRegistryInput("Task does not belong to this Project")
                declared = {item.capability for item in agent.capabilities
                            if item.status == AGENT_CAPABILITY_DECLARED}
                if not ROLE_REQUIRED_CAPABILITIES[role] <= declared:
                    session.rollback()
                    raise InvalidRegistryInput("Agent has not declared the capabilities this role requires")
                # The role allowlist says what the role *may* request; only this Agent's declarations say what it has.
                # An optional capability the Agent never declared is refused instead of being recorded as `unverified`.
                if not set(requested) <= declared:
                    session.rollback()
                    raise InvalidRegistryInput("Agent has not declared one or more requested capabilities")
                self._check_duplicate(session, agent_key, project_id, role, task_key)
                now = _now()
                assignment = RoleAssignment(assignment_id=str(uuid.uuid4()), agent_id=agent_key,
                                            project_id=project_id, task_id=task_key, role=role,
                                            state=ASSIGNMENT_ACTIVE, readiness=READINESS_UNVERIFIED, version=1,
                                            created_at=now, updated_at=now, created_by=actor.actor_id,
                                            created_by_assurance=actor.assurance)
                assignment.capabilities = [AssignmentCapability(capability=item) for item in requested]
                session.add(assignment)
                session.commit()
            except OperationalError:
                session.rollback()
                raise RegistryStoreError("Another registry change is in progress; retry shortly") from None
            except IntegrityError:
                session.rollback()
                raise RegistryConflict("A conflicting assignment already exists") from None
        return assignment_view(assignment)

    @staticmethod
    def _check_duplicate(session: Session, agent_id: str, project_id: int, role: str, task_id: int | None) -> None:
        if role == ROLE_ARCHITECT:
            current = session.scalars(select(RoleAssignment).where(
                RoleAssignment.project_id == project_id, RoleAssignment.role == ROLE_ARCHITECT,
                RoleAssignment.state == ASSIGNMENT_ACTIVE)).first()
            if current is not None:
                raise RegistryConflict("Project already has a current Architect assignment")
        equivalent = session.scalars(select(RoleAssignment).where(
            RoleAssignment.agent_id == agent_id, RoleAssignment.project_id == project_id,
            RoleAssignment.role == role, RoleAssignment.state == ASSIGNMENT_ACTIVE)).all()
        for other in equivalent:
            if other.task_id == task_id:
                raise RegistryConflict("An equivalent active assignment already exists")

    def revoke_assignment(self, actor: ActorContext, project_id: object, assignment_id: object, *,
                          expected_version: object) -> dict:
        self._authorize(actor, ASSIGNMENT_REVOKE, project_id=project_id, assignment_id=assignment_id)
        version = _positive_int(expected_version, "expected_version")
        with self._sessions() as session:
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                if session.get(Project, project_id) is None:
                    session.rollback()
                    raise ProjectNotFound("Project not found")
                assignment = session.get(RoleAssignment, assignment_id)
                if assignment is None or assignment.project_id != project_id:
                    session.rollback()
                    raise AssignmentNotFound("Assignment not found")
                if assignment.state == ASSIGNMENT_REVOKED:
                    session.rollback()
                    raise RegistryConflict("Assignment is already revoked")
                if version != assignment.version:
                    session.rollback()
                    raise RegistryConflict("Assignment was changed by another operation; reload and retry")
                now = _now()
                assignment.state = ASSIGNMENT_REVOKED
                assignment.revoked_at = now
                assignment.revoked_by = actor.actor_id
                assignment.revoked_by_assurance = actor.assurance
                assignment.version += 1
                assignment.updated_at = now
                session.commit()
            except OperationalError:
                session.rollback()
                raise RegistryStoreError("Another registry change is in progress; retry shortly") from None
        return assignment_view(assignment)
