from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, select, text
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
