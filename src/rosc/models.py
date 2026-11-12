from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Root Binding states. Exactly one ``current`` binding per Project; older ones stay as history.
CURRENT_BINDING = "current"
SUPERSEDED_BINDING = "superseded"


class ProjectIdentityError(Exception):
    """Canonical Project identity records are unsafe to use; callers must fail closed."""


class BindingStateError(ProjectIdentityError):
    """Project identity and Root Binding records contradict each other; callers must fail closed."""


class ProjectUUIDError(ProjectIdentityError):
    """The canonical Project UUID in SQLite is not a canonical RFC 4122 UUIDv4."""


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        # Bridge constraint for this slice only: the transitional legacy column may not drift from the canonical
        # registration root. A later Unlink/Rebind slice drops this CHECK together with ``root_path``.
        CheckConstraint("registration_root_path = root_path", name="ck_projects_registration_root"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text)
    # Transitional legacy mirror of ``registration_root_path``; kept UNIQUE so no root is registered twice by accident.
    root_path: Mapped[str] = mapped_column(Text, unique=True)
    mode: Mapped[str] = mapped_column(String(16))
    docs_entrypoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_state: Mapped[str] = mapped_column(String(16), default="provisioning")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # Immutable random identity; never derived from the Project ID, a host path or source content.
    project_uuid: Mapped[str] = mapped_column(String(36), unique=True)
    # Immutable original registration root: the ownership reference for legacy v1/v2 manifests.
    registration_root_path: Mapped[str] = mapped_column(Text)
    bindings: Mapped[list[ProjectRootBinding]] = relationship(
        back_populates="project", lazy="selectin", order_by="ProjectRootBinding.version")

    @property
    def current_binding(self) -> ProjectRootBinding:
        """Return the validated current Root Binding; absence is corruption until Unlink exists."""
        return validate_current_binding(self)


