"""P042A: durable Project + Worker CLI session binding (additive; no data changes, nothing is started)."""

from alembic import op
import sqlalchemy as sa

revision = "0014_worker_sessions"
down_revision = "0013_execution_package_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_sessions",
        sa.Column("session_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent_definitions.agent_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("incarnation", sa.String(36), nullable=False),
        sa.Column("tmux_socket", sa.String(64), nullable=False),
        sa.Column("tmux_session", sa.String(96), nullable=False),
        sa.Column("pane_id", sa.String(32), nullable=False),
        sa.Column("pane_pid", sa.Integer, nullable=False),
        sa.Column("proc_start", sa.Integer, nullable=True),
        sa.Column("workdir", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_state", sa.String(16), nullable=True),
        sa.UniqueConstraint("project_id", "agent_id", name="uq_worker_sessions_project_agent"),
        sa.UniqueConstraint("incarnation", name="uq_worker_sessions_incarnation"),
    )


def downgrade() -> None:
    op.drop_table("worker_sessions")
