"""P059: event-driven Architect context maintenance (Core events, Architect obligations, wakes, canonical Pipeline).

Additive on top of 0023: SEVEN new tables and their triggers.  No existing table, column, CHECK or trigger is changed,
no table is rebuilt and NOTHING is backfilled: events are recorded only for Run Reports, decisive Architect Reviews and
Owner Task decisions committed after the upgrade, so no model turn is ever triggered for historical records.

* ``project_events`` -- immutable Project-scoped Core facts, one per authoritative source row
  (``uq_project_events_source``), with a per-Project sequence.  UPDATE/DELETE abort.
* ``architect_obligations`` -- the Architect's duty to assess one event (``pending -> processing -> completed``, or
  ``failed`` while still actionable).  A ``completed`` row can never change again.
* ``architect_maintenance_wakes`` -- fenced single-shot Core maintenance turns (attempt identity committed before the
  send; an ambiguous one is resolved by lookup, never re-sent blindly).
* ``pipeline_resources`` / ``pipeline_items`` / ``pipeline_changes`` / ``pipeline_proposals`` -- the Core-owned
  canonical ``pipeline/main``.  No resource row means *uninitialized*; only an Owner decision on an Architect proposal
  writes items, and every applied change is an immutable ``pipeline_changes`` row.

It never runs at application start: back up ``rosc.db`` while ROSC is stopped and run ``alembic upgrade head`` against
an explicit ``ROSC_DATA_DIR`` (see the P057 offline DB-upgrade helper).  ``downgrade`` REFUSES (changing nothing) once
any obligation was offered/processed/completed, any wake was sent or any Pipeline resource/proposal exists; otherwise
it drops the tables (untouched pending events only point at source rows that remain).  Never migrate real Owner data from an agent
run.
"""

from alembic import op
import sqlalchemy as sa

revision = "0024_architect_context_maintenance"
down_revision = "0023_project_local_task_number"
branch_labels = None
depends_on = None


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


KINDS = ("run_reported", "review_submitted", "task_accepted", "task_rework_requested")
SOURCES = ("managed_run_report", "managed_run_review", "task_owner_decision")
OBLIGATION_STATES = ("pending", "processing", "completed", "failed")
WAKE_TRIGGERS = ("ready", "event", "owner_retry")
WAKE_STATES = ("sent", "replied", "ambiguous", "failed", "not_received")
ITEM_KINDS = ("phase", "milestone", "step")
ITEM_STATUSES = ("backlog", "running", "completed", "accepted", "cancelled")
PROPOSAL_STATES = ("proposed", "accepted", "rejected", "stale")

_TABLES = ("pipeline_changes", "pipeline_items", "pipeline_proposals", "pipeline_resources",
           "architect_maintenance_wakes", "architect_obligations", "project_events")


def _immutable(table: str) -> dict[str, str]:
    return {
        f"tr_{table}_immutable": f"""CREATE TRIGGER tr_{table}_immutable BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT, '{table} row is immutable'); END""",
        f"tr_{table}_no_delete": f"""CREATE TRIGGER tr_{table}_no_delete BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT, '{table} row is immutable'); END""",
    }


_TRIGGERS = {
    **_immutable("project_events"),
    **_immutable("pipeline_changes"),
    # the event and the obligation must name the same Project; the obligation's sequence is the event's
    "tr_architect_obligations_scope": """CREATE TRIGGER tr_architect_obligations_scope BEFORE INSERT ON
        architect_obligations
        WHEN (SELECT project_id FROM project_events WHERE event_id = NEW.event_id) IS NOT NEW.project_id
          OR (SELECT seq FROM project_events WHERE event_id = NEW.event_id) IS NOT NEW.event_seq
          OR NEW.state != 'pending'
        BEGIN SELECT RAISE(ABORT, 'architect obligation scope'); END""",
    "tr_architect_obligations_terminal": """CREATE TRIGGER tr_architect_obligations_terminal BEFORE UPDATE ON
        architect_obligations
        WHEN OLD.state = 'completed' OR NEW.event_id IS NOT OLD.event_id OR NEW.project_id IS NOT OLD.project_id
          OR NEW.event_seq IS NOT OLD.event_seq
        BEGIN SELECT RAISE(ABORT, 'architect obligation is completed or its identity is immutable'); END""",
    "tr_architect_obligations_no_delete": """CREATE TRIGGER tr_architect_obligations_no_delete BEFORE DELETE ON
        architect_obligations BEGIN SELECT RAISE(ABORT, 'architect_obligations row is immutable'); END""",
    "tr_pipeline_proposals_decided": """CREATE TRIGGER tr_pipeline_proposals_decided BEFORE UPDATE ON pipeline_proposals
        WHEN OLD.state != 'proposed' OR NEW.ops IS NOT OLD.ops OR NEW.base_revision IS NOT OLD.base_revision
          OR NEW.project_id IS NOT OLD.project_id OR NEW.rationale IS NOT OLD.rationale
        BEGIN SELECT RAISE(ABORT, 'pipeline proposal is decided or its content is immutable'); END""",
    "tr_pipeline_resources_revision": """CREATE TRIGGER tr_pipeline_resources_revision BEFORE UPDATE ON
        pipeline_resources WHEN NEW.revision < OLD.revision OR NEW.revision > OLD.revision + 1
          OR NEW.project_id IS NOT OLD.project_id OR NEW.last_item_number < OLD.last_item_number
        BEGIN SELECT RAISE(ABORT, 'pipeline revision must advance by one'); END""",
}


