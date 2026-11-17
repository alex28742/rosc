"""P053: Architect onboarding attempt identity and verified evidence (strictly additive).

Four columns on ``architect_sessions``:

* ``onboarding_attempt`` - index of the latest onboarding provider turn whose correlation id may have reached the
  provider (0 = none).  Written BEFORE a send, so a crash can never leave a sent turn without a lookup identity.
* ``onboarding_sent_at`` / ``onboarding_settled_at`` - the evidence window of that attempt.  ``settled_at`` NULL with
  ``sent_at`` set means the window is still open; the next provider turn of the Project closes it.
* ``onboarding_evidence`` - compact JSON of the required steps already proven by ``architect_tool_audit`` rows (audit
  ids, never Tool arguments or content).

Backfill, without fabricating anything: a pre-P053 row that went through the P029 onboarding (any state other than
``legacy_unverified``) gets ``attempt = 1`` (the deterministic P029 correlation id; a provider lookup decides whether it
was received), ``sent_at = created_at`` (no Tool credential exists before the session row) and ``settled_at`` = the
earliest recorded later provider turn of that session (an Owner delivery or an Architect group turn), or NULL when none
is recorded.  ``onboarding_evidence`` stays NULL: nothing is claimed until the audit is evaluated.  Never applied at
application start; the Owner backs up ``rosc.db`` and runs ``alembic upgrade head`` against an explicit data dir.

``downgrade`` drops only these four columns.
"""

from alembic import op
import sqlalchemy as sa

revision = "0018_architect_onboarding_attempt"
down_revision = "0017_group_round_provenance"
branch_labels = None
depends_on = None

_BACKFILL = """
UPDATE architect_sessions
   SET onboarding_attempt = 1,
       onboarding_sent_at = created_at,
       onboarding_settled_at = (
           SELECT MIN(t) FROM (
               SELECT d.created_at AS t FROM conversation_deliveries d
                WHERE d.architect_session_id = architect_sessions.session_id
                  AND d.created_at > architect_sessions.created_at
               UNION ALL
               SELECT g.created_at AS t FROM group_turns g
                 JOIN conversations c ON c.conversation_id = g.conversation_id
                WHERE g.agent_id = architect_sessions.agent_id
                  AND c.project_id = architect_sessions.project_id
                  AND g.created_at > architect_sessions.created_at))
 WHERE onboarding_state != 'legacy_unverified'
"""


def upgrade() -> None:
    op.add_column("architect_sessions", sa.Column("onboarding_attempt", sa.Integer(), nullable=False,
                                                  server_default="0"))
    op.add_column("architect_sessions", sa.Column("onboarding_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("architect_sessions", sa.Column("onboarding_settled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("architect_sessions", sa.Column("onboarding_evidence", sa.Text(), nullable=True))
    op.execute(_BACKFILL)


def downgrade() -> None:
    with op.batch_alter_table("architect_sessions") as batch:
        batch.drop_column("onboarding_evidence")
        batch.drop_column("onboarding_settled_at")
        batch.drop_column("onboarding_sent_at")
        batch.drop_column("onboarding_attempt")
