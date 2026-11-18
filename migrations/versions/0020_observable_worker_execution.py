"""P054B: observable Worker execution - execution continuation states + Core import of local Worker artifacts.

Additive on top of 0019 (P013 ``run_records``/``report_records``/``evidence_records``, their rows and triggers are never
touched):

* ``managed_run_states`` gains the execution-continuation columns (``exec_token``, ``exec_intent_at``,
  ``exec_submitted_at``, ``reported_at``, ``outcome``) and four states: ``exec_dispatching`` and ``running`` (holding),
  ``reported`` and ``not_executed`` (terminal).  The holding partial unique indexes, the state CHECKs and the
  identity/transition trigger are re-created for the new sets; an execution intent is write-once and a Run can never go
  back from it to E0 states.
* ``managed_run_events`` accepts the new ``to_state`` values.
* New Core-owned import tables: ``managed_run_imports`` (mutable cursor), ``managed_run_journal_events`` and
  ``managed_run_plan_revisions`` (immutable imported Worker claims), ``managed_run_reports`` (managed Report registry:
  file path identity + digest, distinct from the P013 Owner-registered ``report_records``) and
  ``managed_run_handoffs`` (receipt of the ONE Worker -> Architect message per terminal Run).

SQLite cannot alter a CHECK in place, so the two P054A tables are rebuilt (create new, copy every row verbatim, drop old,
rename) in ONE explicit SQLite transaction, as in 0007.  It refuses to run with foreign-key enforcement ON (Alembic's own
connection leaves it off), verifies row counts and ``PRAGMA foreign_key_check`` and rolls everything back on any
mismatch.  It never runs at application start: back up ``rosc.db`` (copy it while ROSC is stopped) and run
``alembic upgrade head`` against an explicit ``ROSC_DATA_DIR``.

``downgrade`` REFUSES (changing nothing) when any Run uses a P054B state/column or any import row exists; it never
deletes Run history.  Restore the pre-upgrade backup instead.  Never downgrade real Owner data.
"""

from alembic import op
import sqlalchemy as sa

revision = "0020_observable_worker_execution"
down_revision = "0019_managed_run_foundation"
branch_labels = None
depends_on = None

_OLD_STATES = ("authorized", "dispatch_pending", "dispatching", "submitted", "acknowledged", "declined", "unknown",
               "not_delivered")
_NEW_STATES = _OLD_STATES + ("exec_dispatching", "running", "reported", "not_executed")
_OLD_HOLDING = ("authorized", "dispatch_pending", "dispatching", "submitted", "acknowledged", "unknown")
_NEW_HOLDING = _OLD_HOLDING + ("exec_dispatching", "running")
_OUTCOMES = ("completed", "partial", "blocked", "failed")
_E0_STATES = ("authorized", "dispatch_pending", "not_delivered", "dispatching", "submitted", "acknowledged", "declined",
              "not_executed")
_STATE_COLUMNS = ("run_id", "project_id", "task_id", "package_id", "prompt_approval_id", "spec_revision", "spec_digest",
                  "prompt_revision", "prompt_digest", "root_binding_version", "agent_id", "worker_session_id",
                  "incarnation", "pane_pid", "proc_start", "protocol_version", "protocol_sha256", "launch_key",
                  "run_token", "ack_timeout_seconds", "state", "reason", "manifest_sha256", "state_revision",
                  "created_at", "updated_at", "dispatch_intent_at", "submitted_at", "acknowledged_at")
_EXEC_COLUMNS = ("exec_token", "exec_intent_at", "exec_submitted_at", "reported_at", "outcome")
_EVENT_COLUMNS = ("event_id", "run_id", "seq", "from_state", "to_state", "reason", "evidence", "actor", "created_at")
_NEW_TABLES = ("managed_run_handoffs", "managed_run_reports", "managed_run_plan_revisions",
               "managed_run_journal_events", "managed_run_imports")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


_SHAPE_TRIGGER = """CREATE TRIGGER tr_run_records_managed_shape BEFORE INSERT ON run_records
        WHEN (NEW.execution_origin = 'managed') IS NOT (NEW.source_kind = 'rosc.managed_run')
          OR (NEW.execution_origin = 'managed' AND (NEW.source_key IS NULL OR NEW.agent_id IS NOT NULL
              OR NEW.assignment_id IS NOT NULL OR NEW.reported_executor IS NOT NULL
              OR NEW.agent_link_status != 'unlinked'))
        BEGIN SELECT RAISE(ABORT, 'managed run record shape'); END"""


