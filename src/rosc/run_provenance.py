"""Owner-only Run / Report / Evidence identity and provenance service (P013 / M2-D).

Chain implemented: ``Task (survives any number of Runs) -> RunRecord -> ReportRecord / EvidenceRecord`` plus an
Owner-curated, append-only correspondence between an external Run and an existing ``AgentDefinition``.

Trust limits (read before relying on anything here):

* This is a *foundation*. There is no Run execution, launch, supervision, session, process, WriterGate or approval.
  ``execution_origin="managed"`` is representable in the schema but this service refuses to create it: it is reserved
  for a future ROSC-launched Run that needs an approved Execution Package. Recording an ``external`` Run is not
  ``run.approve``, does not change Task status and gives no Agent any ROSC authority.
* Everything stored about an external Run, Report or Evidence is a *claim* by its source (``claimed``). References
  (``source_ref`` / ``reference`` / ``declared_digest``) are never opened, hashed or checked: no source file is read,
  no checkout is touched. Nothing here can mark anything verified or Owner-accepted, and no ``verified`` value exists.
* ``agent_id`` on a Run is an Owner-curated correspondence (``owner_curated_correspondence``), not proof of authorship,
  an assignment activation or an effective permission. The original claim (``reported_executor``) is never rewritten
  and an established link cannot be changed (no unlink/reassign policy exists yet).
* Source identity for replay: ``(project, source_kind, source_key)`` for Runs, ``(run, source_key)`` for Reports and
  Evidence. An identical replay returns the existing record; a differing replay is a conflict and changes nothing.
  Without a ``source_key`` creation is explicit manual and *not* idempotent: a retry creates another record.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .agent_registry import _CONTROL, _SECRET_MARKERS, _SECRET_PREFIXES
from .authorization import (RUN_EVIDENCE_REGISTER, RUN_LINK_AGENT, RUN_LIST, RUN_READ, RUN_REGISTER_EXTERNAL,
                            RUN_REPORT_REGISTER, ActorContext, AuthorizationPolicy, ResourceScope, evaluate,
                            is_canonical_uuid4)
from .models import (ASSIGNMENT_ACTIVE, ATTRIBUTION_CLAIMED, EVIDENCE_GIT_REF, EVIDENCE_KINDS, EVIDENCE_TEST_REF,
                     INGEST_MANUAL_OWNER, LINK_OWNER_CURATED, LINK_UNLINKED, ORIGIN_EXTERNAL, ORIGIN_MANAGED,
                     PROVENANCE_CLAIMED, REPORT_KINDS, REPORTED_OUTCOMES, ROLE_WORKER, AgentDefinition, EvidenceRecord,
                     Project, ProjectIdentityError, ReportRecord, RoleAssignment, RunAgentLink, RunRecord, Task,
                     validate_project_identity)

MAX_SOURCE_KIND = 64
MAX_SOURCE_KEY = 128
MAX_REFERENCE = 200
MAX_EXECUTOR = 96

_SOURCE_KIND = re.compile(r"[a-z][a-z0-9._-]{0,63}", re.ASCII)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:#@+=/-]*", re.ASCII)
_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")
_DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)
_GIT_SHA = re.compile(r"[0-9a-f]{7,64}", re.ASCII)
# Long hex/base64-looking runs inside free text look like pasted credentials, not an executor label.
_OPAQUE_RUN = re.compile(r"[A-Za-z0-9+/_=-]{32,}")


class RunProvenanceError(Exception):
    """Base class for the Run provenance boundary. Messages never echo submitted values."""


class RunProvenanceDenied(RunProvenanceError):
    """Actor, action or scope refused before any lookup or write (HTTP 403 in a future adapter)."""


class InvalidRunInput(RunProvenanceError):
    """Malformed, unsupported or contradictory input (422)."""


class RunNotFound(RunProvenanceError):
    """Project, Task, Run, Report or Agent does not exist in the supplied scope (404)."""


class RunConflict(RunProvenanceError):
    """Conflicting replay, stale revision or refused reassignment (409)."""


class UnsafeRunProjectIdentity(RunProvenanceError):
    """Canonical Project identity records are unsafe; fail closed (503)."""


class RunStoreError(RunProvenanceError):
    """The database could not complete the write (503)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _suspicious(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _SECRET_MARKERS) or lowered.startswith(_SECRET_PREFIXES)


