"""P061: read-only, room-scoped change notifications for Project/Task Conversations over Server-Sent Events.

The stream is an *invalidation* channel only.  It never carries message text, agent context, secrets or ids of other
rooms, accepts no commands and starts nothing: the browser re-reads the ordinary authoritative Core GETs when it sees
``invalidate``.  Delivery is at-most-once and unordered by design; a missed event is repaired by the client's own
revalidation on connect, reconnect and focus.

Mechanism (deliberately small, no event bus/daemon/queue): while ONE browser has ONE room open, its connection probes an
opaque change token of that room every ``interval`` seconds through the regular read-only bridge path (authorization
and Project/Task scope are re-checked on every probe).  The token is a digest of compact Core aggregates (message count
and last ordinal, per-state counts/latest timestamps of deliveries, P060 assessment obligations and wakes, group turns,
rounds, members, controls) plus the in-process driver liveness, so ANY canonical change of the room — including a P060
ACK written after ``next=finish`` when no group round is running — changes it.  Connections are capped, live for a
bounded time (the browser then reconnects and revalidates) and are cleaned up on disconnect.

P062 adds ONE typed event to this same stream: ``draft`` — the provisional, non-canonical text of the turn currently
being generated in this room (see ``provisional.py``).  It is runtime/model/role agnostic: it carries only sanitized
assistant-visible text and minimal routing (turn id, agent id, offset, state, the published message id), read from the
in-memory broker for the room this connection was authorized for.  Invalidations keep working exactly as before;
drafts are checked on a short in-memory tick (no DB), the DB probe still runs every ``interval``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Callable

import anyio
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .conversation import ConversationDenied, ConversationNotFound
from .provisional import DraftCursor, DraftView
from .models import (EVENT_SOURCE_MESSAGE, ArchitectMaintenanceWake, ArchitectObligation, ConversationDelivery,
                     ConversationDispatchControl, ConversationMember, ConversationMemberGap, ConversationMessage,
                     GroupRound, GroupRoundGrant, GroupTurn, ProjectEvent)

log = logging.getLogger(__name__)

EVENT_HELLO, EVENT_INVALIDATE, EVENT_HEARTBEAT, EVENT_DRAFT = "hello", "invalidate", "heartbeat", "draft"
STREAM_VERSION = 1
DEFAULT_INTERVAL = 1.0          # probe period (seconds): the UI sees a change within ~1-2 s
DEFAULT_HEARTBEAT = 15.0        # keeps proxies and the browser's connection-liveness honest
DEFAULT_MAX_SECONDS = 300.0     # bounded lifetime; EventSource reconnects and the client revalidates
DEFAULT_MAX_STREAMS = 16        # per Core process; one visible room per tab needs one
DEFAULT_RETRY_MS = 3000
DEFAULT_DRAFT_TICK = 0.08       # in-memory provisional-text check; also coalesces many small deltas into one frame


def _stamp(value) -> str:
    return value.isoformat() if value is not None else ""


def room_fingerprint(session: Session, conversation_id: str) -> str:
    """Opaque digest of the room's canonical state.  Read-only; aggregates only (no bodies, bounded result size)."""
    in_room = select(ConversationMessage.message_id).where(ConversationMessage.conversation_id == conversation_id)
    parts: list = []
    parts.append(tuple(session.execute(select(func.count(), func.max(ConversationMessage.ordinal)).where(
        ConversationMessage.conversation_id == conversation_id)).one()))
    parts.append([tuple(row) for row in session.execute(select(
        ConversationDelivery.state, func.count(), func.sum(ConversationDelivery.attempts),
        func.max(ConversationDelivery.updated_at)).where(ConversationDelivery.message_id.in_(in_room)).group_by(
        ConversationDelivery.state).order_by(ConversationDelivery.state))])
    parts.append([tuple(row) for row in session.execute(select(
        ArchitectObligation.state, func.count(), func.sum(ArchitectObligation.offer_count),
        func.max(ArchitectObligation.updated_at), func.sum(func.length(func.coalesce(ArchitectObligation.outcome, ""))),
        func.count(ArchitectObligation.last_error)).join(
        ProjectEvent, ProjectEvent.event_id == ArchitectObligation.event_id).where(
        ProjectEvent.source_kind == EVENT_SOURCE_MESSAGE, ProjectEvent.source_id.in_(in_room)).group_by(
        ArchitectObligation.state).order_by(ArchitectObligation.state))])
    parts.append([tuple(row) for row in session.execute(select(
        ArchitectMaintenanceWake.state, func.count(), func.max(ArchitectMaintenanceWake.settled_at)).where(
        ArchitectMaintenanceWake.conversation_id == conversation_id).group_by(
        ArchitectMaintenanceWake.state).order_by(ArchitectMaintenanceWake.state))])
    parts.append([tuple(row) for row in session.execute(select(
        GroupTurn.state, func.count(), func.max(GroupTurn.updated_at)).where(
        GroupTurn.conversation_id == conversation_id).group_by(GroupTurn.state).order_by(GroupTurn.state))])
    parts.append([tuple(row) for row in session.execute(select(
        GroupRound.state, func.count(), func.max(GroupRound.closed_at)).where(
        GroupRound.conversation_id == conversation_id).group_by(GroupRound.state).order_by(GroupRound.state))])
    parts.append(session.scalar(select(func.count()).select_from(GroupRoundGrant).join(
        GroupRound, GroupRound.round_id == GroupRoundGrant.round_id).where(
        GroupRound.conversation_id == conversation_id)))
    parts.append([tuple(row) for row in session.execute(select(
        ConversationMember.state, func.count(), func.max(ConversationMember.updated_at)).where(
        ConversationMember.conversation_id == conversation_id).group_by(
        ConversationMember.state).order_by(ConversationMember.state))])
    parts.append(session.scalar(select(func.count()).select_from(ConversationMemberGap).where(
        ConversationMemberGap.conversation_id == conversation_id)))
    parts.append(session.scalar(select(func.max(ConversationDispatchControl.updated_at)).where(
        ConversationDispatchControl.conversation_id == conversation_id)))
    raw = json.dumps(parts, default=_stamp, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


class RoomEventStreams:
    """Owns the per-process cap and lifetime of room SSE connections.  Timing knobs are Python-level test seams."""

    def __init__(self, *, interval: float = DEFAULT_INTERVAL, heartbeat: float = DEFAULT_HEARTBEAT,
                 max_seconds: float = DEFAULT_MAX_SECONDS, max_streams: int = DEFAULT_MAX_STREAMS,
                 retry_ms: int = DEFAULT_RETRY_MS, draft_tick: float = DEFAULT_DRAFT_TICK) -> None:
        self.interval, self.heartbeat, self.max_seconds = interval, heartbeat, max_seconds
        self.draft_tick = draft_tick
        self.max_streams, self.retry_ms = max_streams, retry_ms
        self._lock = threading.Lock()
        self._active = 0

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def _enter(self) -> bool:
        with self._lock:
            if self._active >= self.max_streams:
                return False
            self._active += 1
            return True

    def _leave(self) -> None:
        with self._lock:
            self._active -= 1

    def response(self, request: Request, probe: Callable[[], str], initial: str,
                 drafts: Callable[[], tuple[int, tuple[DraftView, ...]]] | None = None) -> StreamingResponse:
        """``probe`` re-authorizes and returns the room token; ``initial`` was computed (and authorized) by the caller
        BEFORE the stream opens, so a denied or out-of-scope request never gets a ``200 text/event-stream``.
        ``drafts`` (P062, optional) returns the provisional-draft state of exactly the authorized room."""
        if self.active >= self.max_streams:
            raise HTTPException(429, "Too many live conversation streams")
        return StreamingResponse(self._events(request, probe, initial, drafts), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def _events(self, request: Request, probe: Callable[[], str], initial: str, drafts=None):
        if not self._enter():  # lost a race against the cap check: end at once (the client falls back)
            return
        try:
            hello = {"v": STREAM_VERSION, "drafts": True} if drafts is not None else {"v": STREAM_VERSION}
            yield f"retry: {self.retry_ms}\n" + _frame(EVENT_HELLO, hello)
            cursor = DraftCursor() if drafts is not None else None
            last, seq = initial, 0
            started = quiet_since = time.monotonic()
            next_probe = started + self.interval
            tick = min(self.interval, self.draft_tick) if cursor is not None else self.interval
            if cursor is not None:
                for frame in self._draft_frames(cursor, drafts):  # reconnect: the current snapshot, if any
                    yield frame
            while time.monotonic() - started < self.max_seconds:
                await anyio.sleep(max(0.0, min(tick, next_probe - time.monotonic())))
                if await request.is_disconnected():
                    break
                now = time.monotonic()
                if now >= next_probe:
                    next_probe = now + self.interval
                    try:
                        token = await anyio.to_thread.run_sync(probe)
                    except (ConversationDenied, ConversationNotFound, HTTPException):
                        break  # access or scope lost: end the stream; the client's own GET shows the real error
                    except Exception as exc:  # busy/locked database etc.: skip this probe, never claim a change
                        log.debug("Room event probe skipped: %s", type(exc).__name__)
                        token = last
                    now = time.monotonic()
                    if token != last:
                        last, seq, quiet_since = token, seq + 1, now
                        yield _frame(EVENT_INVALIDATE, {"seq": seq})
                    elif now - quiet_since >= self.heartbeat:
                        quiet_since = now
                        yield _frame(EVENT_HEARTBEAT, {})
                if cursor is not None:
                    for frame in self._draft_frames(cursor, drafts):
                        quiet_since = now
                        yield frame
        finally:
            self._leave()

    @staticmethod
    def _draft_frames(cursor: DraftCursor, drafts) -> list[str]:
        try:
            return [_frame(EVENT_DRAFT, frame) for frame in cursor.frames(drafts())]
        except Exception as exc:  # provisional text is best-effort; it never ends or breaks the stream
            log.debug("Draft frames skipped: %s", type(exc).__name__)
            return []
