"""Project Context Access: a content-free Architect tool audit (P019).

Additive only: one new table. No existing row is read, rewritten or deleted.  Never applied at application start;
the Owner backs up ``rosc.db`` (copy it while ROSC is stopped) and runs ``alembic upgrade head`` against an explicit
``ROSC_DATA_DIR``.  Revision 0008 was never applied to real data, so it is edited here rather than superseded.

``downgrade`` drops only this table.
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_project_context_access"
down_revision = "0007_architect_reply_unbounded_body"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "architect_tool_audit",
        sa.Column("audit_id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("tool", sa.String(32), nullable=False),
        sa.Column("target", sa.String(96), nullable=False),
        sa.Column("outcome", sa.String(48), nullable=False),
    )
    op.create_index("ix_architect_tool_audit_project", "architect_tool_audit", ["project_id"])


def downgrade() -> None:
    op.drop_table("architect_tool_audit")
