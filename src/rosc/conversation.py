"""Durable Owner-only Project Conversation service (P014 / M3-A).

Governing chain implemented here: ``Project (canonical identity) -> Conversation -> Participant -> Message``. This is
a ROSC-owned store, not a runtime channel: recording a message starts no process, opens no provider session, creates
no Run and implies no delivery, reply, liveness or approval.

Trust limits (read before relying on anything here):

* Only the trusted ``ActorContext`` resolved by the application may author a message. Request data never selects a
  sender, participant, role or session; there is no ``author_id`` input. The Owner participant records the actual
  ``local_unverified`` assurance of the local bootstrap rather than claiming authenticated identity.
* Reading and writing both pass through the injected ``AuthorizationPolicy`` before any lookup. Agent participants
  are a future integration point: no P014 code path creates one, and ``LocalOwnerPolicy`` still denies
  ``actor_kind="agent"`` whatever assignments exist.
* Message bodies are plain text stored only in the ROSC database. They are never written to a Workspace manifest,
  a log line, a telemetry file or a provider session.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .authorization import (CONVERSATION_MESSAGE_CREATE, CONVERSATION_READ, ActorContext, AuthorizationPolicy,
                            ResourceScope, evaluate)
from .models import (CONVERSATION_KIND_PROJECT, MAX_CLIENT_KEY, MAX_MESSAGE_BODY, PARTICIPANT_ARCHITECT,
                     PARTICIPANT_OWNER, PARTICIPANT_WORKER, Conversation, ConversationMessage, ConversationDelivery, ConversationParticipant,
                     CONVERSATION_KIND_TASK, RECIPIENT_ARCHITECT, Project, ProjectIdentityError, Task,
                     validate_project_identity)

# Failure codes after which nothing can have reached the provider, so an explicit retry cannot duplicate a turn.
SAFE_RETRY_CODES = frozenset({"session_not_ready", "connection_failed", "not_received", "assignment_changed"})
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MIN_CLIENT_KEY = 8

# Opaque but bounded: no whitespace, control characters, separators or JSON/HTML metacharacters.
_CLIENT_KEY = re.compile(r"[A-Za-z0-9._:-]{%d,%d}" % (MIN_CLIENT_KEY, MAX_CLIENT_KEY), re.ASCII)
# Text may contain newlines and tabs; other C0 controls and DEL are refused instead of being persisted.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_UNSTORABLE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\ud800-\udfff]")  # Architect replies: replaced, not rejected


class ConversationError(Exception):
    """Base class for the conversation boundary."""


class ConversationDenied(ConversationError):
    """The actor, action or scope was refused before any lookup or write (HTTP 403)."""


class ConversationNotFound(ConversationError):
    """The named Project does not exist (HTTP 404)."""


class InvalidConversationInput(ConversationError):
    """Malformed, unsupported or contradictory input (HTTP 422)."""


class ConversationConflict(ConversationError):
    """An idempotency key was reused with different content, or the write conflicted (HTTP 409)."""


class UnsafeProjectIdentity(ConversationError):
    """Canonical Project identity records are unsafe to use; fail closed (HTTP 503)."""


class ConversationStoreError(ConversationError):
    """The database could not complete the conversation read/write (HTTP 503)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime) -> str:
    """SQLite drops the offset of the values this slice writes, which are UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _project_label(project_id: int) -> str:
    return f"P{project_id:04d}"


def _task_label(task_id: int) -> str:
    return f"T{task_id:04d}"


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidConversationInput(f"{field} must be a positive integer")
    return value


def _client_key(value: object) -> str:
    """Return the caller's opaque idempotency key after strict bounded validation; never echo it back."""
    if not isinstance(value, str) or _CLIENT_KEY.fullmatch(value) is None:
        raise InvalidConversationInput(
            f"client_key must be {MIN_CLIENT_KEY}-{MAX_CLIENT_KEY} characters of A-Z, a-z, 0-9, '.', '_', ':' or '-'")
    return value


