"""Task Conversation addressee (P038).

One nullable column (no CHECK, so no table rebuild) on ``conversation_messages``: ``recipient`` is ``NULL`` or ``'architect'``. It records the
Owner's chosen addressee only; it is never a delivery, queue entry or proof that anyone read the message. Existing
Project Conversation rows keep ``NULL``.
"""

from alembic import op
import sqlalchemy as sa

revision = "0010_task_conversation_recipient"
down_revision = "0009_architect_onboarding_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ADD COLUMN (no table rebuild) so the 0005/0007 immutability triggers on this table survive. The value is
    # validated by the API and ``conversation._recipient``.
    op.add_column("conversation_messages", sa.Column("recipient", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("conversation_messages", "recipient")  # SQLite ALTER ... DROP COLUMN keeps triggers and indexes