def _label(value: object, field: str, limit: int) -> str:
    """Free-text claim (for example a reported executor name): bounded, no control characters, URL or credential."""
    if not isinstance(value, str):
        raise InvalidRunInput(f"{field} must be text")
    value = value.strip()
    if not value or len(value) > limit:
        raise InvalidRunInput(f"{field} must be 1-{limit} characters")
    if _CONTROL.search(value) or "://" in value or _suspicious(value) or _OPAQUE_RUN.search(value):
        raise InvalidRunInput(f"{field} must be descriptive metadata, not a control sequence, URL or credential")
    return value


def _reference(value: object, field: str, limit: int) -> str:
    """Opaque relative reference/key: never an absolute host path, drive path, traversal, URL or credential."""
    if not isinstance(value, str):
        raise InvalidRunInput(f"{field} must be text")
    if not value or len(value) > limit or value != value.strip():
        raise InvalidRunInput(f"{field} must be 1-{limit} characters without surrounding whitespace")
    if (_CONTROL.search(value) or not _TOKEN.fullmatch(value) or value.startswith("/") or ".." in value
            or "//" in value or "://" in value or _DRIVE_PREFIX.match(value) or _suspicious(value)):
        raise InvalidRunInput(f"{field} must be a bounded relative reference, not a host path, URL or credential")
    return value


def _optional_key(value: object, field: str = "source_key") -> str | None:
    return None if value is None else _reference(value, field, MAX_SOURCE_KEY)