def _body(value: object) -> str:
    """Return the bounded plain-text body with outer whitespace removed; never echo the content in an error."""
    if not isinstance(value, str):
        raise InvalidConversationInput("Message body must be text")
    text = value.strip()
    if not text:
        raise InvalidConversationInput("Message body must not be blank")
    if len(text) > MAX_MESSAGE_BODY:
        raise InvalidConversationInput(f"Message body must be at most {MAX_MESSAGE_BODY} characters")
    if _CONTROL.search(text):
        raise InvalidConversationInput("Message body must not contain control characters")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidConversationInput("Message body must be valid UTF-8 text") from None
    return text


def _architect_body(value: object) -> str:
    """Normalize a COMPLETED Architect reply for storage.  There is deliberately NO length limit: the reply was already
    generated (and paid for) and is stored exactly as produced; only the real resources of the host, SQLite and the
    provider bound it.  Owner input keeps its own 4000-character rule in ``_body``.

    Only representation is normalized: CRLF/CR become LF, and characters that cannot be stored or shown as plain text
    (C0 controls other than tab/newline, DEL, NUL, unpaired surrogates) become U+FFFD instead of discarding the whole
    reply.  Never echoes the content in an error."""
    if not isinstance(value, str):
        raise InvalidConversationInput("Message body must be text")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise InvalidConversationInput("Message body must not be blank")
    return _UNSTORABLE.sub("\ufffd", text)


def _recipient(value: object) -> str | None:
    """Explicit addressee: only ``None`` or ``'architect'``. It is a stored intent, never a delivery."""
    if value is None or value == RECIPIENT_ARCHITECT:
        return value  # type: ignore[return-value]
    raise InvalidConversationInput("recipient must be null or 'architect'")


def participant_view(participant: ConversationParticipant) -> dict:
    """Public participant identity. A participant is attribution history, not liveness or authority."""
    display_name = {PARTICIPANT_OWNER: "Owner", PARTICIPANT_ARCHITECT: "Architect",
                    PARTICIPANT_WORKER: "Worker"}.get(participant.kind, participant.kind)
    return {
        "participant_id": participant.participant_id,
        "kind": participant.kind,
        "display_name": display_name,
        "actor_id": participant.actor_id,
        "assurance": participant.actor_assurance,
        "agent_id": participant.agent_id,
    }


def conversation_view(conversation: Conversation) -> dict:
    return {
        "conversation_id": conversation.conversation_id,
        "kind": conversation.kind,
        "project_id": _project_label(conversation.project_id),
        "task_id": None if conversation.task_id is None else _task_label(conversation.task_id),
        "created_at": _utc_iso(conversation.created_at),
        "participants": [participant_view(participant) for participant in conversation.participants],
    }


LEGACY_REPLY_TOO_LARGE = "reply_too_large"  # P015-P017.2 stored a generated, unlimited-length reply as ``failed``


def recoverable_failure(row: ConversationDelivery) -> bool:
    """A historical delivery that failed only because the (already generated) reply exceeded the old 4000 cap."""
    return row.state == "failed" and row.error_code == LEGACY_REPLY_TOO_LARGE and row.reply_message_id is None


def delivery_state_view(row: ConversationDelivery | None) -> dict:
    """Delivery of one Owner message. No row means only stored: nothing was ever sent (see P015 / M3-B)."""
    if row is None:
        return {"state": "not_sent", "error_code": None, "reply_message_id": None, "can_retry": False,
                "can_reconcile": False}
    return {"state": row.state, "error_code": row.error_code, "reply_message_id": row.reply_message_id,
            "can_retry": row.state == "failed" and row.error_code in SAFE_RETRY_CODES,
            "can_reconcile": row.state in ("unknown", "pending") or recoverable_failure(row)}


def message_view(message: ConversationMessage, sender: ConversationParticipant,
                 delivery: ConversationDelivery | None = None) -> dict:
    view = {
        "message_id": message.message_id,
        "conversation_id": message.conversation_id,
        "ordinal": message.ordinal,
        "body": message.body,
        "created_at": _utc_iso(message.created_at),
        "sender": participant_view(sender),
        "recipient": message.recipient,
    }
    if sender.kind == PARTICIPANT_OWNER:
        view["delivery"] = delivery_state_view(delivery)
    return view


