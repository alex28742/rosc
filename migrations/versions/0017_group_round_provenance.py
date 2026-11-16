"""P052 r05: trusted group-round opening provenance (strictly additive, nullable columns; no data is rewritten).

``group_rounds.opened_via`` records the allowlisted Core origin of a round in the SAME transaction that opens it
(``owner_direct_request``, ``owner_group_message``, ``owner_authorize``, ``owner_renew``, ``owner_continue``,
``agent_invitation``).  ``previous_round_id`` / ``previous_stop_reason`` record the lineage of an explicit Owner
Continue (the pre-close stop reason of the round it continued).  Existing rows keep NULL = origin unknown: no
attribution is fabricated for historical rounds.
"""

from alembic import op
import sqlalchemy as sa

revision = "0017_group_round_provenance"
down_revision = "0016_worker_conversation_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("group_rounds", sa.Column("opened_via", sa.String(40), nullable=True))
    op.add_column("group_rounds", sa.Column("previous_round_id", sa.String(36), nullable=True))
    op.add_column("group_rounds", sa.Column("previous_stop_reason", sa.String(40), nullable=True))


def downgrade() -> None:
    op.drop_column("group_rounds", "previous_stop_reason")
    op.drop_column("group_rounds", "previous_round_id")
    op.drop_column("group_rounds", "opened_via")
