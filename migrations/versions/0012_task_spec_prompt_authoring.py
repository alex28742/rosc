"""Task Spec / Prompt Draft revisions and Owner approval (P040).

Seeds an honest ``legacy_import`` Spec v1 for every existing Task from its current fields.
"""

import hashlib
import html
import json
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0012_task_spec_prompt_authoring"
down_revision = "0011_task_native_sessions"
branch_labels = None
depends_on = None


def _paragraphs(text: str) -> str:
    parts = [p.strip() for p in (text or "").replace("\r\n", "\n").split("\n\n") if p.strip()]
    return "".join("<p>" + "<br>".join(html.escape(line) for line in p.split("\n")) + "</p>" for p in parts)


def upgrade() -> None:
    op.create_table(
        "task_spec_revisions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("scope", sa.Text, nullable=False, server_default=""),
        sa.Column("description_html", sa.Text, nullable=False),
        sa.Column("criteria_html", sa.Text, nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("provenance", sa.String(16), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("actor_assurance", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "revision", name="uq_task_spec_revisions_task_revision"),
        sa.CheckConstraint("revision >= 1", name="ck_task_spec_revisions_revision"),
        sa.CheckConstraint("provenance IN ('owner', 'architect', 'legacy_import')", name="ck_task_spec_revisions_provenance"),
    )
    op.create_index("ix_task_spec_revisions_project_task", "task_spec_revisions", ["project_id", "task_id"])
    op.create_table(
        "prompt_revisions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("source_spec_revision", sa.Integer, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("rules_ref", sa.String(160), nullable=True),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("actor_assurance", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "revision", name="uq_prompt_revisions_task_revision"),
        sa.CheckConstraint("revision >= 1", name="ck_prompt_revisions_revision"),
        sa.CheckConstraint("length(summary) >= 1 AND length(body) >= 1", name="ck_prompt_revisions_text"),
        sa.ForeignKeyConstraint(["task_id", "source_spec_revision"],
                                ["task_spec_revisions.task_id", "task_spec_revisions.revision"],
                                ondelete="RESTRICT", name="fk_prompt_revisions_spec"),
    )
    op.create_index("ix_prompt_revisions_project_task", "prompt_revisions", ["project_id", "task_id"])
    op.create_table(
        "prompt_approvals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("prompt_revision_id", sa.Integer, sa.ForeignKey("prompt_revisions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("project_id", sa.Integer, sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("spec_revision", sa.Integer, nullable=False),
        sa.Column("prompt_revision", sa.Integer, nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("actor_assurance", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("prompt_revision_id", name="uq_prompt_approvals_revision"),
    )
    op.create_index("ix_prompt_approvals_task", "prompt_approvals", ["task_id"])

    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    spec = sa.table("task_spec_revisions", *[sa.column(c) for c in (
        "project_id", "task_id", "revision", "title", "scope", "description_html", "criteria_html", "digest",
        "provenance", "actor_kind", "actor_id", "actor_assurance", "created_at")])
    for task_id, project_id, title, description, criteria in bind.execute(
            sa.text("SELECT id, project_id, title, description, acceptance_criteria FROM tasks")).fetchall():
        d, c = _paragraphs(description), _paragraphs(criteria)
        digest = hashlib.sha256(json.dumps([title, "", d, c], ensure_ascii=False).encode()).hexdigest()
        bind.execute(spec.insert().values(
            project_id=project_id, task_id=task_id, revision=1, title=title, scope="", description_html=d,
            criteria_html=c, digest=digest, provenance="legacy_import", actor_kind="system",
            actor_id="migration-0012", actor_assurance="migration", created_at=now))


def downgrade() -> None:
    op.drop_table("prompt_approvals")
    op.drop_table("prompt_revisions")
    op.drop_table("task_spec_revisions")
