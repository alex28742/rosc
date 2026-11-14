"""P046 (P043D): the small Owner-only bridge between the browser and the P043B Core + P043C Claude CLI transport.

One shared service for the Project Conversation (``task_id=None``) and every Task Conversation; there is no second
chat store: messages are the canonical ``ConversationMessage`` rows the Core writes.

Execution model (deliberately minimal):

* Reads (``status``) never claim, dispatch, reconcile or launch anything; the only provider-side observation they make
  is a read-only look at the transport's evidence/lease (``activity``) for the turn already holding the room.
* A provider turn is launched only by ``start`` — an explicit Owner POST — after an explicit Owner round grant.  It runs
  a bounded in-process driver thread for that room: claim -> dispatch -> evidence-backed publish, repeated only while
  the Owner's grant still has budget, and it stops at the first non-publishing outcome (awaiting Owner, pause/brake,
  catch-up decision, UNKNOWN, failure).  One driver per room; nothing re-starts it automatically; no daemon.
* Restart: the Core state is durable.  ``recover_all`` (application start) turns an interrupted ``submitted`` turn into
  ``unknown`` without any provider call; resolving it is the explicit Owner ``reconcile`` (evidence lookup, never a
  re-send).
* Only Claude Code Workers have a group transport in v1.0.  The Architect (OpenCode) and any other adapter are listed
  as members-in-scope but are not addressable for group turns here; nothing is routed through Claude on their behalf.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .authorization import (CONVERSATION_GROUP_CONTROL, CONVERSATION_READ, ActorContext, AuthorizationPolicy,
                            ResourceScope, evaluate)
from .claude_conversation import ClaudeConversationTransport, TransportConfigError
from .conversation import (ConversationConflict, ConversationDenied, ConversationNotFound, ConversationStoreError,
                           InvalidConversationInput, load_message_view)
from .models import (ASSIGNMENT_ACTIVE, MEMBER_RELEASED, PARTICIPANT_OWNER, ROLE_ARCHITECT, ROLE_WORKER, ROUND_OPEN,
                     TURN_ACCEPTED, TURN_HOLDING_STATES, TURN_QUEUED, TURN_SUBMITTED, TURN_UNKNOWN, AgentDefinition,
                     AgentNativeLane, Conversation, ConversationMessage, ConversationParticipant, GroupRoundGrant,
                     GroupTurn, RoleAssignment)
from .worker_conversation import (LOOKUP_UNKNOWN, SETTINGS_SCHEMA, SUBMIT_NOT_ACCEPTED, LookupOutcome, SubmitOutcome,
                                  WorkerConversationCore)
from .worker_session import SUPPORTED_ADAPTERS

log = logging.getLogger(__name__)

UNSUPPORTED_ARCHITECT = "architect_group_not_integrated"
UNSUPPORTED_ADAPTER = "adapter_not_supported"
MAX_DRIVER_STEPS = 64


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class UnavailableTransport:
    """A transport for an Agent without a supported group adapter: every submit is provably refused before any launch."""

    assurance = "unavailable"

    def __init__(self, code: str) -> None:
        self.code = code

    def open_lane(self, lane) -> None:
        raise TransportConfigError(self.code)

    def busy(self, lane) -> bool:
        return False

    def submit(self, lane, payload) -> SubmitOutcome:
        return SubmitOutcome(SUBMIT_NOT_ACCEPTED, self.code)

    def lookup(self, lane, correlation_id) -> LookupOutcome:
        return LookupOutcome(LOOKUP_UNKNOWN)

    def activity(self, lane, correlation_id) -> str | None:
        return None


TransportFactory = Callable[[AgentDefinition], object]


def claude_transport_factory(session_factory: sessionmaker[Session], evidence_dir: Path, **options) -> TransportFactory:
    """Product composition: one headless Claude CLI transport per turn, on the Agent's registered model id."""
    def build(agent: AgentDefinition):
        try:
            return ClaudeConversationTransport(session_factory, evidence_dir, model=agent.model_id, **options)
        except TransportConfigError:
            return UnavailableTransport("model_invalid")
    return build


