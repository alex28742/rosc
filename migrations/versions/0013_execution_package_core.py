"""Execution Package core (P041): one compact handle per approved Prompt revision. Additive; no data changes."""

from alembic import op
import sqlalchemy as sa

revision = "0013_execution_package_core"
down_revision = "0012_task_spec_prompt_authoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_packages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("prompt_approval_id", sa.Integer, sa.ForeignKey("prompt_approvals.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("root_binding_version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("prompt_approval_id", name="uq_execution_packages_approval"),
        sa.CheckConstraint("root_binding_version >= 1", name="ck_execution_packages_binding"),
        sa.ForeignKeyConstraint(["project_id", "root_binding_version"],
                                ["project_root_bindings.project_id", "project_root_bindings.version"],
                                ondelete="RESTRICT", name="fk_execution_packages_binding"),
    )
    op.create_index("ix_execution_packages_task", "execution_packages", ["project_id", "task_id"])


def downgrade() -> None:
    op.drop_table("execution_packages")
