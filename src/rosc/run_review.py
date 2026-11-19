"""P055: the first explicit Architect Review completion slice for a ``reported`` managed Run.

The ONLY transition event is an authenticated Architect calling the ``rosc_review_submit`` Tool: the verified live
Architect session (resolved by ``ProjectContextBroker``) supplies the identity and the current Project; the model only
names the Run, its verdict and findings.  A chat message - however thorough - never records or changes a review, and
nothing here is inferred from conversation text or backfilled from history.

A recorded review is immutable, stored apart from ``ManagedRunState`` (the Worker Run stays ``reported``) and bound to
the exact registered Worker Report digest.  ``review_passed`` means "the Architect assessed the Worker evidence": never
VERIFIED, never Owner ACCEPTED, never permission to merge or re-run.  An identical replay is idempotent; a contradicting
one is refused; only an ``inconclusive`` verdict can be superseded, explicitly, by the next version with an optimistic
``expected_version`` check.  Nothing here starts a Run, changes a Spec, delegates work, sends a message or calls a model.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .models import (REVIEW_CHANGES_REQUESTED, REVIEW_INCONCLUSIVE, REVIEW_PASSED, REVIEW_VERDICTS, RUN_REPORTED,
                     AgentDefinition, ArchitectToolAudit, ManagedRunReport, ManagedRunReview, ManagedRunState)

MAX_SUMMARY = 4000
MAX_ITEM = 1000
MAX_ITEMS = 30
CONTINUE_BY = ("architect", "owner")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
NOTE = ("Architect Review = the Architect's recorded assessment of Worker-authored evidence. It is not VERIFIED, not "
        "Owner ACCEPTED and no permission to merge or re-run; the Worker Run stays 'reported'.")


class ReviewRefused(Exception):
    """A refused submission; ``code`` is machine-readable and the message carries no private content."""

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


def _text(value: object, name: str, limit: int, *, required: bool) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or "\x00" in value:
        raise ReviewRefused("invalid_argument", f"{name} must be text")
    value = value.strip()
    if required and not value:
        raise ReviewRefused("invalid_argument", f"{name} is required")
    if len(value) > limit:
        raise ReviewRefused("invalid_argument", f"{name} exceeds {limit} characters")
    return value


def _items(value: object, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ReviewRefused("invalid_argument", f"{name} must be a list of at most {MAX_ITEMS} short texts")
    out = [_text(item, name, MAX_ITEM, required=True) for item in value]
    return out


def latest_review(session: Session, run_id: str) -> ManagedRunReview | None:
    return session.scalars(select(ManagedRunReview).where(ManagedRunReview.run_id == run_id)
                           .order_by(ManagedRunReview.version.desc()).limit(1)).first()


def review_view(session: Session, row: ManagedRunReview) -> dict:
    agent = session.get(AgentDefinition, row.architect_agent_id)
    return {"review_id": row.review_id, "run_id": row.run_id, "task_id": f"T{row.task_id:04d}",
            "version": row.version, "supersedes_review_id": row.supersedes_review_id, "verdict": row.verdict,
            "summary": row.summary, "findings": json.loads(row.findings), "limitations": json.loads(row.limitations),
            "continue_by": row.continue_by, "report_path": row.report_path, "report_sha256": row.report_sha256,
            "architect": {"agent_id": row.architect_agent_id,
                          "display_name": agent.display_name if agent is not None else None},
            "recorded_at": _iso(row.created_at), "source": "rosc_review_submit"}


def review_state(session: Session, run_id: str, run_state: str) -> dict:
    """Run-projection review state, derived from the recorded review only (never from chat text)."""
    if run_state != RUN_REPORTED:
        return {"state": "not_ready", "next_actor": None, "latest": None, "note": NOTE}
    row = latest_review(session, run_id)
    if row is None:
        return {"state": "awaiting_architect_review", "next_actor": "Architect", "latest": None, "note": NOTE}
    if row.verdict == REVIEW_PASSED:
        actor = "Owner"
    elif row.verdict == REVIEW_CHANGES_REQUESTED:
        actor = "Owner"
    else:
        actor = "Architect" if row.continue_by == "architect" else "Owner"
    return {"state": row.verdict, "next_actor": actor, "latest": review_view(session, row), "note": NOTE}


def _submission_digest(run_id: str, agent_id: str, verdict: str, summary: str, findings: list[str],
                       limitations: list[str], continue_by: str | None, report_sha256: str) -> str:
    body = {"run_id": run_id, "agent_id": agent_id, "verdict": verdict, "summary": summary, "findings": findings,
            "limitations": limitations, "continue_by": continue_by, "report_sha256": report_sha256}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def submit_review(session: Session, *, project_id: int, architect_session_id: str, architect_agent_id: str,
                  architect_assignment_id: str, run_id: object, task_id: int | None, args: dict) -> dict:
    """Record ONE review (or replay it).  The caller has already verified the live Architect session and, through the
    run-report seam, that the registered Report file still matches its digest."""
    if not isinstance(run_id, str) or not _UUID.match(run_id):
        raise ReviewRefused("not_found", "No such managed Run in the current Project")
    verdict = args.get("verdict")
    if verdict not in REVIEW_VERDICTS:
        raise ReviewRefused("invalid_argument", "verdict must be review_passed, changes_requested or inconclusive")
    summary = _text(args.get("summary"), "summary", MAX_SUMMARY, required=True)
    findings = _items(args.get("blocking_findings"), "blocking_findings")
    limitations = _items(args.get("limitations"), "limitations")
    continue_by = args.get("continue_by")
    if verdict == REVIEW_PASSED and findings:
        raise ReviewRefused("invalid_argument", "review_passed cannot carry blocking findings; use changes_requested")
    if verdict == REVIEW_CHANGES_REQUESTED and not findings:
        raise ReviewRefused("invalid_argument", "changes_requested needs at least one blocking finding")
    if verdict == REVIEW_INCONCLUSIVE:
        if not limitations:
            raise ReviewRefused("invalid_argument", "inconclusive needs the missing evidence in limitations")
        if continue_by not in CONTINUE_BY:
            raise ReviewRefused("invalid_argument", "inconclusive needs continue_by: 'architect' (you can keep reading "
                                "with your Tools) or 'owner' (Owner authorization or external evidence is needed)")
    elif continue_by is not None:
        raise ReviewRefused("invalid_argument", "continue_by applies only to an inconclusive verdict")
    reported_sha = args.get("report_sha256")
    if not isinstance(reported_sha, str) or not _SHA.match(reported_sha):
        raise ReviewRefused("invalid_argument", "report_sha256 must be the 64-hex digest of the Report you reviewed "
                            "(from rosc_report_read)")
    expected = args.get("expected_version")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected < 0):
        raise ReviewRefused("invalid_argument", "expected_version must be the latest review version you saw (0 = none)")

    run = session.get(ManagedRunState, run_id)
    if run is None or run.project_id != project_id:
        raise ReviewRefused("not_found", "No such managed Run in the current Project")
    if task_id is not None and run.task_id != task_id:
        raise ReviewRefused("task_mismatch", "This Run does not belong to that Task")
    if run.state != RUN_REPORTED:
        raise ReviewRefused("run_not_reported", "Only a reported managed Run can be reviewed", run_state=run.state)
    report = session.get(ManagedRunReport, run_id)
    if report is None:
        raise ReviewRefused("report_not_registered", "This Run has no registered Worker Report")
    if report.sha256 != reported_sha:
        raise ReviewRefused("report_digest_mismatch", "report_sha256 is not the registered Report digest of this Run; "
                            "re-read it with rosc_report_read", registered_sha256=report.sha256)
    digest = _submission_digest(run_id, architect_agent_id, verdict, summary, findings, limitations,
                                continue_by, report.sha256)

    try:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        latest = latest_review(session, run_id)
        if latest is not None and latest.submission_sha256 == digest:
            view = review_view(session, latest)
            session.rollback()
            return {"recorded": False, "replayed": True, "review": view,
                    "review_state": review_state(session, run_id, run.state), "note": NOTE}
        if latest is not None:
            if latest.verdict != REVIEW_INCONCLUSIVE:
                session.rollback()
                raise ReviewRefused("review_already_recorded", "A decisive review is already recorded for this Run; it "
                                    "is never silently replaced", current_version=latest.version,
                                    current_verdict=latest.verdict)
            if expected != latest.version:
                session.rollback()
                raise ReviewRefused("review_revision_conflict", "Pass expected_version = the latest review version you "
                                    "read to supersede an inconclusive review", current_version=latest.version)
        elif expected not in (None, 0):
            session.rollback()
            raise ReviewRefused("review_revision_conflict", "No review exists yet for this Run", current_version=0)
        version = 1 if latest is None else latest.version + 1
        now = _now()
        row = ManagedRunReview(
            review_id=str(uuid.uuid4()), run_id=run_id, project_id=run.project_id, task_id=run.task_id,
            version=version, supersedes_review_id=None if latest is None else latest.review_id, verdict=verdict,
            summary=summary, findings=json.dumps(findings, ensure_ascii=False),
            limitations=json.dumps(limitations, ensure_ascii=False), continue_by=continue_by,
            report_path=report.report_path, report_sha256=report.sha256, submission_sha256=digest,
            architect_agent_id=architect_agent_id, architect_assignment_id=architect_assignment_id,
            architect_session_id=architect_session_id, created_at=now)
        session.add(row)
        # audit of the recorded transition in the SAME transaction (ids only, never the review text)
        session.add(ArchitectToolAudit(session_id=architect_session_id, project_id=run.project_id,
                                       agent_id=architect_agent_id, tool="review_record",
                                       target=f"P{run.project_id:04d}:{run_id}:v{version}"[:96],
                                       outcome=f"recorded:{verdict}"[:48], at=now))
        session.commit()
    except ReviewRefused:
        raise
    except (OperationalError, IntegrityError):
        session.rollback()
        raise ReviewRefused("review_conflict", "Another review submission for this Run won the race or the store is "
                            "busy; read the current review state and retry deliberately") from None
    view = review_view(session, row)
    return {"recorded": True, "replayed": False, "review": view,
            "review_state": review_state(session, run_id, run.state), "note": NOTE}


def review_count(session: Session, run_id: str) -> int:
    return session.scalar(select(func.count()).select_from(ManagedRunReview).where(
        ManagedRunReview.run_id == run_id)) or 0