def _state_triggers(extended: bool) -> dict[str, str]:
    terminal = ("declined", "not_delivered", "reported", "not_executed") if extended else ("declined", "not_delivered")
    extra = ""
    if extended:
        extra = (f"""
          OR (OLD.exec_token IS NOT NULL AND NEW.exec_token IS NOT OLD.exec_token)
          OR (OLD.exec_intent_at IS NOT NULL AND NEW.exec_intent_at IS NOT OLD.exec_intent_at)
          OR (OLD.exec_intent_at IS NOT NULL AND {_in('NEW.state', _E0_STATES)})""")
    return {
        "tr_managed_run_states_origin": """CREATE TRIGGER tr_managed_run_states_origin BEFORE INSERT ON managed_run_states
        WHEN (SELECT execution_origin FROM run_records WHERE run_id = NEW.run_id) IS NOT 'managed'
          OR (SELECT project_id FROM run_records WHERE run_id = NEW.run_id) IS NOT NEW.project_id
          OR (SELECT task_id FROM run_records WHERE run_id = NEW.run_id) IS NOT NEW.task_id
          OR (SELECT source_key FROM run_records WHERE run_id = NEW.run_id) IS NOT NEW.launch_key
        BEGIN SELECT RAISE(ABORT, 'managed state requires its managed run record'); END""",
        "tr_managed_run_states_identity": f"""CREATE TRIGGER tr_managed_run_states_identity BEFORE UPDATE ON managed_run_states
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
          OR {_in('OLD.state', terminal)}
          OR (OLD.dispatch_intent_at IS NOT NULL AND NEW.state IN ('authorized', 'dispatch_pending', 'not_delivered')){extra}
        BEGIN SELECT RAISE(ABORT, 'managed run state transition refused'); END""",
        "tr_managed_run_states_no_delete": """CREATE TRIGGER tr_managed_run_states_no_delete BEFORE DELETE ON managed_run_states
        BEGIN SELECT RAISE(ABORT, 'managed run state is permanent'); END""",
    }


_EVENT_TRIGGERS = {
    "tr_managed_run_events_run": """CREATE TRIGGER tr_managed_run_events_run BEFORE INSERT ON managed_run_events
        WHEN (SELECT run_id FROM managed_run_states WHERE run_id = NEW.run_id) IS NULL
        BEGIN SELECT RAISE(ABORT, 'managed run event requires its managed run'); END""",
    "tr_managed_run_events_immutable": """CREATE TRIGGER tr_managed_run_events_immutable BEFORE UPDATE ON managed_run_events
        BEGIN SELECT RAISE(ABORT, 'managed run event is immutable'); END""",
    "tr_managed_run_events_no_delete": """CREATE TRIGGER tr_managed_run_events_no_delete BEFORE DELETE ON managed_run_events
        BEGIN SELECT RAISE(ABORT, 'managed run event is immutable'); END""",
}


def _immutable(table: str) -> dict[str, str]:
    return {
        f"tr_{table}_immutable": f"""CREATE TRIGGER tr_{table}_immutable BEFORE UPDATE ON {table}
        BEGIN SELECT RAISE(ABORT, '{table} row is immutable'); END""",
        f"tr_{table}_no_delete": f"""CREATE TRIGGER tr_{table}_no_delete BEFORE DELETE ON {table}
        BEGIN SELECT RAISE(ABORT, '{table} row is immutable'); END""",
    }


_IMPORT_TRIGGERS = {
    **_immutable("managed_run_journal_events"), **_immutable("managed_run_plan_revisions"),
    **_immutable("managed_run_reports"), **_immutable("managed_run_handoffs"),
    "tr_managed_run_imports_no_delete": """CREATE TRIGGER tr_managed_run_imports_no_delete BEFORE DELETE ON managed_run_imports
        BEGIN SELECT RAISE(ABORT, 'managed_run_imports row is permanent'); END""",
    # a handoff only for a reported Run, attributed in the Run's own Task Conversation
    "tr_managed_run_handoffs_scope": """CREATE TRIGGER tr_managed_run_handoffs_scope BEFORE INSERT ON managed_run_handoffs
        WHEN (SELECT state FROM managed_run_states WHERE run_id = NEW.run_id) IS NOT 'reported'
          OR (SELECT task_id FROM conversations WHERE conversation_id = NEW.conversation_id)
             IS NOT (SELECT task_id FROM managed_run_states WHERE run_id = NEW.run_id)
          OR (SELECT conversation_id FROM conversation_messages WHERE message_id = NEW.message_id)
             IS NOT NEW.conversation_id
        BEGIN SELECT RAISE(ABORT, 'managed run handoff scope'); END""",
}


