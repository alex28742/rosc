"""Architect native-session onboarding state (P029).

Additive only: two columns on ``architect_sessions``.  Every existing row (including the Owner's long-lived native
session) becomes ``legacy_unverified``: it is neither onboarded nor reset, and no model request is made for it.  No
other value is written here.  Never applied at application start; the Owner backs up ``rosc.db`` (copy it while ROSC
is stopped) and runs ``alembic upgrade head`` against an explicit ``ROSC_DATA_DIR``.

``downgrade`` drops only these two columns.
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_architect_onboarding_state"
down_revision = "0008_project_context_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("architect_sessions", sa.Column("onboarding_state", sa.String(20), nullable=False,
                                                  server_default="legacy_unverified"))
    op.add_column("architect_sessions", sa.Column("onboarding_detail", sa.String(48), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("architect_sessions") as batch:
        batch.drop_column("onboarding_detail")
        batch.drop_column("onboarding_state")
