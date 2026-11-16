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
* P047 / R-15.25: an Owner message sent through ``post_owner_turn`` IS the authorization.  In one transaction it
  persists the message and opens a fresh round for the addressed supported Agents (direct: one reply; group: N), closing
  the previous round: only its not-yet-submitted queued turns are superseded; a turn already holding the room finishes
  and is charged to its OLD round.  A plain ``post_owner_message`` still never touches rounds.
* After the first pass an Agent gets another turn only when a peer Agent actually addressed it (structured ``to``) after
  its last input snapshot; remaining quota is a ceiling, never a reason to generate.
* Pause / Emergency Brake forbid new claims and new submits; they never pretend an in-flight turn was cancelled.
* P052: ``@DisplayName`` addressing (``mentions.find_mentions``) is resolved against the room's in-scope Agents.  A
  correlated reply of an Owner-authorized DIRECT request (one granted Agent) that addresses eligible peers turns that
  round ONCE into a bounded discussion (every participant N; the inviter's reply counts as its first).  The same
  happens, through ``bridge_direct_reply``, for an authenticated legacy direct Architect reply.  Agents can never open
  a round, renew a grant or add participants to an existing discussion.  Every dispatched turn carries a trusted,
  advisory room-wide quota snapshot (``TurnPayload.quota``).  A peer reply longer than
  ``GROUP_REPLY_CONTINUE_MAX_CHARS`` is published in full but never earns another Agent turn (deterministic size rule).
"""
from __future__ import annotations

import json
import random
import threading
import time
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
from .conversation_trace import ConversationTrace
from .mentions import find_mentions
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
# P052 deterministic size policy.  Agents are asked to stay under the guide; a reply above the hard continuation limit is
# stored and shown in full (never truncated, never regenerated) but cannot invite a peer or earn another Agent turn.
GROUP_REPLY_GUIDE_CHARS = 1200
GROUP_REPLY_CONTINUE_MAX_CHARS = 3000


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
    # P052: trusted, advisory room-wide quota snapshot frozen with the input (never a source of authorization).
    quota: dict | None = field(default=None, compare=False)


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


def _payload_text(payload: "TurnPayload") -> str:
    """Body-mode trace projection of the exact room input ROSC hands to a dispatch adapter (TurnPayload events)."""
    return json.dumps({"turn_id": payload.turn_id, "input_after_ordinal": payload.input_after_ordinal,
                       "input_through_ordinal": payload.input_through_ordinal, "remaining": payload.remaining_replies,
                       "omitted_ranges": [list(r) for r in payload.omitted_ranges],
                       "events": [{"ordinal": e.ordinal, "message_id": e.message_id, "from": e.sender_kind,
                                   "from_agent_id": e.sender_agent_id, "to": list(e.to), "body": e.body}
                                  for e in payload.events]}, ensure_ascii=False)


def _guidance_text(quota: dict | None) -> str | None:
    if not quota:
        return None
    from .claude_conversation import quota_guidance  # local: avoids an import cycle
    try:
        return quota_guidance(quota)
    except Exception:  # diagnostics never break dispatch
        return None


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
        # P052 composition seam (set by the group bridge): why an in-scope Agent could NOT take a real turn now
        # (``None`` = it could).  DB-only, read-only.  Unset = every invitation fails closed.
        self.peer_problem: Callable[[Session, Conversation, str], str | None] | None = None
        # P052 r03: Owner-switchable conversation diagnostics.  Events noted inside a transaction are written only after
        # it commits (a rolled-back decision is never reported as having happened).
        self.trace = ConversationTrace(None)
        self._local = threading.local()

    # ------------------------------------------------------------------ diagnostics (P052 r03)
    def _note(self, session: Session, event: str, conversation_id: str | None = None, **fields) -> None:
        if not self.trace.enabled:
            return
        if conversation_id is not None:
            room = session.get(Conversation, conversation_id)
            if room is not None:
                fields.setdefault("project_id", room.project_id)
                fields.setdefault("task_id", room.task_id)
                fields.setdefault("scope", "task" if room.task_id else "project")
            fields["conversation_id"] = conversation_id
        pending = getattr(self._local, "events", None)
        if pending is None:
            self.trace.emit(event, **fields)
        else:
            pending.append((event, fields))

    def trace_begin(self) -> None:
        """Start buffering notes for a transaction owned by a caller (e.g. the Architect reply commit)."""
        self._local.events = [] if self.trace.enabled else None

    def trace_end(self, committed: bool) -> None:
        pending = getattr(self._local, "events", None)
        self._local.events = None
        if committed and pending:
            for event, fields in pending:
                self.trace.emit(event, **fields)

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
            outer = getattr(self._local, "events", None) is not None
            if not outer:
                self.trace_begin()
            committed = False
            try:
                self._begin(session)
                result = fn(session)
                session.commit()
                committed = True
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
            finally:
                if not outer:
                    self.trace_end(committed)

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

        A first-time member joins with baseline ``before_ordinal`` (the actual moment it is first brought into the roster):
        messages created before it joined are never part of its native input.  ROSC v1.0 has no bulk history backfill;
        a future explicit read-only grant for specific historical #ordinals is out of this Core's scope."""
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
                    updated_by=actor.actor_id, updated_at=now))
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
        room = session.get(Conversation, conversation_id)
        for ref in to_refs:  # P052: an addressed in-scope non-member keeps its real role
            if ref != PARTY_OWNER_REF and ref not in roles and room is not None:
                assignment = self._assignment(session, room, ref)
                if assignment is not None:
                    roles[ref] = assignment.role
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

    def post_owner_turn(self, actor: ActorContext, project_id: object, *, client_key: object, body: object,
                        to: object, grant: object, task_id: object = None) -> dict:
        """P047: one Owner Send = one canonical message + (for the supported addressees in ``grant``) a fresh round,
        atomically.  ``grant`` of one Agent is a DIRECT request (exactly one reply authorized); two or more is a GROUP
        round with the ROSC default N per Agent.  An identical retry (same client key) replays the message and grants
        nothing new.  Never calls a provider."""
        self._authorize(actor, CONVERSATION_MESSAGE_CREATE, project_id=project_id, task_id=task_id)
        self._authorize(actor, CONVERSATION_GROUP_CONTROL, project_id=project_id, task_id=task_id)
        key, text = _client_key(client_key), _body(body)
        if not isinstance(grant, (list, tuple)) or not isinstance(to, (list, tuple)) or not set(grant) <= set(to):
            raise InvalidConversationInput("granted Agents must be addressees of the message")
        if PARTY_OWNER_REF in grant:
            raise InvalidConversationInput("the Owner is never granted an Agent turn")
        # P052 r02 (Defect A): ``owner`` may be a structured addressee (``@Owner``) but is never resolved, joined or
        # dispatched as an Agent.  An Owner-only message stores once and grants nothing.
        agent_to = [ref for ref in to if ref != PARTY_OWNER_REF]
        to_owner = len(agent_to) != len(to)
        if not agent_to and not to_owner:
            raise InvalidConversationInput("Choose at least one addressee")

        def write(session: Session) -> dict:
            room = self._room(session, project_id, task_id, create=True, actor=actor)  # type: ignore[arg-type]
            owner = self._owner_participant(session, room.conversation_id)
            targets = self._resolve_targets(session, room, agent_to) if agent_to else []
            to_refs = [a for a, _ in targets] + ([PARTY_OWNER_REF] if to_owner else [])  # type: ignore[union-attr]
            existing = session.scalars(select(ConversationMessage).where(
                ConversationMessage.conversation_id == room.conversation_id,
                ConversationMessage.client_key == key)).first()
            if existing is not None:
                view = self._message_view(session, existing)
                if (existing.body != text or existing.sender_participant_id != owner.participant_id
                        or sorted(view["to"]) != sorted(to_refs)):
                    raise ConversationConflict(
                        "This message key was already used for different content; reload the conversation")
                rnd = session.scalars(select(GroupRound).where(
                    GroupRound.authorizing_message_id == existing.message_id)).first()
                envelope = session.get(MessageEnvelope, existing.message_id)
                if rnd is None and envelope is not None and envelope.round_id is not None:
                    rnd = session.get(GroupRound, envelope.round_id)  # a direct request attached to a group round
                return {"message": view, "replayed": True, "round": None if rnd is None else self._round_view(session, rnd)}
            ordinal = self._max_ordinal(session, room.conversation_id) + 1
            if targets:  # an Owner-only note changes no roster state
                self._apply_addressing(session, room, targets, actor, ordinal - 1)
            message = ConversationMessage(
                message_id=str(uuid.uuid4()), conversation_id=room.conversation_id,
                sender_participant_id=owner.participant_id, ordinal=ordinal, body=text, client_key=key,
                recipient=None, created_at=_now())
            session.add(message)
            session.flush()
            envelope = MessageEnvelope(message_id=message.message_id, reply_to_message_id=None,
                                       round_id=None, turn_id=None, sender_agent_id=None)
            session.add(envelope)
            self._write_parties(session, message.message_id, room.conversation_id, to_refs)
            session.flush()
            rnd = None
            granted = [a for a in to_refs if a in set(grant)]  # type: ignore[arg-type]
            self._note(session, "owner_message_committed", room.conversation_id, message_id=message.message_id,
                       ordinal=ordinal, sender_kind="owner", to=to_refs, route="room",
                       delivery_mode="owner_note" if not granted else "direct" if len(granted) == 1 else "group",
                       body=text)
            current = self._current_round(session, room.conversation_id)
            if len(granted) == 1 and self._is_active_group(session, current):
                # P047 r02 (R-15.24): a 1:1 request never closes a running group discussion.
                rnd = self._attach_direct(session, current, granted[0], envelope)  # type: ignore[arg-type]
                self._note(session, "round_opened", room.conversation_id, round_id=rnd.round_id, round_no=rnd.round_no,
                           decision="direct_attached_to_running_group", authorizing_message_id=message.message_id,
                           grants=self._grants_view(session, rnd))
            elif granted:
                rnd = self._open_round(session, room, granted, actor, message.message_id,
                                       1 if len(granted) == 1 else self._setting(session, SETTING_GROUP_QUOTA),
                                       superseded_code="superseded_by_owner_message")
            return {"message": self._message_view(session, message, to_room=False), "replayed": False,
                    "round": None if rnd is None else self._round_view(session, rnd)}
        return self._tx(write)

    @staticmethod
    def _is_active_group(session: Session, rnd: GroupRound | None) -> bool:
        """A GROUP round (two or more granted Agents) that is still running: ``open`` (queued, reserved or in-flight
        work, or a further pass may still be earned).  ``awaiting_owner`` is a finished discussion."""
        if rnd is None or rnd.state != ROUND_OPEN:
            return False
        grants = session.scalar(select(func.count()).select_from(GroupRoundGrant).where(
            GroupRoundGrant.round_id == rnd.round_id)) or 0
        return grants >= 2

    def _attach_direct(self, session: Session, rnd: GroupRound, agent_id: str, envelope: MessageEnvelope) -> GroupRound:
        """P047 r02: an Owner -> one Agent Send while a group round is running.

        Smallest durable representation inside the single active round (no schema change):
        * the Owner message's envelope records ``round_id`` = the group round (marks it as a direct request there);
        * the Agent's grant gets exactly ONE extra slot (``quota + 1``; a new member of the room gets a 1-slot grant), so
          its remaining group allowance is unchanged and the group N is NOT reset;
        * one direct turn is queued at the END of the round's current pass.  Nothing is cancelled: queued, reserved,
          in-flight and UNKNOWN group turns keep their state; the room still dispatches one turn at a time.
        * ordering: earlier-queued turns go first.  A turn of the same Agent queued BEFORE this message has its input
          snapshot capped just before it (``_direct_cap``), so the direct turn answers the direct question and the
          group turn answers the group input: two answers, never one turn swallowing the other and an empty phantom.
        """
        grant = session.get(GroupRoundGrant, (rnd.round_id, agent_id))
        if grant is None:
            session.add(GroupRoundGrant(round_id=rnd.round_id, agent_id=agent_id, quota=1, published_count=0,
                                        failed=False))
        else:
            grant.quota += 1
            grant.failed = False  # an explicit new Owner request re-authorizes the Agent for this one reply
        last = session.scalar(select(func.max(GroupTurn.position)).where(
            GroupTurn.round_id == rnd.round_id, GroupTurn.pass_no == rnd.current_pass))
        session.add(GroupTurn(turn_id=str(uuid.uuid4()), round_id=rnd.round_id, conversation_id=rnd.conversation_id,
                              agent_id=agent_id, pass_no=rnd.current_pass, position=0 if last is None else last + 1,
                              attempt=1, state=TURN_QUEUED, correlation_id=str(uuid.uuid4()), created_at=_now(),
                              updated_at=_now()))
        envelope.round_id = rnd.round_id
        session.flush()
        return rnd

    def _direct_cap(self, session: Session, turn: GroupTurn, after: int) -> int | None:
        """Last ordinal this turn may consume: just before the first Owner direct request to this Agent attached to the
        same round AFTER the turn was queued (a retry keeps its original turn's queue time).  None = no cap."""
        origin = session.get(GroupTurn, turn.retry_of) if turn.retry_of else turn
        queued_at = (origin or turn).created_at
        rows = session.execute(select(ConversationMessage.ordinal, ConversationMessage.created_at).join(
            MessageEnvelope, MessageEnvelope.message_id == ConversationMessage.message_id).join(
            MessageParty, MessageParty.message_id == ConversationMessage.message_id).where(
            ConversationMessage.conversation_id == turn.conversation_id, ConversationMessage.ordinal > after,
            MessageEnvelope.round_id == turn.round_id, MessageEnvelope.sender_agent_id.is_(None),
            MessageParty.role == "to", MessageParty.party_ref == turn.agent_id).order_by(
            ConversationMessage.ordinal)).all()
        for ordinal, created_at in rows:
            if created_at > queued_at:
                return ordinal - 1
        return None

    def _open_round(self, session: Session, room: Conversation, agent_ids: list[str], actor: ActorContext,
                    authorizing_message_id: str | None, quota: int, *, superseded_code: str) -> GroupRound:
        """Close the current round (cancelling ONLY its queued, never-submitted turns) and open a new one."""
        current = self._current_round(session, room.conversation_id)
        if current is not None:
            self._note(session, "round_closed", room.conversation_id, round_id=current.round_id,
                       round_no=current.round_no, from_state=current.state, to_state=ROUND_CLOSED, reason=superseded_code)
            current.state, current.closed_at = ROUND_CLOSED, _now()
            for turn in session.scalars(select(GroupTurn).where(GroupTurn.round_id == current.round_id,
                                                                GroupTurn.state == TURN_QUEUED)):
                turn.state, turn.error_code, turn.updated_at = TURN_CANCELLED, superseded_code, _now()
                self._note(session, "turn_cancelled", room.conversation_id, turn_id=turn.turn_id,
                           round_id=current.round_id, agent_id=turn.agent_id, reason=superseded_code)
            session.flush()
        number = (session.scalar(select(func.max(GroupRound.round_no)).where(
            GroupRound.conversation_id == room.conversation_id)) or 0) + 1
        rnd = GroupRound(round_id=str(uuid.uuid4()), conversation_id=room.conversation_id, round_no=number,
                         state=ROUND_OPEN, per_agent_quota=quota, current_pass=0,
                         authorizing_message_id=authorizing_message_id, authorized_by=actor.actor_id,
                         authorized_by_assurance=actor.assurance, created_at=_now())
        session.add(rnd)
        session.flush()
        for agent_id in agent_ids:
            session.add(GroupRoundGrant(round_id=rnd.round_id, agent_id=agent_id, quota=quota, published_count=0,
                                        failed=False))
        session.flush()
        self._note(session, "round_opened", room.conversation_id, round_id=rnd.round_id, round_no=rnd.round_no,
                   decision="owner_message", authorizing_message_id=authorizing_message_id,
                   grants=self._grants_view(session, rnd))
        self._create_pass(session, rnd, 0, list(agent_ids))
        session.flush()
        return rnd

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
                self._require_current_owner_message(session, room, authorizing_message_id)
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
            # A first-time member joins at the CURRENT room boundary; membership is never moved back in time (an Agent
            # already invited by an addressed Owner message keeps its real join point).
            self._apply_addressing(session, room, targets, actor, self._max_ordinal(session, room.conversation_id))
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
            self._note(session, "round_opened", room.conversation_id, round_id=rnd.round_id, round_no=number,
                       decision="owner_renew" if renew else "owner_authorize",
                       authorizing_message_id=authorizing_message_id, grants=self._grants_view(session, rnd))
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

    def _require_current_owner_message(self, session: Session, room: Conversation, message_id: object) -> None:
        """v1.0 rule: a round may cite only the room's latest Owner message.  A stale, foreign-room or Agent-authored
        id is refused, so the field can neither move membership back in time nor shrink the catch-up boundary."""
        msg = session.get(ConversationMessage, message_id) if isinstance(message_id, str) else None
        if msg is None or msg.conversation_id != room.conversation_id:
            raise InvalidConversationInput("authorizing message is not in this conversation")
        latest = session.scalar(select(func.max(ConversationMessage.ordinal)).join(
            ConversationParticipant,
            ConversationParticipant.participant_id == ConversationMessage.sender_participant_id).where(
            ConversationMessage.conversation_id == room.conversation_id,
            ConversationParticipant.kind == PARTICIPANT_OWNER))
        sender = session.get(ConversationParticipant, msg.sender_participant_id)
        if sender is None or sender.kind != PARTICIPANT_OWNER or msg.ordinal != latest:
            raise InvalidConversationInput("authorizing message must be the latest Owner message of this conversation")

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
            turn_id = str(uuid.uuid4())
            session.add(GroupTurn(turn_id=turn_id, round_id=rnd.round_id, conversation_id=rnd.conversation_id,
                                  agent_id=agent_id, pass_no=pass_no, position=position, attempt=1, state=TURN_QUEUED,
                                  correlation_id=str(uuid.uuid4()), created_at=_now(), updated_at=_now()))
            self._note(session, "turn_queued", rnd.conversation_id, turn_id=turn_id, round_id=rnd.round_id,
                       round_no=rnd.round_no, agent_id=agent_id, pass_no=pass_no, position=position, attempt=1)
        rnd.current_pass = pass_no
        return order

    def _eligible_for_next_pass(self, session: Session, rnd: GroupRound) -> list[str]:
        result = []
        for grant in session.scalars(select(GroupRoundGrant).where(GroupRoundGrant.round_id == rnd.round_id)):
            member = self._member(session, rnd.conversation_id, grant.agent_id)
            room = session.get(Conversation, rnd.conversation_id)
            if (grant.published_count < grant.quota and not grant.failed and member is not None
                    and member.state != MEMBER_RELEASED and self._assignment(session, room, grant.agent_id) is not None
                    and self._addressed_by_peer(session, rnd, grant.agent_id)):
                result.append(grant.agent_id)
        return result

    def _addressed_by_peer(self, session: Session, rnd: GroupRound, agent_id: str) -> bool:
        """P047: a later pass is earned only by real input — a peer Agent's published message that explicitly names this
        Agent in ``to`` after the last snapshot this Agent's turn in this round already consumed (or after the round's
        authorizing message).  No new addressed peer turn = no new generation, whatever quota remains."""
        after = session.scalar(select(func.max(GroupTurn.input_through_ordinal)).where(
            GroupTurn.round_id == rnd.round_id, GroupTurn.agent_id == agent_id))
        if after is None:
            after = self._round_floor(session, rnd)
        hit = session.scalar(select(ConversationMessage.message_id).join(
            MessageEnvelope, MessageEnvelope.message_id == ConversationMessage.message_id).join(
            MessageParty, MessageParty.message_id == ConversationMessage.message_id).where(
            ConversationMessage.conversation_id == rnd.conversation_id, ConversationMessage.ordinal > after,
            MessageEnvelope.sender_agent_id.is_not(None), MessageEnvelope.sender_agent_id != agent_id,
            func.length(ConversationMessage.body) <= GROUP_REPLY_CONTINUE_MAX_CHARS,
            MessageParty.role == "to", MessageParty.party_ref == agent_id).limit(1))
        return hit is not None

    @staticmethod
    def _round_floor(session: Session, rnd: GroupRound) -> int:
        """Last ordinal that already existed when the round was authorized: its authorizing message and anything committed
        before the round row (P052: a bridged or continued round never re-reads addressing that predates it).  A clock
        anomaly can only make the floor higher, i.e. fewer Agent turns, never more."""
        after = 0
        if rnd.authorizing_message_id is not None:
            authorizing = session.get(ConversationMessage, rnd.authorizing_message_id)
            after = authorizing.ordinal if authorizing is not None else 0
        before = session.scalar(select(func.max(ConversationMessage.ordinal)).where(
            ConversationMessage.conversation_id == rnd.conversation_id,
            ConversationMessage.created_at <= rnd.created_at)) or 0
        return max(after, before)

    # ------------------------------------------------------------------ P052 invitation / quota awareness
    @staticmethod
    def _controls_block(session: Session, conversation_id: str) -> str | None:
        controls = session.get(ConversationDispatchControl, conversation_id)
        if controls is not None and controls.emergency_brake:
            return "emergency_brake"
        if controls is not None and controls.paused:
            return "paused"
        return None

    def _invite_problem(self, session: Session, room: Conversation, agent_id: str, *, inviter: str | None) -> str | None:
        """Why ``agent_id`` could NOT be given a real turn in this room now (None = it could).  Read-only; fail-closed
        when the transport seam is not composed."""
        if agent_id == PARTY_OWNER_REF or agent_id == inviter:
            return "not_an_agent"
        if self._assignment(session, room, agent_id) is None:
            return "unassigned"
        member = self._member(session, room.conversation_id, agent_id)
        if member is not None and member.state == MEMBER_RELEASED:
            return "released"
        if self.peer_problem is None:
            return "transport_unknown"
        return self.peer_problem(session, room, agent_id)

    def _join(self, session: Session, room: Conversation, agent_id: str, baseline: int, actor_id: str) -> None:
        """Bring an invited/answering Agent into the roster as Active.  A first-time member joins at ``baseline`` (no
        history backfill before it); a Passive member becomes Active.  Released members are never joined here."""
        member = self._member(session, room.conversation_id, agent_id)
        now = _now()
        if member is None:
            assignment = self._assignment(session, room, agent_id)
            session.add(ConversationMember(
                conversation_id=room.conversation_id, agent_id=agent_id, role=assignment.role,  # type: ignore[union-attr]
                state=MEMBER_ACTIVE, epoch=1, epoch_baseline_ordinal=max(0, baseline), release_offer_ack_missed=0,
                updated_by=actor_id, updated_at=now))
        elif member.state == MEMBER_PASSIVE:
            member.state, member.release_offer_ack_missed = MEMBER_ACTIVE, 0
            member.updated_by, member.updated_at = actor_id, now
        session.flush()

    def _maybe_invite(self, session: Session, turn: GroupTurn, refs: list[str], body: str) -> list[str]:
        """P052 B: the ONE Agent-initiated invitation of an Owner-authorized DIRECT request.  Runs inside ``publish``'s
        transaction, after the inviter's grant was debited.  Returns the invited Agent ids (possibly none)."""
        rnd = session.get(GroupRound, turn.round_id)
        if rnd is None or rnd.state != ROUND_OPEN:
            return []
        grants = list(session.scalars(select(GroupRoundGrant).where(GroupRoundGrant.round_id == rnd.round_id)))
        peers = [r for r in refs if r not in (PARTY_OWNER_REF, turn.agent_id)]
        if not peers:
            return []
        room = session.get(Conversation, turn.conversation_id)

        def declined(reason: str, problems: list | None = None) -> list[str]:
            self._note(session, "invitation_checked", turn.conversation_id, round_id=rnd.round_id,
                       round_no=rnd.round_no, turn_id=turn.turn_id, sender_agent_id=turn.agent_id, to=refs,
                       authorizing_message_id=rnd.authorizing_message_id, decision="declined", reason=reason,
                       declined=problems)
            return []
        if len(grants) != 1 or grants[0].agent_id != turn.agent_id:
            # not a direct request, or the one invitation was already used (peers already in the round are not invited)
            return declined("not_a_direct_request") if any(
                p not in {g.agent_id for g in grants} for p in peers) else []
        if len(body) > GROUP_REPLY_CONTINUE_MAX_CHARS:
            return declined("reply_too_long")
        if self._controls_block(session, turn.conversation_id) is not None:
            return declined(self._controls_block(session, turn.conversation_id))  # type: ignore[arg-type]
        problems = [(p, self._invite_problem(session, room, p, inviter=turn.agent_id)) for p in peers]
        invited = [p for p, problem in problems if problem is None]
        if not invited:
            return declined("peer_unavailable", problems)
        n = self._setting(session, SETTING_GROUP_QUOTA)
        rnd.per_agent_quota = n
        grants[0].quota = max(grants[0].quota, n)  # the inviter's published reply already counts as its first
        baseline = self._round_floor(session, rnd)
        if rnd.authorizing_message_id is not None:
            baseline = session.get(ConversationMessage, rnd.authorizing_message_id).ordinal - 1  # type: ignore[union-attr]
        for agent_id in invited:
            session.add(GroupRoundGrant(round_id=rnd.round_id, agent_id=agent_id, quota=n, published_count=0,
                                        failed=False))
            self._join(session, room, agent_id, baseline, rnd.authorized_by)  # type: ignore[arg-type]
        session.flush()
        self._note(session, "invitation_checked", turn.conversation_id, round_id=rnd.round_id, round_no=rnd.round_no,
                   turn_id=turn.turn_id, sender_agent_id=turn.agent_id, to=refs, decision="accepted",
                   authorizing_message_id=rnd.authorizing_message_id, invited=invited,
                   declined=[(p, problem) for p, problem in problems if problem is not None],
                   grants=self._grants_view(session, rnd))
        return invited

    @staticmethod
    def _grants_view(session: Session, rnd: GroupRound) -> dict:
        session.flush()
        return {g.agent_id: {"quota": g.quota, "published": g.published_count, "failed": g.failed}
                for g in session.scalars(select(GroupRoundGrant).where(GroupRoundGrant.round_id == rnd.round_id))}

    def _display_names(self, session: Session, room: Conversation) -> dict[str, str]:
        return {ref: names[0] for ref, names in self._room_candidates(session, room).items()}

    def _quota_snapshot(self, session: Session, rnd: GroupRound, agent_id: str) -> dict:
        """P052 C: trusted, advisory, room-wide quota state frozen immediately before this Agent's provider submit.
        ``remaining`` is BEFORE the current reply; the current Agent's ``remaining_after_this_reply`` already counts it."""
        room = session.get(Conversation, rnd.conversation_id)
        names = self._display_names(session, room)  # type: ignore[arg-type]
        blocked = self._controls_block(session, rnd.conversation_id)
        grants = list(session.scalars(select(GroupRoundGrant).where(
            GroupRoundGrant.round_id == rnd.round_id).order_by(GroupRoundGrant.agent_id)))
        you: dict = {}
        peers: list[dict] = []
        for grant in grants:
            remaining = max(0, grant.quota - grant.published_count)
            entry = {"agent_id": grant.agent_id, "display_name": names.get(grant.agent_id, grant.agent_id),
                     "quota": grant.quota, "published": grant.published_count, "remaining": remaining}
            if grant.agent_id == agent_id:
                you = {**entry, "remaining_after_this_reply": max(0, remaining - 1)}
                continue
            why = "quota_exhausted" if remaining <= 0 else "excluded_after_failures" if grant.failed \
                else self._invite_problem(session, room, grant.agent_id, inviter=agent_id) or blocked  # type: ignore[arg-type]
            peers.append({**entry, "can_reply_after_your_message": why is None, "why_not": why})
        granted = {g.agent_id for g in grants}
        invitation = None
        direct = len(grants) == 1 and grants[0].agent_id == agent_id and rnd.state == ROUND_OPEN
        for ref in names:
            if ref in granted or ref == PARTY_OWNER_REF:
                continue
            problem = self._invite_problem(session, room, ref, inviter=agent_id)  # type: ignore[arg-type]
            if direct and problem is None and blocked is None:
                if invitation is None:
                    n = self._setting(session, SETTING_GROUP_QUOTA)
                    invitation = {"per_agent_quota": n, "agents": [],
                                  "your_remaining_after_this_reply_if_you_invite":
                                      max(0, max(you.get("quota", 1), n) - you.get("published", 0) - 1)}
                invitation["agents"].append({"agent_id": ref, "display_name": names[ref],
                                             "remaining_if_invited": invitation["per_agent_quota"]})
            else:
                peers.append({"agent_id": ref, "display_name": names[ref], "quota": 0, "published": 0, "remaining": 0,
                              "can_reply_after_your_message": False,
                              "why_not": "not_in_this_discussion" if problem is None else problem})
        before = sum(max(0, g.quota - g.published_count) for g in grants)
        return {"you": you, "peers": peers, "remaining_total_before_this_reply": before,
                "remaining_total_after_this_reply": max(0, before - 1), "invitation": invitation,
                "reply_guide_chars": GROUP_REPLY_GUIDE_CHARS, "continue_max_chars": GROUP_REPLY_CONTINUE_MAX_CHARS}

    def direct_quota_snapshot(self, session: Session, room: Conversation, agent_id: str) -> dict | None:
        """P052 C3: the equivalent truthful pre-turn information for a legacy DIRECT Architect delivery (no Core turn).
        None when no other Agent could take part (the legacy prompt then stays exactly the Owner's text)."""
        names = self._display_names(session, room)
        blocked = self._controls_block(session, room.conversation_id)
        current = self._current_round(session, room.conversation_id)
        if self._is_active_group(session, current):
            snap = self._quota_snapshot(session, current, agent_id)  # type: ignore[arg-type]
            snap["mode"] = "running_discussion"
            snap["this_reply_counts"] = False
            if snap["you"]:
                snap["you"]["remaining_after_this_reply"] = snap["you"]["remaining"]
            snap["remaining_total_after_this_reply"] = snap["remaining_total_before_this_reply"]
            return snap
        agents = [ref for ref in names if ref not in (PARTY_OWNER_REF, agent_id)
                  and self._invite_problem(session, room, ref, inviter=agent_id) is None]
        if not agents:
            return None
        n = self._setting(session, SETTING_GROUP_QUOTA)
        return {"mode": "invitation", "this_reply_counts": True, "blocked": blocked,
                "invitation": None if blocked else {
                    "per_agent_quota": n, "your_remaining_after_this_reply_if_you_invite": max(0, n - 1),
                    "agents": [{"agent_id": a, "display_name": names[a], "remaining_if_invited": n} for a in agents]},
                "reply_guide_chars": GROUP_REPLY_GUIDE_CHARS, "continue_max_chars": GROUP_REPLY_CONTINUE_MAX_CHARS}

    def _latest_owner_message(self, session: Session, conversation_id: str) -> ConversationMessage | None:
        return session.scalars(select(ConversationMessage).join(
            ConversationParticipant,
            ConversationParticipant.participant_id == ConversationMessage.sender_participant_id).where(
            ConversationMessage.conversation_id == conversation_id,
            ConversationParticipant.kind == PARTICIPANT_OWNER).order_by(ConversationMessage.ordinal.desc()).limit(1)).first()

    def bridge_direct_reply(self, session: Session, *, owner_message: ConversationMessage,
                            reply: ConversationMessage, agent_id: str, actor_id: str, actor_assurance: str) -> dict | None:
        """P052 B1(a): runs INSIDE the transaction that commits an authenticated legacy direct Architect reply.

        * No in-scope peer @mentioned: nothing changes (legacy shape, no envelope).
        * Otherwise the reply gets its structured ``to[]`` (envelope + parties).  When the Owner message it answers is
          still the room's latest Owner message, no Owner-authorized round or turn is unfinished (P052 r02: neither a
          direct nor a group round is ever closed by an Agent), dispatch is not paused/braked and the
          reply is within the size rule, ONE round is opened with that Owner message as its durable provenance: every
          participant gets N, the Architect's reply counts as its first published one (N-1 left), and pass 0 queues
          only the invited eligible peers.  A running discussion is never closed or re-granted by an Agent.
        Returns ``{"conversation_id", "round_id" | None, "invited"}`` or None.  Never calls a provider."""
        room = session.get(Conversation, reply.conversation_id)
        found = self.resolve_mentions(session, room, reply.body, exclude=agent_id)  # type: ignore[arg-type]
        refs = list(found.refs)
        peers = [r for r in refs if r != PARTY_OWNER_REF]
        self._note(session, "mentions_resolved", reply.conversation_id, message_id=reply.message_id,
                   ordinal=reply.ordinal, sender_agent_id=agent_id, sender_kind="architect", to=refs,
                   reply_to_id=owner_message.message_id, reply_to_ordinal=owner_message.ordinal,
                   unknown_names=len(found.unknown) + len(found.ambiguous), route="architect_direct")
        if not peers:
            self._note(session, "invitation_checked", reply.conversation_id, message_id=reply.message_id,
                       sender_agent_id=agent_id, considered_message_id=owner_message.message_id, decision="none",
                       reason="no_peer_mention")
            return None
        current = self._current_round(session, room.conversation_id)  # type: ignore[union-attr]
        latest = self._latest_owner_message(session, room.conversation_id)  # type: ignore[union-attr]
        invited: list[str] = []
        not_invited = None
        if latest is None or latest.message_id != owner_message.message_id:
            not_invited = "newer_owner_message"
        elif self._unfinished_work(session, room.conversation_id, current):  # type: ignore[union-attr]
            # P052 r02 (Defect B): an Agent's invitation never closes, supersedes or cancels Owner-authorized work that
            # is still unfinished in this room (an open direct OR group round; any queued / reserved /
            # submitted / accepted / captured / UNKNOWN turn).  The reply and its to[] are kept; no round is opened.
            not_invited = "pending_owner_request"
        elif self._controls_block(session, room.conversation_id) is not None:  # type: ignore[union-attr]
            not_invited = self._controls_block(session, room.conversation_id)  # type: ignore[union-attr]
        elif len(reply.body) > GROUP_REPLY_CONTINUE_MAX_CHARS:
            not_invited = "reply_too_long"
        problems: list = []
        if not_invited is None:
            problems = [(p, self._invite_problem(session, room, p, inviter=agent_id)) for p in peers]  # type: ignore[arg-type]
            invited = [p for p, problem in problems if problem is None]
            if not invited:
                not_invited = "peer_unavailable"
        rnd = None
        closed = None
        if invited:
            if current is not None:  # finished (awaiting_owner) only: nothing queued or holding can be lost here
                closed = current
                self._note(session, "round_closed", room.conversation_id, round_id=current.round_id,  # type: ignore[union-attr]
                           round_no=current.round_no, from_state=current.state, to_state=ROUND_CLOSED,
                           reason="agent_invitation_after_finished_round")
                current.state, current.closed_at = ROUND_CLOSED, _now()
                session.flush()
            n = self._setting(session, SETTING_GROUP_QUOTA)
            number = (session.scalar(select(func.max(GroupRound.round_no)).where(
                GroupRound.conversation_id == room.conversation_id)) or 0) + 1  # type: ignore[union-attr]
            rnd = GroupRound(round_id=str(uuid.uuid4()), conversation_id=room.conversation_id, round_no=number,  # type: ignore[union-attr]
                             state=ROUND_OPEN, per_agent_quota=n, current_pass=0,
                             authorizing_message_id=owner_message.message_id, authorized_by=actor_id,
                             authorized_by_assurance=actor_assurance, created_at=_now())
            session.add(rnd)
            session.flush()
            self._join(session, room, agent_id, owner_message.ordinal, actor_id)  # type: ignore[arg-type]
            session.add(GroupRoundGrant(round_id=rnd.round_id, agent_id=agent_id, quota=n, published_count=1,
                                        failed=False))
            for peer in invited:
                self._join(session, room, peer, owner_message.ordinal - 1, actor_id)  # type: ignore[arg-type]
                session.add(GroupRoundGrant(round_id=rnd.round_id, agent_id=peer, quota=n, published_count=0,
                                            failed=False))
            session.flush()
            self._note(session, "round_opened", room.conversation_id, round_id=rnd.round_id, round_no=number,  # type: ignore[union-attr]
                       decision="architect_direct_invitation", authorizing_message_id=owner_message.message_id,
                       closed_round_id=None if closed is None else closed.round_id,
                       grants=self._grants_view(session, rnd))
            self._create_pass(session, rnd, 0, invited)
        self._note(session, "invitation_checked", room.conversation_id, message_id=reply.message_id,  # type: ignore[union-attr]
                   sender_agent_id=agent_id, to=refs, considered_message_id=owner_message.message_id,
                   latest_owner_message_id=None if latest is None else latest.message_id,
                   decision="accepted" if invited else "declined", reason=not_invited, invited=invited,
                   declined=[(p, problem) for p, problem in problems if problem is not None],
                   round_id=None if rnd is None else rnd.round_id, round_no=None if rnd is None else rnd.round_no,
                   closed_round_id=None if closed is None else closed.round_id,
                   this_reply_counts=bool(invited))
        session.add(MessageEnvelope(message_id=reply.message_id, reply_to_message_id=owner_message.message_id,
                                    round_id=None if rnd is None else rnd.round_id, turn_id=None,
                                    sender_agent_id=agent_id))
        self._write_parties(session, reply.message_id, room.conversation_id, refs)  # type: ignore[union-attr]
        session.flush()
        return {"conversation_id": room.conversation_id, "round_id": None if rnd is None else rnd.round_id,  # type: ignore[union-attr]
                "invited": invited, "not_invited": not_invited}

    @staticmethod
    def _unfinished_work(session: Session, conversation_id: str, current: GroupRound | None) -> bool:
        """P052 r02: True while Owner-authorized work in this room may still produce a reply: the current round is
        ``open`` (direct or group), or any turn is not yet terminal.  ``awaiting_owner`` with no live turn is finished."""
        if current is not None and current.state == ROUND_OPEN:
            return True
        return session.scalars(select(GroupTurn.turn_id).where(
            GroupTurn.conversation_id == conversation_id,
            GroupTurn.state.in_((TURN_QUEUED,) + TURN_HOLDING_STATES))).first() is not None

    def direct_catchup(self, session: Session, room: Conversation, agent_id: str, through: int) -> dict | None:
        """P052 B6: room events a member Agent's native context has not applied yet, for a legacy direct turn (read-only).
        Messages that were themselves handed to the Architect directly are not repeated.  None for a non-member."""
        from .models import ConversationDelivery  # local: the legacy delivery table is not otherwise used here
        member = self._member(session, room.conversation_id, agent_id)
        lane = self._active_lane(session, room.conversation_id, agent_id)
        if member is None or member.state == MEMBER_RELEASED or lane is None:
            return None
        if lane.root_binding_version != self._project(session, room.project_id).current_binding.version:
            return None
        after = self._considered_through(session, member, lane)
        events = [e for e in self._events(session, member, after, through)
                  if session.get(ConversationDelivery, e.message_id) is None] if through > after else []
        return {"after": after, "through": through, "events": events, "epoch": member.epoch, "lane_id": lane.lane_id,
                "omitted_ranges": self._gaps(session, member)}

    @staticmethod
    def advance_direct_cursor(session: Session, conversation_id: str, agent_id: str, epoch: int, lane_id: str,
                              through: int) -> None:
        """After a provider-ANSWERED legacy direct turn that carried the catch-up through ``through``: advance only that
        lane/epoch cursor (inside the reply's commit transaction).  A changed epoch or lane is left untouched."""
        member = session.get(ConversationMember, (conversation_id, agent_id))
        lane = session.get(AgentNativeLane, lane_id)
        if member is None or member.epoch != epoch or lane is None or lane.state != LANE_ACTIVE:
            return
        key = (conversation_id, agent_id, epoch, lane_id)
        cursor = session.get(InboundCursor, key)
        if cursor is None:
            cursor = InboundCursor(conversation_id=conversation_id, agent_id=agent_id, epoch=epoch, lane_id=lane_id,
                                   applied_through_ordinal=0, updated_at=_now())
            session.add(cursor)
        cursor.applied_through_ordinal = max(cursor.applied_through_ordinal, through)
        cursor.updated_at = _now()

    INVITATION_TAG = "|invitation:"

    @classmethod
    def invitation_tag(cls, bridged: dict | None) -> str | None:
        """P052 r03: the durable, content-free outcome of a legacy direct reply's invitation check (recorded on the Owner
        message's delivery row inside the same commit; no schema change)."""
        if bridged is None:
            return None
        return cls.INVITATION_TAG + ("opened" if bridged.get("round_id") else (bridged.get("not_invited") or "none"))

    def invitation_outcome(self, session: Session, reply_message_id: str) -> dict | None:
        """The recorded invitation outcome of one legacy direct Architect reply, or None (not a bridged reply)."""
        from .models import ConversationDelivery  # local: the legacy delivery table is not otherwise used here
        envelope = session.get(MessageEnvelope, reply_message_id)
        if envelope is None or envelope.turn_id is not None or envelope.reply_to_message_id is None:
            return None
        delivery = session.get(ConversationDelivery, envelope.reply_to_message_id)
        tags = (delivery.context_categories or "") if delivery is not None else ""
        code = tags.split(self.INVITATION_TAG, 1)[1] if self.INVITATION_TAG in tags else None
        rnd = session.get(GroupRound, envelope.round_id) if envelope.round_id else None
        return {"decision": "accepted" if rnd is not None else "declined" if code else "unrecorded",
                "reason": None if rnd is not None or code in (None, "opened") else code,
                "round_id": None if rnd is None else rnd.round_id, "round_no": None if rnd is None else rnd.round_no,
                "owner_message_id": envelope.reply_to_message_id}

    def latest_invitation(self, session: Session, conversation_id: str) -> dict | None:
        """Status projection: the invitation outcome of the room's latest Agent message when it is a bridged direct
        Architect reply addressing peers (truthful without the diagnostics switch; never a body)."""
        row = session.execute(select(ConversationMessage.message_id, ConversationMessage.ordinal).join(
            MessageEnvelope, MessageEnvelope.message_id == ConversationMessage.message_id).where(
            ConversationMessage.conversation_id == conversation_id,
            MessageEnvelope.sender_agent_id.is_not(None)).order_by(ConversationMessage.ordinal.desc()).limit(1)).first()
        if row is None:
            return None
        outcome = self.invitation_outcome(session, row[0])
        if outcome is None:
            return None
        return {"message_id": row[0], "ordinal": row[1], **outcome}

    def _unanswered(self, session: Session, room: Conversation, rnd: GroupRound | None) -> list[dict]:
        """P052 D2: explicit Agent addressees of the room's latest Agent message that did not (yet) get a turn, with the
        honest reason.  Read-only; an Agent that is queued or holding the room is pending, not unanswered."""
        row = session.execute(select(ConversationMessage, MessageEnvelope.sender_agent_id).join(
            MessageEnvelope, MessageEnvelope.message_id == ConversationMessage.message_id).where(
            ConversationMessage.conversation_id == room.conversation_id,
            MessageEnvelope.sender_agent_id.is_not(None)).order_by(ConversationMessage.ordinal.desc()).limit(1)).first()
        if row is None:
            return []
        message, sender = row
        targets = [p.party_ref for p in session.scalars(select(MessageParty).where(
            MessageParty.message_id == message.message_id, MessageParty.role == "to"))
            if p.party_ref not in (PARTY_OWNER_REF, sender)]
        blocked = self._controls_block(session, room.conversation_id)
        invitation = self.invitation_outcome(session, message.message_id)
        declined = invitation["reason"] if invitation is not None and invitation["decision"] == "declined" else None
        result = []
        for agent_id in targets:
            answered = session.scalar(select(ConversationMessage.message_id).join(
                MessageEnvelope, MessageEnvelope.message_id == ConversationMessage.message_id).where(
                ConversationMessage.conversation_id == room.conversation_id,
                ConversationMessage.ordinal > message.ordinal, MessageEnvelope.sender_agent_id == agent_id).limit(1))
            if answered is not None:
                continue
            pending = rnd is not None and session.scalar(select(GroupTurn.turn_id).where(
                GroupTurn.round_id == rnd.round_id, GroupTurn.agent_id == agent_id,
                GroupTurn.state.in_((TURN_QUEUED, *TURN_HOLDING_STATES))).limit(1)) is not None
            grant = session.get(GroupRoundGrant, (rnd.round_id, agent_id)) if rnd is not None else None
            if blocked is not None:
                reason = blocked
            elif pending:
                continue
            elif len(message.body) > GROUP_REPLY_CONTINUE_MAX_CHARS:
                reason = "reply_too_long"
            elif grant is None and declined is not None and declined != "peer_unavailable":
                reason = declined  # P052 r03: the recorded reason the Architect's invitation was refused at commit time
            elif grant is None:
                reason = self._invite_problem(session, room, agent_id, inviter=sender) or "not_in_this_discussion"
            elif grant.failed:
                reason = "excluded_after_failures"
            elif grant.published_count >= grant.quota:
                reason = "quota_exhausted"
            else:
                reason = self._invite_problem(session, room, agent_id, inviter=sender)
                if reason is None:
                    if rnd is not None and rnd.state == ROUND_OPEN:
                        continue  # the next claim will build its pass
                    reason = "awaiting_owner"
            result.append({"agent_id": agent_id, "from_agent_id": sender, "message_id": message.message_id,
                           "ordinal": message.ordinal, "reason": reason})
        return result

    def continue_round(self, actor: ActorContext, project_id: object, *, task_id: object = None) -> dict:
        """P052 D2: the explicit Owner continuation of a finished discussion whose last Agent message addressed a peer
        that had no reply left.  Opens a fresh round (same participants, N each, provenance = the room's latest Owner
        message and this Owner actor) whose first pass is ONLY those addressed Agents: the saved message reaches them
        exactly once through their cursor.  Never available to an Agent; an old round never renews itself."""
        self._authorize(actor, CONVERSATION_GROUP_CONTROL, project_id=project_id, task_id=task_id)

        def write(session: Session) -> dict:
            room = self._require_room(session, project_id, task_id)  # type: ignore[arg-type]
            current = self._current_round(session, room.conversation_id)
            if current is None or current.state != ROUND_AWAITING_OWNER:
                raise ConversationConflict("There is no finished discussion to continue")
            waiting = [u["agent_id"] for u in self._unanswered(session, room, current)
                       if u["reason"] == "quota_exhausted"]
            waiting = [a for a in waiting if self._invite_problem(session, room, a, inviter=None) is None]
            if not waiting:
                raise ConversationConflict("No addressed Agent is waiting for a reply; write a new message instead")
            latest = self._latest_owner_message(session, room.conversation_id)
            participants = [g.agent_id for g in session.scalars(select(GroupRoundGrant).where(
                GroupRoundGrant.round_id == current.round_id).order_by(GroupRoundGrant.agent_id))
                if self._invite_problem(session, room, g.agent_id, inviter=None) is None]
            n = self._setting(session, SETTING_GROUP_QUOTA)
            self._note(session, "round_closed", room.conversation_id, round_id=current.round_id,
                       round_no=current.round_no, from_state=current.state, to_state=ROUND_CLOSED,
                       reason="owner_continue")
            current.state, current.closed_at = ROUND_CLOSED, _now()
            session.flush()
            number = (session.scalar(select(func.max(GroupRound.round_no)).where(
                GroupRound.conversation_id == room.conversation_id)) or 0) + 1
            rnd = GroupRound(round_id=str(uuid.uuid4()), conversation_id=room.conversation_id, round_no=number,
                             state=ROUND_OPEN, per_agent_quota=n, current_pass=0,
                             authorizing_message_id=None if latest is None else latest.message_id,
                             authorized_by=actor.actor_id, authorized_by_assurance=actor.assurance, created_at=_now())
            session.add(rnd)
            session.flush()
            for agent_id in participants:
                session.add(GroupRoundGrant(round_id=rnd.round_id, agent_id=agent_id, quota=n, published_count=0,
                                            failed=False))
            self._note(session, "round_opened", room.conversation_id, round_id=rnd.round_id, round_no=number,
                       decision="owner_continue", authorizing_message_id=rnd.authorizing_message_id,
                       grants=self._grants_view(session, rnd))
            self._create_pass(session, rnd, 0, waiting)
            session.flush()
            return self._round_view(session, rnd)
        return self._tx(write)

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
                    cancel = None
                    if member is None or member.state == MEMBER_RELEASED:
                        cancel = "member_released"
                    elif self._assignment(session, room, turn.agent_id) is None:
                        cancel = "assignment_changed"
                    elif grant is None or grant.published_count >= grant.quota or grant.failed:
                        cancel = "quota_exhausted"
                    if cancel is not None:
                        turn.state, turn.error_code = TURN_CANCELLED, cancel
                        self._note(session, "turn_cancelled", conversation_id, turn_id=turn.turn_id,
                                   round_id=rnd.round_id, agent_id=turn.agent_id, reason=cancel)
                        continue
                    if turn.catchup_choice is None:
                        need = self._catchup_need(session, turn)
                        if need is not None:
                            return ClaimResult("catchup_choice_required", turn.turn_id, turn.agent_id, need)
                    lane = self._ensure_lane(session, room, turn.agent_id)
                    turn.lane_id, turn.state, turn.updated_at = lane.lane_id, TURN_RESERVED, _now()
                    session.flush()
                    self._note(session, "turn_reserved", conversation_id, turn_id=turn.turn_id, round_id=rnd.round_id,
                               round_no=rnd.round_no, agent_id=turn.agent_id, pass_no=turn.pass_no,
                               position=turn.position, attempt=turn.attempt, lane_id=lane.lane_id,
                               grants=self._grants_view(session, rnd))
                    return ClaimResult("reserved", turn.turn_id, turn.agent_id,
                                       {"pass": turn.pass_no, "position": turn.position, "attempt": turn.attempt})
                eligible = self._eligible_for_next_pass(session, rnd)
                if not eligible:
                    rnd.state = ROUND_AWAITING_OWNER
                    self._note(session, "round_awaiting_owner", conversation_id, round_id=rnd.round_id,
                               round_no=rnd.round_no, from_state=ROUND_OPEN, to_state=ROUND_AWAITING_OWNER,
                               reason="no_addressed_peer_with_quota", grants=self._grants_view(session, rnd))
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
        audience, so they keep receiving group messages addressed to others).  Messages without an envelope (legacy API)
        are room-wide; pre-join ones are excluded by the member's baseline (its real join point)."""
        rows = session.execute(select(ConversationMessage, ConversationParticipant).join(
            ConversationParticipant,
            ConversationParticipant.participant_id == ConversationMessage.sender_participant_id).where(
            ConversationMessage.conversation_id == member.conversation_id, ConversationMessage.ordinal > after,
            ConversationMessage.ordinal <= through).order_by(ConversationMessage.ordinal)).all()
        events = []
        for message, sender in rows:
            if sender.agent_id == member.agent_id:
                continue  # the Agent's own published replies are never fed back as peer input
            view = self._message_view(session, message)
            if session.get(MessageEnvelope, message.message_id) is not None \
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
            cap = self._direct_cap(session, turn, after)
            if cap is not None:
                through = max(after, min(through, cap))  # the later direct request is the direct turn's input
            events = self._events(session, member, after, through)  # type: ignore[arg-type]
            grant = session.get(GroupRoundGrant, (turn.round_id, turn.agent_id))
            rnd = session.get(GroupRound, turn.round_id)
            payload = TurnPayload(turn.turn_id, turn.correlation_id, turn.conversation_id, turn.agent_id,
                                  turn.round_id, after, through, grant.quota - grant.published_count,  # type: ignore[union-attr]
                                  tuple(events), self._gaps(session, member),  # type: ignore[arg-type]
                                  quota=self._quota_snapshot(session, rnd, turn.agent_id))  # type: ignore[arg-type]
            turn.input_after_ordinal, turn.input_through_ordinal = after, through
            turn.state, turn.updated_at = TURN_SUBMITTED, _now()
            if self.trace.enabled:
                self._note(session, "dispatch_attempted", turn.conversation_id, turn_id=turn.turn_id,
                           round_id=turn.round_id, round_no=rnd.round_no, agent_id=turn.agent_id,  # type: ignore[union-attr]
                           correlation_id=turn.correlation_id, lane_id=lane.lane_id, attempt=turn.attempt,
                           input_after_ordinal=after, input_through_ordinal=through, events=len(events),
                           from_state=TURN_RESERVED, to_state=TURN_SUBMITTED, quota_snapshot=payload.quota,
                           request_text=_payload_text(payload), guidance_text=_guidance_text(payload.quota))
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
            self._note(session, "transport_outcome", turn.conversation_id, turn_id=turn.turn_id,  # type: ignore[union-attr]
                       round_id=turn.round_id, agent_id=turn.agent_id, status=outcome.status,  # type: ignore[union-attr]
                       to_state=turn.state, error_code=turn.error_code)  # type: ignore[union-attr]
            return self._turn_view(turn)  # type: ignore[arg-type]
        # The provider call already happened: a transient lock must not strand the turn as ``submitted``.  ``record`` is
        # idempotent (it acts only while the turn is still ``submitted``), so a bounded retry is safe.
        for attempt in range(5):
            try:
                view = self._tx(record)
                break
            except ConversationStoreError:
                if attempt == 4:
                    raise
                time.sleep(0.5 * (attempt + 1))
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
            refs = self._reply_targets(session, turn, to, body)
            if reply_to is not None:
                parent = session.get(ConversationMessage, reply_to) if isinstance(reply_to, str) else None
                if (parent is None or parent.conversation_id != turn.conversation_id
                        or parent.ordinal > (turn.input_through_ordinal or 0)):
                    raise InvalidConversationInput("reply_to does not name a message this turn received")
            turn.captured_reply, turn.captured_to = body, json.dumps(refs)
            turn.captured_reply_to, turn.state, turn.updated_at = reply_to, TURN_REPLY_CAPTURED, _now()  # type: ignore[assignment]
            self._note(session, "reply_captured", turn.conversation_id, turn_id=turn.turn_id, round_id=turn.round_id,
                       agent_id=turn.agent_id, to=refs, response_text=body)
            return self._turn_view(turn)
        return self._tx(write)

    def _room_candidates(self, session: Session, room: Conversation) -> dict[str, tuple[str, ...]]:
        """P052: trusted ``@name`` candidates of THIS room: Agents actively assigned to its scope (Project Architect,
        Project Workers, Workers of this Task) by their registered display name, the Architect also as ``@Architect``
        (unless an Agent is literally named so), and ``@Owner``.  Never another Project's or Task's Agent."""
        names: dict[str, tuple[str, ...]] = {PARTY_OWNER_REF: ("Owner",)}
        architect = None
        for assignment in session.scalars(select(RoleAssignment).where(
                RoleAssignment.project_id == room.project_id, RoleAssignment.state == ASSIGNMENT_ACTIVE)):
            if assignment.role == ROLE_WORKER and assignment.task_id not in (None, room.task_id):
                continue
            if assignment.role not in (ROLE_WORKER, ROLE_ARCHITECT) or assignment.agent_id in names:
                continue
            agent = session.get(AgentDefinition, assignment.agent_id)
            if agent is None or not agent.display_name.strip():
                continue
            names[agent.agent_id] = (agent.display_name.strip(),)
            if assignment.role == ROLE_ARCHITECT:
                architect = agent.agent_id
        if architect is not None and not any(n[0].casefold() == "architect" for n in names.values()):
            names[architect] = names[architect] + ("Architect",)
        return names

    def resolve_mentions(self, session: Session, room: Conversation, text: str, *, exclude: str | None = None):
        candidates = self._room_candidates(session, room)
        if exclude is not None:
            candidates.pop(exclude, None)
        return find_mentions(text, candidates)

    def _mention_targets(self, session: Session, turn: GroupTurn, text: str) -> list[str]:
        """P047/P052: the authenticated Agent's own correlated reply may address in-scope room Agents (and ``@Owner``)
        by ``@DisplayName``.  An ambiguous name addresses nobody.  Addressing is NOT activation: a turn exists only
        where an Owner-authorized grant allows it."""
        room = session.get(Conversation, turn.conversation_id)
        if room is None:
            return []
        return list(self.resolve_mentions(session, room, text, exclude=turn.agent_id).refs)

    def _reply_targets(self, session: Session, turn: GroupTurn, to: object, text: str = "") -> list[str]:
        if to is None:
            mentioned = self._mention_targets(session, turn, text)
            return mentioned or [PARTY_OWNER_REF]
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
            refs = json.loads(turn.captured_to or "[]")
            grant = session.get(GroupRoundGrant, (turn.round_id, turn.agent_id))
            grant.published_count += 1  # type: ignore[union-attr]  # CHECK forbids overspend
            # P052: the one Agent-initiated invitation of a direct request, atomically with this publication; invited
            # members join BEFORE the parties are written so this message is in their audience.
            self._maybe_invite(session, turn, refs, turn.captured_reply or "")
            self._write_parties(session, message.message_id, turn.conversation_id, refs)
            turn.state, turn.reply_message_id, turn.updated_at = TURN_PUBLISHED, message.message_id, _now()
            session.flush()
            if self.trace.enabled:
                rnd = session.get(GroupRound, turn.round_id)
                self._note(session, "reply_published", turn.conversation_id, turn_id=turn.turn_id,
                           round_id=turn.round_id, round_no=rnd.round_no if rnd else None, agent_id=turn.agent_id,
                           message_id=message.message_id, ordinal=ordinal, sender_kind=member.role,  # type: ignore[union-attr]
                           to=refs, chars=len(message.body),
                           grants=self._grants_view(session, rnd) if rnd else None)
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
                "authorizing_message_id": rnd.authorizing_message_id,
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
            unanswered = self._unanswered(session, room, rnd)
            stop = None
            if rnd is not None and rnd.state == ROUND_AWAITING_OWNER:
                stop = "quota_exhausted" if any(u["reason"] == "quota_exhausted" for u in unanswered) \
                    else "unanswered" if unanswered else "natural"
            return {
                "conversation_id": room.conversation_id,
                "controls": {"paused": bool(controls and controls.paused),
                             "emergency_brake": bool(controls and controls.emergency_brake)},
                "round": None if rnd is None else self._round_view(session, rnd),
                "holding_turn": None if holding is None else self._turn_view(holding),
                "answering": holding.agent_id if holding is not None and holding.state == TURN_ACCEPTED else None,
                "waiting": waiting,
                "failed": failed,
                "unanswered": unanswered,
                "stop_reason": stop,
                "members": [self._member_view(session, m) for m in self._members(session, room.conversation_id)],
            }
