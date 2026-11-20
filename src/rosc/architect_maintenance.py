"""P059: event-driven Architect context maintenance.

Loop: ``authoritative Core row committed -> immutable ProjectEvent (same transaction) -> ArchitectObligation pending ->
bounded Core wake of the ready, familiarized Project Architect -> typed Tool review -> rosc_maintenance_ack -> Core
verifies the referenced persisted records -> completed``.

* Events are written ONLY next to the row they describe, inside its transaction: a registered Worker Run Report
  (``run_reported``), a decisive recorded Architect Review (``review_submitted``; an ``inconclusive`` review is a step
  of the review, not a consequence) and an explicit Owner Task decision (``task_accepted`` / ``task_rework_requested``).
  ``uq_project_events_source`` makes a replay, a duplicate callback or a restart unable to emit twice.  Nothing is
  inferred from Worker text and nothing historical is backfilled.
* An obligation is the Architect's duty to ASSESS one event.  Reading, being sent a wake or replying never completes it;
  only ``ack`` does, and only after Core has checked every referenced Handoff revision, Pipeline proposal/revision or
  ADR document revision.  A refused verification leaves it ``failed`` -- still actionable.
* Handoff: ONE working document (doc id ``handoff``) in the existing ``ArchitectDocumentStore`` -- no second master.
  Typed section reads/updates with revision CAS; created only by the explicit ``handoff_init`` operation, never by a
  read, login or onboarding.  The generic ``docs_*`` write Tools refuse the protected document.
* Pipeline: Core-owned ``pipeline/main`` (tables, not Markdown).  No resource row = ``uninitialized``.  The Architect may
  only PROPOSE a revision-checked change; only an Owner decision applies it.  Task/Run/Review/Owner facts never close an
  item by themselves (there is no approved item<->Task link).

Nothing here calls a model, starts a Run, grants a group round, sends a message or touches the source checkout.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from .architect_store import ArchitectDocumentStore, DocumentNotFound, RevisionConflict
from .models import (EVENT_REVIEW_SUBMITTED, EVENT_RUN_REPORTED, EVENT_SOURCE_DECISION, EVENT_SOURCE_REPORT,
                     EVENT_SOURCE_REVIEW, EVENT_TASK_ACCEPTED, EVENT_TASK_REWORK, OBLIGATION_COMPLETED,
                     OBLIGATION_FAILED, OBLIGATION_PENDING, OBLIGATION_PROCESSING, PIPELINE_ITEM_KINDS,
                     PIPELINE_ITEM_STATUSES, ArchitectMaintenanceWake, ArchitectObligation, PipelineChange,
                     PipelineItem, PipelineProposal, PipelineResource, ProjectEvent)

log = logging.getLogger(__name__)

HANDOFF_DOC_ID = "handoff"
HANDOFF_PATH = "handoff.md"
PROTECTED_DOC_IDS = frozenset({HANDOFF_DOC_ID})
PROTECTED_PATHS = frozenset({HANDOFF_PATH, "pipeline.md"})
HANDOFF_MARKER = "<!-- rosc:handoff v1 -->"
# Fixed section keys -> headings (order of a freshly initialized Handoff).  Unknown historical headings are preserved.
HANDOFF_SECTIONS: tuple[tuple[str, str], ...] = (
    ("current_focus", "Current focus"),
    ("next_action", "Next actor and action"),
    ("blockers", "Blockers"),
    ("pending_owner_decisions", "Pending Owner decisions"),
    ("last_handled", "Last handled events"),
    ("references", "References"),
    ("notes", "Notes"),
)
_HEADING_TO_KEY = {heading.lower(): key for key, heading in HANDOFF_SECTIONS}
MAX_SECTION_CHARS = 20_000
MAX_BATCH = 20
MAX_PAGE = 100
MAX_ASSESSMENT = 2000
MIN_REASON = 8
MAX_OPS = 50
MAX_TITLE = 300
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ITEM = re.compile(r"^PI-[0-9]{1,6}$")
COVERAGE_NOTE = ("Events are recorded from migration 0024 onward for registered Run Reports, decisive Architect Reviews "
                 "and Owner Task decisions; earlier history is not backfilled.")


class MaintenanceError(Exception):
    """``code`` is machine-readable; the message never carries private content."""

    def __init__(self, code: str, message: str, **extra) -> None:
        super().__init__(code)
        self.code, self.message, self.extra = code, message, extra


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _aware(value: datetime) -> datetime:
    """SQLite returns naive UTC datetimes; compare everything as timezone-aware UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _int(value: object, name: str, *, low: int = 0, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise MaintenanceError("invalid_argument", f"{name} must be an integer >= {low}")
    return value


def _text(value: object, name: str, limit: int, *, required: bool = True, minimum: int = 1) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise MaintenanceError("invalid_argument", f"{name} must be text")
    value = value.strip()
    if len(value) < minimum:
        raise MaintenanceError("invalid_argument", f"{name} needs at least {minimum} characters")
    if len(value) > limit:
        raise MaintenanceError("invalid_argument", f"{name} exceeds {limit} characters")
    return value


def _label(project_id: int) -> str:
    return f"P{project_id:04d}"


def _task_label(task_id: int | None) -> str | None:
    return None if task_id is None else f"T{task_id:04d}"


# -- schema presence -------------------------------------------------------------------------------------------------
_SCHEMA: dict[int, bool] = {}


def schema_ready(session: Session) -> bool:
    """Whether migration 0024 is applied (checked once per engine; an upgrade needs a restart).  Emission is skipped on
    an older database so the authoritative Report/Review/Decision transaction itself never breaks."""
    key = id(session.get_bind())
    known = _SCHEMA.get(key)
    if known is None:
        known = bool(session.connection().exec_driver_sql(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='architect_obligations'").scalar())
        _SCHEMA[key] = known
    return known


# -- P0-A: event emission (caller's transaction) ------------------------------------------------------------------------
def emit(session: Session, *, project_id: int, kind: str, source_kind: str, source_id: str, task_id: int | None,
         run_id: str | None = None, review_id: str | None = None, decision_id: str | None = None,
         digest: str | None = None, facts: dict | None = None, when: datetime | None = None) -> ProjectEvent | None:
    """Record ONE event + its obligation in the caller's open transaction (exactly once per source row)."""
    if not schema_ready(session):
        return None
    existing = session.scalars(select(ProjectEvent).where(ProjectEvent.source_kind == source_kind,
                                                          ProjectEvent.source_id == source_id)).first()
    if existing is not None:
        return existing
    when = when or _now()
    seq = (session.scalar(select(func.max(ProjectEvent.seq)).where(ProjectEvent.project_id == project_id)) or 0) + 1
    event = ProjectEvent(event_id=str(uuid.uuid4()), project_id=project_id, seq=seq, kind=kind,
                         source_kind=source_kind, source_id=source_id, task_id=task_id, run_id=run_id,
                         review_id=review_id, decision_id=decision_id, digest=digest,
                         facts=json.dumps(facts or {}, sort_keys=True, ensure_ascii=False)[:2000], created_at=when)
    session.add(event)
    session.flush()
    session.add(ArchitectObligation(obligation_id=str(uuid.uuid4()), event_id=event.event_id, project_id=project_id,
                                    event_seq=seq, state=OBLIGATION_PENDING, offer_count=0, created_at=when,
                                    updated_at=when))
    session.flush()
    return event


def emit_run_reported(session: Session, row, report, when: datetime) -> None:
    emit(session, project_id=row.project_id, kind=EVENT_RUN_REPORTED, source_kind=EVENT_SOURCE_REPORT,
         source_id=row.run_id, task_id=row.task_id, run_id=row.run_id, digest=report.sha256,
         facts={"outcome": report.outcome}, when=when)


def emit_review(session: Session, review) -> None:
    if review.verdict == "inconclusive":
        return  # a step inside the review, not a consequential outcome
    emit(session, project_id=review.project_id, kind=EVENT_REVIEW_SUBMITTED, source_kind=EVENT_SOURCE_REVIEW,
         source_id=review.review_id, task_id=review.task_id, run_id=review.run_id, review_id=review.review_id,
         digest=review.report_sha256, facts={"verdict": review.verdict, "version": review.version},
         when=review.created_at)


def emit_decision(session: Session, decision) -> None:
    kind = EVENT_TASK_ACCEPTED if decision.decision == "accept" else EVENT_TASK_REWORK
    emit(session, project_id=decision.project_id, kind=kind, source_kind=EVENT_SOURCE_DECISION,
         source_id=decision.decision_id, task_id=decision.task_id, run_id=decision.run_id,
         review_id=decision.review_id, decision_id=decision.decision_id, digest=decision.report_sha256,
         facts={"decision": decision.decision, "review_verdict": decision.review_verdict}, when=decision.created_at)


# -- views ------------------------------------------------------------------------------------------------------------
_READ_WITH = {EVENT_RUN_REPORTED: "rosc_run_read / rosc_report_read run_id",
              EVENT_REVIEW_SUBMITTED: "rosc_run_read run_id (latest review)",
              EVENT_TASK_ACCEPTED: "rosc_task_read task_id",
              EVENT_TASK_REWORK: "rosc_task_read task_id"}


def event_view(event: ProjectEvent) -> dict:
    return {"event_id": event.event_id, "seq": event.seq, "kind": event.kind, "project_id": _label(event.project_id),
            "task_id": _task_label(event.task_id), "run_id": event.run_id, "review_id": event.review_id,
            "decision_id": event.decision_id, "digest": event.digest, "facts": json.loads(event.facts or "{}"),
            "source": {"kind": event.source_kind, "id": event.source_id}, "read_with": _READ_WITH.get(event.kind),
            "recorded_at": _iso(event.created_at)}


def obligation_view(row: ArchitectObligation, event: ProjectEvent) -> dict:
    return {"obligation_id": row.obligation_id, "state": row.state, "event": event_view(event),
            "offer_count": row.offer_count, "offered_wake_id": row.offered_wake_id, "last_error": row.last_error,
            "outcome": json.loads(row.outcome) if row.outcome else None, "completed_at": _iso(row.completed_at),
            "updated_at": _iso(row.updated_at)}


OPEN_STATES = (OBLIGATION_PENDING, OBLIGATION_PROCESSING, OBLIGATION_FAILED)


def counts(session: Session, project_id: int) -> dict:
    rows = session.execute(select(ArchitectObligation.state, func.count()).where(
        ArchitectObligation.project_id == project_id).group_by(ArchitectObligation.state)).all()
    found = {state: 0 for state in (OBLIGATION_PENDING, OBLIGATION_PROCESSING, OBLIGATION_FAILED, OBLIGATION_COMPLETED)}
    found.update({state: n for state, n in rows})
    return found


def list_obligations(session: Session, project_id: int, *, states: tuple[str, ...] = OPEN_STATES,
                     after_seq: int = 0, limit: int = MAX_BATCH) -> tuple[list[dict], bool, int | None]:
    """A compact delta page ordered by event sequence: (items, has_more, next_cursor)."""
    rows = session.execute(select(ArchitectObligation, ProjectEvent).join(
        ProjectEvent, ProjectEvent.event_id == ArchitectObligation.event_id).where(
        ArchitectObligation.project_id == project_id, ArchitectObligation.state.in_(states),
        ArchitectObligation.event_seq > after_seq).order_by(ArchitectObligation.event_seq).limit(limit + 1)).all()
    page = rows[:limit]
    items = [obligation_view(o, e) for o, e in page]
    more = len(rows) > limit
    return items, more, (page[-1][0].event_seq if more and page else None)


def last_handled(session: Session, project_id: int) -> dict | None:
    row = session.execute(select(ArchitectObligation, ProjectEvent).join(
        ProjectEvent, ProjectEvent.event_id == ArchitectObligation.event_id).where(
        ArchitectObligation.project_id == project_id, ArchitectObligation.state == OBLIGATION_COMPLETED).order_by(
        ArchitectObligation.completed_at.desc()).limit(1)).first()
    return None if row is None else obligation_view(*row)


def processed_cursor(session: Session, project_id: int) -> int:
    """Highest event sequence S such that every event <= S is completed (never advanced over an open event)."""
    first_open = session.scalar(select(func.min(ArchitectObligation.event_seq)).where(
        ArchitectObligation.project_id == project_id, ArchitectObligation.state != OBLIGATION_COMPLETED))
    if first_open is not None:
        return first_open - 1
    return session.scalar(select(func.max(ArchitectObligation.event_seq)).where(
        ArchitectObligation.project_id == project_id)) or 0


def wake_view(row: ArchitectMaintenanceWake | None) -> dict | None:
    if row is None:
        return None
    return {"wake_id": row.wake_id, "trigger": row.trigger, "state": row.state, "detail": row.detail,
            "attempt": row.attempt, "obligations": len(json.loads(row.obligation_ids)),
            "sent_at": _iso(row.created_at), "settled_at": _iso(row.settled_at)}


def latest_wake(session: Session, project_id: int) -> ArchitectMaintenanceWake | None:
    return session.scalars(select(ArchitectMaintenanceWake).where(ArchitectMaintenanceWake.project_id == project_id)
                           .order_by(ArchitectMaintenanceWake.created_at.desc()).limit(1)).first()


def wake_candidates(session: Session, project_id: int, session_id: str, *, owner_retry: bool) -> list[ArchitectObligation]:
    """Obligations a new wake of ``session_id`` may offer.

    Automatic (ready/event): only when some open obligation was never offered to THIS session -- a new session sees
    the backlog once, a later event wakes once more (re-listing older unacknowledged work in the same bounded turn),
    and a reply without ``ack`` never loops.  ``processing`` (an offered, unresolved turn) is never re-offered.  Owner
    retry: every pending/failed obligation."""
    rows = list(session.scalars(select(ArchitectObligation).where(
        ArchitectObligation.project_id == project_id,
        ArchitectObligation.state.in_((OBLIGATION_PENDING, OBLIGATION_FAILED))).order_by(ArchitectObligation.event_seq)))
    if owner_retry:
        return rows
    offered = {w.wake_id: w.session_id for w in session.scalars(select(ArchitectMaintenanceWake).where(
        ArchitectMaintenanceWake.project_id == project_id))}
    fresh = [r for r in rows if r.offered_wake_id is None or offered.get(r.offered_wake_id) != session_id]
    return rows if fresh else []


def wake_text(project_id: int, items: list[ArchitectObligation], events: dict[str, ProjectEvent], more: int) -> str:
    lines = []
    for row in items:
        event = events[row.event_id]
        facts = json.loads(event.facts or "{}")
        detail = ", ".join(f"{k}={v}" for k, v in sorted(facts.items()))
        ref = " ".join(x for x in (_task_label(event.task_id), f"run_id={event.run_id}" if event.run_id else None) if x)
        lines.append(f"- #{event.seq} {event.kind} {ref} ({detail}) obligation_id={row.obligation_id}")
    tail = f"\n- ... and {more} more (rosc_maintenance_pending pages them)" if more else ""
    return ("[ROSC maintenance] Core housekeeping turn, not an Owner request. Project "
            f"{_label(project_id)} has {len(items) + more} consequential event(s) awaiting your assessment:\n"
            + "\n".join(lines) + tail + "\n"
            "For each: inspect the source with your Tools (rosc_maintenance_pending, rosc_run_read, rosc_report_read, "
            "rosc_task_read), then decide whether the Handoff (rosc_handoff_get_snapshot / rosc_handoff_update_checkpoint"
            "), the Pipeline (rosc_pipeline_get_current / rosc_pipeline_propose_change) or an ADR (rosc_docs_create) "
            "actually needs a change. Record EACH outcome with rosc_maintenance_ack: the revisions you wrote, or "
            "no_change_needed with a short reason. Do not start Runs, approve anything, accept milestones or message "
            "other Agents. Reply with one short line when done.")


# -- P0-A.5: acknowledge with verified outcome ------------------------------------------------------------------------
def _outcome_digest(outcome: dict) -> str:
    return hashlib.sha256(json.dumps(outcome, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def acknowledge(session: Session, store: ArchitectDocumentStore, *, project_id: int, agent_id: str, session_id: str,
                args: dict) -> dict:
    """Complete one or more obligations with ONE verified outcome (all or nothing)."""
    ids = args.get("obligation_ids")
    if isinstance(ids, str):
        ids = [ids]
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BATCH or not all(isinstance(i, str) and _UUID.match(i)
                                                                                for i in ids):
        raise MaintenanceError("invalid_argument", f"obligation_ids must list 1..{MAX_BATCH} obligation ids")
    assessment = _text(args.get("assessment"), "assessment", MAX_ASSESSMENT)
    outcome: dict = {"assessment": assessment}
    no_change = _text(args.get("no_change_needed"), "no_change_needed", MAX_ASSESSMENT, required=False,
                      minimum=MIN_REASON)
    handoff_rev = args.get("handoff_revision")
    proposal_id = args.get("pipeline_proposal_id")
    adr_doc, adr_rev = args.get("adr_doc_id"), args.get("adr_revision")
    pipeline_reason = _text(args.get("pipeline_no_change_reason"), "pipeline_no_change_reason", MAX_ASSESSMENT,
                            required=False, minimum=MIN_REASON)
    adr_reason = _text(args.get("adr_not_needed_reason"), "adr_not_needed_reason", MAX_ASSESSMENT, required=False,
                       minimum=MIN_REASON)
    changed = [x for x in (handoff_rev, proposal_id, adr_doc) if x is not None]
    if no_change is not None and changed:
        raise MaintenanceError("invalid_argument", "no_change_needed cannot be combined with recorded changes")
    if no_change is None and not changed:
        raise MaintenanceError("outcome_required", "Name what you recorded (handoff_revision, pipeline_proposal_id, "
                               "adr_doc_id + adr_revision) or give no_change_needed with a reason; a bare 'done' is "
                               "not an outcome")
    if (adr_doc is None) != (adr_rev is None):
        raise MaintenanceError("invalid_argument", "adr_doc_id and adr_revision go together")

    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    rows = list(session.scalars(select(ArchitectObligation).where(ArchitectObligation.obligation_id.in_(ids))))
    if len(rows) != len(set(ids)) or any(r.project_id != project_id for r in rows):
        session.rollback()
        raise MaintenanceError("not_found", "No such obligation in the current Project")
    events = {e.event_id: e for e in session.scalars(select(ProjectEvent).where(
        ProjectEvent.event_id.in_([r.event_id for r in rows])))}
    # Shared evidence must post-date EVERY covered event: freshness is judged against the LATEST one in the batch.
    latest = max(_aware(events[r.event_id].created_at) for r in rows)
    batch_events = {r.event_id for r in rows}

    problem: tuple[str, str] | None = None
    try:
        if no_change is not None:
            outcome["no_change_needed"] = no_change
        if handoff_rev is not None:
            rev = _int(handoff_rev, "handoff_revision", low=1)
            try:
                document = store.read(project_id, HANDOFF_DOC_ID, rev)
            except DocumentNotFound:
                raise MaintenanceError("handoff_revision_missing", "That Handoff revision does not exist") from None
            entry = document.info.revisions[rev - 1]
            if _parse_iso(entry.created_at) < latest:
                raise MaintenanceError("handoff_revision_stale", "That Handoff revision predates an acknowledged "
                                       "event; record a checkpoint after assessing all of them, or use "
                                       "no_change_needed")
            outcome["handoff"] = {"doc_id": HANDOFF_DOC_ID, "revision": rev, "sha256": entry.sha256}
        if proposal_id is not None:
            proposal = session.get(PipelineProposal, proposal_id) if isinstance(proposal_id, str) else None
            if proposal is None or proposal.project_id != project_id:
                raise MaintenanceError("pipeline_proposal_missing", "No such Pipeline proposal in the current Project")
            if _aware(proposal.created_at) < latest:
                raise MaintenanceError("pipeline_proposal_stale", "That proposal predates an acknowledged event")
            linked = set(json.loads(proposal.source_event_ids or "[]"))
            if not batch_events <= linked:
                raise MaintenanceError("pipeline_proposal_unrelated", "That proposal does not list every acknowledged "
                                       "event in its source_event_ids; propose with those event ids or use "
                                       "pipeline_no_change_reason")
            outcome["pipeline"] = {"proposal_id": proposal.proposal_id, "base_revision": proposal.base_revision,
                                   "state_at_ack": proposal.state}
        elif pipeline_reason is not None:
            outcome["pipeline"] = {"no_change": pipeline_reason}
        if adr_doc is not None:
            rev = _int(adr_rev, "adr_revision", low=1)
            if not isinstance(adr_doc, str) or adr_doc.strip().lower() in PROTECTED_DOC_IDS:
                raise MaintenanceError("invalid_argument", "adr_doc_id must name an ADR document")
            try:
                document = store.read(project_id, adr_doc, rev)
            except DocumentNotFound:
                raise MaintenanceError("adr_revision_missing", "That ADR document revision does not exist") from None
            if _parse_iso(document.info.revisions[rev - 1].created_at) < latest:
                raise MaintenanceError("adr_revision_stale", "That ADR revision predates an acknowledged event; it "
                                       "cannot document an assessment of it (use adr_not_needed_reason if none is "
                                       "warranted)")
            outcome["adr"] = {"doc_id": document.info.doc_id, "revision": rev,
                              "sha256": document.info.revisions[rev - 1].sha256}
        elif adr_reason is not None:
            outcome["adr"] = {"not_needed": adr_reason}
    except MaintenanceError as exc:
        problem = (exc.code, exc.message)
    except Exception as exc:  # store unsafe/invalid: the obligation stays actionable
        problem = ("verification_failed", type(exc).__name__)

    now = _now()
    if problem is not None:
        for row in rows:
            if row.state != OBLIGATION_COMPLETED:
                row.state, row.last_error, row.updated_at = OBLIGATION_FAILED, problem[0][:48], now
        session.commit()
        raise MaintenanceError(problem[0], problem[1] + "; the obligation stays open")

    digest = _outcome_digest(outcome)
    done = [r for r in rows if r.state == OBLIGATION_COMPLETED]
    if done:
        same = all(r.outcome and _outcome_digest({k: v for k, v in json.loads(r.outcome).items()
                                                  if k not in ("by", "verified_at")}) == digest for r in done)
        session.rollback()
        if same and len(done) == len(rows):
            return {"recorded": False, "replayed": True, "completed": [r.obligation_id for r in rows]}
        raise MaintenanceError("already_completed", "An obligation in this batch is already completed with a different "
                               "outcome; completion is never replaced",
                               completed=[r.obligation_id for r in done])
    stored = {**outcome, "by": {"agent_id": agent_id, "session_id": session_id}, "verified_at": _iso(now)}
    for row in rows:
        row.state, row.outcome, row.last_error = OBLIGATION_COMPLETED, json.dumps(stored, ensure_ascii=False), None
        row.completed_agent_id, row.completed_session_id, row.completed_at, row.updated_at = agent_id, session_id, now, now
    session.commit()
    return {"recorded": True, "replayed": False, "completed": [r.obligation_id for r in rows],
            "outcome": stored, "processed_cursor": processed_cursor(session, project_id)}


# -- P0-B: Handoff section document -------------------------------------------------------------------------------------
def _split(text: str) -> tuple[str, list[list[str]]]:
    """(preamble, [[key, heading_line, body], ...]) preserving every byte of the original text."""
    preamble: list[str] = []
    sections: list[list[str]] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("## "):
            heading = line[3:].strip()
            key = _HEADING_TO_KEY.get(heading.lower()) or re.sub(r"[^a-z0-9]+", "_", heading.lower()).strip("_")
            sections.append([key or "section", line, ""])
        elif sections:
            sections[-1][2] += line
        else:
            preamble.append(line)
    return "".join(preamble), sections


def _join(preamble: str, sections: list[list[str]]) -> str:
    return preamble + "".join(heading + body for _key, heading, body in sections)


def _body(text: str) -> str:
    return text.rstrip("\n") + "\n\n"


def handoff_template(project_id: int, focus: str | None) -> str:
    parts = [f"# Architect Handoff - {_label(project_id)}\n\n{HANDOFF_MARKER}\n\n"]
    for key, heading in HANDOFF_SECTIONS:
        content = focus if key == "current_focus" and focus else "(none recorded)"
        parts.append(f"## {heading}\n\n{content}\n\n")
    return "".join(parts)


def _read_handoff(store: ArchitectDocumentStore, project_id: int, revision: int | None = None):
    try:
        return store.read(project_id, HANDOFF_DOC_ID, revision)
    except DocumentNotFound:
        return None


def handoff_snapshot(store: ArchitectDocumentStore, project_id: int, since_revision: object = None) -> dict:
    document = _read_handoff(store, project_id)
    if document is None:
        return {"kind": "handoff_snapshot", "exists": False, "doc_id": HANDOFF_DOC_ID,
                "note": "No Handoff exists. It is created only by rosc_handoff_init (an explicit maintenance step)."}
    info = document.info
    _preamble, sections = _split(document.text)
    previous: dict[str, str] | None = None
    if since_revision is not None:
        since = _int(since_revision, "since_revision", low=1)
        if since > info.revision:
            raise MaintenanceError("invalid_argument", "since_revision is newer than the current revision",
                                   current_revision=info.revision)
        old = store.read(project_id, HANDOFF_DOC_ID, since)
        previous = {key: body for key, _h, body in _split(old.text)[1]}
    view = []
    for key, heading, body in sections:
        item = {"section": key, "heading": heading[3:].strip(), "chars": len(body.strip())}
        if key in ("current_focus", "next_action", "blockers", "pending_owner_decisions"):
            item["text"] = body.strip()[:1000]
            item["truncated"] = len(body.strip()) > 1000
        if previous is not None:
            item["changed_since"] = previous.get(key) != body
        view.append(item)
    result = {"kind": "handoff_snapshot", "exists": True, "doc_id": HANDOFF_DOC_ID, "revision": info.revision,
              "sha256": info.sha256, "updated_at": info.updated_at, "sections": view, "complete": True,
              "read_more_with": "rosc_handoff_get_section"}
    if previous is not None:
        result["since_revision"] = since_revision
        result["removed_since"] = sorted(set(previous) - {key for key, _h, _b in sections})
    return result


def handoff_section(store: ArchitectDocumentStore, project_id: int, section: object, revision: object,
                    offset: object, max_chars: object) -> dict:
    if not isinstance(section, str) or not section:
        raise MaintenanceError("invalid_argument", "section is required (see rosc_handoff_get_snapshot)")
    rev = None if revision is None else _int(revision, "revision", low=1)
    document = _read_handoff(store, project_id, rev)
    if document is None:
        raise MaintenanceError("handoff_absent", "No Handoff exists (rosc_handoff_init creates it explicitly)")
    found = [body for key, _h, body in _split(document.text)[1] if key == section]
    if not found:
        raise MaintenanceError("section_not_found", "No such Handoff section")
    body = found[0].strip("\n")
    start = _int(offset, "offset", default=0)
    size = min(_int(max_chars, "max_chars", low=1, default=8000), MAX_SECTION_CHARS)
    chunk = body[start:start + size]
    return {"kind": "handoff_section", "section": section, "revision": document.revision,
            "current_revision": document.info.revision, "text": chunk, "offset": start,
            "next_offset": start + len(chunk) if start + len(chunk) < len(body) else None, "total_chars": len(body)}


def handoff_init(store: ArchitectDocumentStore, project_id: int, actor_id: str, focus: object) -> dict:
    if _read_handoff(store, project_id) is not None:
        raise MaintenanceError("already_exists", "A Handoff already exists; use rosc_handoff_update_checkpoint")
    text = handoff_template(project_id, _text(focus, "current_focus", 2000, required=False))
    info = store.write(project_id, HANDOFF_DOC_ID, text, actor_id=actor_id, expected_revision=None,
                       media_type="text/markdown", logical_path=HANDOFF_PATH)
    return {"kind": "handoff_saved", "created": True, "revision": info.revision, "sha256": info.sha256}


def handoff_update(store: ArchitectDocumentStore, project_id: int, actor_id: str, args: dict) -> dict:
    expected = _int(args.get("expected_revision"), "expected_revision", low=1)
    updates = args.get("sections")
    append = args.get("append_sections") or []
    if not isinstance(updates, dict) or not updates or len(updates) > len(HANDOFF_SECTIONS) + 10:
        raise MaintenanceError("invalid_argument", "sections must map section keys to new text")
    if not isinstance(append, list) or not all(isinstance(k, str) for k in append):
        raise MaintenanceError("invalid_argument", "append_sections must list section keys")
    sources = args.get("source_event_ids") or []
    if not isinstance(sources, list) or len(sources) > MAX_BATCH or not all(
            isinstance(s, str) and _UUID.match(s) for s in sources):
        raise MaintenanceError("invalid_argument", "source_event_ids must list event ids")
    document = _read_handoff(store, project_id)
    if document is None:
        raise MaintenanceError("handoff_absent", "No Handoff exists (rosc_handoff_init creates it explicitly)")
    if document.info.revision != expected:
        raise RevisionConflict("The Handoff changed", document.info.revision)
    preamble, sections = _split(document.text)
    keys = {key for key, _h, _b in sections}
    known = {key for key, _h in HANDOFF_SECTIONS}
    for key, value in updates.items():
        text = _text(value, f"sections.{key}", MAX_SECTION_CHARS, minimum=0)
        if key not in keys and key not in known:
            raise MaintenanceError("section_not_found", f"Unknown Handoff section {key!r}")
        if key not in keys:  # a fixed section missing from an older document is added at the end
            sections.append([key, f"## {dict(HANDOFF_SECTIONS)[key]}\n", "\n"])
        for entry in sections:
            if entry[0] == key:
                if key in append:
                    entry[2] = _body(entry[2].rstrip("\n") + "\n" + text) if entry[2].strip() else "\n" + _body(text)
                else:
                    entry[2] = "\n" + _body(text)
                break
    new_text = _join(preamble, sections)
    if new_text == document.text:
        return {"kind": "handoff_saved", "created": False, "changed": False, "revision": document.info.revision,
                "sha256": document.info.sha256}
    info = store.write(project_id, HANDOFF_DOC_ID, new_text, actor_id=actor_id, expected_revision=expected,
                       media_type=document.info.media_type)
    return {"kind": "handoff_saved", "created": False, "changed": True, "revision": info.revision,
            "sha256": info.sha256, "sections_changed": sorted(updates), "source_event_ids": sources,
            "note": "Use this revision as handoff_revision in rosc_maintenance_ack."}


# -- P0-C: canonical Pipeline -------------------------------------------------------------------------------------------
def _item_view(item: PipelineItem) -> dict:
    return {"item_id": item.item_id, "kind": item.kind, "parent_id": item.parent_id, "title": item.title,
            "status": item.status, "position": item.position, "created_revision": item.created_revision,
            "updated_revision": item.updated_revision, "updated_at": _iso(item.updated_at)}


def proposal_view(row: PipelineProposal) -> dict:
    return {"proposal_id": row.proposal_id, "base_revision": row.base_revision, "state": row.state,
            "ops": json.loads(row.ops), "rationale": row.rationale,
            "source_event_ids": json.loads(row.source_event_ids), "proposed_by_agent_id": row.proposed_by_agent_id,
            "created_at": _iso(row.created_at), "decided_at": _iso(row.decided_at), "decided_by": row.decided_by,
            "decision_reason": row.decision_reason, "resulting_revision": row.resulting_revision}


def pipeline_current(session: Session, project_id: int) -> dict:
    resource = session.get(PipelineResource, project_id)
    open_rows = list(session.scalars(select(PipelineProposal).where(
        PipelineProposal.project_id == project_id, PipelineProposal.state == "proposed").order_by(
        PipelineProposal.created_at).limit(MAX_BATCH)))
    base = {"kind": "pipeline", "resource": "pipeline/main", "project_id": _label(project_id),
            "open_proposals": [{"proposal_id": p.proposal_id, "base_revision": p.base_revision,
                                "created_at": _iso(p.created_at)} for p in open_rows],
            "authority": "Core owns pipeline/main. Architect proposals take effect only by Owner decision; a Task "
                         "closure or Run report never closes an item by itself."}
    if resource is None:
        return {**base, "state": "uninitialized", "revision": 0, "item_counts": {},
                "note": "No canonical Pipeline exists yet (no Owner-approved plan). Nothing was imported from earlier "
                        "roadmaps. A first proposal uses base_revision 0."}
    rows = session.execute(select(PipelineItem.status, func.count()).where(
        PipelineItem.project_id == project_id).group_by(PipelineItem.status)).all()
    return {**base, "state": "active", "revision": resource.revision, "item_counts": dict(rows),
            "updated_at": _iso(resource.updated_at)}


def pipeline_items(session: Session, project_id: int, args: dict) -> dict:
    resource = session.get(PipelineResource, project_id)
    statuses = args.get("status")
    if isinstance(statuses, str):
        statuses = [statuses]
    if statuses is not None and (not isinstance(statuses, list) or not set(statuses) <= set(PIPELINE_ITEM_STATUSES)):
        raise MaintenanceError("invalid_argument", "status must be among " + ", ".join(PIPELINE_ITEM_STATUSES))
    limit = min(_int(args.get("limit"), "limit", low=1, default=50), MAX_PAGE)
    cursor = args.get("cursor")
    if resource is None:
        return {"kind": "pipeline_items", "state": "uninitialized", "revision": 0, "items": [], "has_more": False,
                "next_cursor": None}
    statement = select(PipelineItem).where(PipelineItem.project_id == project_id)
    if statuses:
        statement = statement.where(PipelineItem.status.in_(statuses))
    if args.get("parent_id") not in (None, ""):
        statement = statement.where(PipelineItem.parent_id == args.get("parent_id"))
    for name, op in (("updated_after", ">="), ("updated_before", "<")):
        value = args.get(name)
        if value not in (None, ""):
            try:
                moment = _parse_iso(str(value))
            except ValueError:
                raise MaintenanceError("invalid_argument", f"{name} must be an ISO-8601 timestamp") from None
            column = PipelineItem.updated_at
            statement = statement.where(column >= moment if op == ">=" else column < moment)
    if cursor not in (None, ""):
        if not isinstance(cursor, str) or not _ITEM.match(cursor):
            raise MaintenanceError("invalid_argument", "cursor must be the next_cursor value you received")
        statement = statement.where(cast(func.substr(PipelineItem.item_id, 4), Integer) > int(cursor[3:]))
    rows = list(session.scalars(statement.order_by(cast(func.substr(PipelineItem.item_id, 4), Integer))
                                .limit(limit + 1)))
    page = rows[:limit]
    more = len(rows) > limit
    return {"kind": "pipeline_items", "state": "active", "revision": resource.revision,
            "items": [_item_view(i) for i in page], "has_more": more,
            "next_cursor": page[-1].item_id if more and page else None}


def pipeline_changes(session: Session, project_id: int, args: dict) -> dict:
    resource = session.get(PipelineResource, project_id)
    since = _int(args.get("since_revision"), "since_revision", default=0)
    limit = min(_int(args.get("limit"), "limit", low=1, default=50), MAX_PAGE)
    current = resource.revision if resource is not None else 0
    if since > current:
        raise MaintenanceError("invalid_argument", "since_revision is newer than the current revision",
                               current_revision=current)
    after_seq = 0
    cursor = args.get("cursor")
    after_rev = since
    if cursor not in (None, ""):
        match = re.fullmatch(r"([0-9]+):([0-9]+)", cursor) if isinstance(cursor, str) else None
        if not match:
            raise MaintenanceError("invalid_argument", "cursor must be the next_cursor value you received")
        after_rev, after_seq = int(match.group(1)), int(match.group(2))
    rows = list(session.scalars(select(PipelineChange).where(
        PipelineChange.project_id == project_id,
        (PipelineChange.revision > after_rev) | ((PipelineChange.revision == after_rev) & (PipelineChange.seq > after_seq)),
        PipelineChange.revision > since).order_by(PipelineChange.revision, PipelineChange.seq).limit(limit + 1)))
    page = rows[:limit]
    more = len(rows) > limit
    return {"kind": "pipeline_changes", "state": "active" if resource else "uninitialized", "since_revision": since,
            "current_revision": current,
            "changes": [{"revision": c.revision, "seq": c.seq, "item_id": c.item_id, "op": c.op,
                         "before": json.loads(c.before) if c.before else None, "after": json.loads(c.after),
                         "proposal_id": c.proposal_id, "decided_by": c.decided_by, "at": _iso(c.created_at)}
                        for c in page],
            "has_more": more, "next_cursor": f"{page[-1].revision}:{page[-1].seq}" if more and page else None}


def _validate_ops(ops: object) -> list[dict]:
    if not isinstance(ops, list) or not 1 <= len(ops) <= MAX_OPS:
        raise MaintenanceError("invalid_argument", f"ops must list 1..{MAX_OPS} operations")
    clean = []
    for op in ops:
        if not isinstance(op, dict) or op.get("op") not in ("add_item", "update_item"):
            raise MaintenanceError("invalid_argument", "each op is {op: add_item|update_item, ...}")
        if op["op"] == "add_item":
            extra = set(op) - {"op", "ref", "kind", "title", "parent_id", "status"}
            kind, status = op.get("kind", "step"), op.get("status", "backlog")
            if extra or kind not in PIPELINE_ITEM_KINDS or status not in ("backlog", "running"):
                raise MaintenanceError("invalid_argument", "add_item takes kind (phase|milestone|step), title, optional "
                                       "parent_id/ref and status backlog|running")
            clean.append({"op": "add_item", "kind": kind, "title": _text(op.get("title"), "title", MAX_TITLE),
                          "parent_id": op.get("parent_id"), "status": status,
                          **({"ref": str(op["ref"])[:32]} if op.get("ref") else {})})
        else:
            extra = set(op) - {"op", "item_id", "title", "status", "parent_id"}
            if extra or not isinstance(op.get("item_id"), str) or not _ITEM.match(op["item_id"]):
                raise MaintenanceError("invalid_argument", "update_item takes item_id and title/status/parent_id")
            change = {"op": "update_item", "item_id": op["item_id"]}
            if "title" in op:
                change["title"] = _text(op.get("title"), "title", MAX_TITLE)
            if "status" in op:
                if op["status"] not in PIPELINE_ITEM_STATUSES:
                    raise MaintenanceError("invalid_argument", "unknown status")
                change["status"] = op["status"]
            if "parent_id" in op:
                change["parent_id"] = op["parent_id"]
            if len(change) == 2:
                raise MaintenanceError("invalid_argument", "update_item changes nothing")
            clean.append(change)
    return clean


def propose(session: Session, *, project_id: int, agent_id: str, session_id: str, args: dict) -> dict:
    base = _int(args.get("base_revision"), "base_revision")
    ops = _validate_ops(args.get("ops"))
    rationale = _text(args.get("rationale"), "rationale", 4000, minimum=MIN_REASON)
    sources = args.get("source_event_ids") or []
    if not isinstance(sources, list) or len(sources) > MAX_BATCH or not all(
            isinstance(s, str) and _UUID.match(s) for s in sources):
        raise MaintenanceError("invalid_argument", "source_event_ids must list event ids")
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    if sources:
        found = set(session.scalars(select(ProjectEvent.event_id).where(
            ProjectEvent.event_id.in_(sources), ProjectEvent.project_id == project_id)))
        if found != set(sources):
            session.rollback()
            raise MaintenanceError("not_found", "A source event is not an event of the current Project")
    resource = session.get(PipelineResource, project_id)
    current = resource.revision if resource is not None else 0
    if base != current:
        session.rollback()
        raise MaintenanceError("revision_conflict", "The Pipeline changed; read it again", current_revision=current)
    row = PipelineProposal(proposal_id=str(uuid.uuid4()), project_id=project_id, base_revision=base,
                           ops=json.dumps(ops, ensure_ascii=False), rationale=rationale,
                           source_event_ids=json.dumps(sources), state="proposed", proposed_by_agent_id=agent_id,
                           proposed_session_id=session_id, created_at=_now())
    session.add(row)
    session.commit()
    return {"kind": "pipeline_proposal", **proposal_view(row),
            "note": "Recorded as a PROPOSAL. It changes nothing until the Owner accepts it."}


def decide(session: Session, *, project_id: int, proposal_id: str, decision: str, reason: str | None,
           actor_id: str) -> dict:
    """Owner-only: apply (accept) or reject one proposal.  Accept re-checks the base revision (stale -> refused)."""
    if decision not in ("accept", "reject"):
        raise MaintenanceError("invalid_argument", "decision must be accept or reject")
    reason = _text(reason, "reason", 2000, required=False)
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    row = session.get(PipelineProposal, proposal_id)
    if row is None or row.project_id != project_id:
        session.rollback()
        raise MaintenanceError("not_found", "No such Pipeline proposal")
    if row.state != "proposed":
        view = proposal_view(row)
        session.rollback()
        if row.state == ("accepted" if decision == "accept" else "rejected"):
            return {"recorded": False, "replayed": True, "proposal": view}
        raise MaintenanceError("already_decided", "This proposal is already decided", state=row.state)
    now = _now()
    resource = session.get(PipelineResource, project_id)
    current = resource.revision if resource is not None else 0
    if decision == "reject":
        row.state, row.decided_at, row.decided_by, row.decision_reason = "rejected", now, actor_id, reason
        session.commit()
        return {"recorded": True, "replayed": False, "proposal": proposal_view(row)}
    if row.base_revision != current:
        row.state, row.decided_at, row.decided_by = "stale", now, actor_id
        row.decision_reason = f"base revision {row.base_revision} != current {current}"
        session.commit()
        raise MaintenanceError("proposal_stale", "The Pipeline changed after this proposal; ask for a new one",
                               current_revision=current)
    revision = current + 1
    if resource is None:
        resource = PipelineResource(project_id=project_id, revision=1, last_item_number=0, created_at=now,
                                    updated_at=now)
        session.add(resource)
        session.flush()
    else:
        resource.revision, resource.updated_at = revision, now
    items = {i.item_id: i for i in session.scalars(select(PipelineItem).where(PipelineItem.project_id == project_id))}
    refs: dict[str, str] = {}
    seq = 0
    try:
        for op in json.loads(row.ops):
            seq += 1
            if op["op"] == "add_item":
                parent = refs.get(op.get("parent_id") or "", op.get("parent_id"))
                if parent is not None and parent not in items:
                    raise MaintenanceError("invalid_operation", f"parent {parent} does not exist")
                resource.last_item_number += 1
                item_id = f"PI-{resource.last_item_number}"
                if op.get("ref"):
                    refs[op["ref"]] = item_id
                item = PipelineItem(project_id=project_id, item_id=item_id, kind=op["kind"], parent_id=parent,
                                    title=op["title"], status=op["status"], position=len(items) + 1,
                                    created_revision=revision, updated_revision=revision, created_at=now,
                                    updated_at=now)
                session.add(item)
                items[item_id] = item
                after = _item_view(item)
                after["updated_at"] = _iso(now)
                session.add(PipelineChange(project_id=project_id, revision=revision, seq=seq, item_id=item_id,
                                           op="add", before=None, after=json.dumps(after, ensure_ascii=False),
                                           proposal_id=row.proposal_id, decided_by=actor_id, created_at=now))
            else:
                item = items.get(op["item_id"])
                if item is None:
                    raise MaintenanceError("invalid_operation", f"item {op['item_id']} does not exist")
                before = _item_view(item)
                for name in ("title", "status", "parent_id"):
                    if name in op:
                        value = refs.get(op[name], op[name]) if name == "parent_id" else op[name]
                        if name == "parent_id" and value is not None and (value not in items or value == item.item_id):
                            raise MaintenanceError("invalid_operation", f"parent {value} is not valid")
                        setattr(item, name, value)
                item.updated_revision, item.updated_at = revision, now
                session.add(PipelineChange(project_id=project_id, revision=revision, seq=seq, item_id=item.item_id,
                                           op="update", before=json.dumps(before, ensure_ascii=False),
                                           after=json.dumps(_item_view(item), ensure_ascii=False),
                                           proposal_id=row.proposal_id, decided_by=actor_id, created_at=now))
        _check_hierarchy(items)  # the RESULTING parent graph must stay a forest (no dangling parent, no cycle)
    except MaintenanceError:
        session.rollback()  # nothing applied: no item, change row, revision or item-number bump; still "proposed"
        raise
    row.state, row.decided_at, row.decided_by = "accepted", now, actor_id
    row.decision_reason, row.resulting_revision = reason, revision
    session.commit()
    return {"recorded": True, "replayed": False, "proposal": proposal_view(row), "revision": revision}


def _check_hierarchy(items: dict[str, PipelineItem]) -> None:
    """Every parent chain of the Project's items must end at a root: refuse self/indirect/longer cycles and parents
    outside this Project's items (dangling or cross-Project)."""
    settled: set[str] = set()
    for start in items:
        path: list[str] = []
        seen: set[str] = set()
        node: str | None = start
        while node is not None and node not in settled:
            if node in seen:
                raise MaintenanceError("invalid_hierarchy", "The resulting Pipeline hierarchy would contain a cycle",
                                       cycle=path[path.index(node):] + [node])
            seen.add(node)
            path.append(node)
            item = items.get(node)
            if item is None:
                raise MaintenanceError("invalid_hierarchy", f"parent {node} does not exist in this Pipeline")
            node = item.parent_id
        settled.update(path)


def list_proposals(session: Session, project_id: int, limit: int = MAX_BATCH) -> list[dict]:
    rows = session.scalars(select(PipelineProposal).where(PipelineProposal.project_id == project_id).order_by(
        PipelineProposal.created_at.desc()).limit(limit))
    return [proposal_view(r) for r in rows]