class GroupConversationBridge:
    def __init__(self, session_factory: sessionmaker[Session], policy: AuthorizationPolicy,
                 core: WorkerConversationCore, transport_factory: TransportFactory) -> None:
        self._sessions = session_factory
        self._policy = policy
        self._core = core
        self._factory = transport_factory
        self._lock = threading.Lock()
        self._drivers: dict[str, threading.Thread] = {}
        self._last: dict[str, dict] = {}

    # ------------------------------------------------------------------ helpers
    def _authorize(self, actor: ActorContext, action: str, project_id: int, task_id: int | None) -> None:
        if not evaluate(self._policy, actor, action, ResourceScope(project_id=project_id, task_id=task_id)).allowed:
            raise ConversationDenied("Not authorized for this operation")

    def _room(self, session: Session, project_id: int, task_id: int | None) -> Conversation | None:
        return self._core._room(session, project_id, task_id)  # noqa: SLF001 - validates Project/Task scope

    def _room_id(self, project_id: int, task_id: int | None) -> str:
        with self._sessions() as session:
            room = self._room(session, project_id, task_id)
            if room is None:
                raise ConversationNotFound("Conversation not found")
            return room.conversation_id

    @staticmethod
    def _support(agent: AgentDefinition, role: str) -> str | None:
        """None when the Agent can take real group turns here; otherwise the honest reason it cannot."""
        if role == ROLE_ARCHITECT:
            return UNSUPPORTED_ARCHITECT
        if agent.runtime_adapter.lower() not in SUPPORTED_ADAPTERS:
            return UNSUPPORTED_ADAPTER
        return None

    def _roster(self, session: Session, project_id: int, task_id: int | None) -> list[dict]:
        """Agents actively assigned to THIS room's scope (Project Architect; Project or same-Task Workers)."""
        entries: dict[str, dict] = {}
        for assignment in session.scalars(select(RoleAssignment).where(
                RoleAssignment.project_id == project_id, RoleAssignment.state == ASSIGNMENT_ACTIVE).order_by(
                RoleAssignment.created_at)):
            if assignment.role == ROLE_WORKER and assignment.task_id not in (None, task_id):
                continue
            if assignment.role not in (ROLE_WORKER, ROLE_ARCHITECT) or assignment.agent_id in entries:
                continue
            agent = session.get(AgentDefinition, assignment.agent_id)
            if agent is None:
                continue
            reason = self._support(agent, assignment.role)
            entries[agent.agent_id] = {"agent_id": agent.agent_id, "display_name": agent.display_name,
                                       "icon_label": agent.icon_label, "role": assignment.role,
                                       "runtime_adapter": agent.runtime_adapter, "model_id": agent.model_id,
                                       "group_supported": reason is None, "unsupported_reason": reason}
        return list(entries.values())

    def _supported_targets(self, session: Session, project_id: int, task_id: int | None, agents: object) -> list[str]:
        if not isinstance(agents, list) or not agents:
            raise InvalidConversationInput("Choose at least one Worker")
        roster = {entry["agent_id"]: entry for entry in self._roster(session, project_id, task_id)}
        for agent_id in agents:
            entry = roster.get(agent_id) if isinstance(agent_id, str) else None
            if entry is None:
                raise InvalidConversationInput("An addressee is not assigned to this conversation")
            if not entry["group_supported"]:
                raise InvalidConversationInput(
                    f"{entry['display_name']}: group turns are not available ({entry['unsupported_reason']})")
        return list(agents)

    def _transport_for(self, session: Session, agent_id: str):
        agent = session.get(AgentDefinition, agent_id)
        if agent is None:
            return UnavailableTransport("agent_missing")
        if agent.runtime_adapter.lower() not in SUPPORTED_ADAPTERS:
            return UnavailableTransport(UNSUPPORTED_ADAPTER)
        return self._factory(agent)

    def transport(self, agent_id: str):
        with self._sessions() as session:
            return self._transport_for(session, agent_id)

    def running(self, conversation_id: str) -> bool:
        with self._lock:
            thread = self._drivers.get(conversation_id)
            return thread is not None and thread.is_alive()

    # ------------------------------------------------------------------ reads
    def status(self, actor: ActorContext, project_id: int, task_id: int | None) -> dict:
        """Typed room state for the UI.  Read-only: never claims, dispatches, reconciles or launches."""
        self._authorize(actor, CONVERSATION_READ, project_id, task_id)
        try:
            return self._status(actor, project_id, task_id)
        except OperationalError:
            raise ConversationStoreError("Group conversation storage is unavailable (database not upgraded)") from None

    def _status(self, actor: ActorContext, project_id: int, task_id: int | None) -> dict:
        with self._sessions() as session:
            room = self._room(session, project_id, task_id)
            roster = self._roster(session, project_id, task_id)
            settings = {key: self._core._setting(session, key) for key in SETTINGS_SCHEMA}  # noqa: SLF001
            conversation_id = room.conversation_id if room is not None else None
            has_owner_message = room is not None and session.scalar(select(ConversationMessage.message_id).join(
                ConversationParticipant,
                ConversationParticipant.participant_id == ConversationMessage.sender_participant_id).where(
                ConversationMessage.conversation_id == room.conversation_id,
                ConversationParticipant.kind == PARTICIPANT_OWNER).limit(1)) is not None
            catchup = self._pending_catchup(session, room.conversation_id) if room is not None else None
        core = self._core.room_status(actor, project_id, task_id=task_id) if room is not None else None
        activity = None
        if core is not None and core["holding_turn"] is not None:
            activity = self._activity(core["holding_turn"])
        names = {entry["agent_id"]: entry["display_name"] for entry in roster}
        return {
            "conversation_id": conversation_id,
            "roster": roster,
            "names": names,
            "settings": settings,
            "has_owner_message": has_owner_message,
            "controls": core["controls"] if core else {"paused": False, "emergency_brake": False},
            "round": core["round"] if core else None,
            "holding_turn": core["holding_turn"] if core else None,
            "activity": activity,
            "answering": core["answering"] if core else None,
            "waiting": core["waiting"] if core else [],
            "failed": core["failed"] if core else [],
            "members": core["members"] if core else [],
            "catchup": catchup,
            "driver": {"running": self.running(conversation_id) if conversation_id else False,
                       "last": self._last.get(conversation_id) if conversation_id else None},
        }

    def _activity(self, turn: dict) -> str | None:
        state = turn["state"]
        if state == TURN_SUBMITTED and turn.get("lane_id"):
            with self._sessions() as session:
                lane = session.get(AgentNativeLane, turn["lane_id"])
                row = session.get(GroupTurn, turn["turn_id"])
                if lane is None or row is None:
                    return None
                transport = self._transport_for(session, turn["agent_id"])
                ref = self._core._lane_ref(lane)  # noqa: SLF001
                correlation = row.correlation_id
            probe = getattr(transport, "activity", None)
            try:
                return probe(ref, correlation) if probe else None
            except Exception:  # an unreadable evidence dir never turns into a claim of activity
                return None
        return None

    def _pending_catchup(self, session: Session, conversation_id: str) -> dict | None:
        """The Owner Send-full/Skip decision the next claim would ask for (read-only mirror of ``claim_next``)."""
        core = self._core
        rnd = core._current_round(session, conversation_id)  # noqa: SLF001
        if rnd is None or rnd.state != ROUND_OPEN or core._holding_turn(session, conversation_id) is not None:  # noqa: SLF001
            return None
        room = session.get(Conversation, conversation_id)
        for turn in session.scalars(select(GroupTurn).where(
                GroupTurn.round_id == rnd.round_id, GroupTurn.pass_no == rnd.current_pass,
                GroupTurn.state == TURN_QUEUED).order_by(GroupTurn.position)):
            member = core._member(session, conversation_id, turn.agent_id)  # noqa: SLF001
            grant = session.get(GroupRoundGrant, (rnd.round_id, turn.agent_id))
            if (member is None or member.state == MEMBER_RELEASED or core._assignment(session, room, turn.agent_id) is None  # noqa: SLF001
                    or grant is None or grant.failed or grant.published_count >= grant.quota):
                continue  # claim would cancel this one
            if turn.catchup_choice is not None:
                return None
            need = core._catchup_need(session, turn)  # noqa: SLF001
            if need is None:
                return None
            events = core._events(session, member, need["from"] - 1, need["through"])  # noqa: SLF001
            return {"turn_id": turn.turn_id, "agent_id": turn.agent_id, "from": need["from"],
                    "through": need["through"], "chars": need["chars"], "limit": need["limit"], "count": len(events)}
        return None

    # ------------------------------------------------------------------ Owner mutations
    def post_message(self, actor: ActorContext, project_id: int, task_id: int | None, *, client_key: object,
                     body: object, to: object, reply_to: object = None) -> dict:
        """One idempotent Owner group message with trusted structured ``to[]`` (supported Workers only).  Never
        dispatches: a model is invoked only by an explicit round grant + ``start``."""
        self._authorize(actor, CONVERSATION_GROUP_CONTROL, project_id, task_id)
        with self._sessions() as session:
            targets = self._supported_targets(session, project_id, task_id, to)
        result = self._core.post_owner_message(actor, project_id, client_key=client_key, body=body, to=targets,
                                               reply_to=reply_to, task_id=task_id)
        with self._sessions() as session:
            return {"message": load_message_view(session, result["message"]["message_id"]),
                    "replayed": result["replayed"]}

    def authorize_round(self, actor: ActorContext, project_id: int, task_id: int | None, *, agents: object,
                        quota: object = None, renew: bool = False) -> dict:
        self._authorize(actor, CONVERSATION_GROUP_CONTROL, project_id, task_id)
        with self._sessions() as session:
            targets = self._supported_targets(session, project_id, task_id, agents)
            room = self._room(session, project_id, task_id)
            if room is None:
                raise ConversationConflict("Write the first Owner message before authorizing a round")
            latest = session.scalars(select(ConversationMessage.message_id).join(
                ConversationParticipant,
                ConversationParticipant.participant_id == ConversationMessage.sender_participant_id).where(
                ConversationMessage.conversation_id == room.conversation_id,
                ConversationParticipant.kind == PARTICIPANT_OWNER).order_by(
                ConversationMessage.ordinal.desc()).limit(1)).first()
        return self._core.authorize_round(actor, project_id, agents=targets, quota=quota, renew=renew,
                                          authorizing_message_id=latest, task_id=task_id)

    def set_controls(self, actor: ActorContext, project_id: int, task_id: int | None, **controls) -> dict:
        return self._core.set_dispatch_controls(actor, project_id, task_id=task_id, **controls)

    def release(self, actor: ActorContext, project_id: int, task_id: int | None, agent_id: str) -> dict:
        return self._core.release_member(actor, project_id, agent_id, task_id=task_id)

    def keep(self, actor: ActorContext, project_id: int, task_id: int | None, agent_id: str) -> dict:
        return self._core.keep_member(actor, project_id, agent_id, task_id=task_id)

    def choose_catchup(self, actor: ActorContext, project_id: int, task_id: int | None, turn_id: str,
                       choice: str) -> dict:
        return self._core.choose_catchup(actor, project_id, turn_id, choice, task_id=task_id)

    # ------------------------------------------------------------------ explicit execution
    def start(self, actor: ActorContext, project_id: int, task_id: int | None) -> dict:
        """Explicit Owner action: run the room's ALREADY-AUTHORIZED queue on a bounded background driver."""
        self._authorize(actor, CONVERSATION_GROUP_CONTROL, project_id, task_id)
        conversation_id = self._room_id(project_id, task_id)
        with self._sessions() as session:
            if self._core._current_round(session, conversation_id) is None:  # noqa: SLF001
                raise ConversationConflict("No authorized round: grant N replies first")
        with self._lock:
            thread = self._drivers.get(conversation_id)
            if thread is not None and thread.is_alive():
                return {"started": False, "running": True}
            thread = threading.Thread(target=self._drive, args=(conversation_id,), daemon=True,
                                      name=f"rosc-group-{conversation_id[:8]}")
            self._drivers[conversation_id] = thread
            self._last[conversation_id] = {"state": "running", "at": _now_iso()}
            thread.start()
        return {"started": True, "running": True}

    def wait(self, conversation_id: str, timeout: float = 30.0) -> None:
        """Test/ops seam: join the room's driver."""
        with self._lock:
            thread = self._drivers.get(conversation_id)
        if thread is not None:
            thread.join(timeout)

    def _step_budget(self, conversation_id: str) -> int:
        with self._sessions() as session:
            rnd = self._core._current_round(session, conversation_id)  # noqa: SLF001
            if rnd is None:
                return 0
            remaining = sum(max(0, g.quota - g.published_count) for g in session.scalars(
                select(GroupRoundGrant).where(GroupRoundGrant.round_id == rnd.round_id)))
        return min(MAX_DRIVER_STEPS, 2 * remaining + 2)  # each slot may take one safe deferred retry

    def _drive(self, conversation_id: str) -> None:
        last: dict = {"state": "stopped", "reason": "budget_exhausted"}
        try:
            for _ in range(self._step_budget(conversation_id)):
                claim = self._core.claim_next(conversation_id)
                if claim.status != "reserved":
                    last = {"state": "stopped", "reason": claim.status, "agent_id": claim.agent_id,
                            "turn_id": claim.turn_id}
                    break
                transport = self.transport(claim.agent_id)
                view = self._core.dispatch(claim.turn_id, transport)
                view.pop("payload", None)
                if view["state"] == TURN_ACCEPTED:
                    view = self._core.reconcile(claim.turn_id, transport)  # evidence lookup -> publish exactly once
                if view["state"] in (TURN_UNKNOWN, TURN_SUBMITTED, TURN_ACCEPTED):
                    last = {"state": "stopped", "reason": view["state"], "agent_id": claim.agent_id,
                            "turn_id": claim.turn_id, "error_code": view.get("error_code")}
                    break
        except Exception as exc:  # never crash the server; the Core state stays what it durably is
            log.warning("Group driver stopped: %s", type(exc).__name__)
            last = {"state": "stopped", "reason": "driver_error"}
        finally:
            with self._lock:
                self._last[conversation_id] = {**last, "at": _now_iso()}

    def reconcile(self, actor: ActorContext, project_id: int, task_id: int | None) -> dict:
        """Explicit Owner evidence reconciliation of the room's held turn.  Never re-submits; inconclusive evidence
        keeps the room held (UNKNOWN) and is reported as such."""
        self._authorize(actor, CONVERSATION_GROUP_CONTROL, project_id, task_id)
        conversation_id = self._room_id(project_id, task_id)
        if self.running(conversation_id):
            raise ConversationConflict("A turn is being processed right now; reconcile after it finishes")
        recovered = self._core.recover(conversation_id)
        with self._sessions() as session:
            pending = [(t.turn_id, t.agent_id) for t in session.scalars(select(GroupTurn).where(
                GroupTurn.conversation_id == conversation_id, GroupTurn.state.in_((TURN_UNKNOWN, TURN_ACCEPTED))))]
        results = [self._core.reconcile(turn_id, self.transport(agent_id)) for turn_id, agent_id in pending]
        return {"recovered": recovered, "reconciled": [{"turn_id": r["turn_id"], "state": r["state"],
                                                        "error_code": r.get("error_code")} for r in results]}

    def recover_all(self) -> int:
        """Application start: interrupted turns become explicit states without any provider call."""
        try:
            with self._sessions() as session:
                rooms = sorted({t.conversation_id for t in session.scalars(select(GroupTurn).where(
                    GroupTurn.state.in_(TURN_HOLDING_STATES)))})
        except OperationalError:
            log.warning("Group conversation recovery skipped: database is not migrated")
            return 0
        for conversation_id in rooms:
            self._core.recover(conversation_id)
        return len(rooms)
