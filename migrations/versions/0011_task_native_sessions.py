"""Task Conversation native sessions (P039).

One small pointer table: which native provider conversation of an Architect host answers a ROSC Task Conversation.
It stores identifiers only (no message text, no credential) and is keyed by (conversation, host incarnation).
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_task_native_sessions"
down_revision = "0010_task_conversation_recipient"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_native_sessions",
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.conversation_id", ondelete="RESTRICT"),
                  primary_key=True),
        sa.Column("architect_session_id", sa.String(36),
                  sa.ForeignKey("architect_sessions.session_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("runtime_session_id", sa.String(96), nullable=False),
        sa.Column("primed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("conversation_native_sessions")
