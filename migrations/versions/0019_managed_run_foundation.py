"""P054A: managed Run foundation - 1:1 managed lifecycle state + append-only lifecycle events (strictly additive).

* ``managed_run_states`` - exactly one row per ``execution_origin='managed'`` P013 ``run_records`` row, keyed by the SAME
  ``run_id`` (one canonical Run identity).  It snapshots the frozen Execution Package, the selected Worker, its CLI
  session/incarnation, the pinned Worker protocol and the launch idempotency key, and carries the Core-trusted delivery
  state (authorized -> dispatch_pending -> dispatching -> submitted -> acknowledged / declined, or unknown /
  not_delivered).
* ``managed_run_events`` - append-only audit of every guarded state transition of that Run.

P013 tables, their rows and their triggers are untouched.  New guards (triggers + partial unique indexes):

* a managed RunRecord must use the namespaced ``rosc.managed_run`` source kind with a launch key and carry no
  external claim/attribution; that source kind is reserved for managed Runs (INSERT only, existing rows unaffected);
* a state row may only attach to a managed RunRecord of the same Project/Task; its identity snapshot is immutable;
  terminal states never change; rows are never deleted;
* at most one *holding* managed Run per Task and per (Project, Worker): a second writer is refused by the database,
  without any Project-wide lock;
* events attach to an existing managed state, are immutable and never deleted.

A RunRecord and its state are created in one transaction by ``rosc.run_launch``; SQLite has no deferred constraint
to force the pair, so that invariant is transactional (and tested), not declarative.

``downgrade`` drops only the P054A tables and triggers; never run it against real user data.
"""

from alembic import op
import sqlalchemy as sa

revision = "0019_managed_run_foundation"
down_revision = "0018_architect_onboarding_attempt"
branch_labels = None
depends_on = None

_STATES = ("authorized", "dispatch_pending", "dispatching", "submitted", "acknowledged", "declined", "unknown",
           "not_delivered")
_HOLDING = ("authorized", "dispatch_pending", "dispatching", "submitted", "acknowledged", "unknown")
_TERMINAL = ("declined", "not_delivered")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


_TRIGGERS = {
    "tr_run_records_managed_shape": """CREATE TRIGGER tr_run_records_managed_shape BEFORE INSERT ON run_records
        WHEN (NEW.execution_origin = 'managed') IS NOT (NEW.source_kind = 'rosc.managed_run')
          OR (NEW.execution_origin = 'managed' AND (NEW.source_key IS NULL OR NEW.agent_id IS NOT NULL
              OR NEW.assignment_id IS NOT NULL OR NEW.reported_executor IS NOT NULL
              OR NEW.agent_link_status != 'unlinked'))
        BEGIN SELECT RAISE(ABORT, 'managed run record shape'); END""",
    "tr_managed_run_states_origin": """CREATE TRIGGER tr_managed_run_states_origin BEFORE INSERT ON managed_run_states
        WHEN (SELECT execution_origin FROM run_records WHERE run_id = NEW.run_id) IS NOT 'managed'
          OR (SELECT project_id FROM run_records WHERE run_id = NEW.run_id) IS NOT NEW.project_id
          OR (SELECT task_id FROM run_records WHERE run_id = NEW.run_id) IS NOT NEW.task_id
          OR (SELECT source_key FROM run_records WHERE run_id = NEW.run_id) IS NOT NEW.launch_key
        BEGIN SELECT RAISE(ABORT, 'managed state requires its managed run record'); END""",
    "tr_managed_run_states_identity": """CREATE TRIGGER tr_managed_run_states_identity BEFORE UPDATE ON managed_run_states
        WHEN NEW.run_id IS NOT OLD.run_id OR NEW.project_id IS NOT OLD.project_id OR NEW.task_id IS NOT OLD.task_id
          OR NEW.package_id IS NOT OLD.package_id OR NEW.prompt_approval_id IS NOT OLD.prompt_approval_id
          OR NEW.spec_revision IS NOT OLD.spec_revision OR NEW.spec_digest IS NOT OLD.spec_digest
          OR NEW.prompt_revision IS NOT OLD.prompt_revision OR NEW.prompt_digest IS NOT OLD.prompt_digest
          OR NEW.root_binding_version IS NOT OLD.root_binding_version OR NEW.agent_id IS NOT OLD.agent_id
          OR NEW.worker_session_id IS NOT OLD.worker_session_id OR NEW.incarnation IS NOT OLD.incarnation
          OR NEW.pane_pid IS NOT OLD.pane_pid OR NEW.proc_start IS NOT OLD.proc_start
          OR NEW.protocol_version IS NOT OLD.protocol_version OR NEW.protocol_sha256 IS NOT OLD.protocol_sha256
          OR NEW.launch_key IS NOT OLD.launch_key OR NEW.run_token IS NOT OLD.run_token
          OR NEW.ack_timeout_seconds IS NOT OLD.ack_timeout_seconds OR NEW.created_at IS NOT OLD.created_at
          OR (OLD.manifest_sha256 IS NOT NULL AND NEW.manifest_sha256 IS NOT OLD.manifest_sha256)
          OR (OLD.dispatch_intent_at IS NOT NULL AND NEW.dispatch_intent_at IS NOT OLD.dispatch_intent_at)
          OR OLD.state IN ('declined', 'not_delivered')
          OR (OLD.dispatch_intent_at IS NOT NULL AND NEW.state IN ('authorized', 'dispatch_pending', 'not_delivered'))
        BEGIN SELECT RAISE(ABORT, 'managed run state transition refused'); END""",
    "tr_managed_run_states_no_delete": """CREATE TRIGGER tr_managed_run_states_no_delete BEFORE DELETE ON managed_run_states
        BEGIN SELECT RAISE(ABORT, 'managed run state is permanent'); END""",
    "tr_managed_run_events_run": """CREATE TRIGGER tr_managed_run_events_run BEFORE INSERT ON managed_run_events
        WHEN (SELECT run_id FROM managed_run_states WHERE run_id = NEW.run_id) IS NULL
        BEGIN SELECT RAISE(ABORT, 'managed run event requires its managed run'); END""",
    "tr_managed_run_events_immutable": """CREATE TRIGGER tr_managed_run_events_immutable BEFORE UPDATE ON managed_run_events
        BEGIN SELECT RAISE(ABORT, 'managed run event is immutable'); END""",
    "tr_managed_run_events_no_delete": """CREATE TRIGGER tr_managed_run_events_no_delete BEFORE DELETE ON managed_run_events
        BEGIN SELECT RAISE(ABORT, 'managed run event is immutable'); END""",
}


