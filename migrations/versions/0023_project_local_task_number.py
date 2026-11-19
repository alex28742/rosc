"""P058: stable Project-local Task ordinal (UI "№N"), separate from the global Task identity ``T%04d``.

Additive on top of 0022: TWO new columns, ONE unique index and FOUR narrow triggers.  No table is rebuilt; ``tasks.id``
(the global Task PK behind ``T0105``), every foreign key, route, Workspace path and historical Run/Review/Decision link
stays exactly as it is.  The local number is a display ordinal only, never an alternative identifier.

* ``projects.last_task_number`` -- the Project's high-water mark.  Allocation bumps it by one in the creating
  transaction; it can never go down, so a number is never recycled even if Tasks were removed later.
* ``tasks.project_number`` -- the Task's immutable ordinal, unique per Project (``uq_tasks_project_number``).  A new
  row must carry exactly the number just allocated on its Project; once set it can never change (nor move the Task to
  another Project).

Backfill (deterministic, idempotent): inside each ``project_id`` the existing Tasks are numbered 1, 2, 3... by increasing
global ``tasks.id``; ``last_task_number`` becomes the Project's maximum (0 without Tasks).  So P0002/T0007, the first
Task of P0002, becomes P0002 Task №1.

It never runs at application start: back up ``rosc.db`` while ROSC is stopped and run ``alembic upgrade head`` against
an explicit ``ROSC_DATA_DIR`` (see the P057 offline DB-upgrade helper).  ``downgrade`` drops the triggers, the index and
both columns; a later re-upgrade recomputes the same numbers as long as no Task was removed.  Never migrate real Owner
data from an agent run.
"""

from alembic import op
import sqlalchemy as sa

revision = "0023_project_local_task_number"
down_revision = "0022_task_lifecycle_owner_decision"
branch_labels = None
depends_on = None

_INDEX = "uq_tasks_project_number"

_TRIGGERS = {
    # a new Task carries exactly the number just allocated (projects.last_task_number) for its own Project
    "tr_tasks_project_number_insert": """CREATE TRIGGER tr_tasks_project_number_insert BEFORE INSERT ON tasks
        WHEN NEW.project_number IS NULL OR NEW.project_number < 1
          OR NEW.project_number IS NOT (SELECT last_task_number FROM projects WHERE id = NEW.project_id)
        BEGIN SELECT RAISE(ABORT, 'task project_number must be the freshly allocated Project-local number'); END""",
    "tr_tasks_project_number_immutable": """CREATE TRIGGER tr_tasks_project_number_immutable
        BEFORE UPDATE OF project_number, project_id ON tasks
        WHEN NEW.project_number IS NOT OLD.project_number OR NEW.project_id IS NOT OLD.project_id
        BEGIN SELECT RAISE(ABORT, 'task project_number is immutable'); END""",
    "tr_projects_last_task_number_monotonic": """CREATE TRIGGER tr_projects_last_task_number_monotonic
        BEFORE UPDATE OF last_task_number ON projects
        WHEN NEW.last_task_number IS NULL OR NEW.last_task_number < OLD.last_task_number
        BEGIN SELECT RAISE(ABORT, 'project last_task_number never decreases'); END""",
    "tr_projects_last_task_number_insert": """CREATE TRIGGER tr_projects_last_task_number_insert
        BEFORE INSERT ON projects WHEN NEW.last_task_number IS NOT 0
        BEGIN SELECT RAISE(ABORT, 'a new Project starts with last_task_number 0'); END""",
}

BACKFILL_TASKS = """
UPDATE tasks SET project_number = (SELECT count(*) FROM tasks AS t WHERE t.project_id = tasks.project_id
                                                                    AND t.id <= tasks.id)
WHERE project_number IS NULL
"""
BACKFILL_PROJECTS = """
UPDATE projects SET last_task_number = (SELECT coalesce(max(t.project_number), 0) FROM tasks AS t
                                        WHERE t.project_id = projects.id)
"""


def upgrade() -> None:
    op.execute("ALTER TABLE projects ADD COLUMN last_task_number INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE tasks ADD COLUMN project_number INTEGER")
    op.execute(BACKFILL_TASKS)
    op.execute(BACKFILL_PROJECTS)
    op.create_index(_INDEX, "tasks", ["project_id", "project_number"], unique=True)
    for statement in _TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    for name in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_index(_INDEX, table_name="tasks")
    op.execute("ALTER TABLE tasks DROP COLUMN project_number")
    op.execute("ALTER TABLE projects DROP COLUMN last_task_number")
