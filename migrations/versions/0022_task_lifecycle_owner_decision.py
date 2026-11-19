"""P056: Task lifecycle (Draft -> Active -> Completed) and the Owner Decision on a reviewed managed Run.

Additive on top of 0021: ONE new table ``task_owner_decisions`` plus its triggers, and ONE bounded, idempotent backfill
of ``tasks.status`` from ``Draft`` to ``Active``.  No existing table, CHECK or trigger is changed and no table is rebuilt.

``task_owner_decisions``: at most one immutable row per managed Run, bound to the Run's latest recorded decisive
Architect Review (id + version + verdict), the registered Worker Report digest and the Run's frozen Spec/Prompt
revisions.  ``accept`` (only against ``review_passed``) completes the Task; ``request_rework`` (against
``review_passed`` or ``changes_requested``, with a reason) keeps it Active.  The scope trigger makes an out-of-scope,
stale or mismatching row fail closed at the database too.

Backfill: a ``Draft`` Task becomes ``Active`` ONLY when durable Task-scoped evidence of Agent collaboration exists: a
managed Run of that Task; an Agent-authored message in its Task Conversation; an Owner message in it explicitly
addressed to an Agent (legacy ``recipient`` or a ``to`` party of kind architect/worker); an Architect-authored Spec
revision after creation (revision > 1) or an Architect-authored Prompt revision.  Nothing else is relabelled, no
timestamp is invented and Owner-only notes, Owner authoring, reads and Project-scoped activity do not count.  Running it
twice changes nothing more.

It never runs at application start: back up ``rosc.db`` (copy it while ROSC is stopped) and run ``alembic upgrade
head`` against an explicit ``ROSC_DATA_DIR``.  ``downgrade`` REFUSES (changing nothing) once any Owner decision or
``Completed`` Task exists; otherwise it returns ``Active`` Tasks to ``Draft`` (the pre-P056 code only edits ``Draft``)
and drops the table.  Never migrate or downgrade real Owner data from an agent run.
"""

from alembic import op
import sqlalchemy as sa

revision = "0022_task_lifecycle_owner_decision"
down_revision = "0021_managed_run_reviews"
branch_labels = None
depends_on = None

DECISIONS = ("accept", "request_rework")
_TABLE = "task_owner_decisions"

_TRIGGERS = {
    "tr_task_owner_decisions_immutable": f"""CREATE TRIGGER tr_task_owner_decisions_immutable BEFORE UPDATE ON {_TABLE}
        BEGIN SELECT RAISE(ABORT, '{_TABLE} row is immutable'); END""",
    "tr_task_owner_decisions_no_delete": f"""CREATE TRIGGER tr_task_owner_decisions_no_delete BEFORE DELETE ON {_TABLE}
        BEGIN SELECT RAISE(ABORT, '{_TABLE} row is immutable'); END""",
    # only for a reported Run of the same Project/Task, against its LATEST recorded review and exact Report digest and
    # the Run's frozen revisions; accept needs review_passed, rework a decisive (passed / changes_requested) review
    "tr_task_owner_decisions_scope": f"""CREATE TRIGGER tr_task_owner_decisions_scope BEFORE INSERT ON {_TABLE}
        WHEN (SELECT state FROM managed_run_states WHERE run_id = NEW.run_id) IS NOT 'reported'
          OR (SELECT project_id FROM managed_run_states WHERE run_id = NEW.run_id) IS NOT NEW.project_id
          OR (SELECT task_id FROM managed_run_states WHERE run_id = NEW.run_id) IS NOT NEW.task_id
          OR (SELECT spec_revision FROM managed_run_states WHERE run_id = NEW.run_id) IS NOT NEW.spec_revision
          OR (SELECT prompt_revision FROM managed_run_states WHERE run_id = NEW.run_id) IS NOT NEW.prompt_revision
          OR (SELECT sha256 FROM managed_run_reports WHERE run_id = NEW.run_id) IS NOT NEW.report_sha256
          OR (SELECT run_id FROM managed_run_reviews WHERE review_id = NEW.review_id) IS NOT NEW.run_id
          OR (SELECT version FROM managed_run_reviews WHERE review_id = NEW.review_id) IS NOT NEW.review_version
          OR (SELECT verdict FROM managed_run_reviews WHERE review_id = NEW.review_id) IS NOT NEW.review_verdict
          OR (SELECT max(version) FROM managed_run_reviews WHERE run_id = NEW.run_id) IS NOT NEW.review_version
          OR (NEW.decision = 'accept' AND NEW.review_verdict != 'review_passed')
          OR (NEW.decision = 'request_rework' AND NEW.review_verdict NOT IN ('review_passed', 'changes_requested'))
        BEGIN SELECT RAISE(ABORT, 'task owner decision scope'); END""",
}

