"""Opt-in, local-only Architect latency diagnostics (P024 / R1).

A measurement capability, never a behaviour change.  Disabled unless ``ROSC_ARCHITECT_LATENCY_TRACE`` is ``1``/``true``/
``yes``/``on``; disabled means zero output (no directory, no file, no thread) and every hook is an immediate return.

Privacy by construction: a record can only carry the allowlisted fields below, and every value is re-validated against a
closed vocabulary, a numeric range or a strict identifier pattern.  There is NO free-text field: message bodies, prompt
text, Tool inputs/targets/results, paths, URLs, exception strings, tokens and credentials cannot be expressed, so they
cannot leak even through a bug in a caller.  A value that fails validation is dropped, never rewritten.

The sink is best effort: any failure (missing/unsafe directory, symlink, loose mode, full disk, size cap) is swallowed and
the Architect delivery path is unaffected.  Writes are small synchronous appends at phase boundaries; nothing is buffered
in memory, no background thread exists, and the SQLite database is never touched.  A crash simply leaves a turn without
its terminal event, which the offline summary reports as ``incomplete`` rather than as success.

Time: each record carries ``at_utc`` (wall clock) and ``t_ms`` (monotonic milliseconds since this PROCESS started) plus a
per-process random ``origin``.  Monotonic stamps are only comparable within one ``origin``.  ``duration_ms`` is the
measured length of one span; the difference of two ``t_ms`` stamps is an inter-event INTERVAL, not a duration.

Explicitly NOT measured here: time to first token, model token-generation time, number/duration of LLM steps,
compaction and client paint.  ``/wait`` time is the time until OpenCode's agent loop became idle and must never be read
as pure model inference.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

ENV_TRACE = "ROSC_ARCHITECT_LATENCY_TRACE"
DIAGNOSTICS_DIRNAME = "diagnostics"
TRACE_FILENAME = "architect-latency.jsonl"
SCHEMA_VERSION = 1
MAX_TRACE_BYTES = 16 * 1024 * 1024  # one file; once reached, further events are dropped (never rotated or deleted)
RETRY_AFTER_FAILURE = 30.0  # seconds before a failed sink is tried again
_TRUE = frozenset({"1", "true", "yes", "on"})

EVENTS = frozenset({
    "trace_started", "owner_commit", "submit", "worker_started", "turn_lock_acquired", "probe", "context_built",
    "prepare", "prompt_admitted", "wait_returned", "lookup", "ask_finished", "tool_call", "commit", "delivery_state",
    "turn_finished", "reconcile_finished"})
PHASES = frozenset({"intake", "deliver", "reconcile", "tool"})
DELIVERY_STATES = frozenset({"not_sent", "pending", "delivered", "failed", "unknown"})
CORRELATIONS = frozenset({"verified", "unverified"})

_CODE = re.compile(r"^[a-z0-9_]{1,48}$")
_TURN_ID = re.compile(r"^msg_rosc[0-9a-f]{32}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TOOL = re.compile(r"^[a-z_]{3,32}$")
# field -> kind.  "count" = non-negative int, "ms" = finite non-negative float, others are closed vocabularies/patterns.
FIELDS: dict[str, str] = {
    "duration_ms": "ms", "timeout_ms": "ms",
    "project_id": "count", "attempts": "count", "messages": "count", "pages": "count", "requests": "count",
    "fallbacks": "count", "bytes": "count", "chars": "count", "result_chars": "count",
    "identity_chars": "count", "rules_chars": "count", "tool_guidance_chars": "count", "recap_chars": "count",
    "recap_messages": "count", "framing_chars": "count", "total_chars": "count", "tools": "count",
    "outcome": "code", "state": "state", "phase": "phase", "correlation": "correlation", "tool": "tool",
    "turn_id": "turn_id", "rosc_session_id": "uuid",
}
_MAX_COUNT = 2 ** 53


def _clean(kind: str, value: object) -> object | None:
    """Return the validated value or ``None`` (drop).  Never coerces free text."""
    if kind == "count":
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_COUNT else None
    if kind == "ms":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return round(number, 3) if math.isfinite(number) and 0.0 <= number < 1e12 else None
    if not isinstance(value, str):
        return None
    if kind == "code":
        return value if _CODE.match(value) else None
    if kind == "state":
        return value if value in DELIVERY_STATES else None
    if kind == "phase":
        return value if value in PHASES else None
    if kind == "correlation":
        return value if value in CORRELATIONS else None
    if kind == "tool":
        return value if _TOOL.match(value) else None
    if kind == "turn_id":
        return value if _TURN_ID.match(value) else None
    if kind == "uuid":
        return value if _UUID.match(value) else None
    return None


class LatencyTrace:
    """Process-wide sink.  ``LatencyTrace(None)`` / ``DISABLED`` is the zero-cost off switch."""

    def __init__(self, directory: Path | None = None, *, max_bytes: int = MAX_TRACE_BYTES) -> None:
        self._dir = Path(directory) if directory is not None else None
        self._max = max_bytes
        self._lock = threading.Lock()
        self._seq = 0
        self._origin = secrets.token_hex(4)
        self._t0 = time.monotonic()
        self._blocked_until = 0.0
        self._warned = False
        self._local = threading.local()
        self._turns: dict[str, tuple[str, int | None]] = {}  # rosc session id -> (turn id, project id)
        self.dropped = 0  # events lost to a sink problem (diagnostic counter only)
        if self._dir is not None:
            self.emit("trace_started")

    # -- construction ------------------------------------------------------------------------------------------------
    @classmethod
    def from_environment(cls, data_dir: Path, environ: Mapping[str, str] | None = None) -> "LatencyTrace":
        env = os.environ if environ is None else environ
        if (env.get(ENV_TRACE) or "").strip().lower() not in _TRUE:
            return DISABLED
        return cls(Path(data_dir) / DIAGNOSTICS_DIRNAME)

    @property
    def enabled(self) -> bool:
        return self._dir is not None

    @property
    def path(self) -> Path | None:
        return None if self._dir is None else self._dir / TRACE_FILENAME

    # -- time --------------------------------------------------------------------------------------------------------
    @staticmethod
    def now() -> float:
        return time.monotonic()

    @staticmethod
    def since(started: float) -> float:
        return (time.monotonic() - started) * 1000.0

    # -- per-thread scope (turn / project / phase inherited by nested emits) --------------------------------------------
    @contextlib.contextmanager
    def _scope_cm(self, values: dict):
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        stack.append(values)
        try:
            yield self
        finally:
            stack.pop()

    def scope(self, **values):
        """Context manager: nested ``emit`` calls on THIS thread inherit these (validated at emit time) fields."""
        if self._dir is None:
            return _NULL
        return self._scope_cm(dict(values))

    def bind(self, **values) -> None:
        stack = getattr(self._local, "stack", None)
        if self._dir is not None and stack:
            stack[-1].update(values)

    # -- turn registry (Tool <-> turn correlation, only where it is provable) -------------------------------------------
    def begin_turn(self, session_id: str, turn_id: str, project_id: int | None) -> None:
        if self._dir is not None:
            with self._lock:
                self._turns[session_id] = (turn_id, project_id)

    def end_turn(self, session_id: str, turn_id: str) -> None:
        if self._dir is not None:
            with self._lock:
                if self._turns.get(session_id, (None,))[0] == turn_id:
                    del self._turns[session_id]

    def active_turn(self, session_id: str) -> tuple[str, int | None] | None:
        if self._dir is None:
            return None
        with self._lock:
            return self._turns.get(session_id)

    # -- emit --------------------------------------------------------------------------------------------------------
    def emit(self, event: str, **fields) -> None:
        """Best effort; never raises, never blocks the caller on anything but one small append."""
        if self._dir is None or event not in EVENTS:
            return
        try:
            merged: dict = {}
            for layer in getattr(self._local, "stack", None) or ():
                merged.update(layer)
            merged.update(fields)
            record: dict = {}
            for key, value in merged.items():
                kind = FIELDS.get(key)
                if kind is None or value is None:
                    continue
                cleaned = _clean(kind, value)
                if cleaned is not None:
                    record[key] = cleaned
            with self._lock:
                self._seq += 1
                head = {"schema_version": SCHEMA_VERSION, "event": event, "origin": self._origin, "seq": self._seq,
                        "at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                        "t_ms": round((time.monotonic() - self._t0) * 1000.0, 3)}
                line = (json.dumps({**head, **record}, ensure_ascii=True, separators=(",", ":"),
                                   allow_nan=False) + "\n").encode("ascii")
                self._append(line)
        except Exception:  # diagnostics must never affect the product path
            self._failed()

    # -- sink --------------------------------------------------------------------------------------------------------
    def _failed(self) -> None:
        self.dropped += 1
        self._blocked_until = time.monotonic() + RETRY_AFTER_FAILURE
        if not self._warned:
            self._warned = True
            log.warning("Architect latency trace is unavailable; events are being dropped")  # content-free

    def _append(self, line: bytes) -> None:
        if time.monotonic() < self._blocked_until:
            self.dropped += 1
            return
        dir_fd = file_fd = None
        try:
            dir_fd = self._open_dir()
            file_fd = os.open(TRACE_FILENAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                              0o600, dir_fd=dir_fd)
            info = os.fstat(file_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or info.st_nlink != 1):
                raise OSError("unsafe trace file")
            if info.st_size + len(line) > self._max:
                self.dropped += 1  # full: stop quietly, never rotate or delete the Owner's data
                self._blocked_until = float("inf")
                return
            if os.write(file_fd, line) != len(line):
                raise OSError("short write")
        except Exception:
            self._failed()
        finally:
            for fd in (file_fd, dir_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _open_dir(self) -> int:
        assert self._dir is not None
        try:
            os.mkdir(self._dir, 0o700)
        except FileExistsError:
            pass
        fd = os.open(self._dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OSError("unsafe trace directory")
        except Exception:
            os.close(fd)
            raise
        return fd


_NULL = contextlib.nullcontext()
DISABLED = LatencyTrace(None)
