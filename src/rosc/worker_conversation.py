"""P043B (Prompt 044) Worker Conversation Core: durable sequential group turn dispatch and independent native lanes.

Internal backend service only.  There is NO HTTP endpoint, NO background dispatcher and NO real provider in this slice:
every provider interaction goes through the small ``ConversationTransport`` contract, implemented here only by tests
(a deterministic fake).  Real Claude CLI native delivery is P043C, chat UI/streaming is P043D.

Invariants (each is backed by a DB constraint or a single ``BEGIN IMMEDIATE`` transaction, never a Python lock):

* One canonical message store (``ConversationMessage``); group metadata is additive (envelope + parties).
* Each ordinary room message is visible to every current member; Passive visibility never invokes a model.
* Native lane = (Project, stable Agent, Conversation) pinned to a Root Binding version; a binding change invalidates it.
* At most one holding (reserved/submitted/accepted/reply_captured/unknown) turn per room and per lane.
* The inbound cursor advances ONLY to the frozen input snapshot of a provider-ACCEPTED submit; never to the Agent's own
  reply ordinal, never on UNKNOWN, never on Owner Skip (which is an audited gap instead).
* A round grant is debited ONLY in the transaction that commits the Agent's published canonical reply.
* Owner messages are persisted immediately and never reset quotas, passes or the queue; only explicit Renew does.
* Pause / Emergency Brake forbid new claims and new submits; they never pretend an in-flight turn was cancelled.
"""
from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .authorization import (CONVERSATION_GROUP_CONTROL, CONVERSATION_MESSAGE_CREATE, CONVERSATION_READ,
                            ROSC_SETTINGS_READ, ROSC_SETTINGS_UPDATE, ActorContext, AuthorizationPolicy, ResourceScope,
                            evaluate)
from .conversation import (ConversationConflict, ConversationDenied, ConversationNotFound, ConversationStoreError,
                           InvalidConversationInput, UnsafeProjectIdentity, _architect_body, _body, _client_key)
from .models import (ASSIGNMENT_ACTIVE, CONVERSATION_KIND_PROJECT, CONVERSATION_KIND_TASK, GAP_OWNER_SKIP,
                     GAP_RELEASED_PERIOD, LANE_ACTIVE, LANE_INVALIDATED, MEMBER_ACTIVE, MEMBER_PASSIVE,
                     MEMBER_RELEASED, PARTICIPANT_OWNER, PARTY_OWNER_REF, ROLE_ARCHITECT, ROLE_WORKER, ROUND_AWAITING_OWNER,
                     ROUND_CLOSED, ROUND_OPEN, TURN_ACCEPTED, TURN_CANCELLED, TURN_DEFERRED, TURN_FAILED,
                     TURN_HOLDING_STATES, TURN_PUBLISHED, TURN_QUEUED, TURN_REPLY_CAPTURED, TURN_RESERVED,
                     TURN_SUBMITTED, TURN_UNKNOWN, AgentDefinition, AgentNativeLane, Conversation,
                     ConversationDispatchControl, ConversationMember, ConversationMemberGap, ConversationMessage,
                     ConversationParticipant, GroupRound, GroupRoundGrant, GroupTurn, InboundCursor, MessageEnvelope,
                     MessageParty, Project, ProjectIdentityError, RoleAssignment, RoscSetting, Task,
                     validate_project_identity)

# -- canonical ROSC Settings schema (the ONLY place these defaults exist) ----------------------------------------------
SETTING_PASSIVE_MISSED_THRESHOLD = "passive_missed_message_threshold"
SETTING_CATCHUP_CHARS = "catchup_confirmation_chars"
SETTING_GROUP_QUOTA = "group_reply_quota_default"


@dataclass(frozen=True)
class SettingSpec:
    default: int
    minimum: int
    maximum: int


SETTINGS_SCHEMA: dict[str, SettingSpec] = {
    SETTING_PASSIVE_MISSED_THRESHOLD: SettingSpec(10, 1, 1000),
    SETTING_CATCHUP_CHARS: SettingSpec(2000, 1, 1_000_000),
    SETTING_GROUP_QUOTA: SettingSpec(3, 1, 20),
}

MAX_TARGETS = 16
CATCHUP_SEND_FULL, CATCHUP_SKIP_BACKLOG = "send_full", "skip_backlog"
CLIENT_KEY_REPLY_PREFIX = "group-reply:"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# -- transport contract (provider-neutral; implementations are P043C) -------------------------------------------------
SUBMIT_ACCEPTED, SUBMIT_NOT_ACCEPTED, SUBMIT_UNKNOWN = "accepted", "not_accepted", "unknown"
LOOKUP_NOT_RECEIVED, LOOKUP_ACCEPTED, LOOKUP_REPLY, LOOKUP_UNKNOWN = "not_received", "accepted", "reply", "unknown"


@dataclass(frozen=True)
class LaneRef:
    lane_id: str
    project_id: int
    agent_id: str
    conversation_id: str
    adapter: str
    native_session_id: str
    root_binding_version: int


@dataclass(frozen=True)
class InputEvent:
    ordinal: int
    message_id: str
    sender_kind: str
    sender_agent_id: str | None
    to: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class TurnPayload:
    """Minimal trusted envelope + ONLY the not-yet-applied eligible room events, frozen at submit time."""

    turn_id: str
    correlation_id: str
    conversation_id: str
    agent_id: str
    round_id: str
    input_after_ordinal: int
    input_through_ordinal: int
    remaining_replies: int
    events: tuple[InputEvent, ...]
    omitted_ranges: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class SubmitOutcome:
    status: str  # accepted | not_accepted (provably nothing reached the model) | unknown
    code: str | None = None


@dataclass(frozen=True)
class LookupOutcome:
    status: str  # not_received | accepted | reply | unknown
    text: str | None = None
    to: tuple[str, ...] | None = None
    reply_to: str | None = None


class ConversationTransport(Protocol):
    assurance: str  # recorded on the Agent participant; e.g. "fake_transport" in tests

    def open_lane(self, lane: LaneRef) -> None: ...

    def busy(self, lane: LaneRef) -> bool: ...

    def submit(self, lane: LaneRef, payload: TurnPayload) -> SubmitOutcome: ...

    def lookup(self, lane: LaneRef, correlation_id: str) -> LookupOutcome: ...


# -- results ------------------------------------------------------------------------------------------------------------
@dataclass
class ClaimResult:
    status: str  # reserved | busy | paused | idle | awaiting_owner | catchup_choice_required
    turn_id: str | None = None
    agent_id: str | None = None
    detail: dict = field(default_factory=dict)