class ProjectRootBinding(Base):
    """Durable, versioned record of the root a Project is currently bound to.

    Only ``current`` bindings reserve a root path. This slice creates the initial version 1 binding only; binding
    mutation (Unlink/Rebind) is a later slice.
    """

    __tablename__ = "project_root_bindings"
    __table_args__ = (
        CheckConstraint("state IN ('current', 'superseded')", name="ck_root_bindings_state"),
        CheckConstraint("version >= 1", name="ck_root_bindings_version"),
        UniqueConstraint("project_id", "version", name="uq_root_bindings_project_version"),
        Index("ux_root_bindings_current_project", "project_id", unique=True, sqlite_where=text("state = 'current'")),
        Index("ux_root_bindings_current_root", "root_path", unique=True, sqlite_where=text("state = 'current'")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    root_path: Mapped[str] = mapped_column(Text)
    docs_entrypoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), default=CURRENT_BINDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    project: Mapped[Project] = relationship(back_populates="bindings")


def validate_project_uuid(value: object) -> str:
    """Return a canonical lowercase RFC 4122 UUIDv4 string without normalizing corrupt DB state."""
    if not isinstance(value, str):
        raise ProjectUUIDError("Project UUID is not a string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ProjectUUIDError("Project UUID is malformed") from exc
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122 or str(parsed) != value:
        raise ProjectUUIDError("Project UUID is not canonical RFC 4122 UUIDv4")
    return value


def validate_current_binding(project: Project) -> ProjectRootBinding:
    """Validate the transitional M2-A current-binding invariant for one existing Project."""
    current = [binding for binding in project.bindings if binding.state == CURRENT_BINDING]
    if len(current) != 1:
        raise BindingStateError(f"Project P{project.id} does not have exactly one current Root Binding")
    binding = current[0]
    if binding.version == 1 and binding.root_path != project.registration_root_path:
        raise BindingStateError(f"Project P{project.id} initial Root Binding disagrees with its registration root")
    return binding


def validate_project_identity(project: Project) -> ProjectRootBinding:
    """Validate canonical UUID and current Root Binding through one reusable Core gate."""
    validate_project_uuid(project.project_uuid)
    return validate_current_binding(project)


def reserved_roots(session) -> list[str]:
    """Roots reserved by the current Root Bindings; the only source of truth for new root availability.

    Fails closed when a Project has zero or several current bindings, or when its initial binding disagrees with
    its registration root: a root must never look free because a binding row is missing or contradictory.
    """
    projects = {project.id: project for project in session.scalars(select(Project))}
    current: dict[int, list[ProjectRootBinding]] = {}
    for binding in session.scalars(select(ProjectRootBinding).where(ProjectRootBinding.state == CURRENT_BINDING)):
        current.setdefault(binding.project_id, []).append(binding)
    validated = [validate_project_identity(project) for project in projects.values()]
    if set(current) - set(projects):
        raise BindingStateError("A Root Binding refers to a Project that does not exist")
    return [binding.root_path for binding in validated]


# M2-C agent registry. Roles are assignment values, never durable Agent properties, and readiness never has a
# ``verified`` value: a persisted declaration is intent, not an authenticated runtime capability.
ROLE_ARCHITECT = "architect"
ROLE_WORKER = "worker"
AGENT_CAPABILITY_DECLARED = "declared"
AGENT_CAPABILITY_UNAVAILABLE = "unavailable"
ASSIGNMENT_ACTIVE = "active"
ASSIGNMENT_REVOKED = "revoked"
READINESS_UNVERIFIED = "unverified"
READINESS_BLOCKED = "blocked"


class AgentDefinition(Base):
    """Stable global logical Agent. Not a model, runtime process, session, token or Owner.

    The identifier is an immutable random UUIDv4: it survives restart and edits, and it is never derived from a
    Project, model name, display label or provider session. Only descriptive metadata is stored here; credentials,
    host paths, commands and current provider sessions are deliberately not representable.
    """

    __tablename__ = "agent_definitions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_agent_definitions_version"),
        CheckConstraint("length(display_name) BETWEEN 1 AND 80", name="ck_agent_definitions_display_name"),
        CheckConstraint("icon_label IS NULL OR length(icon_label) BETWEEN 1 AND 32", name="ck_agent_definitions_icon"),
        CheckConstraint("length(runtime_adapter) BETWEEN 1 AND 64", name="ck_agent_definitions_adapter"),
        CheckConstraint("length(provider_id) BETWEEN 1 AND 64", name="ck_agent_definitions_provider"),
        CheckConstraint("length(model_id) BETWEEN 1 AND 96", name="ck_agent_definitions_model"),
    )

    agent_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80))
    icon_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    runtime_adapter: Mapped[str] = mapped_column(String(64))
    provider_id: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[str] = mapped_column(String(96))
    # Optimistic concurrency token for Owner edits; bumping it never changes the identity.
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    capabilities: Mapped[list[AgentCapability]] = relationship(
        back_populates="agent", lazy="selectin", order_by="AgentCapability.capability",
        cascade="all, delete-orphan")


class AgentCapability(Base):
    """One declared/expected capability of an Agent. ``declared`` is a declaration, never verification."""

    __tablename__ = "agent_capabilities"
    __table_args__ = (
        CheckConstraint("capability NOT GLOB '*[^a-z0-9._]*'", name="ck_agent_capabilities_syntax"),
        CheckConstraint("length(capability) BETWEEN 3 AND 64", name="ck_agent_capabilities_length"),
        CheckConstraint("status IN ('declared', 'unavailable')", name="ck_agent_capabilities_status"),
    )

    agent_id: Mapped[str] = mapped_column(ForeignKey("agent_definitions.agent_id", ondelete="CASCADE"), primary_key=True)
    capability: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default=AGENT_CAPABILITY_DECLARED)
    declared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    agent: Mapped[AgentDefinition] = relationship(back_populates="capabilities")


