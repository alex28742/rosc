"""Project identity, initial Root Binding and v3 manifest compatibility.

Forward-only. Every existing Project receives exactly one immutable random ``project_uuid`` and one
version 1 ``current`` Root Binding derived from its registration root. Existing Project and Task rows are
preserved; no manifest file is touched here.

``downgrade`` is deliberately lossy for identity data (the UUIDs and binding history are removed by the
schema shape this revision introduced) and exists only so an explicit, reviewed rollback of an empty or
disposable database is possible. It never deletes Project or Task rows.
"""

import uuid

from alembic import op
import sqlalchemy as sa

revision = "0002_project_identity_binding"
down_revision = "0001_project_task"
branch_labels = None
depends_on = None

# Declared explicitly for the downgrade rebuild: batch mode refuses to reflect a table it is about to rewrite, and the
# two new columns must be listed before they can be dropped. No constraint referencing them is declared, so the
# rebuilt table is exactly the 0001 shape (rows preserved; no Project or Task row deleted).
PROJECTS_WITH_IDENTITY = sa.Table(
    "projects",
    sa.MetaData(),
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("name", sa.String(160), nullable=False),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("root_path", sa.Text(), nullable=False, unique=True),
    sa.Column("mode", sa.String(16), nullable=False),
    sa.Column("docs_entrypoint", sa.Text(), nullable=True),
    sa.Column("workspace_state", sa.String(16), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("project_uuid", sa.String(36), nullable=False),
    sa.Column("registration_root_path", sa.Text(), nullable=False),
)


def upgrade() -> None:
    connection = op.get_bind()

    # Nullable first: SQLite cannot add a NOT NULL column without a literal default, and a default would be
    # a lie for existing rows. The values are backfilled below, then made NOT NULL by the table rebuild.
    op.add_column("projects", sa.Column("project_uuid", sa.String(36), nullable=True))
    op.add_column("projects", sa.Column("registration_root_path", sa.Text(), nullable=True))

    rows = connection.execute(sa.text("SELECT id, root_path FROM projects ORDER BY id")).mappings().all()
    for row in rows:
        connection.execute(
            sa.text("UPDATE projects SET project_uuid = :project_uuid, registration_root_path = :root_path WHERE id = :id"),
            {"project_uuid": str(uuid.uuid4()), "root_path": row["root_path"], "id": row["id"]},
        )

    with op.batch_alter_table("projects", recreate="always") as batch:
        batch.alter_column("project_uuid", existing_type=sa.String(36), nullable=False)
        batch.alter_column("registration_root_path", existing_type=sa.Text(), nullable=False)
        batch.create_unique_constraint("uq_projects_project_uuid", ["project_uuid"])
        # Bridge constraint: the transitional legacy column may not drift from the canonical registration root.
        batch.create_check_constraint("ck_projects_registration_root", "registration_root_path = root_path")

    op.create_table(
        "project_root_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("docs_entrypoint", sa.Text(), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('current', 'superseded')", name="ck_root_bindings_state"),
        sa.CheckConstraint("version >= 1", name="ck_root_bindings_version"),
        sa.UniqueConstraint("project_id", "version", name="uq_root_bindings_project_version"),
    )
    op.create_index("ix_root_bindings_project_id", "project_root_bindings", ["project_id"])
    # At most one current binding per Project, and no two Projects reserve the same current root.
    op.create_index("ux_root_bindings_current_project", "project_root_bindings", ["project_id"], unique=True,
                    sqlite_where=sa.text("state = 'current'"))
    op.create_index("ux_root_bindings_current_root", "project_root_bindings", ["root_path"], unique=True,
                    sqlite_where=sa.text("state = 'current'"))

    connection.execute(sa.text(
        "INSERT INTO project_root_bindings (project_id, version, root_path, docs_entrypoint, mode, state, created_at) "
        "SELECT id, 1, root_path, docs_entrypoint, mode, 'current', created_at FROM projects ORDER BY id"
    ))


def downgrade() -> None:
    op.drop_index("ux_root_bindings_current_root", table_name="project_root_bindings")
    op.drop_index("ux_root_bindings_current_project", table_name="project_root_bindings")
    op.drop_index("ix_root_bindings_project_id", table_name="project_root_bindings")
    op.drop_table("project_root_bindings")
    with op.batch_alter_table("projects", copy_from=PROJECTS_WITH_IDENTITY) as batch:
        batch.drop_column("registration_root_path")
        batch.drop_column("project_uuid")