def upgrade() -> None:
    op.create_table(
        "managed_run_states",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run_records.run_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("package_id", sa.Integer(), sa.ForeignKey("execution_packages.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("prompt_approval_id", sa.Integer(), sa.ForeignKey("prompt_approvals.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("spec_revision", sa.Integer(), nullable=False),
        sa.Column("spec_digest", sa.String(64), nullable=False),
        sa.Column("prompt_revision", sa.Integer(), nullable=False),
        sa.Column("prompt_digest", sa.String(64), nullable=False),
        sa.Column("root_binding_version", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agent_definitions.agent_id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("worker_session_id", sa.String(36),
                  sa.ForeignKey("worker_sessions.session_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("incarnation", sa.String(36), nullable=False),
        sa.Column("pane_pid", sa.Integer(), nullable=False),
        sa.Column("proc_start", sa.Integer(), nullable=True),
        sa.Column("protocol_version", sa.String(32), nullable=False),
        sa.Column("protocol_sha256", sa.String(64), nullable=False),
        sa.Column("launch_key", sa.String(96), nullable=False),
        sa.Column("run_token", sa.String(36), nullable=False),
        sa.Column("ack_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(48), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=True),
        sa.Column("state_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatch_intent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_in("state", _STATES), name="ck_managed_run_states_state"),
        sa.CheckConstraint("ack_timeout_seconds BETWEEN 30 AND 3600", name="ck_managed_run_states_timeout"),
        sa.CheckConstraint("state_revision >= 1", name="ck_managed_run_states_revision"),
        sa.CheckConstraint("spec_revision >= 1 AND prompt_revision >= 1 AND root_binding_version >= 1",
                           name="ck_managed_run_states_revisions"),
        sa.CheckConstraint("state NOT IN ('dispatching', 'submitted', 'acknowledged', 'declined') "
                           "OR (dispatch_intent_at IS NOT NULL AND manifest_sha256 IS NOT NULL)",
                           name="ck_managed_run_states_intent"),
        sa.UniqueConstraint("run_token", name="uq_managed_run_states_token"),
        sa.ForeignKeyConstraint(["project_id", "root_binding_version"],
                                ["project_root_bindings.project_id", "project_root_bindings.version"],
                                ondelete="RESTRICT", name="fk_managed_run_states_binding"),
    )
    op.create_index("ix_managed_run_states_task", "managed_run_states", ["project_id", "task_id"])
    op.create_index("ux_managed_run_states_task_holding", "managed_run_states", ["task_id"], unique=True,
                    sqlite_where=sa.text(_in("state", _HOLDING)))
    op.create_index("ux_managed_run_states_worker_holding", "managed_run_states", ["project_id", "agent_id"],
                    unique=True, sqlite_where=sa.text(_in("state", _HOLDING)))

    op.create_table(
        "managed_run_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("managed_run_states.run_id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(16), nullable=True),
        sa.Column("to_state", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(48), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("seq >= 1", name="ck_managed_run_events_seq"),
        sa.CheckConstraint(_in("to_state", _STATES), name="ck_managed_run_events_state"),
        sa.CheckConstraint("evidence IS NULL OR length(evidence) <= 2000", name="ck_managed_run_events_evidence"),
        sa.UniqueConstraint("run_id", "seq", name="uq_managed_run_events_seq"),
    )

    for statement in _TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    for name in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table("managed_run_events")
    op.drop_table("managed_run_states")
