"""P056: the human Task lifecycle and the Owner Decision on a reviewed managed Run.

``Task.status`` is a plain, stable lifecycle - not a workflow engine:

* ``Draft``     - a saved idea.  Creating, opening, reading, Owner authoring, activating an Agent session, Owner-only
  notes and Project-scoped activity never change it.
* ``Active``    - set ONCE, inside the same transaction, by the first durable Task-scoped Agent collaboration event:
  an Owner message/turn in the Task Conversation explicitly addressed to an Agent, an Agent reply published in it,
  an Architect Tool edit of an EXISTING Task's Spec/Prompt, or the Owner's first formal managed Run launch.  The write
  is a conditional ``UPDATE ... WHERE status = 'Draft'``, so replays, retries and page loads never write again.
* ``Completed`` - ONLY by an explicit Owner ``accept`` decision on a reported Run whose latest recorded Architect Review
  is ``review_passed``.  Worker ``reported``, ``review_passed``, tests or message text never complete a Task.

The current work STEP (awaiting review, awaiting Owner decision, rework needed ...) is derived for display from the
Run, Review and decision rows; it is not stored on the Task.  Spec/Prompt editability is a separate gate
(``EDITABLE_STATUSES``): Draft and Active Tasks keep the normal versioned authoring; a Completed Task is closed.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from . import architect_maintenance as maintenance
from .models import (DECISION_ACCEPT, DECISION_REWORK, MAX_CLIENT_KEY, OWNER_DECISIONS, REVIEW_CHANGES_REQUESTED,
                     REVIEW_PASSED, RUN_REPORTED, TASK_ACTIVE, TASK_COMPLETED, TASK_DRAFT, Conversation,
                     ManagedRunReport, ManagedRunState, Task, TaskOwnerDecision)

EDITABLE_STATUSES = (TASK_DRAFT, TASK_ACTIVE)
MAX_REASON = 2000
_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,%d}$" % MAX_CLIENT_KEY)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
DECIDABLE = {DECISION_ACCEPT: (REVIEW_PASSED,), DECISION_REWORK: (REVIEW_PASSED, REVIEW_CHANGES_REQUESTED)}
NOTE = ("Owner decision on the Worker result. Architect Review is an assessment of Worker-authored evidence, not an "
        "independent VERIFIED result; accepting does not recast it as VERIFIED, merge anything or start a Run.")


def activate_task(session: Session, task_id: int | None) -> bool:
    """Draft -> Active, at most once (conditional write in the caller's transaction).  Never touches Completed."""
    if task_id is None:
        return False
    result = session.execute(update(Task).where(Task.id == task_id, Task.status == TASK_DRAFT)
                             .values(status=TASK_ACTIVE).execution_options(synchronize_session="fetch"))
    return bool(result.rowcount)


def activate_for_conversation(session: Session, conversation_id: str | None) -> bool:
    """Activate the Task of a Task-scoped conversation (a Project conversation never activates anything)."""
    conversation = session.get(Conversation, conversation_id) if conversation_id else None
    if conversation is None or conversation.kind != "task":
        return False
    return activate_task(session, conversation.task_id)


class DecisionRefused(Exception):
    """``code`` is machine-readable; ``http`` is the API status."""

    def __init__(self, code: str, message: str, http: int = 409) -> None:
        super().__init__(message)
        self.code, self.http = code, http


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def latest_decision(session: Session, run_id: str) -> TaskOwnerDecision | None:
    return session.scalars(select(TaskOwnerDecision).where(TaskOwnerDecision.run_id == run_id)).first()


def decision_view(row: TaskOwnerDecision) -> dict:
    return {"decision_id": row.decision_id, "run_id": row.run_id, "task_id": f"T{row.task_id:04d}",
            "decision": row.decision, "reason": row.reason, "review_id": row.review_id,
            "review_version": row.review_version, "review_verdict": row.review_verdict,
            "report_sha256": row.report_sha256, "spec_revision": row.spec_revision,
            "prompt_revision": row.prompt_revision,
            "actor": {"kind": row.actor_kind, "id": row.actor_id, "assurance": row.actor_assurance},
            "decided_at": _iso(row.created_at)}


def record_decision(session: Session, actor, project_id: int, task_id: int, args: dict) -> dict:
    """Record ONE Owner decision (or replay the identical one).  The caller has authorized the Owner.

    Required: ``run_id``, ``decision``, ``review_id``, ``review_version`` (the review the Owner saw: a newer or other
    review is a stale conflict), ``request_key`` (duplicate-click / retry key), and ``reason`` for a rework request."""
    run_id, decision = args.get("run_id"), args.get("decision")
    review_id, review_version, key = args.get("review_id"), args.get("review_version"), args.get("request_key")
    if not isinstance(run_id, str) or not _UUID.match(run_id):
        raise DecisionRefused("not_found", "Run not found in this Task", 404)
    if decision not in OWNER_DECISIONS:
        raise DecisionRefused("invalid", "decision must be accept or request_rework", 422)
    if not isinstance(review_id, str) or not _UUID.match(review_id):
        raise DecisionRefused("invalid", "review_id must name the recorded Architect Review you saw", 422)
    if isinstance(review_version, bool) or not isinstance(review_version, int) or review_version < 1:
        raise DecisionRefused("invalid", "review_version must be the version of the review you saw", 422)
    if not isinstance(key, str) or not _KEY.match(key):
        raise DecisionRefused("invalid", "request_key must be 8-128 characters [A-Za-z0-9._:-]", 422)
    reason = args.get("reason")
    if reason is not None and (not isinstance(reason, str) or "\x00" in reason):
        raise DecisionRefused("invalid", "reason must be text", 422)
    reason = (reason or "").replace("\r\n", "\n").strip() or None
    if decision == DECISION_REWORK and reason is None:
        raise DecisionRefused("invalid", "A short reason is required to request rework", 422)
    if reason is not None and len(reason) > MAX_REASON:
        raise DecisionRefused("invalid", f"reason exceeds {MAX_REASON} characters", 422)
    if decision == DECISION_ACCEPT:
        reason = None  # acceptance carries no reason; keep the record minimal

    from .run_review import latest_review  # local import: run_review imports models only
    try:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        task = session.get(Task, task_id)
        if task is None or task.project_id != project_id:
            raise DecisionRefused("not_found", "Task not found", 404)
        run = session.get(ManagedRunState, run_id)
        if run is None or run.project_id != project_id or run.task_id != task_id:
            raise DecisionRefused("not_found", "Run not found in this Task", 404)
        existing = latest_decision(session, run_id)
        if existing is None:
            existing = session.scalars(select(TaskOwnerDecision).where(
                TaskOwnerDecision.task_id == task_id, TaskOwnerDecision.request_key == key)).first()
        if existing is not None:
            same = (existing.run_id == run_id and existing.decision == decision and existing.reason == reason
                    and existing.review_id == review_id and existing.request_key == key)
            view, status = decision_view(existing), task.status
            session.rollback()
            if same:
                return {"recorded": False, "replayed": True, "decision": view, "task_status": status, "note": NOTE}
            raise DecisionRefused("decision_already_recorded", "An Owner decision is already recorded for this Run; it "
                                  "is never replaced. Reload the Task to see it.")
        if run.state != RUN_REPORTED:
            raise DecisionRefused("run_not_reported", "Only a reported Run can be decided")
        report = session.get(ManagedRunReport, run_id)
        review = latest_review(session, run_id)
        if report is None or review is None:
            raise DecisionRefused("review_missing", "No recorded Architect Review exists for this Run yet")
        if review.review_id != review_id or review.version != review_version:
            raise DecisionRefused("review_stale", "The Architect Review changed since you opened it; reload the Task")
        if review.verdict not in DECIDABLE[decision] or review.report_sha256 != report.sha256:
            raise DecisionRefused("decision_not_allowed", "This decision is not available for the current Review "
                                  f"({review.verdict})")
        if task.status == TASK_COMPLETED:
            raise DecisionRefused("task_completed", "The Task is already Completed")
        row = TaskOwnerDecision(
            decision_id=str(uuid.uuid4()), project_id=project_id, task_id=task_id, run_id=run_id,
            review_id=review.review_id, review_version=review.version, review_verdict=review.verdict,
            report_sha256=report.sha256, spec_revision=run.spec_revision, prompt_revision=run.prompt_revision,
            decision=decision, reason=reason, request_key=key, actor_kind=actor.actor_kind, actor_id=actor.actor_id,
            actor_assurance=actor.assurance, created_at=datetime.now(timezone.utc))
        session.add(row)
        session.flush()
        maintenance.emit_decision(session, row)  # P059: same transaction, once per decision row
        task.status = TASK_COMPLETED if decision == DECISION_ACCEPT else TASK_ACTIVE
        session.commit()
    except DecisionRefused:
        session.rollback()
        raise
    except (OperationalError, IntegrityError):
        session.rollback()
        raise DecisionRefused("decision_conflict", "Another decision for this Run won the race or the store is busy; "
                              "reload the Task") from None
    return {"recorded": True, "replayed": False, "decision": decision_view(row), "task_status": task.status,
            "note": NOTE}
