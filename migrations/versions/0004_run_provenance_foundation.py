"""Run / Report / Evidence identity and provenance foundation (P013 / M2-D).

Additive only: four new tables, their indexes and guard triggers. Existing ``projects``, ``project_root_bindings``,
``tasks``, ``agent_definitions``, ``agent_capabilities``, ``role_assignments`` and ``assignment_capabilities`` rows, and
every Workspace manifest, are left row- and byte-identical.

Triggers (SQLite cannot express them as declarative constraints without altering legacy tables):
* a Run's Task must belong to the Run's Project; an Evidence record's Report must belong to the same Run;
* Run identity/claim columns, Reports, Evidence and link history are immutable (only the Owner-curated agent link
  columns of a Run may change, and never away from an established link).

``downgrade`` drops only the P013-owned tables and triggers; never run it against real user data.
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_run_provenance_foundation"
down_revision = "0003_agent_registry_assignments"
branch_labels = None
depends_on = None

_REPORT_KINDS = "'executor_report', 'review_report', 'other_report'"
_EVIDENCE_KINDS = "'report_reference', 'git_commit_reference', 'test_result_reference', 'other_source_reference'"
_DIGEST = "(length({c}) = 64 AND {c} NOT GLOB '*[^0-9a-f]*')"

_TRIGGERS = {
    "tr_run_records_task_project": """CREATE TRIGGER tr_run_records_task_project BEFORE INSERT ON run_records
        WHEN (SELECT project_id FROM tasks WHERE id = NEW.task_id) IS NOT NEW.project_id
        BEGIN SELECT RAISE(ABORT, 'run task/project mismatch'); END""",
    "tr_run_records_immutable": """CREATE TRIGGER tr_run_records_immutable BEFORE UPDATE ON run_records
        WHEN NEW.run_id IS NOT OLD.run_id OR NEW.project_id IS NOT OLD.project_id OR NEW.task_id IS NOT OLD.task_id
          OR NEW.execution_origin IS NOT OLD.execution_origin OR NEW.source_kind IS NOT OLD.source_kind
          OR NEW.source_key IS NOT OLD.source_key OR NEW.reported_executor IS NOT OLD.reported_executor
          OR NEW.source_attribution_assurance IS NOT OLD.source_attribution_assurance
          OR NEW.registered_by IS NOT OLD.registered_by OR NEW.registered_by_assurance IS NOT OLD.registered_by_assurance
          OR NEW.created_at IS NOT OLD.created_at
          OR (OLD.agent_id IS NOT NULL AND NEW.agent_id IS NOT OLD.agent_id)
        BEGIN SELECT RAISE(ABORT, 'run record is immutable'); END""",
    "tr_run_records_no_delete": """CREATE TRIGGER tr_run_records_no_delete BEFORE DELETE ON run_records
        BEGIN SELECT RAISE(ABORT, 'run record is immutable'); END""",
    "tr_run_agent_links_immutable": """CREATE TRIGGER tr_run_agent_links_immutable BEFORE UPDATE ON run_agent_links
        BEGIN SELECT RAISE(ABORT, 'link history is immutable'); END""",
    "tr_run_agent_links_no_delete": """CREATE TRIGGER tr_run_agent_links_no_delete BEFORE DELETE ON run_agent_links
        BEGIN SELECT RAISE(ABORT, 'link history is immutable'); END""",
    "tr_report_records_immutable": """CREATE TRIGGER tr_report_records_immutable BEFORE UPDATE ON report_records
        BEGIN SELECT RAISE(ABORT, 'report record is immutable'); END""",
    "tr_report_records_no_delete": """CREATE TRIGGER tr_report_records_no_delete BEFORE DELETE ON report_records
        BEGIN SELECT RAISE(ABORT, 'report record is immutable'); END""",
    "tr_evidence_records_report_run": """CREATE TRIGGER tr_evidence_records_report_run BEFORE INSERT ON evidence_records
        WHEN NEW.report_id IS NOT NULL
         AND (SELECT run_id FROM report_records WHERE report_id = NEW.report_id) IS NOT NEW.run_id
        BEGIN SELECT RAISE(ABORT, 'evidence report/run mismatch'); END""",
    "tr_evidence_records_immutable": """CREATE TRIGGER tr_evidence_records_immutable BEFORE UPDATE ON evidence_records
        BEGIN SELECT RAISE(ABORT, 'evidence record is immutable'); END""",
    "tr_evidence_records_no_delete": """CREATE TRIGGER tr_evidence_records_no_delete BEFORE DELETE ON evidence_records
        BEGIN SELECT RAISE(ABORT, 'evidence record is immutable'); END""",
}


def upgrade() -> None:
    op.create_table(
        "run_records",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("execution_origin", sa.String(16), nullable=False),
        sa.Column("source_kind", sa.String(64), nullable=False),
        sa.Column("source_key", sa.String(128), nullable=True),
        sa.Column("source_attribution_assurance", sa.String(16), nullable=False),
        sa.Column("reported_executor", sa.String(96), nullable=True),
        sa.Column("agent_id", sa.String(36),
                  sa.ForeignKey("agent_definitions.agent_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("assignment_id", sa.String(36),
                  sa.ForeignKey("role_assignments.assignment_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("agent_link_status", sa.String(32), nullable=False),
        sa.Column("attribution_revision", sa.Integer(), nullable=False),
        sa.Column("registered_by", sa.String(64), nullable=False),
        sa.Column("registered_by_assurance", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("execution_origin IN ('external', 'managed')", name="ck_run_records_origin"),
        sa.CheckConstraint("source_attribution_assurance = 'claimed'", name="ck_run_records_assurance"),
        sa.CheckConstraint("agent_link_status IN ('unlinked', 'owner_curated_correspondence')", name="ck_run_records_link"),
        sa.CheckConstraint("(agent_link_status = 'unlinked') = (agent_id IS NULL)", name="ck_run_records_link_agent"),
        sa.CheckConstraint("assignment_id IS NULL OR agent_id IS NOT NULL", name="ck_run_records_link_assignment"),
        sa.CheckConstraint("attribution_revision >= 1", name="ck_run_records_revision"),
        sa.CheckConstraint("source_key IS NULL OR length(source_key) BETWEEN 1 AND 128", name="ck_run_records_source_key"),
        sa.CheckConstraint("length(source_kind) BETWEEN 1 AND 64", name="ck_run_records_source_kind"),
        sa.CheckConstraint("reported_executor IS NULL OR length(reported_executor) BETWEEN 1 AND 96",
                           name="ck_run_records_executor"),
    )
    op.create_index("ix_run_records_project_id", "run_records", ["project_id"])
    op.create_index("ix_run_records_task_id", "run_records", ["task_id"])
    op.create_index("ix_run_records_agent_id", "run_records", ["agent_id"])
    op.create_index("ux_run_records_source", "run_records", ["project_id", "source_kind", "source_key"], unique=True,
                    sqlite_where=sa.text("source_key IS NOT NULL"))

    op.create_table(
        "run_agent_links",
        sa.Column("link_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run_records.run_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("from_agent_id", sa.String(36), nullable=True),
        sa.Column("agent_id", sa.String(36),
                  sa.ForeignKey("agent_definitions.agent_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("assignment_id", sa.String(36),
                  sa.ForeignKey("role_assignments.assignment_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("original_claim", sa.String(96), nullable=True),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("resulting_revision", sa.Integer(), nullable=False),
        sa.Column("linked_by", sa.String(64), nullable=False),
        sa.Column("linked_by_assurance", sa.String(32), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("expected_revision >= 1 AND resulting_revision = expected_revision + 1",
                           name="ck_run_agent_links_revision"),
    )
    op.create_index("ux_run_agent_links_run_revision", "run_agent_links", ["run_id", "resulting_revision"], unique=True)
    op.create_index("ix_run_agent_links_agent_id", "run_agent_links", ["agent_id"])

    op.create_table(
        "report_records",
        sa.Column("report_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run_records.run_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_key", sa.String(128), nullable=True),
        sa.Column("report_kind", sa.String(32), nullable=False),
        sa.Column("report_revision", sa.Integer(), nullable=True),
        sa.Column("source_ref", sa.String(200), nullable=False),
        sa.Column("declared_digest", sa.String(64), nullable=True),
        sa.Column("provenance", sa.String(16), nullable=False),
        sa.Column("ingest_origin", sa.String(32), nullable=False),
        sa.Column("registered_by", sa.String(64), nullable=False),
        sa.Column("registered_by_assurance", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"report_kind IN ({_REPORT_KINDS})", name="ck_report_records_kind"),
        sa.CheckConstraint("provenance = 'claimed'", name="ck_report_records_provenance"),
        sa.CheckConstraint("ingest_origin = 'manual_owner_registration'", name="ck_report_records_ingest"),
        sa.CheckConstraint("length(source_ref) BETWEEN 1 AND 200", name="ck_report_records_ref"),
        sa.CheckConstraint("source_key IS NULL OR length(source_key) BETWEEN 1 AND 128", name="ck_report_records_source_key"),
        sa.CheckConstraint("report_revision IS NULL OR report_revision >= 1", name="ck_report_records_revision"),
        sa.CheckConstraint("declared_digest IS NULL OR " + _DIGEST.format(c="declared_digest"),
                           name="ck_report_records_digest"),
    )
    op.create_index("ix_report_records_run_id", "report_records", ["run_id"])
    op.create_index("ux_report_records_source", "report_records", ["run_id", "source_key"], unique=True,
                    sqlite_where=sa.text("source_key IS NOT NULL"))

    op.create_table(
        "evidence_records",
        sa.Column("evidence_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("run_records.run_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("report_id", sa.String(36),
                  sa.ForeignKey("report_records.report_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("source_key", sa.String(128), nullable=True),
        sa.Column("evidence_kind", sa.String(32), nullable=False),
        sa.Column("reference", sa.String(200), nullable=False),
        sa.Column("declared_digest", sa.String(64), nullable=True),
        sa.Column("reported_outcome", sa.String(16), nullable=True),
        sa.Column("provenance", sa.String(16), nullable=False),
        sa.Column("ingest_origin", sa.String(32), nullable=False),
        sa.Column("registered_by", sa.String(64), nullable=False),
        sa.Column("registered_by_assurance", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"evidence_kind IN ({_EVIDENCE_KINDS})", name="ck_evidence_records_kind"),
        sa.CheckConstraint("provenance = 'claimed'", name="ck_evidence_records_provenance"),
        sa.CheckConstraint("ingest_origin = 'manual_owner_registration'", name="ck_evidence_records_ingest"),
        sa.CheckConstraint("length(reference) BETWEEN 1 AND 200", name="ck_evidence_records_ref"),
        sa.CheckConstraint("source_key IS NULL OR length(source_key) BETWEEN 1 AND 128", name="ck_evidence_records_source_key"),
        sa.CheckConstraint("reported_outcome IS NULL OR reported_outcome IN ('reported_pass', 'reported_fail', 'reported_unknown')",
                           name="ck_evidence_records_outcome"),
        sa.CheckConstraint("reported_outcome IS NULL OR evidence_kind = 'test_result_reference'",
                           name="ck_evidence_records_outcome_kind"),
        sa.CheckConstraint("declared_digest IS NULL OR " + _DIGEST.format(c="declared_digest"),
                           name="ck_evidence_records_digest"),
    )
    op.create_index("ix_evidence_records_run_id", "evidence_records", ["run_id"])
    op.create_index("ix_evidence_records_report_id", "evidence_records", ["report_id"])
    op.create_index("ux_evidence_records_source", "evidence_records", ["run_id", "source_key"], unique=True,
                    sqlite_where=sa.text("source_key IS NOT NULL"))

    for statement in _TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    for name in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table("evidence_records")
    op.drop_table("report_records")
    op.drop_table("run_agent_links")
    op.drop_table("run_records")