class RoleAssignment(Base):
    """Durable, versioned bounded intent: one Agent, one role, one Project and optionally one Task.

    An assignment is not activation, execution approval, a Run, a capability grant or effective Core write
    permission. Revocation keeps the row and its identifier as history instead of deleting it.
    """

    __tablename__ = "role_assignments"
    __table_args__ = (
        CheckConstraint("role IN ('architect', 'worker')", name="ck_role_assignments_role"),
        CheckConstraint("state IN ('active', 'revoked')", name="ck_role_assignments_state"),
        CheckConstraint("readiness IN ('unverified', 'blocked')", name="ck_role_assignments_readiness"),
        CheckConstraint("version >= 1", name="ck_role_assignments_version"),
        CheckConstraint("role <> 'architect' OR task_id IS NULL", name="ck_role_assignments_architect_scope"),
        CheckConstraint("state = 'active' OR revoked_at IS NOT NULL", name="ck_role_assignments_revoked_at"),
        Index("ix_role_assignments_agent_id", "agent_id"),
        Index("ix_role_assignments_project_id", "project_id"),
        Index("ix_role_assignments_task_id", "task_id"),
        # At most one current (active) Architect per Project.
        Index("ux_role_assignments_active_architect", "project_id", unique=True,
              sqlite_where=text("state = 'active' AND role = 'architect'")),
        # No duplicate active equivalent assignment: project-level and task-level scope are separate keys.
        Index("ux_role_assignments_active_project_scope", "agent_id", "project_id", "role", unique=True,
              sqlite_where=text("state = 'active' AND task_id IS NULL")),
        Index("ux_role_assignments_active_task_scope", "agent_id", "project_id", "task_id", "role", unique=True,
              sqlite_where=text("state = 'active' AND task_id IS NOT NULL")),
    )

    assignment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent_definitions.agent_id", ondelete="RESTRICT"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), default=ASSIGNMENT_ACTIVE)
    # ``unverified`` means the requested capabilities are merely declared; nothing here is an effective grant.
    readiness: Mapped[str] = mapped_column(String(16), default=READINESS_UNVERIFIED)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Explicit Owner attribution of the administrative decision (never an agent claim).
    created_by: Mapped[str] = mapped_column(String(64))
    created_by_assurance: Mapped[str] = mapped_column(String(32))
    revoked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revoked_by_assurance: Mapped[str | None] = mapped_column(String(32), nullable=True)
    capabilities: Mapped[list[AssignmentCapability]] = relationship(
        back_populates="assignment", lazy="selectin", order_by="AssignmentCapability.capability",
        cascade="all, delete-orphan")


class AssignmentCapability(Base):
    """One requested/declared capability of an assignment. Intent only; never an effective runtime grant."""

    __tablename__ = "assignment_capabilities"
    __table_args__ = (
        CheckConstraint("capability NOT GLOB '*[^a-z0-9._]*'", name="ck_assignment_capabilities_syntax"),
        CheckConstraint("length(capability) BETWEEN 3 AND 64", name="ck_assignment_capabilities_length"),
    )

    assignment_id: Mapped[str] = mapped_column(ForeignKey("role_assignments.assignment_id", ondelete="CASCADE"),
                                               primary_key=True)
    capability: Mapped[str] = mapped_column(String(64), primary_key=True)
    assignment: Mapped[RoleAssignment] = relationship(back_populates="capabilities")


# M2-D run provenance foundation (P013). ``external`` is observed/reported work done through the incumbent workflow;
# ``managed`` is reserved for a future ROSC-launched Run and cannot be created by the P013 service. No value here
# means verified, approved or accepted: every attribute of an external record is a claim by its source.
ORIGIN_EXTERNAL = "external"
ORIGIN_MANAGED = "managed"
ATTRIBUTION_CLAIMED = "claimed"
LINK_UNLINKED = "unlinked"
LINK_OWNER_CURATED = "owner_curated_correspondence"
PROVENANCE_CLAIMED = "claimed"
INGEST_MANUAL_OWNER = "manual_owner_registration"
EVIDENCE_REPORT_REF = "report_reference"
EVIDENCE_GIT_REF = "git_commit_reference"
EVIDENCE_TEST_REF = "test_result_reference"
EVIDENCE_OTHER_REF = "other_source_reference"
EVIDENCE_KINDS = (EVIDENCE_REPORT_REF, EVIDENCE_GIT_REF, EVIDENCE_TEST_REF, EVIDENCE_OTHER_REF)
REPORT_KINDS = ("executor_report", "review_report", "other_report")
REPORTED_OUTCOMES = ("reported_pass", "reported_fail", "reported_unknown")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


