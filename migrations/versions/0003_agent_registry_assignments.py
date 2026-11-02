"""Agent identity registry and scoped role assignments (P011 / M2-C).

Additive only: four new tables, their indexes and foreign keys. Existing ``projects``, ``project_root_bindings`` and
``tasks`` rows, plus every existing Workspace manifest, are left byte- and row-identical. No Project, Task, Agent or
assignment data is deleted or rewritten by the upgrade.

``downgrade`` drops only the four P011-owned tables (and their indexes). It is provided so an explicit, reviewed
rollback of an empty or disposable database is possible and must never be run against real user data.
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_agent_registry_assignments"
down_revision = "0002_project_identity_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_definitions",
        sa.Column("agent_id", sa.String(36), primary_key=True),
        sa.Column("display_name", sa.String(80), nullable=False),
        sa.Column("icon_label", sa.String(32), nullable=True),
        sa.Column("runtime_adapter", sa.String(64), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(96), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_agent_definitions_version"),
        sa.CheckConstraint("length(display_name) BETWEEN 1 AND 80", name="ck_agent_definitions_display_name"),
        sa.CheckConstraint("icon_label IS NULL OR length(icon_label) BETWEEN 1 AND 32", name="ck_agent_definitions_icon"),
        sa.CheckConstraint("length(runtime_adapter) BETWEEN 1 AND 64", name="ck_agent_definitions_adapter"),
        sa.CheckConstraint("length(provider_id) BETWEEN 1 AND 64", name="ck_agent_definitions_provider"),
        sa.CheckConstraint("length(model_id) BETWEEN 1 AND 96", name="ck_agent_definitions_model"),
    )

    op.create_table(
        "agent_capabilities",
        sa.Column("agent_id", sa.String(36),
                  sa.ForeignKey("agent_definitions.agent_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("capability", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("declared_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("capability NOT GLOB '*[^a-z0-9._]*'", name="ck_agent_capabilities_syntax"),
        sa.CheckConstraint("length(capability) BETWEEN 3 AND 64", name="ck_agent_capabilities_length"),
        sa.CheckConstraint("status IN ('declared', 'unavailable')", name="ck_agent_capabilities_status"),
    )

    op.create_table(
        "role_assignments",
        sa.Column("assignment_id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36),
                  sa.ForeignKey("agent_definitions.agent_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("readiness", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_by_assurance", sa.String(32), nullable=False),
        sa.Column("revoked_by", sa.String(64), nullable=True),
        sa.Column("revoked_by_assurance", sa.String(32), nullable=True),
        sa.CheckConstraint("role IN ('architect', 'worker')", name="ck_role_assignments_role"),
        sa.CheckConstraint("state IN ('active', 'revoked')", name="ck_role_assignments_state"),
        sa.CheckConstraint("readiness IN ('unverified', 'blocked')", name="ck_role_assignments_readiness"),
        sa.CheckConstraint("version >= 1", name="ck_role_assignments_version"),
        sa.CheckConstraint("role <> 'architect' OR task_id IS NULL", name="ck_role_assignments_architect_scope"),
        sa.CheckConstraint("state = 'active' OR revoked_at IS NOT NULL", name="ck_role_assignments_revoked_at"),
    )
    op.create_index("ix_role_assignments_agent_id", "role_assignments", ["agent_id"])
    op.create_index("ix_role_assignments_project_id", "role_assignments", ["project_id"])
    op.create_index("ix_role_assignments_task_id", "role_assignments", ["task_id"])
    op.create_index("ux_role_assignments_active_architect", "role_assignments", ["project_id"], unique=True,
                    sqlite_where=sa.text("state = 'active' AND role = 'architect'"))
    op.create_index("ux_role_assignments_active_project_scope", "role_assignments",
                    ["agent_id", "project_id", "role"], unique=True,
                    sqlite_where=sa.text("state = 'active' AND task_id IS NULL"))
    op.create_index("ux_role_assignments_active_task_scope", "role_assignments",
                    ["agent_id", "project_id", "task_id", "role"], unique=True,
                    sqlite_where=sa.text("state = 'active' AND task_id IS NOT NULL"))

    op.create_table(
        "assignment_capabilities",
        sa.Column("assignment_id", sa.String(36),
                  sa.ForeignKey("role_assignments.assignment_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("capability", sa.String(64), primary_key=True),
        sa.CheckConstraint("capability NOT GLOB '*[^a-z0-9._]*'", name="ck_assignment_capabilities_syntax"),
        sa.CheckConstraint("length(capability) BETWEEN 3 AND 64", name="ck_assignment_capabilities_length"),
    )


def downgrade() -> None:
    op.drop_table("assignment_capabilities")
    op.drop_index("ux_role_assignments_active_task_scope", table_name="role_assignments")
    op.drop_index("ux_role_assignments_active_project_scope", table_name="role_assignments")
    op.drop_index("ux_role_assignments_active_architect", table_name="role_assignments")
    op.drop_index("ix_role_assignments_task_id", table_name="role_assignments")
    op.drop_index("ix_role_assignments_project_id", table_name="role_assignments")
    op.drop_index("ix_role_assignments_agent_id", table_name="role_assignments")
    op.drop_table("role_assignments")
    op.drop_table("agent_capabilities")
    op.drop_table("agent_definitions")