def _digest(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise InvalidRunInput("declared_digest must be 64 lowercase hex characters")
    return value


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidRunInput(f"{field} must be a positive integer")
    return value


def _uuid(value: object, field: str) -> str:
    if not is_canonical_uuid4(value):
        raise InvalidRunInput(f"{field} must be a canonical UUIDv4 string")
    return value  # type: ignore[return-value]


def _project_label(project_id: int) -> str:
    return f"P{project_id:04d}"


def _task_label(task_id: int) -> str:
    return f"T{task_id:04d}"


def link_view(link: RunAgentLink) -> dict:
    return {"link_id": link.link_id, "from_agent_id": link.from_agent_id, "agent_id": link.agent_id,
            "assignment_id": link.assignment_id, "original_claim": link.original_claim,
            "expected_revision": link.expected_revision, "resulting_revision": link.resulting_revision,
            "linked_by": link.linked_by, "linked_by_assurance": link.linked_by_assurance,
            "linked_at": _utc_iso(link.linked_at)}


def report_view(report: ReportRecord) -> dict:
    return {"report_id": report.report_id, "run_id": report.run_id, "source_key": report.source_key,
            "report_kind": report.report_kind, "report_revision": report.report_revision,
            "source_ref": report.source_ref, "declared_digest": report.declared_digest,
            "provenance": report.provenance, "ingest_origin": report.ingest_origin,
            "registered_by": report.registered_by, "registered_by_assurance": report.registered_by_assurance,
            "created_at": _utc_iso(report.created_at)}


def evidence_view(item: EvidenceRecord) -> dict:
    return {"evidence_id": item.evidence_id, "run_id": item.run_id, "report_id": item.report_id,
            "source_key": item.source_key, "evidence_kind": item.evidence_kind, "reference": item.reference,
            "declared_digest": item.declared_digest, "reported_outcome": item.reported_outcome,
            "provenance": item.provenance, "ingest_origin": item.ingest_origin,
            "registered_by": item.registered_by, "registered_by_assurance": item.registered_by_assurance,
            "created_at": _utc_iso(item.created_at)}


def run_view(run: RunRecord) -> dict:
    """Read model. Deliberately has no verified/accepted/success/status field: a Run record is a registered claim."""
    return {"run_id": run.run_id, "project_id": _project_label(run.project_id), "task_id": _task_label(run.task_id),
            "execution_origin": run.execution_origin, "source_kind": run.source_kind, "source_key": run.source_key,
            "source_attribution_assurance": run.source_attribution_assurance,
            "reported_executor": run.reported_executor, "agent_id": run.agent_id, "assignment_id": run.assignment_id,
            "agent_link_status": run.agent_link_status, "attribution_revision": run.attribution_revision,
            "registered_by": run.registered_by, "registered_by_assurance": run.registered_by_assurance,
            "created_at": _utc_iso(run.created_at)}


class RunProvenanceService:
    """Canonical Core boundary for external Run, Report and Evidence provenance records.

    Every method authorizes through the injected ``AuthorizationPolicy`` before it reads or writes anything. The
    registering actor is whatever trusted ``ActorContext`` the caller holds; no input can name one.
    """

    def __init__(self, session_factory: sessionmaker[Session], policy: AuthorizationPolicy) -> None:
        self._sessions = session_factory
        self._policy = policy

    def _authorize(self, actor: ActorContext, action: str, **scope) -> None:
        if not evaluate(self._policy, actor, action, ResourceScope(**scope)).allowed:
            raise RunProvenanceDenied("Not authorized for this operation")

    @staticmethod
    def _project(session: Session, project_id: int) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            session.rollback()
            raise RunNotFound("Project not found")
        try:
            validate_project_identity(project)
        except ProjectIdentityError:
            session.rollback()
            raise UnsafeRunProjectIdentity("Project identity records are inconsistent") from None
        return project

    @staticmethod
    def _run(session: Session, project_id: int, run_id: str) -> RunRecord:
        run = session.get(RunRecord, run_id)
        if run is None or run.project_id != project_id:
            session.rollback()
            raise RunNotFound("Run not found")
        return run

    def _write(self, session: Session, body, conflict: str) -> None:
        """Run ``body`` inside BEGIN IMMEDIATE; map store failures to typed, sanitized errors with no partial rows."""
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            body()
            session.commit()
        except OperationalError:
            session.rollback()
            raise RunStoreError("Another change is in progress; retry shortly") from None
        except IntegrityError:
            session.rollback()
            raise RunConflict(conflict) from None
        except RunProvenanceError:
            session.rollback()
            raise

    # -- Runs ------------------------------------------------------------------------------------------------
    def register_external_run(self, actor: ActorContext, project_id: object, task_id: object, *,
                              source_kind: object, source_key: object = None, reported_executor: object = None,
                              execution_origin: object = ORIGIN_EXTERNAL) -> dict:
        """Register an observed/reported external Run of an existing Task. Returns the view plus ``created``."""
        self._authorize(actor, RUN_REGISTER_EXTERNAL, project_id=project_id, task_id=task_id)
        if execution_origin == ORIGIN_MANAGED:
            raise RunProvenanceDenied("A managed Run cannot be created before an approved Execution Package exists")
        if execution_origin != ORIGIN_EXTERNAL:
            raise InvalidRunInput("Unsupported execution_origin")
        if not isinstance(source_kind, str) or not _SOURCE_KIND.fullmatch(source_kind):
            raise InvalidRunInput("source_kind must be a short lowercase identifier")
        key = _optional_key(source_key)
        executor = None if reported_executor is None else _label(reported_executor, "reported_executor", MAX_EXECUTOR)
        holder: dict = {}

        def body() -> None:
            self._project(session, project_id)  # type: ignore[arg-type]
            task = session.get(Task, task_id)
            if task is None or task.project_id != project_id:
                raise RunNotFound("Task not found in this Project")
            if key is not None:
                existing = session.scalars(select(RunRecord).where(
                    RunRecord.project_id == project_id, RunRecord.source_kind == source_kind,
                    RunRecord.source_key == key)).first()
                if existing is not None:
                    if (existing.task_id, existing.reported_executor, existing.execution_origin) != (
                            task_id, executor, ORIGIN_EXTERNAL):
                        raise RunConflict("A Run with this source identity is already registered differently")
                    holder["run"], holder["created"] = existing, False
                    return
            run = RunRecord(run_id=str(uuid.uuid4()), project_id=project_id, task_id=task_id,
                            execution_origin=ORIGIN_EXTERNAL, source_kind=source_kind, source_key=key,
                            source_attribution_assurance=ATTRIBUTION_CLAIMED, reported_executor=executor,
                            agent_id=None, assignment_id=None, agent_link_status=LINK_UNLINKED,
                            attribution_revision=1, registered_by=actor.actor_id,
                            registered_by_assurance=actor.assurance, created_at=_now())
            session.add(run)
            session.flush()
            holder["run"], holder["created"] = run, True

        with self._sessions() as session:
            self._write(session, body, "Run could not be registered")
            return {**run_view(holder["run"]), "created": holder["created"]}

    def list_runs(self, actor: ActorContext, project_id: object, task_id: object = None) -> list[dict]:
        scope = {"project_id": project_id} if task_id is None else {"project_id": project_id, "task_id": task_id}
        self._authorize(actor, RUN_LIST, **scope)
        with self._sessions() as session:
            self._project(session, project_id)  # type: ignore[arg-type]
            statement = select(RunRecord).where(RunRecord.project_id == project_id)
            if task_id is not None:
                statement = statement.where(RunRecord.task_id == task_id)
            rows = session.scalars(statement.order_by(RunRecord.created_at, RunRecord.run_id)).all()
            return [run_view(row) for row in rows]

    def get_run(self, actor: ActorContext, project_id: object, run_id: object) -> dict:
        self._authorize(actor, RUN_READ, project_id=project_id, run_id=run_id)
        with self._sessions() as session:
            self._project(session, project_id)  # type: ignore[arg-type]
            run = self._run(session, project_id, run_id)  # type: ignore[arg-type]
            reports = session.scalars(select(ReportRecord).where(ReportRecord.run_id == run_id).order_by(
                ReportRecord.created_at, ReportRecord.report_id)).all()
            evidence = session.scalars(select(EvidenceRecord).where(EvidenceRecord.run_id == run_id).order_by(
                EvidenceRecord.created_at, EvidenceRecord.evidence_id)).all()
            links = session.scalars(select(RunAgentLink).where(RunAgentLink.run_id == run_id).order_by(
                RunAgentLink.resulting_revision)).all()
            return {**run_view(run), "reports": [report_view(r) for r in reports],
                    "evidence": [evidence_view(e) for e in evidence], "agent_link_history": [link_view(x) for x in links]}

    # -- Reports and evidence --------------------------------------------------------------------------------
    def register_report_reference(self, actor: ActorContext, project_id: object, run_id: object, *,
                                  report_kind: object, source_ref: object, source_key: object = None,
                                  report_revision: object = None, declared_digest: object = None) -> dict:
        self._authorize(actor, RUN_REPORT_REGISTER, project_id=project_id, run_id=run_id)
        if not isinstance(report_kind, str) or report_kind not in REPORT_KINDS:
            raise InvalidRunInput("Unsupported report_kind")
        ref = _reference(source_ref, "source_ref", MAX_REFERENCE)
        key = _optional_key(source_key)
        revision = None if report_revision is None else _positive_int(report_revision, "report_revision")
        digest = _digest(declared_digest)
        wanted = (report_kind, revision, ref, digest)
        holder: dict = {}

        def body() -> None:
            self._project(session, project_id)  # type: ignore[arg-type]
            self._run(session, project_id, run_id)  # type: ignore[arg-type]
            if key is not None:
                existing = session.scalars(select(ReportRecord).where(
                    ReportRecord.run_id == run_id, ReportRecord.source_key == key)).first()
                if existing is not None:
                    if (existing.report_kind, existing.report_revision, existing.source_ref,
                            existing.declared_digest) != wanted:
                        raise RunConflict("A Report with this source identity is already registered differently")
                    holder["row"], holder["created"] = existing, False
                    return
            row = ReportRecord(report_id=str(uuid.uuid4()), run_id=run_id, source_key=key, report_kind=report_kind,
                               report_revision=revision, source_ref=ref, declared_digest=digest,
                               provenance=PROVENANCE_CLAIMED, ingest_origin=INGEST_MANUAL_OWNER,
                               registered_by=actor.actor_id, registered_by_assurance=actor.assurance,
                               created_at=_now())
            session.add(row)
            session.flush()
            holder["row"], holder["created"] = row, True

        with self._sessions() as session:
            self._write(session, body, "Report could not be registered")
            return {**report_view(holder["row"]), "created": holder["created"]}

    def register_evidence_reference(self, actor: ActorContext, project_id: object, run_id: object, *,
                                    evidence_kind: object, reference: object, source_key: object = None,
                                    report_id: object = None, declared_digest: object = None,
                                    reported_outcome: object = None) -> dict:
        self._authorize(actor, RUN_EVIDENCE_REGISTER, project_id=project_id, run_id=run_id)
        if not isinstance(evidence_kind, str) or evidence_kind not in EVIDENCE_KINDS:
            raise InvalidRunInput("Unsupported evidence_kind")
        ref = _reference(reference, "reference", MAX_REFERENCE)
        if evidence_kind == EVIDENCE_GIT_REF and not _GIT_SHA.fullmatch(ref):
            raise InvalidRunInput("A Git commit reference must be a lowercase hexadecimal object id")
        key = _optional_key(source_key)
        report_key = None if report_id is None else _uuid(report_id, "report_id")
        digest = _digest(declared_digest)
        if reported_outcome is not None:
            if evidence_kind != EVIDENCE_TEST_REF or not isinstance(reported_outcome, str) \
                    or reported_outcome not in REPORTED_OUTCOMES:
                raise InvalidRunInput("reported_outcome is only valid for test result references")
        wanted = (evidence_kind, ref, report_key, digest, reported_outcome)
        holder: dict = {}

        def body() -> None:
            self._project(session, project_id)  # type: ignore[arg-type]
            self._run(session, project_id, run_id)  # type: ignore[arg-type]
            if report_key is not None:
                report = session.get(ReportRecord, report_key)
                if report is None or report.run_id != run_id:
                    raise InvalidRunInput("Report does not belong to this Run")
            if key is not None:
                existing = session.scalars(select(EvidenceRecord).where(
                    EvidenceRecord.run_id == run_id, EvidenceRecord.source_key == key)).first()
                if existing is not None:
                    if (existing.evidence_kind, existing.reference, existing.report_id, existing.declared_digest,
                            existing.reported_outcome) != wanted:
                        raise RunConflict("Evidence with this source identity is already registered differently")
                    holder["row"], holder["created"] = existing, False
                    return
            row = EvidenceRecord(evidence_id=str(uuid.uuid4()), run_id=run_id, report_id=report_key, source_key=key,
                                 evidence_kind=evidence_kind, reference=ref, declared_digest=digest,
                                 reported_outcome=reported_outcome, provenance=PROVENANCE_CLAIMED,
                                 ingest_origin=INGEST_MANUAL_OWNER, registered_by=actor.actor_id,
                                 registered_by_assurance=actor.assurance, created_at=_now())
            session.add(row)
            session.flush()
            holder["row"], holder["created"] = row, True

        with self._sessions() as session:
            self._write(session, body, "Evidence could not be registered")
            return {**evidence_view(holder["row"]), "created": holder["created"]}

    # -- Owner-curated attribution link ---------------------------------------------------------------------
    def link_external_agent(self, actor: ActorContext, project_id: object, run_id: object, *, agent_id: object,
                            expected_revision: object, assignment_id: object = None) -> dict:
        """Record that the Owner curates this external Run as corresponding to an existing Agent.

        Not verification of authorship, not an assignment activation, not a permission. The original claim is kept;
        an established link is never changed (a different target or assignment is a conflict).
        """
        self._authorize(actor, RUN_LINK_AGENT, project_id=project_id, run_id=run_id, agent_id=agent_id)
        revision = _positive_int(expected_revision, "expected_revision")
        assignment_key = None if assignment_id is None else _uuid(assignment_id, "assignment_id")
        holder: dict = {}

        def body() -> None:
            self._project(session, project_id)  # type: ignore[arg-type]
            run = self._run(session, project_id, run_id)  # type: ignore[arg-type]
            if run.execution_origin != ORIGIN_EXTERNAL:
                raise InvalidRunInput("Only an external Run can be linked")
            if session.get(AgentDefinition, agent_id) is None:
                raise RunNotFound("Agent not found")
            if assignment_key is not None:
                assignment = session.get(RoleAssignment, assignment_key)
                if (assignment is None or assignment.agent_id != agent_id or assignment.project_id != project_id
                        or (assignment.task_id is not None and assignment.task_id != run.task_id)
                        or assignment.role != ROLE_WORKER or assignment.state != ASSIGNMENT_ACTIVE):
                    raise InvalidRunInput("Assignment is not an active Worker assignment of this Agent for this Run")
            if run.agent_id is not None:
                if run.agent_id == agent_id and run.assignment_id == assignment_key:
                    holder["changed"] = False
                    holder["run"] = run
                    return
                raise RunConflict("Run is already linked; changing an established link is not supported")
            if revision != run.attribution_revision:
                raise RunConflict("Run attribution was changed by another operation; reload and retry")
            run.agent_id = agent_id
            run.assignment_id = assignment_key
            run.agent_link_status = LINK_OWNER_CURATED
            run.attribution_revision = revision + 1
            session.add(RunAgentLink(link_id=str(uuid.uuid4()), run_id=run.run_id, from_agent_id=None,
                                     agent_id=agent_id, assignment_id=assignment_key,
                                     original_claim=run.reported_executor, expected_revision=revision,
                                     resulting_revision=revision + 1, linked_by=actor.actor_id,
                                     linked_by_assurance=actor.assurance, linked_at=_now()))
            session.flush()
            holder["changed"], holder["run"] = True, run

        with self._sessions() as session:
            self._write(session, body, "Run could not be linked")
            return {**run_view(holder["run"]), "changed": holder["changed"]}