class RunRecord(Base):
    """Durable identity of one Run of a Task. Not the Task, an Agent, an assignment, a process or a provider Session.

    A Task may have any number of Runs. ``external`` rows are Owner-registered observations; every ``reported_executor``
    / ``source_*`` fact is an unverified claim and is never rewritten. ``agent_id`` is an Owner-curated correspondence,
    never proof of authorship, an activation or a permission.
    """

    __tablename__ = "run_records"
    __table_args__ = (
        CheckConstraint("execution_origin IN ('external', 'managed')", name="ck_run_records_origin"),
        CheckConstraint("source_attribution_assurance = 'claimed'", name="ck_run_records_assurance"),
        CheckConstraint("agent_link_status IN ('unlinked', 'owner_curated_correspondence')", name="ck_run_records_link"),
        CheckConstraint("(agent_link_status = 'unlinked') = (agent_id IS NULL)", name="ck_run_records_link_agent"),
        CheckConstraint("assignment_id IS NULL OR agent_id IS NOT NULL", name="ck_run_records_link_assignment"),
        CheckConstraint("attribution_revision >= 1", name="ck_run_records_revision"),
        CheckConstraint("source_key IS NULL OR length(source_key) BETWEEN 1 AND 128", name="ck_run_records_source_key"),
        CheckConstraint("length(source_kind) BETWEEN 1 AND 64", name="ck_run_records_source_kind"),
        CheckConstraint("reported_executor IS NULL OR length(reported_executor) BETWEEN 1 AND 96",
                        name="ck_run_records_executor"),
        Index("ix_run_records_project_id", "project_id"),
        Index("ix_run_records_task_id", "task_id"),
        Index("ix_run_records_agent_id", "agent_id"),
        Index("ux_run_records_source", "project_id", "source_kind", "source_key", unique=True,
              sqlite_where=text("source_key IS NOT NULL")),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"))
    execution_origin: Mapped[str] = mapped_column(String(16))
    source_kind: Mapped[str] = mapped_column(String(64))
    source_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_attribution_assurance: Mapped[str] = mapped_column(String(16), default=ATTRIBUTION_CLAIMED)
    reported_executor: Mapped[str | None] = mapped_column(String(96), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agent_definitions.agent_id", ondelete="RESTRICT"),
                                                 nullable=True)
    assignment_id: Mapped[str | None] = mapped_column(ForeignKey("role_assignments.assignment_id", ondelete="RESTRICT"),
                                                      nullable=True)
    agent_link_status: Mapped[str] = mapped_column(String(32), default=LINK_UNLINKED)
    attribution_revision: Mapped[int] = mapped_column(Integer, default=1)
    registered_by: Mapped[str] = mapped_column(String(64))
    registered_by_assurance: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class RunAgentLink(Base):
    """Immutable history of one Owner-curated Agent correspondence. The original claim stays on the RunRecord."""

    __tablename__ = "run_agent_links"
    __table_args__ = (
        CheckConstraint("expected_revision >= 1 AND resulting_revision = expected_revision + 1", name="ck_run_agent_links_revision"),
        Index("ux_run_agent_links_run_revision", "run_id", "resulting_revision", unique=True),
        Index("ix_run_agent_links_agent_id", "agent_id"),
    )

    link_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("run_records.run_id", ondelete="RESTRICT"))
    from_agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent_definitions.agent_id", ondelete="RESTRICT"))
    assignment_id: Mapped[str | None] = mapped_column(ForeignKey("role_assignments.assignment_id", ondelete="RESTRICT"),
                                                      nullable=True)
    original_claim: Mapped[str | None] = mapped_column(String(96), nullable=True)
    expected_revision: Mapped[int] = mapped_column(Integer)
    resulting_revision: Mapped[int] = mapped_column(Integer)
    linked_by: Mapped[str] = mapped_column(String(64))
    linked_by_assurance: Mapped[str] = mapped_column(String(32))
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ReportRecord(Base):
    """Reference to an externally produced report. Body, bytes and digest correctness are never held or checked."""

    __tablename__ = "report_records"
    __table_args__ = (
        CheckConstraint(_in_list("report_kind", REPORT_KINDS), name="ck_report_records_kind"),
        CheckConstraint("provenance = 'claimed'", name="ck_report_records_provenance"),
        CheckConstraint("ingest_origin = 'manual_owner_registration'", name="ck_report_records_ingest"),
        CheckConstraint("length(source_ref) BETWEEN 1 AND 200", name="ck_report_records_ref"),
        CheckConstraint("source_key IS NULL OR length(source_key) BETWEEN 1 AND 128", name="ck_report_records_source_key"),
        CheckConstraint("report_revision IS NULL OR report_revision >= 1", name="ck_report_records_revision"),
        CheckConstraint("declared_digest IS NULL OR (length(declared_digest) = 64 AND declared_digest NOT GLOB '*[^0-9a-f]*')",
                        name="ck_report_records_digest"),
        Index("ix_report_records_run_id", "run_id"),
        Index("ux_report_records_source", "run_id", "source_key", unique=True, sqlite_where=text("source_key IS NOT NULL")),
    )

    report_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("run_records.run_id", ondelete="RESTRICT"))
    source_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    report_kind: Mapped[str] = mapped_column(String(32))
    report_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_ref: Mapped[str] = mapped_column(String(200))
    declared_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance: Mapped[str] = mapped_column(String(16), default=PROVENANCE_CLAIMED)
    ingest_origin: Mapped[str] = mapped_column(String(32), default=INGEST_MANUAL_OWNER)
    registered_by: Mapped[str] = mapped_column(String(64))
    registered_by_assurance: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class EvidenceRecord(Base):
    """Reference to externally claimed evidence. A reference is never verification of what it points to."""

    __tablename__ = "evidence_records"
    __table_args__ = (
        CheckConstraint(_in_list("evidence_kind", EVIDENCE_KINDS), name="ck_evidence_records_kind"),
        CheckConstraint("provenance = 'claimed'", name="ck_evidence_records_provenance"),
        CheckConstraint("ingest_origin = 'manual_owner_registration'", name="ck_evidence_records_ingest"),
        CheckConstraint("length(reference) BETWEEN 1 AND 200", name="ck_evidence_records_ref"),
        CheckConstraint("source_key IS NULL OR length(source_key) BETWEEN 1 AND 128", name="ck_evidence_records_source_key"),
        CheckConstraint("reported_outcome IS NULL OR " + _in_list("reported_outcome", REPORTED_OUTCOMES),
                        name="ck_evidence_records_outcome"),
        CheckConstraint("reported_outcome IS NULL OR evidence_kind = 'test_result_reference'",
                        name="ck_evidence_records_outcome_kind"),
        CheckConstraint("declared_digest IS NULL OR (length(declared_digest) = 64 AND declared_digest NOT GLOB '*[^0-9a-f]*')",
                        name="ck_evidence_records_digest"),
        Index("ix_evidence_records_run_id", "run_id"),
        Index("ix_evidence_records_report_id", "report_id"),
        Index("ux_evidence_records_source", "run_id", "source_key", unique=True, sqlite_where=text("source_key IS NOT NULL")),
    )

    evidence_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("run_records.run_id", ondelete="RESTRICT"))
    report_id: Mapped[str | None] = mapped_column(ForeignKey("report_records.report_id", ondelete="RESTRICT"),
                                                  nullable=True)
    source_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence_kind: Mapped[str] = mapped_column(String(32))
    reference: Mapped[str] = mapped_column(String(200))
    declared_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reported_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    provenance: Mapped[str] = mapped_column(String(16), default=PROVENANCE_CLAIMED)
    ingest_origin: Mapped[str] = mapped_column(String(32), default=INGEST_MANUAL_OWNER)
    registered_by: Mapped[str] = mapped_column(String(64))
    registered_by_assurance: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# M3-A Project Conversation (P014). A conversation is a durable ROSC record. It is not a runtime process, a provider
