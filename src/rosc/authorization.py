"""Canonical Core actor context, authentication-provider seam and default-deny authorization policy (P010 / M2-B).

Contract: ``AuthenticationProvider -> trusted ActorContext -> independent AuthorizationPolicy -> guarded Core operation``.

Trust limits (read before relying on anything here):

* ``LocalOwnerProvider`` designates the configured local single-Owner *execution context*. Its actor carries
  ``assurance="local_unverified"``: nobody was authenticated, no credential was checked, and no browser identity, login,
  token or remote user exists. It is a local-first bootstrap, not protection against another process running as the
  same OS user. A Python dataclass is not cryptographic proof of origin, so any in-process code can build an
  ``ActorContext``; the policy therefore never trusts a role *string* alone (kind, stable id and assurance must all match
  the single local Owner) and the HTTP layer never lets a caller choose any of them.
* Authentication, authorization, assignment, execution session and attribution are different things. ``actor_id`` is
  provenance (it is what Architect revisions record), never proof of authority.
* Agents have no authority here. Agent authority needs durable AgentDefinition/RoleAssignment and an authorized Run
  (M2-C/M2-D). Until those exist an ``agent`` actor is denied everything; it is never inferred from a role label,
  agent name, model/provider or mere session presence.
* Future policy decisions will additionally need: agent_id, assignment_id, project_id/task_id, Root Binding version,
  runtime/Run scope and current lifecycle state. None of this is modelled or faked in memory now.

This protects ROSC-owned Core/Workspace state. It is not a hostile-OS sandbox and not a proxy for native Worker tools.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

ACTOR_OWNER = "owner"
ACTOR_AGENT = "agent"
ASSURANCE_LOCAL_UNVERIFIED = "local_unverified"
LOCAL_OWNER_ID = "local-owner"

# Currently implemented Core operations. Anything else is denied.
PROJECT_LIST = "project.list"
PROJECT_REGISTER = "project.register"
PROJECT_RECONCILE = "project.reconcile"
PROJECT_REPAIR = "project.repair"
TASK_LIST = "task.list"
TASK_CREATE = "task.create"
TASK_RECONCILE = "task.reconcile"
TASK_REPAIR = "task.repair"
DOCUMENT_LIST = "architect_document.list"
DOCUMENT_READ = "architect_document.read"
DOCUMENT_WRITE = "architect_document.write"
DOCUMENT_RECONCILE = "architect_document.reconcile"
# M2-C Owner-only registry and assignment administration. These actions describe administrative bookkeeping, never
# agent execution authority: holding one does not let an Agent reach a Project, Task or Architect document.
AGENT_LIST = "agent.list"
AGENT_REGISTER = "agent.register"
AGENT_READ = "agent.read"
AGENT_UPDATE = "agent.update"
ASSIGNMENT_LIST = "assignment.list"
ASSIGNMENT_CREATE = "assignment.create"
ASSIGNMENT_REVOKE = "assignment.revoke"
# M2-D Owner-only external Run provenance bookkeeping (P013). Recording an external Run/Report/Evidence reference or
# an Owner-curated Agent correspondence is administrative history: it is NOT ``run.approve``, launches nothing and
# grants no Agent any authority.
RUN_REGISTER_EXTERNAL = "run.register_external"
RUN_LIST = "run.list"
RUN_READ = "run.read"
RUN_REPORT_REGISTER = "run.report.register"
RUN_EVIDENCE_REGISTER = "run.evidence.register"
RUN_LINK_AGENT = "run.link_agent"
# M3-A durable Project Conversation (P014). Reading and Owner-authored messages are separate operations. Neither
# activates an Agent, creates a Run or grants an Agent participant any authority.
CONVERSATION_READ = "conversation.read"
CONVERSATION_MESSAGE_CREATE = "conversation.message.create"
# M3-B Project Architect (P015). Activation is a distinct Owner action; status is Owner-readable. The reply commit and
# delivery reconciliation are internal Core operations performed by the service on behalf of the Owner who activated
# the session. None of these is ever granted to an ``agent`` actor.
ARCHITECT_STATUS = "architect.status"
ARCHITECT_ACTIVATE = "architect.activate"
ARCHITECT_DEACTIVATE = "architect.deactivate"
ARCHITECT_REPLY_COMMIT = "conversation.architect_reply.commit"
CONVERSATION_DELIVERY_RECONCILE = "conversation.delivery.reconcile"
# P019 Project Context Access. Reading the effective access summary is an Owner action only. The Architect's own tool
# calls are NOT authorized through this policy: they go through the session-scoped broker (``project_context``) which
# never impersonates the Owner.
ARCHITECT_CONTEXT_READ = "architect.context.read"

# Resource shapes: which identifiers an action must carry and which it may carry.
# (required fields, optional fields); every other field must be absent.
_SHAPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    PROJECT_LIST: (frozenset(), frozenset()),
    PROJECT_REGISTER: (frozenset(), frozenset()),
    PROJECT_RECONCILE: (frozenset({"project_id"}), frozenset()),
    PROJECT_REPAIR: (frozenset({"project_id"}), frozenset()),
    TASK_LIST: (frozenset({"project_id"}), frozenset()),
    TASK_CREATE: (frozenset({"project_id"}), frozenset()),
    TASK_RECONCILE: (frozenset({"project_id", "task_id"}), frozenset()),
    TASK_REPAIR: (frozenset({"project_id", "task_id"}), frozenset()),
    DOCUMENT_LIST: (frozenset({"project_id"}), frozenset()),
    DOCUMENT_READ: (frozenset({"project_id", "doc_id"}), frozenset()),
    DOCUMENT_WRITE: (frozenset({"project_id", "doc_id"}), frozenset()),
    # A diagnostic id may not name a document (for example a staged creation), so doc_id is optional here.
    DOCUMENT_RECONCILE: (frozenset({"project_id"}), frozenset({"doc_id"})),
    AGENT_LIST: (frozenset(), frozenset()),
    AGENT_REGISTER: (frozenset(), frozenset()),
    AGENT_READ: (frozenset({"agent_id"}), frozenset()),
    AGENT_UPDATE: (frozenset({"agent_id"}), frozenset()),
    ASSIGNMENT_LIST: (frozenset({"project_id"}), frozenset()),
    ASSIGNMENT_CREATE: (frozenset({"project_id"}), frozenset()),
    ASSIGNMENT_REVOKE: (frozenset({"project_id", "assignment_id"}), frozenset()),
    RUN_REGISTER_EXTERNAL: (frozenset({"project_id", "task_id"}), frozenset()),
    RUN_LIST: (frozenset({"project_id"}), frozenset({"task_id"})),
    RUN_READ: (frozenset({"project_id", "run_id"}), frozenset()),
    RUN_REPORT_REGISTER: (frozenset({"project_id", "run_id"}), frozenset()),
    RUN_EVIDENCE_REGISTER: (frozenset({"project_id", "run_id"}), frozenset()),
    # agent_id is the *target* of the Owner-curated correspondence; it never selects the actor.
    RUN_LINK_AGENT: (frozenset({"project_id", "run_id", "agent_id"}), frozenset()),
    CONVERSATION_READ: (frozenset({"project_id"}), frozenset()),
    CONVERSATION_MESSAGE_CREATE: (frozenset({"project_id"}), frozenset()),
    ARCHITECT_STATUS: (frozenset({"project_id"}), frozenset()),
    ARCHITECT_ACTIVATE: (frozenset({"project_id"}), frozenset()),
    ARCHITECT_DEACTIVATE: (frozenset({"project_id"}), frozenset()),
    ARCHITECT_REPLY_COMMIT: (frozenset({"project_id"}), frozenset()),
    CONVERSATION_DELIVERY_RECONCILE: (frozenset({"project_id"}), frozenset()),
    ARCHITECT_CONTEXT_READ: (frozenset({"project_id"}), frozenset()),
}
IMPLEMENTED_ACTIONS = frozenset(_SHAPES)

# Owner-only operations that have no Core method yet. They are deliberately NOT in IMPLEMENTED_ACTIONS and are listed
# only so tests can prove that the mere existence of the name grants nothing.
RESERVED_OWNER_ACTIONS = frozenset({"run.approve", "task.accept", "project.archive", "project.trash", "project.unlink",
                                    "project.rebind", "scope.expand"})


@dataclass(frozen=True)
class ActorContext:
    """Immutable semantic identity. Not a display name, model, role label, session or approval."""

    actor_kind: str
    actor_id: str
    assurance: str


@dataclass(frozen=True)
class ResourceScope:
    """Typed scope of one operation.

    Identifiers are canonical numeric Project/Task ids, a document id and, for registry administration only,
    canonical UUIDv4 Agent/assignment/Run ids. Agent ids appear in a scope because an administrative action targets a
    registered Agent; they never select the actor.
    """

    project_id: int | None = None
    task_id: int | None = None
    doc_id: str | None = None
    agent_id: str | None = None
    assignment_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: str  # machine-readable, never contains paths, secrets or provider data


def deny(reason: str) -> AuthorizationDecision:
    return AuthorizationDecision(False, reason)


class AuthenticationProvider(Protocol):
    """Controlled application/runtime entrypoint that yields the trusted actor. Core operations never read credentials,
    request-supplied actor ids or HTTP headers themselves."""

    def actor(self) -> ActorContext: ...


class AuthorizationPolicy(Protocol):
    def decide(self, actor: ActorContext, action: str, resource: ResourceScope) -> AuthorizationDecision: ...


class LocalOwnerProvider:
    """The configured local single-Owner execution context, ``local_unverified``. Never claims ``authenticated``."""

    def actor(self) -> ActorContext:
        return ActorContext(ACTOR_OWNER, LOCAL_OWNER_ID, ASSURANCE_LOCAL_UNVERIFIED)


def _valid_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def is_canonical_uuid4(value: object) -> bool:
    """True only for a canonical lowercase RFC 4122 UUIDv4 string, without normalizing anything."""
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 4 and parsed.variant == uuid.RFC_4122 and str(parsed) == value


def resource_problem(action: str, resource: object) -> str | None:
    """Return a reason code when the scope is missing, contradictory or malformed for ``action``."""
    shape = _SHAPES.get(action)
    if shape is None:
        return "unknown_action"
    if not isinstance(resource, ResourceScope):
        return "invalid_resource"
    required, optional = shape
    values = {"project_id": resource.project_id, "task_id": resource.task_id, "doc_id": resource.doc_id,
              "agent_id": resource.agent_id, "assignment_id": resource.assignment_id,
              "run_id": resource.run_id}
    for name, value in values.items():
        if name in required:
            if name in ("doc_id", "agent_id", "assignment_id", "run_id"):
                # doc_id is syntactically validated by the document store itself; here it only has to be present text.
                valid = is_canonical_uuid4(value) if name != "doc_id" else isinstance(value, str)
            else:
                valid = _valid_id(value)
            if not valid:
                return "invalid_resource"
        elif value is not None:
            if name not in optional:
                return "invalid_resource"
            if name in ("project_id", "task_id") and not _valid_id(value):
                return "invalid_resource"
            if name in ("doc_id", "agent_id", "assignment_id", "run_id") and not isinstance(value, str):
                return "invalid_resource"
    return None


class LocalOwnerPolicy:
    """Default-deny policy for local single-Owner mode.

    Allows only the implemented operations, only for the local Owner actor, only for a well-formed resource scope.
    Owner confirmation, Root Binding integrity and Workspace/manifest checks remain independent and mandatory:
    authorization is necessary, never sufficient.
    """

    def decide(self, actor: ActorContext, action: str, resource: ResourceScope) -> AuthorizationDecision:
        if not isinstance(actor, ActorContext):
            return deny("untrusted_actor")
        if action not in IMPLEMENTED_ACTIONS:
            return deny("unknown_action")
        problem = resource_problem(action, resource)
        if problem:
            return deny(problem)
        if actor.actor_kind == ACTOR_AGENT:
            return deny("agent_authority_not_implemented")
        if (actor.actor_kind == ACTOR_OWNER and actor.actor_id == LOCAL_OWNER_ID
                and actor.assurance == ASSURANCE_LOCAL_UNVERIFIED):
            return AuthorizationDecision(True, "local_owner")
        return deny("actor_not_authorized")


def evaluate(policy: AuthorizationPolicy, actor: object, action: str, resource: object) -> AuthorizationDecision:
    """Fail-closed wrapper used by every Core entrypoint: a bad actor, a bad scope, a policy error or a non-decision
    all become a denial, and a policy can never allow what the canonical scope rules reject."""
    if not isinstance(actor, ActorContext):
        return deny("untrusted_actor")
    problem = resource_problem(action, resource)
    if problem:
        return deny(problem)
    try:
        decision = policy.decide(actor, action, resource)  # type: ignore[arg-type]
    except Exception:
        return deny("policy_error")
    if not isinstance(decision, AuthorizationDecision):
        return deny("invalid_decision")
    return decision
