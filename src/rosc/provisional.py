"""P062: provisional (non-canonical) agent text while a turn is being generated — runtime/model/role agnostic.

ROSC keeps three independent dimensions apart: the Agent ROLE (Worker/Architect), the MODEL/provider and the
RUNTIME adapter (Claude Code CLI, OpenCode, a future Codex CLI ...).  Nothing in this module, in the room SSE stream or
in the PC/TC UI may branch on any of them.  The boundary is:

* a runtime adapter is the ONLY layer that understands native events (Claude ``stream-json`` lines, OpenCode
  ``/api/event`` frames, ...).  It reports whether it can stream (``provisional_capability()`` ->
  ``supported`` / ``unsupported`` / ``unverified``; absent = ``unsupported``) and, when a sink is bound to the turn it is
  executing, pushes ONLY validated assistant-visible text through ``ProvisionalSink.segment/delta/replace``.  Segment
  keys are adapter-local opaque strings that never leave this process.
* Core (the room driver) binds a sink to ONE Core-validated turn identity before the transport call
  (``ProvisionalBroker.open`` + ``scoped``) and alone ends it after the canonical outcome is known
  (``ProvisionalBroker.finish``).  An adapter can neither choose the room/turn nor end/publish a draft.
* the broker keeps a small bounded in-memory draft per turn (never SQLite/Drive/Git/logs); the room SSE stream turns
  its snapshots into generic ``draft`` DTOs via ``DraftCursor``.  Provisional text is never a canonical message, never
  enters another Agent's context and never counts as a delivery, ACK or quota use.

A new runtime needs only its own native-event adapter; the contract, the SSE events and the UI stay as they are.  (A
future Architect on another runtime may need other lifecycle/tool/authorization work outside P062 — this module only
promises streaming neutrality.)
"""
from __future__ import annotations

import contextlib
import contextvars
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator

CAP_SUPPORTED, CAP_UNSUPPORTED, CAP_UNVERIFIED = "supported", "unsupported", "unverified"
CAPABILITIES = frozenset({CAP_SUPPORTED, CAP_UNSUPPORTED, CAP_UNVERIFIED})

STATE_STREAMING, STATE_PUBLISHED, STATE_ENDED, STATE_GONE = "streaming", "published", "ended", "gone"

DEFAULT_MAX_CHARS = 100_000      # raw text per draft; beyond it the draft is frozen (the canonical reply is unaffected)
DEFAULT_MAX_SEGMENTS = 256
DEFAULT_MAX_DRAFTS_PER_ROOM = 4
DEFAULT_MAX_ROOMS = 128
DEFAULT_IDLE_TTL = 1800.0        # a streaming draft without updates (lost process/driver) is dropped after this
DEFAULT_ENDED_TTL = 60.0         # an ended/published draft stays briefly so a reconnecting browser sees the end


def capability_of(adapter: object) -> str:
    """The adapter's own declaration; never inferred from role, model or adapter name."""
    probe = getattr(adapter, "provisional_capability", None)
    if not callable(probe):
        return CAP_UNSUPPORTED
    try:
        value = probe()
    except Exception:
        return CAP_UNVERIFIED
    return value if value in CAPABILITIES else CAP_UNVERIFIED


@dataclass(frozen=True)
class TurnIdentity:
    """Core-validated routing of one physical Agent turn (read from the durable GroupTurn, never from the adapter)."""

    project_id: int
    conversation_id: str
    task_id: int | None
    turn_id: str
    agent_id: str
    correlation_id: str


@dataclass(frozen=True)
class DraftView:
    turn_id: str
    agent_id: str
    text: str            # sanitized, assistant-visible text only
    state: str
    message_id: str | None
    truncated: bool


_CONTROL_LINE = re.compile(r"[ \t]*<rosc-control\b[^>\n]*>[ \t]*(?:\n|$)")
_CONTROL_PREFIX = "<rosc-control"


def visible_text(raw: str) -> str:
    """The text a reader may see: ROSC control lines removed, a trailing (possibly incomplete) control tag held back."""
    text = _CONTROL_LINE.sub("", raw)
    cut = text.rfind("<")
    if cut != -1 and ">" not in text[cut:] and _CONTROL_PREFIX.startswith(text[cut:cut + len(_CONTROL_PREFIX)]):
        text = text[:cut]
    return text.rstrip()


class ProvisionalSink:
    """Adapter-facing handle of ONE turn's draft.  Methods never raise and become no-ops once Core ended the draft."""

    def __init__(self, broker: "ProvisionalBroker", identity: TurnIdentity, token: object) -> None:
        self._broker = broker
        self.identity = identity
        self._token = token

    @property
    def correlation_id(self) -> str:
        return self.identity.correlation_id

    def segment(self, key: str) -> None:
        """Start (or re-select) an assistant-visible text segment, e.g. one model message/part."""
        self._broker._mutate(self, str(key), None, append=True)

    def delta(self, key: str, text: str) -> None:
        if isinstance(text, str) and text:
            self._broker._mutate(self, str(key), text, append=True)

    def replace(self, key: str, text: str) -> None:
        """Authoritative snapshot of a segment (corrects duplicated/missed deltas)."""
        if isinstance(text, str):
            self._broker._mutate(self, str(key), text, append=False)


