"""Remove the 4000-character upper bound on ``conversation_messages.body`` (P018).

Owner input stays capped at 4000 characters by the API and ``conversation._body``; a completed Project Architect reply
has no application-level length limit, so the table CHECK becomes ``length(body) >= 1``.

SQLite cannot alter a CHECK in place, so the table is rebuilt (create new, copy every row verbatim, drop old, rename)
inside the migration transaction.  Row identity (``message_id``), ordinals, attribution, ``client_key``, timestamps,
unique constraints, the sender index and the immutability / sender-match triggers are preserved; the dependent
``conversation_deliveries`` foreign keys keep resolving by name.  The migration refuses to run when foreign-key
enforcement is on (Alembic's own connection leaves it off), verifies row counts and ``PRAGMA foreign_key_check`` and
aborts, rolling everything back, on any mismatch.  It never runs at application start; back up ``rosc.db`` first
(copy it while ROSC is stopped) and run ``alembic upgrade head`` against an explicit ``ROSC_DATA_DIR``.

``downgrade`` restores the 4000-character CHECK, but REFUSES (changing nothing) if any stored body is longer: it never
truncates or deletes a reply.  Restore the pre-upgrade backup instead if that happens.
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_architect_reply_unbounded_body"
down_revision = "0006_architect_runtime_sessions"
branch_labels = None
depends_on = None

MAX_OWNER_BODY = 4000
MAX_KEY = 128

_TRIGGERS = {
    "tr_conversation_messages_sender": """CREATE TRIGGER tr_conversation_messages_sender BEFORE INSERT ON conversation_messages
        WHEN (SELECT conversation_id FROM conversation_participants
              WHERE participant_id = NEW.sender_participant_id) IS NOT NEW.conversation_id
        BEGIN SELECT RAISE(ABORT, 'message sender/conversation mismatch'); END""",
    "tr_conversation_messages_immutable": """CREATE TRIGGER tr_conversation_messages_immutable BEFORE UPDATE ON conversation_messages
        BEGIN SELECT RAISE(ABORT, 'message is immutable'); END""",
    "tr_conversation_messages_no_delete": """CREATE TRIGGER tr_conversation_messages_no_delete BEFORE DELETE ON conversation_messages
        BEGIN SELECT RAISE(ABORT, 'message is immutable'); END""",
}


def _rebuild(body_check: str) -> None:
    """Alembic's SQLite DDL is not transactional by itself, so the whole swap is one explicit SQLite transaction:
    any failure rolls back to the untouched original table."""
    bind = op.get_bind()
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError("Refusing to rebuild conversation_messages with foreign-key enforcement ON; "
                           "run through Alembic's own connection (PRAGMA foreign_keys=OFF).")
    bind.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        _swap(bind, body_check)
    except BaseException:
        bind.exec_driver_sql("ROLLBACK")
        raise
    bind.exec_driver_sql("COMMIT")


def _swap(bind, body_check: str) -> None:
    before = bind.exec_driver_sql("SELECT count(*) FROM conversation_messages").scalar()
    bind.exec_driver_sql("DROP TABLE IF EXISTS conversation_messages_new")
    op.create_table(
        "conversation_messages_new",
        sa.Column("message_id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36),
                  sa.ForeignKey("conversations.conversation_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sender_participant_id", sa.String(36),
                  sa.ForeignKey("conversation_participants.participant_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("client_key", sa.String(MAX_KEY), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ordinal >= 1", name="ck_conversation_messages_ordinal"),
        sa.CheckConstraint(body_check, name="ck_conversation_messages_body"),
        sa.CheckConstraint(f"length(client_key) BETWEEN 8 AND {MAX_KEY}", name="ck_conversation_messages_key"),
        sa.UniqueConstraint("conversation_id", "ordinal", name="uq_conversation_messages_ordinal"),
        sa.UniqueConstraint("conversation_id", "client_key", name="uq_conversation_messages_client_key"),
    )
    bind.exec_driver_sql(
        "INSERT INTO conversation_messages_new (message_id, conversation_id, sender_participant_id, ordinal, body, "
        "client_key, created_at) SELECT message_id, conversation_id, sender_participant_id, ordinal, body, client_key, "
        "created_at FROM conversation_messages ORDER BY rowid")
    if bind.exec_driver_sql("SELECT count(*) FROM conversation_messages_new").scalar() != before:
        raise RuntimeError("conversation_messages rebuild lost rows; aborted")
    op.drop_table("conversation_messages")  # also drops its index and triggers
    op.rename_table("conversation_messages_new", "conversation_messages")
    op.create_index("ix_conversation_messages_sender", "conversation_messages", ["sender_participant_id"])
    for statement in _TRIGGERS.values():
        op.execute(statement)
    if bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("foreign_key_check failed after rebuilding conversation_messages; aborted")


def upgrade() -> None:
    _rebuild("length(body) >= 1")


def downgrade() -> None:
    bind = op.get_bind()
    longest = bind.exec_driver_sql("SELECT max(length(body)) FROM conversation_messages").scalar() or 0
    if longest > MAX_OWNER_BODY:
        raise RuntimeError(f"Refusing to downgrade: a stored message has {longest} characters (> {MAX_OWNER_BODY}). "
                           "Downgrading would lose content; restore the pre-upgrade backup instead.")
    _rebuild(f"length(body) BETWEEN 1 AND {MAX_OWNER_BODY}")
