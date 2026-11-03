"""Durable Project Conversation foundation (P014 / M3-A).

Additive only: three new tables, their indexes and guard triggers. Every existing ``projects``,
``project_root_bindings``, ``tasks``, ``agent_definitions``, ``agent_capabilities``, ``role_assignments``,
``assignment_capabilities``, ``run_records``, ``run_agent_links``, ``report_records`` and ``evidence_records`` row, and
every Workspace manifest, is left row- and byte-identical.

Triggers (SQLite cannot express them as declarative constraints without altering legacy tables):
* a message's sender participant must belong to the message's conversation;
* conversations, participants and messages are immutable: no update, no delete.

``downgrade`` drops only the P014-owned tables and triggers; never run it against real user data.
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_project_conversation_foundation"
down_revision = "0004_run_provenance_foundation"
branch_labels = None
depends_on = None

MAX_BODY = 4000
MAX_KEY = 128

_TRIGGERS = {
    "tr_conversation_messages_sender": """CREATE TRIGGER tr_conversation_messages_sender BEFORE INSERT ON conversation_messages
        WHEN (SELECT conversation_id FROM conversation_participants
              WHERE participant_id = NEW.sender_participant_id) IS NOT NEW.conversation_id
        BEGIN SELECT RAISE(ABORT, 'message sender/conversation mismatch'); END""",
    "tr_conversations_immutable": """CREATE TRIGGER tr_conversations_immutable BEFORE UPDATE ON conversations
        BEGIN SELECT RAISE(ABORT, 'conversation is immutable'); END""",
    "tr_conversations_no_delete": """CREATE TRIGGER tr_conversations_no_delete BEFORE DELETE ON conversations
        BEGIN SELECT RAISE(ABORT, 'conversation is immutable'); END""",
    "tr_conversation_participants_immutable": """CREATE TRIGGER tr_conversation_participants_immutable BEFORE UPDATE ON conversation_participants
        BEGIN SELECT RAISE(ABORT, 'participant is immutable'); END""",
    "tr_conversation_participants_no_delete": """CREATE TRIGGER tr_conversation_participants_no_delete BEFORE DELETE ON conversation_participants
        BEGIN SELECT RAISE(ABORT, 'participant is immutable'); END""",
    "tr_conversation_messages_immutable": """CREATE TRIGGER tr_conversation_messages_immutable BEFORE UPDATE ON conversation_messages
        BEGIN SELECT RAISE(ABORT, 'message is immutable'); END""",
    "tr_conversation_messages_no_delete": """CREATE TRIGGER tr_conversation_messages_no_delete BEFORE DELETE ON conversation_messages
        BEGIN SELECT RAISE(ABORT, 'message is immutable'); END""",
}


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('project', 'task')", name="ck_conversations_kind"),
        sa.CheckConstraint("(kind = 'project') = (task_id IS NULL)", name="ck_conversations_scope"),
    )
    op.create_index("ix_conversations_project_id", "conversations", ["project_id"])
    op.create_index("ux_conversations_project", "conversations", ["project_id"], unique=True,
                    sqlite_where=sa.text("kind = 'project'"))
    op.create_index("ux_conversations_task", "conversations", ["task_id"], unique=True,
                    sqlite_where=sa.text("kind = 'task'"))

    op.create_table(
        "conversation_participants",
        sa.Column("participant_id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36),
                  sa.ForeignKey("conversations.conversation_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("actor_assurance", sa.String(32), nullable=False),
        sa.Column("agent_id", sa.String(36),
                  sa.ForeignKey("agent_definitions.agent_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('owner', 'architect', 'worker')", name="ck_conversation_participants_kind"),
        sa.CheckConstraint("(kind = 'owner') = (agent_id IS NULL)", name="ck_conversation_participants_agent"),
        sa.CheckConstraint("length(actor_id) BETWEEN 1 AND 64", name="ck_conversation_participants_actor"),
        sa.CheckConstraint("length(actor_assurance) BETWEEN 1 AND 32", name="ck_conversation_participants_assurance"),
        sa.UniqueConstraint("conversation_id", "kind", "actor_id", name="uq_conversation_participants_identity"),
    )
    op.create_index("ux_conversation_participants_owner", "conversation_participants", ["conversation_id"],
                    unique=True, sqlite_where=sa.text("kind = 'owner'"))

    op.create_table(
        "conversation_messages",
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
        sa.CheckConstraint(f"length(body) BETWEEN 1 AND {MAX_BODY}", name="ck_conversation_messages_body"),
        sa.CheckConstraint(f"length(client_key) BETWEEN 8 AND {MAX_KEY}", name="ck_conversation_messages_key"),
        sa.UniqueConstraint("conversation_id", "ordinal", name="uq_conversation_messages_ordinal"),
        sa.UniqueConstraint("conversation_id", "client_key", name="uq_conversation_messages_client_key"),
    )
    op.create_index("ix_conversation_messages_sender", "conversation_messages", ["sender_participant_id"])

    for statement in _TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    for name in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table("conversation_messages")
    op.drop_table("conversation_participants")
    op.drop_table("conversations")