def _empty_page(project_id: int, limit: int, task_id: int | None = None) -> dict:
    return {
        "project_id": _project_label(project_id),
        "task_id": None if task_id is None else _task_label(task_id),
        "conversation": None,
        "messages": [],
        "page": {"limit": limit, "has_more": False, "next_before": None},
    }


class ProjectConversationService:
    """Canonical business boundary for the durable Project Conversation.

    Every method authorizes through the injected ``AuthorizationPolicy`` before it reads or writes, so a direct Core
    call has the same guard as the HTTP handler and no caller can select its own actor.
    """

    def __init__(self, session_factory: sessionmaker[Session], policy: AuthorizationPolicy) -> None:
        self._sessions = session_factory
        self._policy = policy

    def _authorize(self, actor: ActorContext, action: str, **scope) -> None:
        if not evaluate(self._policy, actor, action, ResourceScope(**scope)).allowed:
            raise ConversationDenied("Not authorized for this operation")

    @staticmethod
    def _project(session: Session, project_id: int) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise ConversationNotFound("Project not found")
        try:
            validate_project_identity(project)
        except ProjectIdentityError:
            raise UnsafeProjectIdentity("Project identity records are inconsistent") from None
        return project

    @staticmethod
    def _project_conversation(session: Session, project_id: int) -> Conversation | None:
        return session.scalars(select(Conversation).where(
            Conversation.project_id == project_id, Conversation.kind == CONVERSATION_KIND_PROJECT)).first()

    @staticmethod
    def _task(session: Session, project_id: int, task_id: object) -> Task:
        """The Task must exist and belong to the named Project; otherwise the same 404 as an unknown Task."""
        task = session.get(Task, task_id) if isinstance(task_id, int) and not isinstance(task_id, bool) else None
        if task is None or task.project_id != project_id:
            raise ConversationNotFound("Task not found")
        return task

    @staticmethod
    def _task_conversation(session: Session, task_id: int) -> Conversation | None:
        return session.scalars(select(Conversation).where(
            Conversation.task_id == task_id, Conversation.kind == CONVERSATION_KIND_TASK)).first()

    def _scoped_conversation(self, session: Session, project: Project, task_id: object) -> tuple[Conversation | None, int | None]:
        if task_id is None:
            return self._project_conversation(session, project.id), None
        task = self._task(session, project.id, task_id)
        return self._task_conversation(session, task.id), task.id

    @staticmethod
    def _owner_participant(session: Session, conversation_id: str) -> ConversationParticipant:
        owner = session.scalars(select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.kind == PARTICIPANT_OWNER)).first()
        if owner is None:
            raise ConversationStoreError("Conversation records are inconsistent")
        return owner

    def read_conversation(self, actor: ActorContext, project_id: object, *, before: object = None,
                          limit: object = DEFAULT_PAGE_SIZE, task_id: object = None) -> dict:
        """Read the Project (or, with ``task_id``, that Task's) conversation and one bounded page of messages;
        never creates or mutates anything."""
        self._authorize(actor, CONVERSATION_READ, project_id=project_id, task_id=task_id)
        page_limit = _positive_int(limit, "limit")
        if page_limit > MAX_PAGE_SIZE:
            raise InvalidConversationInput(f"limit must be at most {MAX_PAGE_SIZE}")
        cursor = None if before is None else _positive_int(before, "before")
        with self._sessions() as session:
            project = self._project(session, project_id)  # type: ignore[arg-type]
            conversation, scoped_task = self._scoped_conversation(session, project, task_id)
            if conversation is None:
                return _empty_page(project.id, page_limit, scoped_task)
            return self._page(session, conversation, cursor, page_limit)

    def _page(self, session: Session, conversation: Conversation, before: int | None, limit: int) -> dict:
        statement = select(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation.conversation_id)
        if before is not None:
            statement = statement.where(ConversationMessage.ordinal < before)
        rows = session.scalars(statement.order_by(ConversationMessage.ordinal.desc()).limit(limit + 1)).all()
        has_more = len(rows) > limit
        window = list(reversed(rows[:limit]))
        senders = {participant.participant_id: participant for participant in conversation.participants}
        deliveries = {d.message_id: d for d in session.scalars(select(ConversationDelivery).where(
            ConversationDelivery.message_id.in_([row.message_id for row in window])))}
        messages = []
        for row in window:
            sender = senders.get(row.sender_participant_id)
            if sender is None:
                raise ConversationStoreError("Conversation records are inconsistent")
            messages.append(message_view(row, sender, deliveries.get(row.message_id)))
        return {
            "project_id": _project_label(conversation.project_id),
            "task_id": None if conversation.task_id is None else _task_label(conversation.task_id),
            "conversation": conversation_view(conversation),
            "messages": messages,
            "page": {"limit": limit, "has_more": has_more,
                     "next_before": window[0].ordinal if has_more and window else None},
        }

    def create_message(self, actor: ActorContext, project_id: object, *, client_key: object,
                       body: object, task_id: object = None, recipient: object = None) -> dict:
        """Persist one Owner message atomically; replay an identical retry, refuse an altered one.

        Return value: ``{"message": <view>, "replayed": bool}``. ``replayed`` is true only when the same key and the
        same body already produced exactly this message, in which case nothing is written.
        """
        self._authorize(actor, CONVERSATION_MESSAGE_CREATE, project_id=project_id, task_id=task_id)
        key = _client_key(client_key)
        text = _body(body)
        addressee = _recipient(recipient)
        if task_id is None and addressee is not None:
            raise InvalidConversationInput("Project Conversation messages do not take a recipient")
        created = False
        with self._sessions() as session:
            try:
                # Single-writer critical section: created conversation, ordinal assignment and key lookup interleave
                # with no other writer, so one Project can never get two conversations or two equal ordinals.
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                project = self._project(session, project_id)  # type: ignore[arg-type]
                conversation, scoped_task = self._scoped_conversation(session, project, task_id)
                if conversation is None:
                    conversation = Conversation(
                        conversation_id=str(uuid.uuid4()), project_id=project.id, task_id=scoped_task,
                        kind=CONVERSATION_KIND_PROJECT if scoped_task is None else CONVERSATION_KIND_TASK,
                        created_at=_now())
                    session.add(conversation)
                    session.flush()
                    owner = ConversationParticipant(
                        participant_id=str(uuid.uuid4()), conversation_id=conversation.conversation_id,
                        kind=PARTICIPANT_OWNER, actor_id=actor.actor_id, actor_assurance=actor.assurance,
                        agent_id=None, created_at=conversation.created_at)
                    session.add(owner)
                    session.flush()
                else:
                    owner = self._owner_participant(session, conversation.conversation_id)
                existing = session.scalars(select(ConversationMessage).where(
                    ConversationMessage.conversation_id == conversation.conversation_id,
                    ConversationMessage.client_key == key)).first()
                if existing is not None:
                    if (existing.body != text or existing.sender_participant_id != owner.participant_id
                            or existing.recipient != addressee):
                        session.rollback()
                        raise ConversationConflict(
                            "This message key was already used for different content; reload the conversation")
                    replay = message_view(existing, owner, session.get(ConversationDelivery, existing.message_id))
                    session.rollback()  # a replay writes nothing, not even the conversation it may have ensured
                    return {"message": replay, "replayed": True}
                ordinal = (session.scalar(select(func.max(ConversationMessage.ordinal)).where(
                    ConversationMessage.conversation_id == conversation.conversation_id)) or 0) + 1
                message = ConversationMessage(
                    message_id=str(uuid.uuid4()), conversation_id=conversation.conversation_id,
                    sender_participant_id=owner.participant_id, ordinal=ordinal, body=text, client_key=key,
                    recipient=addressee, created_at=_now())
                session.add(message)
                session.commit()
                created = True
                view = message_view(message, owner)
            except OperationalError:
                session.rollback()
                raise ConversationStoreError("Another conversation change is in progress; retry shortly") from None
            except IntegrityError:
                session.rollback()
                raise ConversationStoreError("The message could not be stored") from None
        return {"message": view, "replayed": not created}
