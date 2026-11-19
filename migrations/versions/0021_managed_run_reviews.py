"""P055: recorded Architect Review of a reported managed Run (the first explicit review-completion slice).

Purely additive on top of 0020: ONE new table ``managed_run_reviews`` plus its triggers; no existing table, row, CHECK
or trigger is touched, so no table rebuild is needed.

A row is the structured verdict an authenticated Architect submitted through the ``rosc_review_submit`` Tool for ONE
``reported`` managed Run and the EXACT registered Worker Report digest.  It is independent of ``managed_run_states``
(the Worker Run stays ``reported``).  Rows are immutable history: a revised verdict is a NEW row with the next
``version`` that names the row it supersedes (optimistic revision check in the service; the unique ``(run_id,
version)`` index and the scope trigger make a competing or out-of-scope insert fail closed at the database too).
Review pass means "the Architect assessed the Worker evidence", never VERIFIED, Owner ACCEPTED or permission to merge.

It never runs at application start: back up ``rosc.db`` (copy it while ROSC is stopped) and run ``alembic upgrade
head`` against an explicit ``ROSC_DATA_DIR``.  ``downgrade`` REFUSES (changing nothing) once any review row exists; it
never deletes review history.  Never migrate or downgrade real Owner data from an agent run.
"""

from alembic import op
import sqlalchemy as sa

revision = "0021_managed_run_reviews"
down_revision = "0020_observable_worker_execution"
branch_labels = None
depends_on = None

VERDICTS = ("review_passed", "changes_requested", "inconclusive")
_TABLE = "managed_run_reviews"

_TRIGGERS = {
    "tr_managed_run_reviews_immutable": f"""CREATE TRIGGER tr_managed_run_reviews_immutable BEFORE UPDATE ON {_TABLE}
        BEGIN SELECT RAISE(ABORT, '{_TABLE} row is immutable'); END""",
    "tr_managed_run_reviews_no_delete": f"""CREATE TRIGGER tr_managed_run_reviews_no_delete BEFORE DELETE ON {_TABLE}
        BEGIN SELECT RAISE(ABORT, '{_TABLE} row is immutable'); END""",
    # only for a reported Run of the same Project/Task, against its exact registered Report digest; versions are a
    # gap-free chain where each later row supersedes the current latest one
    "tr_managed_run_reviews_scope": f"""CREATE TRIGGER tr_managed_run_reviews_scope BEFORE INSERT ON {_TABLE}
        WHEN (SELECT state FROM managed_run_states WHERE run_id = NEW.run_id) IS NOT 'reported'
          OR (SELECT project_id FROM managed_run_states WHERE run_id = NEW.run_id) IS NOT NEW.project_id
          OR (SELECT task_id FROM managed_run_states WHERE run_id = NEW.run_id) IS NOT NEW.task_id
          OR (SELECT sha256 FROM managed_run_reports WHERE run_id = NEW.run_id) IS NOT NEW.report_sha256
          OR NEW.version IS NOT (SELECT count(*) FROM {_TABLE} WHERE run_id = NEW.run_id) + 1
          OR (NEW.version = 1 AND NEW.supersedes_review_id IS NOT NULL)
          OR (NEW.version > 1 AND NEW.supersedes_review_id IS NOT
              (SELECT review_id FROM {_TABLE} WHERE run_id = NEW.run_id AND version = NEW.version - 1))
        BEGIN SELECT RAISE(ABORT, 'managed run review scope'); END""",
}


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("review_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("managed_run_states.run_id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_review_id", sa.String(36), sa.ForeignKey(f"{_TABLE}.review_id", ondelete="RESTRICT"),
                  nullable=True),
        sa.Column("verdict", sa.String(24), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("findings", sa.Text(), nullable=False),        # JSON list of blocking findings
        sa.Column("limitations", sa.Text(), nullable=False),     # JSON list of evidence/read limitations
        sa.Column("continue_by", sa.String(16), nullable=True),  # inconclusive only: 'architect' | 'owner'
        sa.Column("report_path", sa.String(64), nullable=False),
        sa.Column("report_sha256", sa.String(64), nullable=False),
        sa.Column("submission_sha256", sa.String(64), nullable=False),  # canonical submission: idempotent replay
        sa.Column("architect_agent_id", sa.String(36), nullable=False),
        sa.Column("architect_assignment_id", sa.String(36), nullable=False),
        sa.Column("architect_session_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("verdict IN (" + ", ".join(f"'{v}'" for v in VERDICTS) + ")",
                           name="ck_managed_run_reviews_verdict"),
        sa.CheckConstraint("version >= 1", name="ck_managed_run_reviews_version"),
        sa.CheckConstraint("(verdict = 'inconclusive' AND continue_by IN ('architect', 'owner')) "
                           "OR (verdict != 'inconclusive' AND continue_by IS NULL)",
                           name="ck_managed_run_reviews_continue_by"),
        sa.CheckConstraint("verdict != 'review_passed' OR findings = '[]'", name="ck_managed_run_reviews_pass_clean"),
        sa.UniqueConstraint("run_id", "version", name="uq_managed_run_reviews_version"),
    )
    op.create_index("ix_managed_run_reviews_task", _TABLE, ["task_id"])
    for statement in _TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.exec_driver_sql(f"SELECT count(*) FROM {_TABLE}").scalar():
        raise RuntimeError("Refusing to downgrade: recorded Architect Reviews exist. Downgrading would lose review "
                           "history; restore the pre-upgrade backup instead.")
    for name in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_index("ix_managed_run_reviews_task", table_name=_TABLE)
    op.drop_table(_TABLE)
