"""Project Context Access: standing provider-disclosure scopes and a content-free tool audit (P019).

Additive only: two new tables. No existing row is read, rewritten or deleted.  Never applied at application start;
the Owner backs up ``rosc.db`` (copy it while ROSC is stopped) and runs ``alembic upgrade head`` against an explicit
``ROSC_DATA_DIR``.  Until a scope row exists and is enabled nothing private is returned to a provider.

``downgrade`` drops only these two tables.
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_project_context_access"
down_revision = "0007_architect_reply_unbounded_body"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_disclosure_scopes",
        sa.Column("scope_id", sa.String(36), primary_key=True),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("covered", sa.String(8), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("category IN ('project_docs_own', 'project_docs_other', 'project_source')",
                           name="ck_disclosure_scopes_category"),
        sa.CheckConstraint("enabled IN (0, 1)", name="ck_disclosure_scopes_enabled"),
        sa.UniqueConstraint("provider_id", "model_id", "category", "covered", name="uq_disclosure_scopes_target"),
    )
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
    op.drop_table("provider_disclosure_scopes")
