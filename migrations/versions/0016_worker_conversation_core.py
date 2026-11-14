"""P043B (Prompt 044): Worker Conversation Core (additive tables only; existing rows untouched, nothing is started)."""

from alembic import op
import sqlalchemy as sa

revision = "0016_worker_conversation_core"
down_revision = "0015_worker_bootstrap"
branch_labels = None
depends_on = None

_HOLDING = "state IN ('reserved', 'submitted', 'accepted', 'reply_captured', 'unknown')"
_TURN_STATES = ("'queued', 'reserved', 'submitted', 'accepted', 'reply_captured', 'published', 'deferred', 'failed', "
                "'unknown', 'cancelled'")


def _fk(target: str):
    return sa.ForeignKey(target, ondelete="RESTRICT")


def _ts(name: str, nullable: bool = False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "group_rounds",
        sa.Column("round_id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), _fk("conversations.conversation_id"), nullable=False),
        sa.Column("round_no", sa.Integer, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("per_agent_quota", sa.Integer, nullable=False),
        sa.Column("current_pass", sa.Integer, nullable=False),
        sa.Column("authorizing_message_id", sa.String(36), _fk("conversation_messages.message_id"), nullable=True),
        sa.Column("authorized_by", sa.String(64), nullable=False),
        sa.Column("authorized_by_assurance", sa.String(32), nullable=False),
        _ts("created_at"), _ts("closed_at", True),
        sa.CheckConstraint("state IN ('open', 'awaiting_owner', 'closed')", name="ck_group_rounds_state"),
        sa.CheckConstraint("per_agent_quota >= 1", name="ck_group_rounds_quota"),
        sa.CheckConstraint("round_no >= 1 AND current_pass >= 0", name="ck_group_rounds_numbers"),
        sa.UniqueConstraint("conversation_id", "round_no", name="uq_group_rounds_number"),
    )
    op.create_index("ux_group_rounds_current", "group_rounds", ["conversation_id"], unique=True,
                    sqlite_where=sa.text("state <> 'closed'"))

    op.create_table(
        "agent_native_lanes",
        sa.Column("lane_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.Integer, _fk("projects.id"), nullable=False),
        sa.Column("agent_id", sa.String(36), _fk("agent_definitions.agent_id"), nullable=False),
        sa.Column("conversation_id", sa.String(36), _fk("conversations.conversation_id"), nullable=False),
        sa.Column("root_binding_version", sa.Integer, nullable=False),
        sa.Column("adapter", sa.String(64), nullable=False),
        sa.Column("native_session_id", sa.String(36), nullable=False, unique=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("invalidated_reason", sa.String(48), nullable=True),
        _ts("created_at"), _ts("invalidated_at", True),
        sa.CheckConstraint("state IN ('active', 'invalidated')", name="ck_native_lanes_state"),
        sa.CheckConstraint("length(adapter) BETWEEN 1 AND 64", name="ck_native_lanes_adapter"),
        sa.ForeignKeyConstraint(["project_id", "root_binding_version"],
                                ["project_root_bindings.project_id", "project_root_bindings.version"],
                                ondelete="RESTRICT", name="fk_native_lanes_binding"),
    )
    op.create_index("ux_native_lanes_active", "agent_native_lanes", ["agent_id", "conversation_id"], unique=True,
                    sqlite_where=sa.text("state = 'active'"))
    op.create_index("ix_native_lanes_project", "agent_native_lanes", ["project_id"])

    op.create_table(
        "group_turns",
        sa.Column("turn_id", sa.String(36), primary_key=True),
        sa.Column("round_id", sa.String(36), _fk("group_rounds.round_id"), nullable=False),
        sa.Column("conversation_id", sa.String(36), _fk("conversations.conversation_id"), nullable=False),
        sa.Column("agent_id", sa.String(36), _fk("agent_definitions.agent_id"), nullable=False),
        sa.Column("lane_id", sa.String(36), _fk("agent_native_lanes.lane_id"), nullable=True),
        sa.Column("pass_no", sa.Integer, nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("retry_of", sa.String(36), _fk("group_turns.turn_id"), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("correlation_id", sa.String(36), nullable=False, unique=True),
        sa.Column("input_after_ordinal", sa.Integer, nullable=True),
        sa.Column("input_through_ordinal", sa.Integer, nullable=True),
        sa.Column("catchup_choice", sa.String(16), nullable=True),
        sa.Column("captured_reply", sa.Text, nullable=True),
        sa.Column("captured_to", sa.Text, nullable=True),
        sa.Column("captured_reply_to", sa.String(36), nullable=True),
        sa.Column("reply_message_id", sa.String(36), _fk("conversation_messages.message_id"), nullable=True,
                  unique=True),
        sa.Column("error_code", sa.String(48), nullable=True),
        _ts("created_at"), _ts("updated_at"),
        sa.CheckConstraint(f"state IN ({_TURN_STATES})", name="ck_group_turns_state"),
        sa.CheckConstraint("attempt IN (1, 2)", name="ck_group_turns_attempt"),
        sa.CheckConstraint("(attempt = 1) = (retry_of IS NULL)", name="ck_group_turns_retry"),
        sa.CheckConstraint("pass_no >= 0 AND position >= 0", name="ck_group_turns_position"),
        sa.CheckConstraint("catchup_choice IS NULL OR catchup_choice IN ('send_full', 'skip_backlog')",
                           name="ck_group_turns_catchup"),
        sa.CheckConstraint("(state = 'published') = (reply_message_id IS NOT NULL)", name="ck_group_turns_published"),
        sa.UniqueConstraint("round_id", "pass_no", "position", name="uq_group_turns_slot"),
    )
    op.create_index("ux_group_turns_room_holding", "group_turns", ["conversation_id"], unique=True,
                    sqlite_where=sa.text(_HOLDING))
    op.create_index("ux_group_turns_lane_holding", "group_turns", ["lane_id"], unique=True,
                    sqlite_where=sa.text(_HOLDING + " AND lane_id IS NOT NULL"))
    op.create_index("ix_group_turns_round", "group_turns", ["round_id", "pass_no"])

    op.create_table(
        "group_round_grants",
        sa.Column("round_id", sa.String(36), _fk("group_rounds.round_id"), primary_key=True),
        sa.Column("agent_id", sa.String(36), _fk("agent_definitions.agent_id"), primary_key=True),
        sa.Column("quota", sa.Integer, nullable=False),
        sa.Column("published_count", sa.Integer, nullable=False),
        sa.Column("failed", sa.Boolean, nullable=False),
        sa.CheckConstraint("quota >= 1 AND published_count >= 0 AND published_count <= quota",
                           name="ck_group_round_grants_quota"),
    )

    op.create_table(
        "conversation_message_envelopes",
        sa.Column("message_id", sa.String(36), _fk("conversation_messages.message_id"), primary_key=True),
        sa.Column("reply_to_message_id", sa.String(36), _fk("conversation_messages.message_id"), nullable=True),
        sa.Column("round_id", sa.String(36), _fk("group_rounds.round_id"), nullable=True),
        sa.Column("turn_id", sa.String(36), _fk("group_turns.turn_id"), nullable=True, unique=True),
        sa.Column("sender_agent_id", sa.String(36), _fk("agent_definitions.agent_id"), nullable=True),
    )

    op.create_table(
        "conversation_message_parties",
        sa.Column("message_id", sa.String(36), _fk("conversation_messages.message_id"), primary_key=True),
        sa.Column("role", sa.String(16), primary_key=True),
        sa.Column("party_ref", sa.String(36), primary_key=True),
        sa.Column("party_kind", sa.String(16), nullable=False),
        sa.Column("agent_id", sa.String(36), _fk("agent_definitions.agent_id"), nullable=True),
        sa.CheckConstraint("role IN ('to', 'audience')", name="ck_message_parties_role"),
        sa.CheckConstraint("party_kind IN ('owner', 'architect', 'worker')", name="ck_message_parties_kind"),
        sa.CheckConstraint("(party_kind = 'owner') = (agent_id IS NULL)", name="ck_message_parties_agent"),
        sa.CheckConstraint("party_ref = coalesce(agent_id, 'owner')", name="ck_message_parties_ref"),
    )

    op.create_table(
        "conversation_members",
        sa.Column("conversation_id", sa.String(36), _fk("conversations.conversation_id"), primary_key=True),
        sa.Column("agent_id", sa.String(36), _fk("agent_definitions.agent_id"), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("epoch", sa.Integer, nullable=False),
        sa.Column("epoch_baseline_ordinal", sa.Integer, nullable=False),
        sa.Column("release_offer_ack_missed", sa.Integer, nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        _ts("updated_at"),
        sa.CheckConstraint("role IN ('architect', 'worker')", name="ck_conversation_members_role"),
        sa.CheckConstraint("state IN ('active', 'passive', 'released')", name="ck_conversation_members_state"),
        sa.CheckConstraint("epoch >= 1", name="ck_conversation_members_epoch"),
        sa.CheckConstraint("epoch_baseline_ordinal >= 0", name="ck_conversation_members_baseline"),
        sa.CheckConstraint("release_offer_ack_missed >= 0", name="ck_conversation_members_ack"),
    )

    op.create_table(
        "conversation_member_gaps",
        sa.Column("gap_id", sa.Integer, primary_key=True),
        sa.Column("conversation_id", sa.String(36), _fk("conversations.conversation_id"), nullable=False),
        sa.Column("agent_id", sa.String(36), _fk("agent_definitions.agent_id"), nullable=False),
        sa.Column("epoch", sa.Integer, nullable=False),
        sa.Column("from_ordinal", sa.Integer, nullable=False),
        sa.Column("through_ordinal", sa.Integer, nullable=False),
        sa.Column("reason", sa.String(24), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        _ts("created_at"),
        sa.CheckConstraint("reason IN ('owner_skip', 'released_period')", name="ck_member_gaps_reason"),
        sa.CheckConstraint("from_ordinal >= 1 AND through_ordinal >= from_ordinal", name="ck_member_gaps_range"),
    )
    op.create_index("ix_member_gaps_member", "conversation_member_gaps", ["conversation_id", "agent_id", "epoch"])

    op.create_table(
        "agent_inbound_cursors",
        sa.Column("conversation_id", sa.String(36), _fk("conversations.conversation_id"), primary_key=True),
        sa.Column("agent_id", sa.String(36), _fk("agent_definitions.agent_id"), primary_key=True),
        sa.Column("epoch", sa.Integer, primary_key=True),
        sa.Column("lane_id", sa.String(36), _fk("agent_native_lanes.lane_id"), primary_key=True),
        sa.Column("applied_through_ordinal", sa.Integer, nullable=False),
        sa.Column("last_turn_id", sa.String(36), _fk("group_turns.turn_id"), nullable=True),
        _ts("updated_at"),
        sa.CheckConstraint("applied_through_ordinal >= 0", name="ck_inbound_cursors_ordinal"),
    )

    op.create_table(
        "conversation_dispatch_controls",
        sa.Column("conversation_id", sa.String(36), _fk("conversations.conversation_id"), primary_key=True),
        sa.Column("paused", sa.Boolean, nullable=False),
        sa.Column("emergency_brake", sa.Boolean, nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        _ts("updated_at"),
    )

    op.create_table(
        "rosc_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value_int", sa.Integer, nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        _ts("updated_at"),
    )


def downgrade() -> None:
    for table in ("rosc_settings", "conversation_dispatch_controls", "agent_inbound_cursors",
                  "conversation_member_gaps", "conversation_members", "conversation_message_parties",
                  "conversation_message_envelopes", "group_round_grants", "group_turns", "agent_native_lanes",
                  "group_rounds"):
        op.drop_table(table)
