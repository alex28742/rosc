"""Project Architect activation sessions and Owner-message delivery state (P015 / M3-B).

Additive only: two new tables and their indexes. No existing row, including every conversation, participant and
message written by P014, is read, rewritten or deleted. Owner back-up before upgrade: copy ``rosc.db`` while ROSC is
stopped, then run ``alembic upgrade head`` against an explicit ``ROSC_DATA_DIR``.

Existing Owner messages get no delivery row: absence of a row means "not sent" and nothing is ever delivered
retroactively.

``downgrade`` drops only the two P015 tables (and with them every session/delivery record, but never a message).
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_architect_runtime_sessions"
down_revision = "0005_project_conversation_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "architect_sessions",
        sa.Column("session_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent_definitions.agent_id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("assignment_id", sa.String(36),
                  sa.ForeignKey("role_assignments.assignment_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("runtime_session_id", sa.String(96), nullable=True),
        sa.Column("process_pid", sa.Integer(), nullable=True),
        sa.Column("process_ticks", sa.Integer(), nullable=True),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("activated_by", sa.String(64), nullable=False),
        sa.Column("activated_by_assurance", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('starting', 'ready', 'error', 'stopped')", name="ck_architect_sessions_state"),
    )
    op.create_index("ix_architect_sessions_project_id", "architect_sessions", ["project_id"])
    op.create_index("ux_architect_sessions_live", "architect_sessions", ["project_id"], unique=True,
                    sqlite_where=sa.text("state IN ('starting', 'ready')"))

    op.create_table(
        "conversation_deliveries",
        sa.Column("message_id", sa.String(36),
                  sa.ForeignKey("conversation_messages.message_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("architect_session_id", sa.String(36),
                  sa.ForeignKey("architect_sessions.session_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("runtime_message_id", sa.String(64), nullable=True),
        sa.Column("reply_message_id", sa.String(36),
                  sa.ForeignKey("conversation_messages.message_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("context_categories", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('not_sent', 'pending', 'delivered', 'failed', 'unknown')",
                           name="ck_conversation_deliveries_state"),
        sa.CheckConstraint("attempts >= 0", name="ck_conversation_deliveries_attempts"),
    )


def downgrade() -> None:
    op.drop_table("conversation_deliveries")
    op.drop_table("architect_sessions")