# session, an activation, a delivery channel or an approval. ``owner`` is the only participant kind that may author in
# this slice; ``architect``/``worker`` are reserved logical identities that no P014 code path creates.
CONVERSATION_KIND_PROJECT = "project"
CONVERSATION_KIND_TASK = "task"
RECIPIENT_ARCHITECT = "architect"  # P038: stored addressee intent of a Task Conversation message; not a delivery
PARTICIPANT_OWNER = "owner"
PARTICIPANT_ARCHITECT = "architect"
PARTICIPANT_WORKER = "worker"
MAX_MESSAGE_BODY = 4000  # Owner-authored input only (API + ``conversation._body``); Architect replies have no cap
MAX_CLIENT_KEY = 128


class Conversation(Base):
    """One durable conversation. A Project has at most one canonical ``project`` conversation.

    ``task_id`` exists so a later Task Conversation uses the same model instead of a second one; P014 implements only
    the project scope and the service refuses to create a ``task`` conversation.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("kind IN ('project', 'task')", name="ck_conversations_kind"),
        CheckConstraint("(kind = 'project') = (task_id IS NULL)", name="ck_conversations_scope"),
        Index("ux_conversations_project", "project_id", unique=True, sqlite_where=text("kind = 'project'")),
        Index("ux_conversations_task", "task_id", unique=True, sqlite_where=text("kind = 'task'")),
        Index("ix_conversations_project_id", "project_id"),
    )

    conversation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    participants: Mapped[list[ConversationParticipant]] = relationship(
        back_populates="conversation", lazy="selectin", order_by="ConversationParticipant.created_at")


class ConversationParticipant(Base):
    """Logical participant identity inside one conversation, independent of any runtime session.

    ``actor_id``/``actor_assurance`` record the trusted ``ActorContext`` that created the participant. Membership is
    attribution history, never a permission: an Agent participant would still be denied by ``LocalOwnerPolicy``.
    """

    __tablename__ = "conversation_participants"
    __table_args__ = (
        CheckConstraint("kind IN ('owner', 'architect', 'worker')", name="ck_conversation_participants_kind"),
        CheckConstraint("(kind = 'owner') = (agent_id IS NULL)", name="ck_conversation_participants_agent"),
        CheckConstraint("length(actor_id) BETWEEN 1 AND 64", name="ck_conversation_participants_actor"),
        CheckConstraint("length(actor_assurance) BETWEEN 1 AND 32", name="ck_conversation_participants_assurance"),
        UniqueConstraint("conversation_id", "kind", "actor_id", name="uq_conversation_participants_identity"),
        Index("ux_conversation_participants_owner", "conversation_id", unique=True,
              sqlite_where=text("kind = 'owner'")),
    )

    participant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(64))
    actor_assurance: Mapped[str] = mapped_column(String(32))
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agent_definitions.agent_id", ondelete="RESTRICT"),
                                                 nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    conversation: Mapped[Conversation] = relationship(back_populates="participants")


class ConversationMessage(Base):
    """Immutable Owner-authored plain-text message with a stable per-conversation display order.

    ``client_key`` is a bounded, opaque caller-generated idempotency key scoped to one conversation; it is never a
    canonical identity and never the message text. ``ordinal`` is assigned inside the write transaction so concurrent
    inserts keep a deterministic order.
    """

    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint("ordinal >= 1", name="ck_conversation_messages_ordinal"),
        CheckConstraint("length(body) >= 1", name="ck_conversation_messages_body"),
        CheckConstraint(f"length(client_key) BETWEEN 8 AND {MAX_CLIENT_KEY}", name="ck_conversation_messages_key"),
        UniqueConstraint("conversation_id", "ordinal", name="uq_conversation_messages_ordinal"),
        UniqueConstraint("conversation_id", "client_key", name="uq_conversation_messages_client_key"),
        Index("ix_conversation_messages_sender", "sender_participant_id"),
    )

    message_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id", ondelete="RESTRICT"))
    sender_participant_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_participants.participant_id", ondelete="RESTRICT"))
    ordinal: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    client_key: Mapped[str] = mapped_column(String(MAX_CLIENT_KEY))
    recipient: Mapped[str | None] = mapped_column(String(16), nullable=True)  # None | 'architect' (intent only)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# M3-B Project Architect runtime (P015). A session row is ROSC's record of one explicitly activated runtime process and
# provider session. It is NOT canonical memory (the Project Conversation is) and a row never proves liveness: every
# ``ready`` claim is re-probed. Deliveries are mutable bookkeeping about one Owner message, kept apart from the
# immutable message rows.
SESSION_STARTING = "starting"
SESSION_READY = "ready"
ONBOARDING_LEGACY, ONBOARDING_INITIALIZING, ONBOARDING_READY = "legacy_unverified", "initializing", "ready"
ONBOARDING_BLOCKED, ONBOARDING_UNKNOWN = "blocked", "unknown"
SESSION_ERROR = "error"
SESSION_STOPPED = "stopped"
DELIVERY_NOT_SENT = "not_sent"
DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_FAILED = "failed"
DELIVERY_UNKNOWN = "unknown"
ASSURANCE_ADAPTER_VERIFIED = "adapter_verified"


class ArchitectSession(Base):
    __tablename__ = "architect_sessions"
    __table_args__ = (
        CheckConstraint("state IN ('starting', 'ready', 'error', 'stopped')", name="ck_architect_sessions_state"),
        Index("ix_architect_sessions_project_id", "project_id"),
        # At most one live (starting/ready) session per Project: a concurrent second activation loses at INSERT.
        Index("ux_architect_sessions_live", "project_id", unique=True,
              sqlite_where=text("state IN ('starting', 'ready')")),
    )

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent_definitions.agent_id", ondelete="RESTRICT"))
    assignment_id: Mapped[str] = mapped_column(ForeignKey("role_assignments.assignment_id", ondelete="RESTRICT"))
    state: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # Opaque provider-side identifiers; never a credential. ``process_ticks`` is the /proc start time that, with the
    # pid and a per-session marker, lets a later ROSC process prove it launched the process before touching it.
    runtime_session_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    process_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    process_ticks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    activated_by: Mapped[str] = mapped_column(String(64))
    activated_by_assurance: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # P029: honest state of the one-time model onboarding of a NEW native session.  ``legacy_unverified`` is every
    # session that existed before P029 (and is never onboarded afterwards); the other values are
    # initializing | ready | blocked | unknown.  Not a liveness or Tools signal.
    onboarding_state: Mapped[str] = mapped_column(String(20), default="legacy_unverified",
                                                  server_default="legacy_unverified")
    onboarding_detail: Mapped[str | None] = mapped_column(String(48), nullable=True)


class ConversationDelivery(Base):
    __tablename__ = "conversation_deliveries"
    __table_args__ = (
        CheckConstraint("state IN ('not_sent', 'pending', 'delivered', 'failed', 'unknown')",
                        name="ck_conversation_deliveries_state"),
        CheckConstraint("attempts >= 0", name="ck_conversation_deliveries_attempts"),
    )

    message_id: Mapped[str] = mapped_column(ForeignKey("conversation_messages.message_id", ondelete="RESTRICT"),
                                            primary_key=True)
    architect_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("architect_sessions.session_id", ondelete="RESTRICT"), nullable=True)
    state: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # Deterministic id sent to the provider so the attempt can be located again without re-sending.
    runtime_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reply_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_messages.message_id", ondelete="RESTRICT"), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    context_categories: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ConversationNativeSession(Base):
    """P039: which native provider conversation of ONE Architect host answers one ROSC Task Conversation.

    One logical Project Architect host (``ArchitectSession``) serves the Project Conversation through its own native
    session and each Task Conversation through a separate native session of the same OpenCode server.  The row is a
    pointer, not memory: it holds no text, and it is keyed by host incarnation, so a new host never reuses an old id.
    ``primed`` records that the short Task context has already been sent into this native session.
    """

    __tablename__ = "conversation_native_sessions"

    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.conversation_id", ondelete="RESTRICT"),
                                                 primary_key=True)
    architect_session_id: Mapped[str] = mapped_column(
        ForeignKey("architect_sessions.session_id", ondelete="RESTRICT"), primary_key=True)
    runtime_session_id: Mapped[str] = mapped_column(String(96))
    primed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# P019 Project Context Access. The audit rows record WHICH tool operation ran, never its content.


class ArchitectToolAudit(Base):
    __tablename__ = "architect_tool_audit"
    __table_args__ = (Index("ix_architect_tool_audit_project", "project_id"),)

    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    session_id: Mapped[str] = mapped_column(String(36))
    project_id: Mapped[int] = mapped_column(Integer)
    agent_id: Mapped[str] = mapped_column(String(36))
    tool: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(96))  # target Project and document/path identifier, no content
    outcome: Mapped[str] = mapped_column(String(48))


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="Draft")
    workspace_state: Mapped[str] = mapped_column(String(16), default="provisioning")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# P040 Task authoring. A Spec / Prompt revision is an immutable row; ``head``, ``stale`` and ``approved`` are derived
# from the rows (never stored as flags). ``Task.title/description/acceptance_criteria`` stay as a denormalized
# projection of the current Spec head, updated in the same transaction as the revision.
PROVENANCE_OWNER = "owner"
PROVENANCE_ARCHITECT = "architect"
PROVENANCE_LEGACY = "legacy_import"


class TaskSpecRevision(Base):
    __tablename__ = "task_spec_revisions"
    __table_args__ = (
        UniqueConstraint("task_id", "revision", name="uq_task_spec_revisions_task_revision"),
        CheckConstraint("revision >= 1", name="ck_task_spec_revisions_revision"),
        CheckConstraint("provenance IN ('owner', 'architect', 'legacy_import')", name="ck_task_spec_revisions_provenance"),
        Index("ix_task_spec_revisions_project_task", "project_id", "task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"))
    revision: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(160))
    scope: Mapped[str] = mapped_column(Text, default="")
    description_html: Mapped[str] = mapped_column(Text)  # canonical whitelist HTML (see rosc.rich_text)
    criteria_html: Mapped[str] = mapped_column(Text)
    digest: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[str] = mapped_column(String(16))
    actor_kind: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(64))
    actor_assurance: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PromptRevision(Base):
    __tablename__ = "prompt_revisions"
    __table_args__ = (
        UniqueConstraint("task_id", "revision", name="uq_prompt_revisions_task_revision"),
        CheckConstraint("revision >= 1", name="ck_prompt_revisions_revision"),
        CheckConstraint("length(summary) >= 1 AND length(body) >= 1", name="ck_prompt_revisions_text"),
        ForeignKeyConstraint(["task_id", "source_spec_revision"],
                             ["task_spec_revisions.task_id", "task_spec_revisions.revision"],
                             ondelete="RESTRICT", name="fk_prompt_revisions_spec"),
        Index("ix_prompt_revisions_project_task", "project_id", "task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"))
    revision: Mapped[int] = mapped_column(Integer)
    source_spec_revision: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    digest: Mapped[str] = mapped_column(String(64))
    rules_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)  # only a verified instruction/rules reference
    actor_kind: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(64))
    actor_assurance: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PromptApproval(Base):
    """Owner approval of ONE exact saved Prompt revision. Not a Run authorization."""

    __tablename__ = "prompt_approvals"
    __table_args__ = (
        UniqueConstraint("prompt_revision_id", name="uq_prompt_approvals_revision"),
        Index("ix_prompt_approvals_task", "task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prompt_revision_id: Mapped[int] = mapped_column(ForeignKey("prompt_revisions.id", ondelete="RESTRICT"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"))
    spec_revision: Mapped[int] = mapped_column(Integer)
    prompt_revision: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    actor_kind: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(64))
    actor_assurance: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ExecutionPackage(Base):
    """P041: minimal durable handle of the frozen contract of ONE approved Prompt revision.

    Spec/Prompt content is never copied; it is reconstructed from the immutable revisions reachable through
    ``prompt_approval_id``. Not a Run authorization and not bound to a Worker; created only by the internal Core builder.
    """

    __tablename__ = "execution_packages"
    __table_args__ = (
        UniqueConstraint("prompt_approval_id", name="uq_execution_packages_approval"),
        ForeignKeyConstraint(["project_id", "root_binding_version"],
                             ["project_root_bindings.project_id", "project_root_bindings.version"],
                             ondelete="RESTRICT", name="fk_execution_packages_binding"),
        CheckConstraint("root_binding_version >= 1", name="ck_execution_packages_binding"),
        Index("ix_execution_packages_task", "project_id", "task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"))
    prompt_approval_id: Mapped[int] = mapped_column(ForeignKey("prompt_approvals.id", ondelete="RESTRICT"))
    root_binding_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class WorkerSession(Base):
    """P042A: durable *binding* of one Project + registered Worker to an independently running CLI session in a
    ROSC-owned tmux server.  A row is evidence needed to re-check the session after an Engine restart; it is never
    proof that the session is alive or ready (state is derived from live tmux/process/ack observation on each read).
    Not a Run, not a Task context and not an AgentDefinition attribute."""

    __tablename__ = "worker_sessions"
    __table_args__ = (
        UniqueConstraint("project_id", "agent_id", name="uq_worker_sessions_project_agent"),
        UniqueConstraint("incarnation", name="uq_worker_sessions_incarnation"),
    )

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent_definitions.agent_id", ondelete="RESTRICT"))
    # Random nonce of the CURRENT process incarnation; the CLI-side acknowledgement must echo it.
    incarnation: Mapped[str] = mapped_column(String(36))
    tmux_socket: Mapped[str] = mapped_column(String(64))
    tmux_session: Mapped[str] = mapped_column(String(96))
    pane_id: Mapped[str] = mapped_column(String(32))
    pane_pid: Mapped[int] = mapped_column(Integer)
    # /proc start time (clock ticks since boot): guards against PID reuse.
    proc_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    workdir: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    # Last observation, for diagnostics only; never trusted as current state.
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