class _Draft:
    __slots__ = ("identity", "token", "segments", "raw_chars", "text", "state", "message_id", "truncated", "updated")

    def __init__(self, identity: TurnIdentity, token: object, now: float) -> None:
        self.identity, self.token = identity, token
        self.segments: dict[str, str] = {}
        self.raw_chars = 0
        self.text = ""
        self.state = STATE_STREAMING
        self.message_id: str | None = None
        self.truncated = False
        self.updated = now


class ProvisionalBroker:
    """Room-scoped, bounded, thread-safe in-memory drafts.  Not durable: a restart simply shows Core's state."""

    def __init__(self, *, max_chars: int = DEFAULT_MAX_CHARS, max_segments: int = DEFAULT_MAX_SEGMENTS,
                 max_drafts_per_room: int = DEFAULT_MAX_DRAFTS_PER_ROOM, max_rooms: int = DEFAULT_MAX_ROOMS,
                 idle_ttl: float = DEFAULT_IDLE_TTL, ended_ttl: float = DEFAULT_ENDED_TTL,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_chars, self.max_segments = max_chars, max_segments
        self.max_drafts_per_room, self.max_rooms = max_drafts_per_room, max_rooms
        self.idle_ttl, self.ended_ttl = idle_ttl, ended_ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._rooms: dict[str, dict[str, _Draft]] = {}
        self._versions: dict[str, int] = {}
        self._counter = 0

    # ------------------------------------------------------------------ Core side
    def open(self, identity: TurnIdentity) -> ProvisionalSink:
        token = object()
        with self._lock:
            now = self._clock()
            self._prune(now)
            room = self._rooms.setdefault(identity.conversation_id, {})
            # A new physical turn of this room: earlier STREAMING drafts can no longer be current (one driver per room).
            for draft in room.values():
                if draft.state == STATE_STREAMING:
                    draft.state, draft.updated = STATE_ENDED, now
            room[identity.turn_id] = _Draft(identity, token, now)
            while len(room) > self.max_drafts_per_room:
                room.pop(min(room, key=lambda k: room[k].updated))
            while len(self._rooms) > self.max_rooms:
                oldest = min(self._rooms, key=lambda c: max((d.updated for d in self._rooms[c].values()), default=0))
                self._rooms.pop(oldest)
                self._bump(oldest)
            self._bump(identity.conversation_id)
        return ProvisionalSink(self, identity, token)

    def finish(self, sink: ProvisionalSink | None, *, published: bool, message_id: str | None = None) -> None:
        """Core only: the canonical outcome of the turn is known.  ``published`` requires the committed reply id."""
        if sink is None:
            return
        with self._lock:
            draft = self._own(sink)
            if draft is None or draft.state != STATE_STREAMING:
                return
            draft.state = STATE_PUBLISHED if published and message_id else STATE_ENDED
            draft.message_id = message_id if draft.state == STATE_PUBLISHED else None
            draft.updated = self._clock()
            self._bump(sink.identity.conversation_id)

    # ------------------------------------------------------------------ reads (SSE)
    def room_state(self, conversation_id: str | None) -> tuple[int, tuple[DraftView, ...]]:
        if not conversation_id:
            return 0, ()
        with self._lock:
            self._prune(self._clock())
            room = self._rooms.get(conversation_id, {})
            views = tuple(DraftView(d.identity.turn_id, d.identity.agent_id, d.text, d.state, d.message_id,
                                    d.truncated) for d in sorted(room.values(), key=lambda d: d.updated))
            return self._versions.get(conversation_id, 0), views

    def room_version(self, conversation_id: str | None) -> int:
        with self._lock:
            return self._versions.get(conversation_id or "", 0)

    # ------------------------------------------------------------------ internals
    def _bump(self, conversation_id: str) -> None:
        self._counter += 1
        self._versions[conversation_id] = self._counter

    def _own(self, sink: ProvisionalSink) -> _Draft | None:
        draft = self._rooms.get(sink.identity.conversation_id, {}).get(sink.identity.turn_id)
        return draft if draft is not None and draft.token is sink._token else None  # noqa: SLF001

    def _mutate(self, sink: ProvisionalSink, key: str, text: str | None, *, append: bool) -> None:
        try:
            with self._lock:
                draft = self._own(sink)
                if draft is None or draft.state != STATE_STREAMING or draft.truncated:
                    return
                if key not in draft.segments:
                    if len(draft.segments) >= self.max_segments:
                        draft.truncated = True
                        return
                    draft.segments[key] = ""
                if text is None:
                    return
                current = draft.segments[key]
                new = current + text if append else text
                total = draft.raw_chars - len(current) + len(new)
                if total > self.max_chars:
                    draft.truncated = True
                    new = new[:max(0, len(new) - (total - self.max_chars))]
                    total = self.max_chars
                draft.segments[key] = new
                draft.raw_chars = total
                text_now = visible_text("\n\n".join(s for s in draft.segments.values() if s.strip()))
                draft.updated = self._clock()
                if text_now != draft.text or draft.truncated:
                    draft.text = text_now
                    self._bump(sink.identity.conversation_id)
        except Exception:  # a provisional draft must never break a turn
            return

    def _prune(self, now: float) -> None:
        for cid in list(self._rooms):
            room = self._rooms[cid]
            for turn_id in [t for t, d in room.items()
                            if (d.state == STATE_STREAMING and now - d.updated > self.idle_ttl)
                            or (d.state != STATE_STREAMING and now - d.updated > self.ended_ttl)]:
                room.pop(turn_id)
                self._bump(cid)
            if not room:
                self._rooms.pop(cid)


# -- binding of the current turn's sink to the executing thread (no transport API change) ------------------------------
# Core binds the sink for the claimed turn (``scoped``).  An adapter that has the turn payload takes it only for its exact
# correlation (``current_for``).  A lower-level runtime call that receives no payload (e.g. ``ArchitectRuntime.ask``)
# sees a sink only inside ``narrowed(correlation_id)`` opened by its caller for that very turn, so any other call of
# the same runtime on the same thread (a legacy direct reply, a maintenance wake) never streams into the draft.
_BOUND: contextvars.ContextVar[ProvisionalSink | None] = contextvars.ContextVar("rosc_provisional_bound", default=None)
_NARROWED: contextvars.ContextVar[ProvisionalSink | None] = contextvars.ContextVar("rosc_provisional_narrowed",
                                                                                   default=None)


@contextlib.contextmanager
def scoped(sink: ProvisionalSink | None) -> Iterator[None]:
    """Core only: bind the claimed turn's sink (or None) for the duration of the transport call."""
    token, narrow = _BOUND.set(sink), _NARROWED.set(None)
    try:
        yield
    finally:
        _NARROWED.reset(narrow)
        _BOUND.reset(token)


def current_for(correlation_id: str) -> ProvisionalSink | None:
    """The sink Core bound for exactly this turn correlation, else None (an adapter never streams into another turn)."""
    sink = _BOUND.get()
    return sink if sink is not None and sink.correlation_id == correlation_id else None


@contextlib.contextmanager
def narrowed(correlation_id: str) -> Iterator[None]:
    """A role transport hands the bound sink to its runtime call for exactly this correlation (else None)."""
    token = _NARROWED.set(current_for(correlation_id))
    try:
        yield
    finally:
        _NARROWED.reset(token)


def current() -> ProvisionalSink | None:
    """For runtime calls without a payload: the sink narrowed by the caller for the call being executed, else None."""
    return _NARROWED.get()


# -- generic SSE DTOs ---------------------------------------------------------------------------------------------------
class DraftCursor:
    """Per-connection view: emits only what THIS browser has not received yet (append from its offset, or a full
    replacement at offset 0), so frames are never duplicated or gapped within one connection.  A reconnect starts a new
    cursor and therefore a fresh snapshot.  DTOs carry no provider payload, segment keys, ids of other rooms or roles."""

    def __init__(self) -> None:
        self._version = -1
        self._sent: dict[str, tuple[str, str]] = {}  # turn_id -> (state, text sent)

    def frames(self, state: tuple[int, tuple[DraftView, ...]]) -> list[dict]:
        version, views = state
        if version == self._version:
            return []
        self._version = version
        out: list[dict] = []
        present = set()
        for view in views:
            present.add(view.turn_id)
            prev_state, prev_text = self._sent.get(view.turn_id, (None, None))
            if prev_state == view.state and prev_text == view.text:
                continue
            if prev_text is not None and view.text.startswith(prev_text):
                offset, chunk = len(prev_text), view.text[len(prev_text):]
            else:
                offset, chunk = 0, view.text
            if prev_state is None and view.state != STATE_STREAMING and not view.text:
                self._sent[view.turn_id] = (view.state, view.text)
                continue  # nothing was ever shown here: an empty finished draft is not news
            out.append({"turn": view.turn_id, "agent": view.agent_id, "offset": offset, "text": chunk,
                        "state": view.state, "message": view.message_id, "truncated": view.truncated})
            self._sent[view.turn_id] = (view.state, view.text)
        for turn_id in [t for t in self._sent if t not in present]:
            self._sent.pop(turn_id)
            out.append({"turn": turn_id, "agent": None, "offset": 0, "text": "", "state": STATE_GONE, "message": None,
                        "truncated": False})
        return out