def upgrade() -> None:
    op.create_table(
        "project_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("review_id", sa.String(36), nullable=True),
        sa.Column("decision_id", sa.String(36), nullable=True),
        sa.Column("digest", sa.String(64), nullable=True),
        sa.Column("facts", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_in("kind", KINDS), name="ck_project_events_kind"),
        sa.CheckConstraint(_in("source_kind", SOURCES), name="ck_project_events_source"),
        sa.CheckConstraint("seq >= 1", name="ck_project_events_seq"),
        sa.CheckConstraint("length(facts) <= 2000", name="ck_project_events_facts"),
        sa.UniqueConstraint("project_id", "seq", name="uq_project_events_seq"),
        sa.UniqueConstraint("source_kind", "source_id", name="uq_project_events_source"),
    )
    op.create_table(
        "architect_obligations",
        sa.Column("obligation_id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("project_events.event_id", ondelete="RESTRICT"),
                  nullable=False, unique=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("offered_wake_id", sa.String(36), nullable=True),
        sa.Column("offer_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("completed_agent_id", sa.String(36), nullable=True),
        sa.Column("completed_session_id", sa.String(36), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(48), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_in("state", OBLIGATION_STATES), name="ck_architect_obligations_state"),
    )
    op.create_index("ix_architect_obligations_project_state", "architect_obligations",
                    ["project_id", "state", "event_seq"])
    op.create_table(
        "architect_maintenance_wakes",
        sa.Column("wake_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("architect_sessions.session_id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("obligation_ids", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("detail", sa.String(48), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_in("trigger", WAKE_TRIGGERS), name="ck_architect_wakes_trigger"),
        sa.CheckConstraint(_in("state", WAKE_STATES), name="ck_architect_wakes_state"),
        sa.UniqueConstraint("session_id", "attempt", name="uq_architect_wakes_attempt"),
    )
    op.create_index("ix_architect_wakes_project", "architect_maintenance_wakes", ["project_id", "created_at"])
    op.create_table(
        "pipeline_resources",
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("last_item_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_pipeline_resources_revision"),
    )
    op.create_table(
        "pipeline_items",
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("pipeline_resources.project_id", ondelete="RESTRICT"),
                  primary_key=True),
        sa.Column("item_id", sa.String(16), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("parent_id", sa.String(16), nullable=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_revision", sa.Integer(), nullable=False),
        sa.Column("updated_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_in("kind", ITEM_KINDS), name="ck_pipeline_items_kind"),
        sa.CheckConstraint(_in("status", ITEM_STATUSES), name="ck_pipeline_items_status"),
    )
    op.create_index("ix_pipeline_items_status", "pipeline_items", ["project_id", "status"])
    op.create_table(
        "pipeline_changes",
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("pipeline_resources.project_id", ondelete="RESTRICT"),
                  primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.String(16), nullable=False),
        sa.Column("op", sa.String(16), nullable=False),
        sa.Column("before", sa.Text(), nullable=True),
        sa.Column("after", sa.Text(), nullable=False),
        sa.Column("proposal_id", sa.String(36), nullable=False),
        sa.Column("decided_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "pipeline_proposals",
        sa.Column("proposal_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("ops", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("source_event_ids", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("proposed_by_agent_id", sa.String(36), nullable=False),
        sa.Column("proposed_session_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(64), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("resulting_revision", sa.Integer(), nullable=True),
        sa.CheckConstraint(_in("state", PROPOSAL_STATES), name="ck_pipeline_proposals_state"),
        sa.CheckConstraint("base_revision >= 0", name="ck_pipeline_proposals_base"),
    )
    op.create_index("ix_pipeline_proposals_project", "pipeline_proposals", ["project_id", "created_at"])
    for statement in _TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    # Un-handled events only point at source rows that survive the downgrade (Report/Review/Decision), so dropping
    # them loses no authoritative fact.  Architect/Owner-authored maintenance history is never dropped.
    if (bind.exec_driver_sql("SELECT count(*) FROM architect_obligations WHERE state != 'pending' "
                             "OR offer_count > 0").scalar()
            or bind.exec_driver_sql("SELECT count(*) FROM architect_maintenance_wakes").scalar()
            or bind.exec_driver_sql("SELECT count(*) FROM pipeline_resources").scalar()
            or bind.exec_driver_sql("SELECT count(*) FROM pipeline_proposals").scalar()):
        raise RuntimeError("Refusing to downgrade: Architect maintenance outcomes, wakes or Pipeline records exist. "
                           "Downgrading would lose them; restore the pre-upgrade backup instead.")
    for name in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_index("ix_pipeline_proposals_project", table_name="pipeline_proposals")
    op.drop_index("ix_pipeline_items_status", table_name="pipeline_items")
    op.drop_index("ix_architect_wakes_project", table_name="architect_maintenance_wakes")
    op.drop_index("ix_architect_obligations_project_state", table_name="architect_obligations")
    for table in _TABLES:
        op.drop_table(table)
