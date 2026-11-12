"""P042A r02: bootstrap-request evidence columns on worker_sessions (additive, nullable; nothing is started)."""

from alembic import op
import sqlalchemy as sa

revision = "0015_worker_bootstrap"
down_revision = "0014_worker_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("worker_sessions", sa.Column("bootstrap_token", sa.String(36), nullable=True))
    op.add_column("worker_sessions", sa.Column("bootstrap_digest", sa.String(64), nullable=True))
    op.add_column("worker_sessions", sa.Column("bootstrap_sources", sa.Text, nullable=True))
    op.add_column("worker_sessions", sa.Column("bootstrap_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("worker_sessions") as batch:
        batch.drop_column("bootstrap_sent_at")
        batch.drop_column("bootstrap_sources")
        batch.drop_column("bootstrap_digest")
        batch.drop_column("bootstrap_token")
