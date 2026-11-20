"""P060: guaranteed Architect assessment of Worker messages addressed to the Project Architect.

Additive on top of 0024, nothing backfilled:

* ``project_events`` CHECKs gain the event kind ``worker_message`` and the source kind ``conversation_message``.  SQLite
  cannot alter a CHECK in place, so the table is rebuilt (create new, copy every row verbatim, drop old, rename) in ONE
  explicit SQLite transaction, as in 0020.  Its immutability triggers and the obligation-scope trigger that names it are
  recreated unchanged.  It refuses to run with foreign-key enforcement ON (Alembic's own connection leaves it off),
  verifies row counts and ``PRAGMA foreign_key_check`` and rolls everything back on any failure.
* ``architect_maintenance_wakes.conversation_id`` (nullable): the PC/TC room whose native Architect session received a
  chat-assessment wake; NULL keeps the P059 meaning (the Project session).

Historical messages (before the upgrade) get no event.  It never runs at application start: back up ``rosc.db`` while
ROSC is stopped and use the P057 offline DB-upgrade helper.  ``downgrade`` REFUSES (changing nothing) once any
``worker_message`` event or room wake exists.
"""

from alembic import op
import sqlalchemy as sa

revision = "0025_worker_message_assessment"
down_revision = "0024_architect_context_maintenance"
branch_labels = None
depends_on = None

OLD_KINDS = ("run_reported", "review_submitted", "task_accepted", "task_rework_requested")
OLD_SOURCES = ("managed_run_report", "managed_run_review", "task_owner_decision")
NEW_KINDS = OLD_KINDS + ("worker_message",)
NEW_SOURCES = OLD_SOURCES + ("conversation_message",)
_COLUMNS = ("event_id", "project_id", "seq", "kind", "source_kind", "source_id", "task_id", "run_id", "review_id",
            "decision_id", "digest", "facts", "created_at")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


_TRIGGERS = {
    "tr_project_events_immutable": """CREATE TRIGGER tr_project_events_immutable BEFORE UPDATE ON project_events
            BEGIN SELECT RAISE(ABORT, 'project_events row is immutable'); END""",
    "tr_project_events_no_delete": """CREATE TRIGGER tr_project_events_no_delete BEFORE DELETE ON project_events
            BEGIN SELECT RAISE(ABORT, 'project_events row is immutable'); END""",
    "tr_architect_obligations_scope": """CREATE TRIGGER tr_architect_obligations_scope BEFORE INSERT ON
        architect_obligations
        WHEN (SELECT project_id FROM project_events WHERE event_id = NEW.event_id) IS NOT NEW.project_id
          OR (SELECT seq FROM project_events WHERE event_id = NEW.event_id) IS NOT NEW.event_seq
          OR NEW.state != 'pending'
        BEGIN SELECT RAISE(ABORT, 'architect obligation scope'); END""",
}


def _create(name: str, kinds: tuple[str, ...], sources: tuple[str, ...]) -> None:
    op.create_table(
        name,
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
        sa.CheckConstraint(_in("kind", kinds), name="ck_project_events_kind"),
        sa.CheckConstraint(_in("source_kind", sources), name="ck_project_events_source"),
        sa.CheckConstraint("seq >= 1", name="ck_project_events_seq"),
        sa.CheckConstraint("length(facts) <= 2000", name="ck_project_events_facts"),
        sa.UniqueConstraint("project_id", "seq", name="uq_project_events_seq"),
        sa.UniqueConstraint("source_kind", "source_id", name="uq_project_events_source"),
    )


def _swap(bind, kinds: tuple[str, ...], sources: tuple[str, ...]) -> None:
    before = bind.exec_driver_sql("SELECT count(*) FROM project_events").scalar()
    bind.exec_driver_sql("DROP TABLE IF EXISTS project_events_new")
    _create("project_events_new", kinds, sources)
    columns = ", ".join(_COLUMNS)
    bind.exec_driver_sql(f"INSERT INTO project_events_new ({columns}) SELECT {columns} FROM project_events "
                         "ORDER BY rowid")
    if bind.exec_driver_sql("SELECT count(*) FROM project_events_new").scalar() != before:
        raise RuntimeError("project_events rebuild lost rows; aborted")
    bind.exec_driver_sql("DROP TRIGGER IF EXISTS tr_architect_obligations_scope")  # names the table being swapped
    op.drop_table("project_events")  # also drops its own triggers
    op.rename_table("project_events_new", "project_events")
    for statement in _TRIGGERS.values():
        op.execute(statement)


def _transaction(bind, body) -> None:
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError("Refusing to rebuild project_events with foreign-key enforcement ON; "
                           "run through Alembic's own connection (PRAGMA foreign_keys=OFF).")
    bind.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        body()
        if bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("foreign_key_check failed after rebuilding project_events; aborted")
    except BaseException:
        bind.exec_driver_sql("ROLLBACK")
        raise
    bind.exec_driver_sql("COMMIT")


def upgrade() -> None:
    bind = op.get_bind()

    def body() -> None:
        _swap(bind, NEW_KINDS, NEW_SOURCES)
        bind.exec_driver_sql("ALTER TABLE architect_maintenance_wakes ADD COLUMN conversation_id VARCHAR(36)")
    _transaction(bind, body)


def downgrade() -> None:
    bind = op.get_bind()
    if (bind.exec_driver_sql("SELECT count(*) FROM project_events WHERE kind = 'worker_message'").scalar()
            or bind.exec_driver_sql("SELECT count(*) FROM architect_maintenance_wakes "
                                    "WHERE conversation_id IS NOT NULL").scalar()):
        raise RuntimeError("Refusing to downgrade: Worker message assessment events or room wakes exist. Downgrading "
                           "would lose them; restore the pre-upgrade backup instead.")

    def body() -> None:
        bind.exec_driver_sql("ALTER TABLE architect_maintenance_wakes DROP COLUMN conversation_id")
        _swap(bind, OLD_KINDS, OLD_SOURCES)
    _transaction(bind, body)