class WorkerConversationCore:
    """Bounded internal Core for group rooms (PC and every TC).  Owner actions take the trusted ``ActorContext`` and are
    authorized before any lookup; engine steps (claim/dispatch/capture/publish/recover/reconcile) take no actor and can
    only act on durable state that an Owner action already authorized."""

    def __init__(self, session_factory: sessionmaker[Session], policy: AuthorizationPolicy, *,
                 rng: random.Random | None = None) -> None:
        self._sessions = session_factory
        self._policy = policy
        self._rng = rng or random.Random()
        # Test seam for fault injection inside the publish transaction (simulated crash before commit).
        self.before_publish_commit: Callable[[], None] | None = None

    # ------------------------------------------------------------------ helpers
    def _authorize(self, actor: ActorContext, action: str, **scope) -> None:
        if not evaluate(self._policy, actor, action, ResourceScope(**scope)).allowed:
            raise ConversationDenied("Not authorized for this operation")

    @staticmethod
    def _begin(session: Session) -> None:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")

    def _tx(self, fn):
        """Run ``fn(session)`` inside one single-writer transaction; map store errors to safe exceptions."""
        with self._sessions() as session:
            try:
                self._begin(session)
                result = fn(session)
                session.commit()
                return result
            except (ConversationDenied, ConversationNotFound, InvalidConversationInput, ConversationConflict,
                    UnsafeProjectIdentity, ConversationStoreError):
                session.rollback()
                raise
            except OperationalError:
                session.rollback()
                raise ConversationStoreError("Another conversation change is in progress; retry shortly") from None
            except IntegrityError:
                session.rollback()
                raise ConversationStoreError("The conversation change could not be stored") from None

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

    def _room(self, session: Session, project_id: int, task_id: int | None, *, create: bool = False,
              actor: ActorContext | None = None) -> Conversation | None:
        project = self._project(session, project_id)
        if task_id is not None:
            task = session.get(Task, task_id) if isinstance(task_id, int) and not isinstance(task_id, bool) else None
            if task is None or task.project_id != project.id:
                raise ConversationNotFound("Task not found")
            statement = select(Conversation).where(Conversation.task_id == task_id,
                                                   Conversation.kind == CONVERSATION_KIND_TASK)
        else:
            statement = select(Conversation).where(Conversation.project_id == project.id,
                                                   Conversation.kind == CONVERSATION_KIND_PROJECT)
        room = session.scalars(statement).first()
        if room is None and create:
            room = Conversation(conversation_id=str(uuid.uuid4()), project_id=project.id, task_id=task_id,
                                kind=CONVERSATION_KIND_PROJECT if task_id is None else CONVERSATION_KIND_TASK,
                                created_at=_now())
            session.add(room)
            session.flush()
            session.add(ConversationParticipant(
                participant_id=str(uuid.uuid4()), conversation_id=room.conversation_id, kind=PARTICIPANT_OWNER,
                actor_id=actor.actor_id, actor_assurance=actor.assurance, agent_id=None,  # type: ignore[union-attr]
                created_at=room.created_at))
            session.flush()
        return room

    def _require_room(self, session: Session, project_id: int, task_id: int | None) -> Conversation:
        room = self._room(session, project_id, task_id)
        if room is None:
            raise ConversationNotFound("Conversation not found")
        return room

    @staticmethod
    def _assignment(session: Session, room: Conversation, agent_id: str) -> RoleAssignment | None:
        """The active assignment that makes ``agent_id`` a legitimate member of this room, or None."""
        rows = session.scalars(select(RoleAssignment).where(
            RoleAssignment.agent_id == agent_id, RoleAssignment.project_id == room.project_id,
            RoleAssignment.state == ASSIGNMENT_ACTIVE)).all()
        for row in rows:
            if row.role == ROLE_ARCHITECT:
                return row
            if row.role == ROLE_WORKER and (row.task_id is None or row.task_id == room.task_id):
                return row
        return None

    @staticmethod
    def _max_ordinal(session: Session, conversation_id: str) -> int:
        return session.scalar(select(func.max(ConversationMessage.ordinal)).where(
            ConversationMessage.conversation_id == conversation_id)) or 0

    @staticmethod
    def _owner_participant(session: Session, conversation_id: str) -> ConversationParticipant:
        owner = session.scalars(select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.kind == PARTICIPANT_OWNER)).first()
        if owner is None:
            raise ConversationStoreError("Conversation records are inconsistent")
        return owner

    @staticmethod
    def _member(session: Session, conversation_id: str, agent_id: str) -> ConversationMember | None:
        return session.get(ConversationMember, (conversation_id, agent_id))

    @staticmethod
    def _members(session: Session, conversation_id: str) -> list[ConversationMember]:
        return list(session.scalars(select(ConversationMember).where(
            ConversationMember.conversation_id == conversation_id).order_by(ConversationMember.agent_id)))

    @staticmethod
    def _controls(session: Session, conversation_id: str) -> ConversationDispatchControl | None:
        return session.get(ConversationDispatchControl, conversation_id)

    @staticmethod
    def _current_round(session: Session, conversation_id: str) -> GroupRound | None:
        return session.scalars(select(GroupRound).where(GroupRound.conversation_id == conversation_id,
                                                        GroupRound.state != ROUND_CLOSED)).first()

    @staticmethod
    def _holding_turn(session: Session, conversation_id: str) -> GroupTurn | None:
        return session.scalars(select(GroupTurn).where(GroupTurn.conversation_id == conversation_id,
                                                       GroupTurn.state.in_(TURN_HOLDING_STATES))).first()

    @staticmethod
    def _active_lane(session: Session, conversation_id: str, agent_id: str) -> AgentNativeLane | None:
        return session.scalars(select(AgentNativeLane).where(
            AgentNativeLane.conversation_id == conversation_id, AgentNativeLane.agent_id == agent_id,
            AgentNativeLane.state == LANE_ACTIVE)).first()

    @staticmethod
    def _lane_ref(lane: AgentNativeLane) -> LaneRef:
        return LaneRef(lane.lane_id, lane.project_id, lane.agent_id, lane.conversation_id, lane.adapter,
                       lane.native_session_id, lane.root_binding_version)

    # ------------------------------------------------------------------ settings
    def _setting(self, session: Session, key: str) -> int:
        row = session.get(RoscSetting, key)
        return SETTINGS_SCHEMA[key].default if row is None else row.value_int

    def read_settings(self, actor: ActorContext) -> dict[str, int]:
        self._authorize(actor, ROSC_SETTINGS_READ)
        with self._sessions() as session:
            return {key: self._setting(session, key) for key in SETTINGS_SCHEMA}

    def update_setting(self, actor: ActorContext, key: object, value: object) -> dict[str, int]:
        self._authorize(actor, ROSC_SETTINGS_UPDATE)
        if not isinstance(key, str) or key not in SETTINGS_SCHEMA:
            raise InvalidConversationInput("Unknown setting")
        spec = SETTINGS_SCHEMA[key]
        if not isinstance(value, int) or isinstance(value, bool) or not spec.minimum <= value <= spec.maximum:
            raise InvalidConversationInput(f"{key} must be an integer from {spec.minimum} to {spec.maximum}")

        def write(session: Session) -> None:
            row = session.get(RoscSetting, key)
            if row is None:
                session.add(RoscSetting(key=key, value_int=value, updated_by=actor.actor_id, updated_at=_now()))
            else:
                row.value_int, row.updated_by, row.updated_at = value, actor.actor_id, _now()
        self._tx(write)
        return self.read_settings(actor)

    # ------------------------------------------------------------------ addressing / membership
    def _resolve_targets(self, session: Session, room: Conversation, to: object) -> list[tuple[str, RoleAssignment]] | None:
        """Validate explicit Agent addressees: registered, actively assigned to THIS room's scope. ``None`` = room."""
        if to is None:
            return None
        if not isinstance(to, (list, tuple)) or not to or len(to) > MAX_TARGETS:
            raise InvalidConversationInput(f"to must be a non-empty list of at most {MAX_TARGETS} Agent ids")
        resolved: list[tuple[str, RoleAssignment]] = []
        for agent_id in to:
            if not isinstance(agent_id, str) or session.get(AgentDefinition, agent_id) is None:
                raise InvalidConversationInput("to names an unknown Agent")
            if agent_id in (a for a, _ in resolved):
                raise InvalidConversationInput("to contains a duplicate Agent")
            assignment = self._assignment(session, room, agent_id)
            if assignment is None:
                raise InvalidConversationInput("to names an Agent that is not assigned to this conversation")
            resolved.append((agent_id, assignment))
        return resolved

    def _considered_through(self, session: Session, member: ConversationMember, lane: AgentNativeLane | None) -> int:
        """Highest ordinal the member's current native context has applied OR deliberately skipped (gaps contiguous
        with the cursor).  Gaps are reported separately and are never presented as applied."""
        through = member.epoch_baseline_ordinal
        if lane is not None:
            cursor = session.get(InboundCursor, (member.conversation_id, member.agent_id, member.epoch, lane.lane_id))
            if cursor is not None:
                through = max(through, cursor.applied_through_ordinal)
        gaps = session.scalars(select(ConversationMemberGap).where(
            ConversationMemberGap.conversation_id == member.conversation_id,
            ConversationMemberGap.agent_id == member.agent_id, ConversationMemberGap.epoch == member.epoch
        ).order_by(ConversationMemberGap.from_ordinal)).all()
        for gap in gaps:
            if gap.from_ordinal <= through + 1:
                through = max(through, gap.through_ordinal)
        return through

    def _applied_through(self, session: Session, member: ConversationMember, lane: AgentNativeLane | None) -> int:
        if lane is None:
            return member.epoch_baseline_ordinal
        cursor = session.get(InboundCursor, (member.conversation_id, member.agent_id, member.epoch, lane.lane_id))
        return member.epoch_baseline_ordinal if cursor is None else max(member.epoch_baseline_ordinal,
                                                                         cursor.applied_through_ordinal)

    def _missed(self, session: Session, member: ConversationMember) -> int:
        lane = self._active_lane(session, member.conversation_id, member.agent_id)
        after = self._considered_through(session, member, lane)
        return len(self._events(session, member, after, self._max_ordinal(session, member.conversation_id)))

    def _apply_addressing(self, session: Session, room: Conversation, targets: list[tuple[str, RoleAssignment]] | None,
                          actor: ActorContext, before_ordinal: int) -> None:
        """Owner addressing changes roster state: addressees become Active (a Released one is re-invited into a new
        epoch without backfill), other Active members become Passive.  ``targets=None`` addresses the whole room.

        A first-time member joins with baseline ``before_ordinal``: messages created before it joined are NOT part of
        its context unless the Owner explicitly shares that history (``share_history``)."""
        now = _now()
        members = {m.agent_id: m for m in self._members(session, room.conversation_id)}
        addressed = set(members) - {a for a, m in members.items() if m.state == MEMBER_RELEASED} if targets is None \
            else {agent_id for agent_id, _ in targets}
        for agent_id, assignment in (targets or []):
            member = members.get(agent_id)
            if member is None:
                session.add(ConversationMember(
                    conversation_id=room.conversation_id, agent_id=agent_id, role=assignment.role,
                    state=MEMBER_ACTIVE, epoch=1, epoch_baseline_ordinal=before_ordinal, release_offer_ack_missed=0,
                    history_shared_through_ordinal=None, updated_by=actor.actor_id, updated_at=now))
            elif member.state == MEMBER_RELEASED:
                lane = self._active_lane(session, room.conversation_id, agent_id)
                prior = self._considered_through(session, member, lane)
                member.epoch += 1
                member.epoch_baseline_ordinal = before_ordinal
                if before_ordinal > prior:
                    session.add(ConversationMemberGap(
                        conversation_id=room.conversation_id, agent_id=agent_id, epoch=member.epoch,
                        from_ordinal=prior + 1, through_ordinal=before_ordinal, reason=GAP_RELEASED_PERIOD,
                        created_by=actor.actor_id, created_at=now))
                member.state, member.release_offer_ack_missed = MEMBER_ACTIVE, 0
                member.updated_by, member.updated_at = actor.actor_id, now
        for agent_id, member in members.items():
            if member.state == MEMBER_RELEASED:
                continue
            if agent_id in addressed and member.state != MEMBER_ACTIVE:
                member.state, member.release_offer_ack_missed = MEMBER_ACTIVE, 0
                member.updated_by, member.updated_at = actor.actor_id, now
            elif agent_id not in addressed and member.state == MEMBER_ACTIVE:
                member.state, member.release_offer_ack_missed = MEMBER_PASSIVE, 0
                member.updated_by, member.updated_at = actor.actor_id, now
        session.flush()

    def _write_parties(self, session: Session, message_id: str, conversation_id: str, to_refs: Sequence[str]) -> None:
        roles = {m.agent_id: m.role for m in self._members(session, conversation_id)}
        audience = [PARTY_OWNER_REF] + [m.agent_id for m in self._members(session, conversation_id)
                                        if m.state != MEMBER_RELEASED]
        for role, refs in (("to", to_refs), ("audience", audience)):
            for ref in refs:
                if ref == PARTY_OWNER_REF:
                    session.add(MessageParty(message_id=message_id, role=role, party_ref=ref, party_kind="owner",
                                             agent_id=None))
                else:
                    session.add(MessageParty(message_id=message_id, role=role, party_ref=ref,
                                             party_kind=roles.get(ref, ROLE_WORKER), agent_id=ref))

    # ------------------------------------------------------------------ Owner message
    def post_owner_message(self, actor: ActorContext, project_id: object, *, client_key: object, body: object,
                           to: object = None, reply_to: object = None, task_id: object = None) -> dict:
        """Persist one Owner group message once (idempotent by client key).  Never dispatches, never resets a round."""
        self._authorize(actor, CONVERSATION_MESSAGE_CREATE, project_id=project_id, task_id=task_id)
        key, text = _client_key(client_key), _body(body)
        if reply_to is not None and not isinstance(reply_to, str):
            raise InvalidConversationInput("reply_to must be a message id")

        def write(session: Session) -> dict:
            room = self._room(session, project_id, task_id, create=True, actor=actor)  # type: ignore[arg-type]
            owner = self._owner_participant(session, room.conversation_id)
            targets = self._resolve_targets(session, room, to)
            to_refs = [a for a, _ in targets] if targets is not None else None
            existing = session.scalars(select(ConversationMessage).where(
                ConversationMessage.conversation_id == room.conversation_id,
                ConversationMessage.client_key == key)).first()
            if existing is not None:
                view = self._message_view(session, existing)
                same_to = (sorted(view["to"]) == sorted(to_refs)) if to_refs is not None else view["to_room"]
                if (existing.body != text or existing.sender_participant_id != owner.participant_id
                        or not same_to or view["reply_to"] != reply_to):
                    raise ConversationConflict(
                        "This message key was already used for different content; reload the conversation")
                session.rollback()
                return {"message": view, "replayed": True}
            if reply_to is not None:
                parent = session.get(ConversationMessage, reply_to)
                if parent is None or parent.conversation_id != room.conversation_id:
                    raise InvalidConversationInput("reply_to does not name a message of this conversation")
            ordinal = self._max_ordinal(session, room.conversation_id) + 1
            self._apply_addressing(session, room, targets, actor, ordinal - 1)
            if to_refs is None:
                to_refs = [m.agent_id for m in self._members(session, room.conversation_id)
                           if m.state != MEMBER_RELEASED]
            message = ConversationMessage(
                message_id=str(uuid.uuid4()), conversation_id=room.conversation_id,
                sender_participant_id=owner.participant_id, ordinal=ordinal, body=text, client_key=key,
                recipient=None, created_at=_now())
            session.add(message)
            session.flush()
            session.add(MessageEnvelope(message_id=message.message_id, reply_to_message_id=reply_to,
                                        round_id=None, turn_id=None, sender_agent_id=None))
            self._write_parties(session, message.message_id, room.conversation_id, to_refs)
            session.flush()
            return {"message": self._message_view(session, message, to_room=to is None), "replayed": False}

        with self._sessions() as session:
            try:
                self._begin(session)
                result = write(session)
                if not result["replayed"]:
                    session.commit()
                return result
            except (ConversationDenied, ConversationNotFound, InvalidConversationInput, ConversationConflict,
                    UnsafeProjectIdentity, ConversationStoreError):
                session.rollback()
                raise
            except OperationalError:
                session.rollback()
                raise ConversationStoreError("Another conversation change is in progress; retry shortly") from None
            except IntegrityError:
                session.rollback()
                raise ConversationStoreError("The message could not be stored") from None

    def _message_view(self, session: Session, message: ConversationMessage, *, to_room: bool | None = None) -> dict:
        sender = session.get(ConversationParticipant, message.sender_participant_id)
        envelope = session.get(MessageEnvelope, message.message_id)
        parties = session.scalars(select(MessageParty).where(MessageParty.message_id == message.message_id)).all()
        to = [p.party_ref for p in parties if p.role == "to"]
        audience = [p.party_ref for p in parties if p.role == "audience"]
        if envelope is None:  # legacy message: addressing derived from the stored P038 recipient intent
            to = ["architect"] if message.recipient == "architect" else []
        if to_room is None:
            members_at_time = [p for p in audience if p != PARTY_OWNER_REF]
            to_room = envelope is not None and sender is not None and sender.kind == PARTICIPANT_OWNER \
                and sorted(to) == sorted(members_at_time)
        return {
            "message_id": message.message_id, "conversation_id": message.conversation_id,
            "ordinal": message.ordinal, "body": message.body,
            "sender": {"kind": sender.kind if sender else None, "agent_id": sender.agent_id if sender else None},
            "to": to, "to_room": bool(to_room), "audience": audience,
            "reply_to": envelope.reply_to_message_id if envelope else None,
            "round_id": envelope.round_id if envelope else None,
            "turn_id": envelope.turn_id if envelope else None,
            "legacy_recipient": message.recipient,
        }

    def lookup_message(self, actor: ActorContext, project_id: object, ordinal: object, *, task_id: object = None) -> dict:
        """Resolve the scoped human reference ``#<ordinal>`` (or ``T0013/#<ordinal>``) to exactly one message."""
        self._authorize(actor, CONVERSATION_READ, project_id=project_id, task_id=task_id)
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
            raise InvalidConversationInput("ordinal must be a positive integer")
        with self._sessions() as session:
            room = self._require_room(session, project_id, task_id)  # type: ignore[arg-type]
            message = session.scalars(select(ConversationMessage).where(
                ConversationMessage.conversation_id == room.conversation_id,
                ConversationMessage.ordinal == ordinal)).first()
            if message is None:
                raise ConversationNotFound("Message not found")
            return self._message_view(session, message)

    # ------------------------------------------------------------------ Owner controls
    def authorize_round(self, actor: ActorContext, project_id: object, *, agents: object, quota: object = None,
                        renew: bool = False, authorizing_message_id: object = None, task_id: object = None) -> dict:
        """Explicit Owner grant of a bounded discussion: N published replies per listed Agent.  A Renew closes the
        current round (its queued turns are cancelled; a turn already holding the room finishes in its old round)."""
        self._authorize(actor, CONVERSATION_GROUP_CONTROL, project_id=project_id, task_id=task_id)

        def write(session: Session) -> dict:
            room = self._require_room(session, project_id, task_id)  # type: ignore[arg-type]
            targets = self._resolve_targets(session, room, agents)
            if targets is None:
                raise InvalidConversationInput("agents must list at least one Agent")
            n = self._setting(session, SETTING_GROUP_QUOTA) if quota is None else quota
            spec = SETTINGS_SCHEMA[SETTING_GROUP_QUOTA]
            if not isinstance(n, int) or isinstance(n, bool) or not spec.minimum <= n <= spec.maximum:
                raise InvalidConversationInput(f"quota must be an integer from {spec.minimum} to {spec.maximum}")
            if authorizing_message_id is not None:
                msg = session.get(ConversationMessage, authorizing_message_id) \
                    if isinstance(authorizing_message_id, str) else None
                if msg is None or msg.conversation_id != room.conversation_id:
                    raise InvalidConversationInput("authorizing message is not in this conversation")
            current = self._current_round(session, room.conversation_id)
            if current is not None:
                if not renew:
                    raise ConversationConflict("A group round is already authorized; use Renew to start a new one")
                current.state, current.closed_at = ROUND_CLOSED, _now()
                for turn in session.scalars(select(GroupTurn).where(GroupTurn.round_id == current.round_id,
                                                                    GroupTurn.state == TURN_QUEUED)):
                    turn.state, turn.error_code, turn.updated_at = TURN_CANCELLED, "round_renewed", _now()
                session.flush()
            elif renew:
                raise ConversationConflict("There is no current round to renew")
            join_point = self._max_ordinal(session, room.conversation_id)
            if authorizing_message_id is not None:  # the authorizing message itself belongs to the new members' context
                join_point = session.get(ConversationMessage, authorizing_message_id).ordinal - 1
            self._apply_addressing(session, room, targets, actor, join_point)
            number = (session.scalar(select(func.max(GroupRound.round_no)).where(
                GroupRound.conversation_id == room.conversation_id)) or 0) + 1
            rnd = GroupRound(round_id=str(uuid.uuid4()), conversation_id=room.conversation_id, round_no=number,
                             state=ROUND_OPEN, per_agent_quota=n, current_pass=0,
                             authorizing_message_id=authorizing_message_id, authorized_by=actor.actor_id,
                             authorized_by_assurance=actor.assurance, created_at=_now())
            session.add(rnd)
            session.flush()
            agent_ids = [agent_id for agent_id, _ in targets]
            for agent_id in agent_ids:
                session.add(GroupRoundGrant(round_id=rnd.round_id, agent_id=agent_id, quota=n, published_count=0,
                                            failed=False))
            self._create_pass(session, rnd, 0, agent_ids)
            session.flush()
            return self._round_view(session, rnd)
        return self._tx(write)

    def set_dispatch_controls(self, actor: ActorContext, project_id: object, *, paused: object = None,
                              emergency_brake: object = None, task_id: object = None) -> dict:
        """Persisted Pause Dispatch / Emergency Brake.  Clearing either is an explicit Owner action."""
        self._authorize(actor, CONVERSATION_GROUP_CONTROL, project_id=project_id, task_id=task_id)
        for value in (paused, emergency_brake):
            if value is not None and not isinstance(value, bool):
                raise InvalidConversationInput("controls must be booleans")

        def write(session: Session) -> dict:
            room = self._require_room(session, project_id, task_id)  # type: ignore[arg-type]
            row = self._controls(session, room.conversation_id)
            if row is None:
                row = ConversationDispatchControl(conversation_id=room.conversation_id, paused=False,
                                                  emergency_brake=False, updated_by=actor.actor_id, updated_at=_now())
                session.add(row)
            if paused is not None:
                row.paused = paused
            if emergency_brake is not None:
                row.emergency_brake = emergency_brake
            row.updated_by, row.updated_at = actor.actor_id, _now()
            return {"paused": row.paused, "emergency_brake": row.emergency_brake}
        return self._tx(write)

    def release_member(self, actor: ActorContext, project_id: object, agent_id: object, *, task_id: object = None) -> dict:
        """Owner-only exclusion from the current discussion.  History and native lane stay; queued turns of this Agent
        are cancelled (explicit release policy)."""
        self._authorize(actor, CONVERSATION_GROUP_CONTROL, project_id=project_id, task_id=task_id)

        def write(session: Session) -> dict:
            room = self._require_room(session, project_id, task_id)  # type: ignore[arg-type]
            member = self._member(session, room.conversation_id, agent_id) if isinstance(agent_id, str) else None
            if member is None:
                raise ConversationNotFound("Member not found")
            member.state, member.updated_by, member.updated_at = MEMBER_RELEASED, actor.actor_id, _now()
            for turn in session.scalars(select(GroupTurn).where(GroupTurn.conversation_id == room.conversation_id,
                                                                GroupTurn.agent_id == agent_id,
                                                                GroupTurn.state == TURN_QUEUED)):
                turn.state, turn.error_code, turn.updated_at = TURN_CANCELLED, "member_released", _now()
            return self._member_view(session, member)
        return self._tx(write)

    def keep_member(self, actor: ActorContext, project_id: object, agent_id: object, *, task_id: object = None) -> dict:
        """Owner declines the current Release suggestion; the next one appears only at the next multiple of N."""
        self._authorize(actor, CONVERSATION_GROUP_CONTROL, project_id=project_id, task_id=task_id)

        def write(session: Session) -> dict:
            room = self._require_room(session, project_id, task_id)  # type: ignore[arg-type]
            member = self._member(session, room.conversation_id, agent_id) if isinstance(agent_id, str) else None
            if member is None:
                raise ConversationNotFound("Member not found")
            member.release_offer_ack_missed = self._missed(session, member)
            member.updated_by, member.updated_at = actor.actor_id, _now()
            return self._member_view(session, member)
        return self._tx(write)

    def share_history(self, actor: ActorContext, project_id: object, agent_id: object, *,
                      task_id: object = None) -> dict:
        """Explicit Owner permission to give a first-epoch member the room history created before it joined.  The
        normal catch-up rule (Send full / Skip above the configured size) still applies before any dispatch.  Not
        available after a release/re-invite: a released period never gets mandatory backfill."""
        self._authorize(actor, CONVERSATION_GROUP_CONTROL, project_id=project_id, task_id=task_id)

        def write(session: Session) -> dict:
            room = self._require_room(session, project_id, task_id)  # type: ignore[arg-type]
            member = self._member(session, room.conversation_id, agent_id) if isinstance(agent_id, str) else None
            if member is None:
                raise ConversationNotFound("Member not found")
            if member.epoch != 1 or member.state == MEMBER_RELEASED:
                raise ConversationConflict("History can be shared only with a current first-time member")
            if member.epoch_baseline_ordinal > 0:
                member.history_shared_through_ordinal = member.epoch_baseline_ordinal
                member.epoch_baseline_ordinal = 0
            member.updated_by, member.updated_at = actor.actor_id, _now()
            return self._member_view(session, member)
        return self._tx(write)

    def choose_catchup(self, actor: ActorContext, project_id: object, turn_id: object, choice: object, *,
                       task_id: object = None) -> dict:
        """Owner pre-dispatch decision for a large historical delta: send it in full, or skip the backlog (recorded as
        an audited ``owner_skip`` gap, never as applied)."""
        self._authorize(actor, CONVERSATION_GROUP_CONTROL, project_id=project_id, task_id=task_id)
        if choice not in (CATCHUP_SEND_FULL, CATCHUP_SKIP_BACKLOG):
            raise InvalidConversationInput("choice must be send_full or skip_backlog")

        def write(session: Session) -> dict:
            room = self._require_room(session, project_id, task_id)  # type: ignore[arg-type]
            turn = session.get(GroupTurn, turn_id) if isinstance(turn_id, str) else None
            if turn is None or turn.conversation_id != room.conversation_id:
                raise ConversationNotFound("Turn not found")
            if turn.state != TURN_QUEUED or turn.catchup_choice is not None:
                raise ConversationConflict("This turn no longer needs a catch-up decision")
            info = self._catchup_need(session, turn)
            turn.catchup_choice, turn.updated_at = choice, _now()
            if choice == CATCHUP_SKIP_BACKLOG and info is not None:
                member = self._member(session, room.conversation_id, turn.agent_id)
                session.add(ConversationMemberGap(
                    conversation_id=room.conversation_id, agent_id=turn.agent_id, epoch=member.epoch,  # type: ignore[union-attr]
                    from_ordinal=info["from"], through_ordinal=info["through"], reason=GAP_OWNER_SKIP,
                    created_by=actor.actor_id, created_at=_now()))
            return self._turn_view(turn)
        return self._tx(write)

    # ------------------------------------------------------------------ passes
    def _last_published_agent(self, session: Session, conversation_id: str) -> str | None:
        row = session.execute(select(MessageEnvelope.sender_agent_id).join(
            ConversationMessage, ConversationMessage.message_id == MessageEnvelope.message_id).where(
            ConversationMessage.conversation_id == conversation_id, MessageEnvelope.sender_agent_id.is_not(None)
        ).order_by(ConversationMessage.ordinal.desc()).limit(1)).first()
        return None if row is None else row[0]

    def _create_pass(self, session: Session, rnd: GroupRound, pass_no: int, agent_ids: list[str]) -> list[str]:
        order = sorted(agent_ids)
        self._rng.shuffle(order)
        last = self._last_published_agent(session, rnd.conversation_id)
        if len(order) >= 2 and order[0] == last:
            swap = self._rng.randrange(1, len(order))
            order[0], order[swap] = order[swap], order[0]
        for position, agent_id in enumerate(order):
            session.add(GroupTurn(turn_id=str(uuid.uuid4()), round_id=rnd.round_id, conversation_id=rnd.conversation_id,
                                  agent_id=agent_id, pass_no=pass_no, position=position, attempt=1, state=TURN_QUEUED,
                                  correlation_id=str(uuid.uuid4()), created_at=_now(), updated_at=_now()))
        rnd.current_pass = pass_no
        return order

    def _eligible_for_next_pass(self, session: Session, rnd: GroupRound) -> list[str]:
        result = []
        for grant in session.scalars(select(GroupRoundGrant).where(GroupRoundGrant.round_id == rnd.round_id)):
            member = self._member(session, rnd.conversation_id, grant.agent_id)
            room = session.get(Conversation, rnd.conversation_id)
            if (grant.published_count < grant.quota and not grant.failed and member is not None
                    and member.state != MEMBER_RELEASED and self._assignment(session, room, grant.agent_id) is not None):
                result.append(grant.agent_id)
        return result

    # ------------------------------------------------------------------ catch-up decision
    def _catchup_need(self, session: Session, turn: GroupTurn) -> dict | None:
        """Large historical backlog (before the round's authorizing message) on the Agent's first turn of a round."""
        rnd = session.get(GroupRound, turn.round_id)
        earlier = session.scalar(select(func.count()).select_from(GroupTurn).where(
            GroupTurn.round_id == turn.round_id, GroupTurn.agent_id == turn.agent_id,
            GroupTurn.turn_id != turn.turn_id, GroupTurn.state.notin_((TURN_QUEUED, TURN_CANCELLED)))) or 0
        if earlier or turn.attempt != 1:
            return None
        member = self._member(session, turn.conversation_id, turn.agent_id)
        lane = self._active_lane(session, turn.conversation_id, turn.agent_id)
        after = self._considered_through(session, member, lane)  # type: ignore[arg-type]
        boundary = self._max_ordinal(session, turn.conversation_id)
        if rnd.authorizing_message_id is not None:
            boundary = session.get(ConversationMessage, rnd.authorizing_message_id).ordinal - 1
        if boundary <= after:
            return None
        events = self._events(session, member, after, boundary)  # type: ignore[arg-type]
        chars = sum(len(e.body) for e in events)
        limit = self._setting(session, SETTING_CATCHUP_CHARS)
        if chars <= limit:
            return None
        return {"from": after + 1, "through": boundary, "chars": chars, "limit": limit}

    # ------------------------------------------------------------------ engine: claim
    def claim_next(self, conversation_id: str) -> ClaimResult:
        """Atomically reserve exactly one turn of this room, or report honestly why none can be reserved."""
        def write(session: Session) -> ClaimResult:
            room = session.get(Conversation, conversation_id)
            if room is None:
                raise ConversationNotFound("Conversation not found")
            controls = self._controls(session, conversation_id)
            if controls is not None and (controls.paused or controls.emergency_brake):
                return ClaimResult("paused", detail={"paused": controls.paused,
                                                     "emergency_brake": controls.emergency_brake})
            holding = self._holding_turn(session, conversation_id)
            if holding is not None:
                return ClaimResult("busy", holding.turn_id, holding.agent_id, {"state": holding.state})
            rnd = self._current_round(session, conversation_id)
            if rnd is None:
                return ClaimResult("idle")
            if rnd.state == ROUND_AWAITING_OWNER:
                return ClaimResult("awaiting_owner")
            for _ in range(2):  # current pass, then at most one freshly built pass
                for turn in session.scalars(select(GroupTurn).where(
                        GroupTurn.round_id == rnd.round_id, GroupTurn.pass_no == rnd.current_pass,
                        GroupTurn.state == TURN_QUEUED).order_by(GroupTurn.position)).all():
                    grant = session.get(GroupRoundGrant, (rnd.round_id, turn.agent_id))
                    member = self._member(session, conversation_id, turn.agent_id)
                    if member is None or member.state == MEMBER_RELEASED:
                        turn.state, turn.error_code = TURN_CANCELLED, "member_released"
                        continue
                    if self._assignment(session, room, turn.agent_id) is None:
                        turn.state, turn.error_code = TURN_CANCELLED, "assignment_changed"
                        continue
                    if grant is None or grant.published_count >= grant.quota or grant.failed:
                        turn.state, turn.error_code = TURN_CANCELLED, "quota_exhausted"
                        continue
                    if turn.catchup_choice is None:
                        need = self._catchup_need(session, turn)
                        if need is not None:
                            return ClaimResult("catchup_choice_required", turn.turn_id, turn.agent_id, need)
                    lane = self._ensure_lane(session, room, turn.agent_id)
                    turn.lane_id, turn.state, turn.updated_at = lane.lane_id, TURN_RESERVED, _now()
                    session.flush()
                    return ClaimResult("reserved", turn.turn_id, turn.agent_id,
                                       {"pass": turn.pass_no, "position": turn.position, "attempt": turn.attempt})
                eligible = self._eligible_for_next_pass(session, rnd)
                if not eligible:
                    rnd.state = ROUND_AWAITING_OWNER
                    return ClaimResult("awaiting_owner")
                self._create_pass(session, rnd, rnd.current_pass + 1, eligible)
                session.flush()
            raise ConversationStoreError("Turn queue is inconsistent")  # pragma: no cover - defensive
        return self._tx(write)

    def _ensure_lane(self, session: Session, room: Conversation, agent_id: str) -> AgentNativeLane:
        """get_or_create the native lane of (Project, Agent, Conversation) for the CURRENT Root Binding version."""
        project = self._project(session, room.project_id)
        version = project.current_binding.version
        lane = self._active_lane(session, room.conversation_id, agent_id)
        if lane is not None and lane.root_binding_version == version:
            return lane
        if lane is not None:
            lane.state, lane.invalidated_reason, lane.invalidated_at = LANE_INVALIDATED, "root_binding_changed", _now()
            session.flush()
        agent = session.get(AgentDefinition, agent_id)
        lane = AgentNativeLane(lane_id=str(uuid.uuid4()), project_id=room.project_id, agent_id=agent_id,
                               conversation_id=room.conversation_id, root_binding_version=version,
                               adapter=agent.runtime_adapter, native_session_id=str(uuid.uuid4()),  # type: ignore[union-attr]
                               state=LANE_ACTIVE, created_at=_now())
        session.add(lane)
        session.flush()
        return lane

    def get_or_create_native_lane(self, conversation_id: str, agent_id: str) -> LaneRef:
        def write(session: Session) -> LaneRef:
            room = session.get(Conversation, conversation_id)
            if room is None:
                raise ConversationNotFound("Conversation not found")
            if self._assignment(session, room, agent_id) is None:
                raise InvalidConversationInput("Agent is not assigned to this conversation")
            return self._lane_ref(self._ensure_lane(session, room, agent_id))
        return self._tx(write)

    # ------------------------------------------------------------------ engine: dispatch
    def _events(self, session: Session, member: ConversationMember, after: int, through: int) -> list[InputEvent]:
        """Room events in (after, through] that THIS member may receive: never its own replies; a message carrying a
        group envelope only if its historical audience snapshot included the member (Passive members are in every
        audience, so they keep receiving group messages addressed to others) or the Owner explicitly shared history up
        to that ordinal.  Messages without an envelope (legacy API) are room-wide; pre-join ones are already excluded by
        the member's baseline."""
        rows = session.execute(select(ConversationMessage, ConversationParticipant).join(
            ConversationParticipant,
            ConversationParticipant.participant_id == ConversationMessage.sender_participant_id).where(
            ConversationMessage.conversation_id == member.conversation_id, ConversationMessage.ordinal > after,
            ConversationMessage.ordinal <= through).order_by(ConversationMessage.ordinal)).all()
        shared = member.history_shared_through_ordinal or 0
        events = []
        for message, sender in rows:
            if sender.agent_id == member.agent_id:
                continue  # the Agent's own published replies are never fed back as peer input
            view = self._message_view(session, message)
            if session.get(MessageEnvelope, message.message_id) is not None and message.ordinal > shared \
                    and member.agent_id not in view["audience"]:
                continue  # not visible to this member when it was created (before joining / while released)
            events.append(InputEvent(message.ordinal, message.message_id, sender.kind, sender.agent_id,
                                     tuple(view["to"]), message.body))
        return events

    def _gaps(self, session: Session, member: ConversationMember) -> tuple[tuple[int, int], ...]:
        return tuple((g.from_ordinal, g.through_ordinal) for g in session.scalars(select(ConversationMemberGap).where(
            ConversationMemberGap.conversation_id == member.conversation_id,
            ConversationMemberGap.agent_id == member.agent_id,
            ConversationMemberGap.epoch == member.epoch).order_by(ConversationMemberGap.from_ordinal)))

    def dispatch(self, turn_id: str, transport: ConversationTransport) -> dict:
        """Freeze the just-in-time input snapshot, mark ``submitted`` durably, THEN call the transport once and record
        its outcome.  A crash between the two transactions leaves ``submitted`` which recovery turns into UNKNOWN."""
        def prepare(session: Session):
            turn = session.get(GroupTurn, turn_id)
            if turn is None:
                raise ConversationNotFound("Turn not found")
            if turn.state != TURN_RESERVED:
                raise ConversationConflict("Turn is not reserved for dispatch")
            # Re-authorize immediately before ``submitted``: Owner may have released the member, revoked the assignment
            # or renewed the round after the claim.  Nothing has reached the provider yet, so the turn is closed
            # without submit, cursor advance or quota debit (and it releases the room).
            member = self._member(session, turn.conversation_id, turn.agent_id)
            revoked = self._revocation(session, turn, member)
            if revoked is not None:
                turn.state, turn.error_code, turn.updated_at = TURN_CANCELLED, revoked, _now()
                return None, None
            controls = self._controls(session, turn.conversation_id)
            if controls is not None and (controls.paused or controls.emergency_brake):
                turn.state, turn.lane_id, turn.updated_at = TURN_QUEUED, None, _now()  # nothing was sent
                return None, None
            lane = session.get(AgentNativeLane, turn.lane_id) if turn.lane_id else None
            version = self._project(session, lane.project_id).current_binding.version if lane is not None else None
            if lane is None or lane.state != LANE_ACTIVE or lane.root_binding_version != version:
                turn.state, turn.lane_id, turn.updated_at = TURN_QUEUED, None, _now()  # never resume a stale context
                return None, None
            after = self._considered_through(session, member, lane)  # type: ignore[arg-type]
            through = self._max_ordinal(session, turn.conversation_id)
            events = self._events(session, member, after, through)  # type: ignore[arg-type]
            grant = session.get(GroupRoundGrant, (turn.round_id, turn.agent_id))
            payload = TurnPayload(turn.turn_id, turn.correlation_id, turn.conversation_id, turn.agent_id,
                                  turn.round_id, after, through, grant.quota - grant.published_count,  # type: ignore[union-attr]
                                  tuple(events), self._gaps(session, member))  # type: ignore[arg-type]
            turn.input_after_ordinal, turn.input_through_ordinal = after, through
            turn.state, turn.updated_at = TURN_SUBMITTED, _now()
            return payload, self._lane_ref(lane)

        payload, lane = self._tx(prepare)
        if payload is None:
            with self._sessions() as session:
                turn = session.get(GroupTurn, turn_id)
                return {"status": "not_dispatched", "turn_id": turn_id, "state": turn.state,  # type: ignore[union-attr]
                        "error_code": turn.error_code}  # type: ignore[union-attr]
        try:
            outcome = SubmitOutcome(SUBMIT_NOT_ACCEPTED, "lane_busy") if transport.busy(lane) \
                else transport.submit(lane, payload)
            if not isinstance(outcome, SubmitOutcome) or outcome.status not in (
                    SUBMIT_ACCEPTED, SUBMIT_NOT_ACCEPTED, SUBMIT_UNKNOWN):
                outcome = SubmitOutcome(SUBMIT_UNKNOWN, "malformed_outcome")
        except Exception:
            outcome = SubmitOutcome(SUBMIT_UNKNOWN, "transport_error")

        def record(session: Session) -> dict:
            turn = session.get(GroupTurn, turn_id)
            if turn.state != TURN_SUBMITTED:  # type: ignore[union-attr]
                return self._turn_view(turn)  # type: ignore[arg-type]
            if outcome.status == SUBMIT_ACCEPTED:
                self._accept(session, turn)  # type: ignore[arg-type]
            elif outcome.status == SUBMIT_NOT_ACCEPTED:
                self._safe_failure(session, turn, outcome.code or "not_accepted")  # type: ignore[arg-type]
            else:
                turn.state, turn.error_code, turn.updated_at = TURN_UNKNOWN, outcome.code or "submit_unknown", _now()  # type: ignore[union-attr]
            return self._turn_view(turn)  # type: ignore[arg-type]
        view = self._tx(record)
        view["payload"] = payload
        return view

    def _revocation(self, session: Session, turn: GroupTurn, member: ConversationMember | None) -> str | None:
        """Why a reserved (never submitted) turn is no longer authorized, or None if it still is."""
        if member is None or member.state == MEMBER_RELEASED:
            return "member_released"
        room = session.get(Conversation, turn.conversation_id)
        if self._assignment(session, room, turn.agent_id) is None:  # type: ignore[arg-type]
            return "assignment_changed"
        rnd = session.get(GroupRound, turn.round_id)
        if rnd is None or rnd.state == ROUND_CLOSED:
            return "round_closed"
        grant = session.get(GroupRoundGrant, (turn.round_id, turn.agent_id))
        if grant is None or grant.failed or grant.published_count >= grant.quota:
            return "quota_exhausted"
        return None

    def _accept(self, session: Session, turn: GroupTurn) -> None:
        """Provider confirmed the frozen input batch: advance ONLY this lane/epoch cursor, to the snapshot end."""
        member = self._member(session, turn.conversation_id, turn.agent_id)
        key = (turn.conversation_id, turn.agent_id, member.epoch, turn.lane_id)  # type: ignore[union-attr]
        cursor = session.get(InboundCursor, key)
        if cursor is None:
            cursor = InboundCursor(conversation_id=key[0], agent_id=key[1], epoch=key[2], lane_id=key[3],
                                   applied_through_ordinal=0, updated_at=_now())
            session.add(cursor)
        cursor.applied_through_ordinal = max(cursor.applied_through_ordinal, turn.input_through_ordinal or 0)
        cursor.last_turn_id, cursor.updated_at = turn.turn_id, _now()
        turn.state, turn.error_code, turn.updated_at = TURN_ACCEPTED, None, _now()

    def _safe_failure(self, session: Session, turn: GroupTurn, code: str) -> None:
        """Provably not accepted: first attempt is deferred to the end of the SAME pass once; a second safe failure
        is a terminal, Owner-visible ``failed`` that excludes the Agent from later passes of the round."""
        turn.error_code, turn.updated_at = code, _now()
        if turn.attempt == 1:
            turn.state = TURN_DEFERRED
            last = session.scalar(select(func.max(GroupTurn.position)).where(
                GroupTurn.round_id == turn.round_id, GroupTurn.pass_no == turn.pass_no)) or 0
            session.add(GroupTurn(turn_id=str(uuid.uuid4()), round_id=turn.round_id,
                                  conversation_id=turn.conversation_id, agent_id=turn.agent_id, pass_no=turn.pass_no,
                                  position=last + 1, attempt=2, retry_of=turn.turn_id, state=TURN_QUEUED,
                                  catchup_choice=turn.catchup_choice, correlation_id=str(uuid.uuid4()),
                                  created_at=_now(), updated_at=_now()))
        else:
            turn.state = TURN_FAILED
            grant = session.get(GroupRoundGrant, (turn.round_id, turn.agent_id))
            if grant is not None:
                grant.failed = True

    # ------------------------------------------------------------------ engine: reply
    def capture_reply(self, turn_id: str, text: object, *, to: object = None, reply_to: object = None) -> dict:
        """Durably store the correlated reply of an ACCEPTED turn before publication (no quota spent yet)."""
        body = _architect_body(text)

        def write(session: Session) -> dict:
            turn = session.get(GroupTurn, turn_id)
            if turn is None:
                raise ConversationNotFound("Turn not found")
            if turn.state in (TURN_REPLY_CAPTURED, TURN_PUBLISHED):
                if turn.captured_reply != body:
                    raise ConversationConflict("A different reply was already captured for this turn")
                return self._turn_view(turn)
            if turn.state != TURN_ACCEPTED:
                raise ConversationConflict("Only an accepted turn can capture a reply")
            refs = self._reply_targets(session, turn, to)
            if reply_to is not None:
                parent = session.get(ConversationMessage, reply_to) if isinstance(reply_to, str) else None
                if (parent is None or parent.conversation_id != turn.conversation_id
                        or parent.ordinal > (turn.input_through_ordinal or 0)):
                    raise InvalidConversationInput("reply_to does not name a message this turn received")
            turn.captured_reply, turn.captured_to = body, json.dumps(refs)
            turn.captured_reply_to, turn.state, turn.updated_at = reply_to, TURN_REPLY_CAPTURED, _now()  # type: ignore[assignment]
            return self._turn_view(turn)
        return self._tx(write)

    def _reply_targets(self, session: Session, turn: GroupTurn, to: object) -> list[str]:
        if to is None:
            return [PARTY_OWNER_REF]
        if not isinstance(to, (list, tuple)) or not to or len(to) > MAX_TARGETS:
            raise InvalidConversationInput("to must be a non-empty list")
        members = {m.agent_id for m in self._members(session, turn.conversation_id) if m.state != MEMBER_RELEASED}
        refs: list[str] = []
        for ref in to:
            if ref != PARTY_OWNER_REF and (ref not in members or ref == turn.agent_id):
                raise InvalidConversationInput("reply target is not a member of this conversation")
            if ref in refs:
                raise InvalidConversationInput("duplicate reply target")
            refs.append(ref)  # type: ignore[arg-type]
        return refs

    def publish(self, turn_id: str) -> dict:
        """Commit exactly one canonical Agent message for a captured reply, debit exactly one grant slot and release
        the room — all in ONE transaction.  Idempotent: an already published turn is returned unchanged."""
        def write(session: Session) -> dict:
            turn = session.get(GroupTurn, turn_id)
            if turn is None:
                raise ConversationNotFound("Turn not found")
            if turn.state == TURN_PUBLISHED:
                return self._turn_view(turn)
            if turn.state != TURN_REPLY_CAPTURED:
                raise ConversationConflict("Turn has no captured reply to publish")
            member = self._member(session, turn.conversation_id, turn.agent_id)
            lane = session.get(AgentNativeLane, turn.lane_id)
            participant = session.scalars(select(ConversationParticipant).where(
                ConversationParticipant.conversation_id == turn.conversation_id,
                ConversationParticipant.kind == member.role,  # type: ignore[union-attr]
                ConversationParticipant.agent_id == turn.agent_id)).first()
            if participant is None:
                participant = ConversationParticipant(
                    participant_id=str(uuid.uuid4()), conversation_id=turn.conversation_id, kind=member.role,  # type: ignore[union-attr]
                    actor_id=turn.agent_id, actor_assurance=f"adapter:{lane.adapter}"[:32],  # type: ignore[union-attr]
                    agent_id=turn.agent_id, created_at=_now())
                session.add(participant)
                session.flush()
            ordinal = self._max_ordinal(session, turn.conversation_id) + 1
            message = ConversationMessage(
                message_id=str(uuid.uuid4()), conversation_id=turn.conversation_id,
                sender_participant_id=participant.participant_id, ordinal=ordinal, body=turn.captured_reply,
                client_key=f"{CLIENT_KEY_REPLY_PREFIX}{turn.turn_id}", recipient=None, created_at=_now())
            session.add(message)
            session.flush()
            session.add(MessageEnvelope(message_id=message.message_id, reply_to_message_id=turn.captured_reply_to,
                                        round_id=turn.round_id, turn_id=turn.turn_id, sender_agent_id=turn.agent_id))
            self._write_parties(session, message.message_id, turn.conversation_id, json.loads(turn.captured_to or "[]"))
            grant = session.get(GroupRoundGrant, (turn.round_id, turn.agent_id))
            grant.published_count += 1  # type: ignore[union-attr]  # CHECK forbids overspend
            turn.state, turn.reply_message_id, turn.updated_at = TURN_PUBLISHED, message.message_id, _now()
            session.flush()
            if self.before_publish_commit is not None:
                self.before_publish_commit()
            view = self._turn_view(turn)
            view["ordinal"] = ordinal
            return view
        return self._tx(write)

    def complete(self, turn_id: str, text: object, *, to: object = None, reply_to: object = None) -> dict:
        self.capture_reply(turn_id, text, to=to, reply_to=reply_to)
        return self.publish(turn_id)

    # ------------------------------------------------------------------ engine: recovery / reconciliation
    def recover(self, conversation_id: str) -> dict:
        """Restart recovery without any provider call: reserved (never submitted) -> queued; submitted (outcome lost)
        -> unknown; reply_captured -> published exactly once."""
        captured: list[str] = []

        def write(session: Session) -> dict:
            counts = {"requeued": 0, "unknown": 0}
            for turn in session.scalars(select(GroupTurn).where(GroupTurn.conversation_id == conversation_id,
                                                                GroupTurn.state.in_(TURN_HOLDING_STATES))):
                if turn.state == TURN_RESERVED:
                    turn.state, turn.lane_id, turn.updated_at = TURN_QUEUED, None, _now()
                    counts["requeued"] += 1
                elif turn.state == TURN_SUBMITTED:
                    turn.state, turn.error_code, turn.updated_at = TURN_UNKNOWN, "submit_outcome_lost", _now()
                    counts["unknown"] += 1
                elif turn.state == TURN_REPLY_CAPTURED:
                    captured.append(turn.turn_id)
            return counts
        counts = self._tx(write)
        counts["published"] = sum(1 for turn_id in captured if self.publish(turn_id)["state"] == TURN_PUBLISHED)
        return counts

    def reconcile(self, turn_id: str, transport: ConversationTransport) -> dict:
        """Evidence-backed resolution of an UNKNOWN (or accepted, reply-pending) turn by correlation lookup.  Never
        re-submits; an inconclusive lookup leaves the reservation held."""
        with self._sessions() as session:
            turn = session.get(GroupTurn, turn_id)
            if turn is None:
                raise ConversationNotFound("Turn not found")
            if turn.state not in (TURN_UNKNOWN, TURN_ACCEPTED):
                return self._turn_view(turn)
            lane = self._lane_ref(session.get(AgentNativeLane, turn.lane_id))  # type: ignore[arg-type]
            correlation = turn.correlation_id
        try:
            found = transport.lookup(lane, correlation)
        except Exception:
            found = LookupOutcome(LOOKUP_UNKNOWN)

        def write(session: Session) -> dict:
            turn = session.get(GroupTurn, turn_id)
            if turn.state == TURN_UNKNOWN:  # type: ignore[union-attr]
                if found.status == LOOKUP_NOT_RECEIVED:
                    self._safe_failure(session, turn, "not_received")  # type: ignore[arg-type]
                elif found.status in (LOOKUP_ACCEPTED, LOOKUP_REPLY):
                    self._accept(session, turn)  # type: ignore[arg-type]
            return self._turn_view(turn)  # type: ignore[arg-type]
        view = self._tx(write)
        if view["state"] == TURN_ACCEPTED and found.status == LOOKUP_REPLY and found.text:
            view = self.complete(turn_id, found.text, to=list(found.to) if found.to else None, reply_to=found.reply_to)
        return view

    # ------------------------------------------------------------------ views
    @staticmethod
    def _turn_view(turn: GroupTurn) -> dict:
        return {"turn_id": turn.turn_id, "round_id": turn.round_id, "agent_id": turn.agent_id, "state": turn.state,
                "pass": turn.pass_no, "position": turn.position, "attempt": turn.attempt,
                "lane_id": turn.lane_id, "error_code": turn.error_code, "catchup_choice": turn.catchup_choice,
                "input_after_ordinal": turn.input_after_ordinal, "input_through_ordinal": turn.input_through_ordinal,
                "reply_message_id": turn.reply_message_id}

    def _round_view(self, session: Session, rnd: GroupRound) -> dict:
        grants = session.scalars(select(GroupRoundGrant).where(GroupRoundGrant.round_id == rnd.round_id).order_by(
            GroupRoundGrant.agent_id)).all()
        turns = session.scalars(select(GroupTurn).where(GroupTurn.round_id == rnd.round_id).order_by(
            GroupTurn.pass_no, GroupTurn.position)).all()
        return {"round_id": rnd.round_id, "round_no": rnd.round_no, "state": rnd.state,
                "per_agent_quota": rnd.per_agent_quota, "current_pass": rnd.current_pass,
                "grants": {g.agent_id: {"quota": g.quota, "published": g.published_count, "failed": g.failed}
                           for g in grants},
                "turns": [self._turn_view(t) for t in turns]}

    def _member_view(self, session: Session, member: ConversationMember) -> dict:
        lane = self._active_lane(session, member.conversation_id, member.agent_id)
        missed = self._missed(session, member) if member.state == MEMBER_PASSIVE else 0
        threshold = self._setting(session, SETTING_PASSIVE_MISSED_THRESHOLD)
        step, acked = missed // threshold, member.release_offer_ack_missed // threshold
        return {"agent_id": member.agent_id, "role": member.role, "state": member.state, "epoch": member.epoch,
                "applied_through": self._applied_through(session, member, lane),
                "considered_through": self._considered_through(session, member, lane),
                "omitted_ranges": [list(r) for r in self._gaps(session, member)],
                "history_shared_through": member.history_shared_through_ordinal,
                "missed": missed,
                "release_offer": ({"missed": missed, "step": step, "threshold": threshold}
                                  if member.state == MEMBER_PASSIVE and step >= 1 and step > acked else None),
                "lane": None if lane is None else {"lane_id": lane.lane_id, "native_session_id": lane.native_session_id,
                                                   "root_binding_version": lane.root_binding_version}}

    def room_status(self, actor: ActorContext, project_id: object, *, task_id: object = None) -> dict:
        """Typed Control-Plane state of one room (never a chat message).  ``answering`` names an Agent only while the
        provider has actually accepted its turn; queued turns are ``waiting``."""
        self._authorize(actor, CONVERSATION_READ, project_id=project_id, task_id=task_id)
        with self._sessions() as session:
            room = self._require_room(session, project_id, task_id)  # type: ignore[arg-type]
            controls = self._controls(session, room.conversation_id)
            rnd = self._current_round(session, room.conversation_id)
            holding = self._holding_turn(session, room.conversation_id)
            waiting = [] if rnd is None else [t.agent_id for t in session.scalars(select(GroupTurn).where(
                GroupTurn.round_id == rnd.round_id, GroupTurn.state == TURN_QUEUED).order_by(
                GroupTurn.pass_no, GroupTurn.position))]
            failed = [] if rnd is None else [self._turn_view(t) for t in session.scalars(select(GroupTurn).where(
                GroupTurn.round_id == rnd.round_id, GroupTurn.state == TURN_FAILED))]
            return {
                "conversation_id": room.conversation_id,
                "controls": {"paused": bool(controls and controls.paused),
                             "emergency_brake": bool(controls and controls.emergency_brake)},
                "round": None if rnd is None else self._round_view(session, rnd),
                "holding_turn": None if holding is None else self._turn_view(holding),
                "answering": holding.agent_id if holding is not None and holding.state == TURN_ACCEPTED else None,
                "waiting": waiting,
                "failed": failed,
                "members": [self._member_view(session, m) for m in self._members(session, room.conversation_id)],
            }