# Grounded, Task-scoped evidence of Agent collaboration (see the module docstring).  Idempotent: only Draft rows move.
BACKFILL_ACTIVE = """
UPDATE tasks SET status = 'Active' WHERE status = 'Draft' AND (
  EXISTS (SELECT 1 FROM managed_run_states m WHERE m.task_id = tasks.id)
  OR EXISTS (SELECT 1 FROM conversations c
             JOIN conversation_messages cm ON cm.conversation_id = c.conversation_id
             JOIN conversation_participants p ON p.participant_id = cm.sender_participant_id
             WHERE c.kind = 'task' AND c.task_id = tasks.id AND (
               p.kind IN ('architect', 'worker')
               OR cm.recipient IS NOT NULL
               OR EXISTS (SELECT 1 FROM conversation_message_parties mp WHERE mp.message_id = cm.message_id
                          AND mp.role = 'to' AND mp.party_kind IN ('architect', 'worker'))))
  OR EXISTS (SELECT 1 FROM task_spec_revisions s WHERE s.task_id = tasks.id AND s.provenance = 'architect'
             AND s.revision > 1)
  OR EXISTS (SELECT 1 FROM prompt_revisions pr WHERE pr.task_id = tasks.id AND pr.actor_kind = 'architect'))
"""


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("decision_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("managed_run_states.run_id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("review_id", sa.String(36), sa.ForeignKey("managed_run_reviews.review_id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("review_verdict", sa.String(24), nullable=False),
        sa.Column("report_sha256", sa.String(64), nullable=False),
        sa.Column("spec_revision", sa.Integer(), nullable=False),
        sa.Column("prompt_revision", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("actor_assurance", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("decision IN (" + ", ".join(f"'{v}'" for v in DECISIONS) + ")",
                           name="ck_task_owner_decisions_decision"),
        sa.CheckConstraint("decision != 'request_rework' OR length(trim(reason)) >= 1",
                           name="ck_task_owner_decisions_reason"),
        sa.UniqueConstraint("run_id", name="uq_task_owner_decisions_run"),
        sa.UniqueConstraint("task_id", "request_key", name="uq_task_owner_decisions_request"),
    )
    op.create_index("ix_task_owner_decisions_task", _TABLE, ["task_id"])
    for statement in _TRIGGERS.values():
        op.execute(statement)
    op.execute(BACKFILL_ACTIVE)


def downgrade() -> None:
    bind = op.get_bind()
    if (bind.exec_driver_sql(f"SELECT count(*) FROM {_TABLE}").scalar()
            or bind.exec_driver_sql("SELECT count(*) FROM tasks WHERE status = 'Completed'").scalar()):
        raise RuntimeError("Refusing to downgrade: Owner decisions / Completed Tasks exist. Downgrading would lose the "
                           "Owner's decision history; restore the pre-upgrade backup instead.")
    op.execute("UPDATE tasks SET status = 'Draft' WHERE status = 'Active'")
    for name in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_index("ix_task_owner_decisions_task", table_name=_TABLE)
    op.drop_table(_TABLE)