def _create_states(name: str, extended: bool) -> None:
    states = _NEW_STATES if extended else _OLD_STATES
    intent = ("'dispatching', 'submitted', 'acknowledged', 'declined', 'exec_dispatching', 'running', 'reported', "
              "'not_executed'" if extended else "'dispatching', 'submitted', 'acknowledged', 'declined'")
    columns = [
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
    ]
    constraints = [
        sa.CheckConstraint(_in("state", states), name="ck_managed_run_states_state"),
        sa.CheckConstraint("ack_timeout_seconds BETWEEN 30 AND 3600", name="ck_managed_run_states_timeout"),
        sa.CheckConstraint("state_revision >= 1", name="ck_managed_run_states_revision"),
        sa.CheckConstraint("spec_revision >= 1 AND prompt_revision >= 1 AND root_binding_version >= 1",
                           name="ck_managed_run_states_revisions"),
        sa.CheckConstraint(f"state NOT IN ({intent}) OR (dispatch_intent_at IS NOT NULL AND manifest_sha256 IS NOT NULL)",
                           name="ck_managed_run_states_intent"),
        sa.UniqueConstraint("run_token", name="uq_managed_run_states_token"),
        sa.ForeignKeyConstraint(["project_id", "root_binding_version"],
                                ["project_root_bindings.project_id", "project_root_bindings.version"],
                                ondelete="RESTRICT", name="fk_managed_run_states_binding"),
    ]
    if extended:
        columns += [
            sa.Column("exec_token", sa.String(36), nullable=True),
            sa.Column("exec_intent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("exec_submitted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("outcome", sa.String(16), nullable=True),
        ]
        constraints += [
            sa.CheckConstraint("state NOT IN ('exec_dispatching', 'running', 'reported') OR (exec_intent_at IS NOT NULL "
                               "AND exec_token IS NOT NULL AND acknowledged_at IS NOT NULL)",
                               name="ck_managed_run_states_exec"),
            sa.CheckConstraint("state != 'not_executed' OR (acknowledged_at IS NOT NULL AND exec_intent_at IS NULL)",
                               name="ck_managed_run_states_not_executed"),
            sa.CheckConstraint(f"(state = 'reported') = (outcome IS NOT NULL) AND (outcome IS NULL OR "
                               f"{_in('outcome', _OUTCOMES)})", name="ck_managed_run_states_outcome"),
            sa.UniqueConstraint("exec_token", name="uq_managed_run_states_exec_token"),
        ]
    op.create_table(name, *columns, *constraints)


def _create_events(name: str, extended: bool) -> None:
    op.create_table(
        name,
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
        sa.CheckConstraint(_in("to_state", _NEW_STATES if extended else _OLD_STATES), name="ck_managed_run_events_state"),
        sa.CheckConstraint("evidence IS NULL OR length(evidence) <= 2000", name="ck_managed_run_events_evidence"),
        sa.UniqueConstraint("run_id", "seq", name="uq_managed_run_events_seq"),
    )


def _create_import_tables() -> None:
    op.create_table(
        "managed_run_imports",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("managed_run_states.run_id", ondelete="RESTRICT"),
                  primary_key=True),
        sa.Column("journal_seq", sa.Integer(), nullable=False),
        sa.Column("journal_hash", sa.String(64), nullable=True),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("checkpoint_sha256", sa.String(64), nullable=True),
        sa.Column("checkpoint_seq", sa.Integer(), nullable=True),
        sa.Column("checkpoint_body", sa.Text(), nullable=True),
        sa.Column("torn_tail_bytes", sa.Integer(), nullable=False),
        sa.Column("problem", sa.String(48), nullable=True),
        sa.Column("problem_detail", sa.String(200), nullable=True),
        sa.Column("last_event_at", sa.String(32), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("journal_seq >= 0 AND plan_revision >= 0", name="ck_managed_run_imports_cursor"),
    )
    op.create_table(
        "managed_run_journal_events",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("managed_run_states.run_id", ondelete="RESTRICT"),
                  primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(32), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("step_id", sa.String(32), nullable=True),
        sa.Column("plan_revision", sa.Integer(), nullable=True),
        sa.Column("at", sa.String(32), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("seq >= 1", name="ck_managed_run_journal_events_seq"),
        sa.CheckConstraint("length(data) <= 65536", name="ck_managed_run_journal_events_data"),
        sa.UniqueConstraint("run_id", "event_id", name="uq_managed_run_journal_events_id"),
    )
    op.create_table(
        "managed_run_plan_revisions",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("managed_run_states.run_id", ondelete="RESTRICT"),
                  primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_managed_run_plan_revisions_revision"),
    )
    op.create_table(
        "managed_run_reports",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("managed_run_states.run_id", ondelete="RESTRICT"),
                  primary_key=True),
        sa.Column("report_path", sa.String(64), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("journal_seq", sa.Integer(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_in("outcome", _OUTCOMES), name="ck_managed_run_reports_outcome"),
    )
    op.create_table(
        "managed_run_handoffs",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("managed_run_states.run_id", ondelete="RESTRICT"),
                  primary_key=True),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("conversation_messages.message_id", ondelete="RESTRICT"),
                  nullable=False, unique=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.conversation_id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("addressed_to", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def _swap(bind, extended: bool) -> None:
    counts = {t: bind.exec_driver_sql(f"SELECT count(*) FROM {t}").scalar()
              for t in ("managed_run_states", "managed_run_events")}
    for name in ("managed_run_states_new", "managed_run_events_new"):
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {name}")
    _create_states("managed_run_states_new", extended)
    _create_events("managed_run_events_new", extended)
    columns = ", ".join(_STATE_COLUMNS)
    bind.exec_driver_sql(f"INSERT INTO managed_run_states_new ({columns}) SELECT {columns} FROM managed_run_states "
                         "ORDER BY rowid")
    events = ", ".join(_EVENT_COLUMNS)
    bind.exec_driver_sql(f"INSERT INTO managed_run_events_new ({events}) SELECT {events} FROM managed_run_events "
                         "ORDER BY rowid")
    for table, before in counts.items():
        if bind.exec_driver_sql(f"SELECT count(*) FROM {table}_new").scalar() != before:
            raise RuntimeError(f"{table} rebuild lost rows; aborted")
    op.drop_table("managed_run_events")   # also drops its triggers
    op.drop_table("managed_run_states")   # also drops its indexes and triggers
    op.rename_table("managed_run_states_new", "managed_run_states")
    op.rename_table("managed_run_events_new", "managed_run_events")
    holding = _NEW_HOLDING if extended else _OLD_HOLDING
    op.create_index("ix_managed_run_states_task", "managed_run_states", ["project_id", "task_id"])
    op.create_index("ux_managed_run_states_task_holding", "managed_run_states", ["task_id"], unique=True,
                    sqlite_where=sa.text(_in("state", holding)))
    op.create_index("ux_managed_run_states_worker_holding", "managed_run_states", ["project_id", "agent_id"],
                    unique=True, sqlite_where=sa.text(_in("state", holding)))
    for statement in (*_state_triggers(extended).values(), *_EVENT_TRIGGERS.values()):
        op.execute(statement)
    if bind.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE name = 'tr_run_records_managed_shape'").scalar() != 1:
        op.execute(_SHAPE_TRIGGER)


def _transaction(bind, body) -> None:
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError("Refusing to rebuild managed_run_states with foreign-key enforcement ON; "
                           "run through Alembic's own connection (PRAGMA foreign_keys=OFF).")
    bind.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        body()
        if bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("foreign_key_check failed after rebuilding the managed Run tables; aborted")
    except BaseException:
        bind.exec_driver_sql("ROLLBACK")
        raise
    bind.exec_driver_sql("COMMIT")


def upgrade() -> None:
    bind = op.get_bind()

    def body() -> None:
        _swap(bind, extended=True)
        _create_import_tables()
        for statement in _IMPORT_TRIGGERS.values():
            op.execute(statement)
    _transaction(bind, body)


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.exec_driver_sql(
        f"SELECT count(*) FROM managed_run_states WHERE {_in('state', _NEW_STATES[len(_OLD_STATES):])} "
        "OR exec_token IS NOT NULL OR exec_intent_at IS NOT NULL OR outcome IS NOT NULL").scalar()
    rows = sum(bind.exec_driver_sql(f"SELECT count(*) FROM {t}").scalar() for t in _NEW_TABLES)
    if used or rows:
        raise RuntimeError("Refusing to downgrade: P054B execution history exists (states/import rows). Downgrading "
                           "would lose Run history; restore the pre-upgrade backup instead.")

    def body() -> None:
        for table in _NEW_TABLES:
            op.drop_table(table)
        _swap(bind, extended=False)
    _transaction(bind, body)
