"""P052 r03: Owner-switchable, local-only Conversation Diagnostics (structured JSONL).

Two Owner settings (ROSC Settings, persisted as 0/1 in ``rosc_settings``; read once at startup and applied in-process on
every change, so no restart is needed):

* ``conversation_diagnostics_enabled`` (default 0).  0 = OFF: every hook is an immediate return; no directory, no file,
  no thread, no buffering, nothing retroactive.  1 = ON: subsequent events are appended.
* ``conversation_diagnostics_bodies`` (default 0), subordinate.  Effective only while diagnostics are ON.  0 = metadata
  only; 1 = metadata PLUS the ROSC-owned conversation text at that boundary (message bodies, the exact request text ROSC
  hands to a dispatch adapter / the Architect runtime, the reply text ROSC receives, quota/invitation guidance).

Privacy by construction (metadata mode): a record can only carry the allowlisted fields below, each re-validated against a
strict pattern, a finite vocabulary, a numeric range or a structured projection.  Text fields (``TEXT_FIELDS``) are
written ONLY in body mode; in metadata mode they are replaced by their lengths.  Callers never pass headers, tokens,
environment, key files, process argv or tool secrets; there is no generic ``dict`` dump, so an accidentally serialized
request/response object cannot be expressed.  Full-body logs CAN contain whatever a user or agent wrote into a message
(including secrets pasted into a message): there is no general-purpose redaction of user-generated text.

Storage: ``<data>/diagnostics/conversations/conversation-trace.jsonl`` (0700 directories, 0600 files, no symlinks, owner
only), rotated at ``max_file_bytes`` into ``conversation-trace.<stamp>-<seq>.jsonl``; only the newest ``max_files`` rotated
files are kept and ONLY files matching that exact pattern inside this directory are ever removed.  A sink failure (unsafe
path, full disk, permission) drops the event, increments ``dropped`` and records a content-free problem code; it never
raises into, retries, delays or changes the conversation path.  Turning diagnostics OFF never deletes existing files.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SETTING_ENABLED = "conversation_diagnostics_enabled"
SETTING_BODIES = "conversation_diagnostics_bodies"
SETTING_KEYS = (SETTING_ENABLED, SETTING_BODIES)
DIAGNOSTICS_DIRNAME = "diagnostics"
CONVERSATIONS_DIRNAME = "conversations"
TRACE_FILENAME = "conversation-trace.jsonl"
ROTATED = re.compile(r"^conversation-trace\.[0-9]{8}T[0-9]{6}Z-[0-9]{1,9}\.jsonl$")
SCHEMA_VERSION = 1
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_FILES = 6  # rotated files kept beside the current one -> at most ~28 MiB in total
MAX_TEXT_CHARS = 200_000  # one text field; longer text is cut and flagged (``<field>_truncated``)
RETRY_AFTER_FAILURE = 30.0

MODE_OFF, MODE_METADATA, MODE_FULL = "off", "metadata", "full"

EVENTS = frozenset({
    "trace_mode_changed", "owner_message_committed", "mentions_resolved", "architect_request_sent",
    "architect_reply_received", "architect_delivery_state", "invitation_checked", "round_opened", "round_closed",
    "round_awaiting_owner", "turn_queued", "turn_reserved", "turn_cancelled", "dispatch_attempted", "transport_request",
    "transport_outcome", "reply_captured", "reply_published", "driver_started", "driver_stopped", "wake_skipped",
    "wake_failed", "next_turn_decided"})

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")  # ROSC-defined reason/state/outcome constants only
_MAX_COUNT = 2 ** 53

# field -> kind
FIELDS: dict[str, str] = {
    "project_id": "count", "task_id": "count", "ordinal": "count", "reply_to_ordinal": "count", "round_no": "count",
    "pass_no": "count", "position": "count", "attempt": "count", "chars": "count", "request_chars": "count",
    "response_chars": "count", "events": "count", "input_after_ordinal": "count", "input_through_ordinal": "count",
    "steps": "count", "attempts": "count", "quota": "count", "published": "count",
    "duration_ms": "ms",
    "conversation_id": "uuid", "message_id": "uuid", "reply_to_id": "uuid", "round_id": "uuid", "turn_id": "uuid",
    "agent_id": "uuid", "sender_agent_id": "uuid", "authorizing_message_id": "uuid", "considered_message_id": "uuid",
    "latest_owner_message_id": "uuid", "closed_round_id": "uuid", "lane_id": "uuid", "correlation_id": "uuid",
    "scope": "code", "sender_kind": "code", "delivery_mode": "code", "route": "code", "state": "code",
    "from_state": "code", "to_state": "code", "reason": "code", "outcome": "code", "status": "code",
    "error_code": "code", "transport": "code", "stop_reason": "code", "mode": "code", "previous_mode": "code",
    "decision": "code", "claim": "code", "intent": "code", "intent_source": "code", "to_source": "code",
    "replayed": "bool", "this_reply_counts": "bool",
    "to": "refs", "typed_refs": "refs", "selected_refs": "refs", "invited": "refs", "next_turn": "refs",
    "unknown_names": "count",
    "declined": "declined", "quota_snapshot": "quota", "grants": "grants",
}
# Written only in body mode; in metadata mode only ``<field>_chars`` is recorded.
TEXT_FIELDS = frozenset({"body", "request_text", "response_text", "guidance_text"})


def _ref(value: object) -> str | None:
    return value if isinstance(value, str) and (value == "owner" or _UUID.match(value)) else None


def _clean(kind: str, value: object) -> object | None:
    if kind == "count":
        return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_COUNT else None
    if kind == "ms":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return None
        return round(float(value), 3) if 0 <= float(value) < 1e12 else None
    if kind == "bool":
        return value if isinstance(value, bool) else None
    if kind == "uuid":
        return value if isinstance(value, str) and _UUID.match(value) else None
    if kind == "code":
        return value if isinstance(value, str) and _CODE.match(value) else ("other" if isinstance(value, str) else None)
    if kind == "refs":
        if not isinstance(value, (list, tuple)):
            return None
        return [r for r in (_ref(v) for v in value[:32]) if r is not None]
    if kind == "declined":
        if not isinstance(value, (list, tuple)):
            return None
        out = []
        for item in value[:32]:
            if isinstance(item, (list, tuple)) and len(item) == 2 and _ref(item[0]):
                out.append({"agent_id": item[0], "reason": _clean("code", item[1])})
        return out
    if kind == "grants":
        if not isinstance(value, dict):
            return None
        out = []
        for agent_id, grant in list(value.items())[:32]:
            if _ref(agent_id) and isinstance(grant, dict):
                out.append({"agent_id": agent_id, **{k: _clean("count", grant.get(k)) for k in ("quota", "published")},
                            "failed": bool(grant.get("failed"))})
        return out
    if kind == "quota":
        return _quota(value)
    return None


def _quota(snapshot: object) -> dict | None:
    """Structured projection of a P052 quota snapshot (never its free-text guidance)."""
    if not isinstance(snapshot, dict):
        return None

    def entry(e: object) -> dict | None:
        if not isinstance(e, dict) or not _ref(e.get("agent_id")):
            return None
        out = {"agent_id": e["agent_id"]}
        for key in ("quota", "published", "remaining", "remaining_after_this_reply", "remaining_if_invited"):
            if _clean("count", e.get(key)) is not None:
                out[key] = e[key]
        if isinstance(e.get("can_reply_after_your_message"), bool):
            out["can_reply"] = e["can_reply_after_your_message"]
        if e.get("why_not") is not None:
            out["why_not"] = _clean("code", e.get("why_not"))
        return out
    result: dict = {}
    if isinstance(snapshot.get("mode"), str):
        result["mode"] = _clean("code", snapshot["mode"])
    if isinstance(snapshot.get("this_reply_counts"), bool):
        result["this_reply_counts"] = snapshot["this_reply_counts"]
    if snapshot.get("blocked") is not None:
        result["blocked"] = _clean("code", snapshot.get("blocked"))
    you = entry(snapshot.get("you"))
    if you:
        result["you"] = you
    peers = [p for p in (entry(p) for p in snapshot.get("peers") or [] if isinstance(snapshot.get("peers"), list)) if p]
    if peers:
        result["peers"] = peers
    for key in ("remaining_total_before_this_reply", "remaining_total_after_this_reply"):
        if _clean("count", snapshot.get(key)) is not None:
            result[key] = snapshot[key]
    reply = snapshot.get("reply")
    if isinstance(reply, dict):  # P052 r04: the turn's causal trigger and default recipient (never a name or body)
        result["reply"] = {"reply_to_ordinal": _clean("count", reply.get("reply_to_ordinal")),
                           "default_to": _ref(reply.get("default_to")),
                           "default_to_can_answer": bool(reply.get("default_to_can_answer")),
                           "default_to_remaining": _clean("count", reply.get("default_to_remaining"))}
    invitation = snapshot.get("invitation")
    if isinstance(invitation, dict):
        agents = [a for a in (entry(a) for a in invitation.get("agents") or [] if isinstance(invitation.get("agents"), list)) if a]
        result["invitation"] = {"per_agent_quota": _clean("count", invitation.get("per_agent_quota")), "agents": agents}
    return result


class ConversationTrace:
    """Process-wide sink.  ``ConversationTrace(None)`` never writes (used when no data directory is composed)."""

    def __init__(self, data_dir: Path | None, *, max_file_bytes: int = MAX_FILE_BYTES, max_files: int = MAX_FILES) -> None:
        self._root = Path(data_dir) / DIAGNOSTICS_DIRNAME if data_dir is not None else None
        self._max_file = max_file_bytes
        self._max_files = max_files
        self._lock = threading.Lock()
        self._enabled = False
        self._bodies = False
        self._origin = secrets.token_hex(4)
        self._seq = 0
        self._blocked_until = 0.0
        self.writes = 0
        self.dropped = 0
        self.problem: str | None = None

    # -- configuration -----------------------------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled and self._root is not None

    @property
    def bodies(self) -> bool:
        return self.enabled and self._bodies

    @property
    def mode(self) -> str:
        return MODE_OFF if not self.enabled else MODE_FULL if self._bodies else MODE_METADATA

    @property
    def directory(self) -> Path | None:
        return None if self._root is None else self._root / CONVERSATIONS_DIRNAME

    def configure(self, *, enabled: bool, bodies: bool) -> None:
        previous = self.mode
        with self._lock:
            self._enabled, self._bodies = bool(enabled), bool(bodies)
            self._blocked_until = 0.0  # an explicit Owner change retries a previously failing sink at once
        if self.mode != previous and self.enabled:
            self.emit("trace_mode_changed", mode=self.mode, previous_mode=previous)

    def status(self) -> dict:
        return {"mode": self.mode, "writes": self.writes, "dropped": self.dropped, "problem": self.problem,
                "location": f"{DIAGNOSTICS_DIRNAME}/{CONVERSATIONS_DIRNAME}/{TRACE_FILENAME}"}

    # -- emit --------------------------------------------------------------------------------------------------------
    def emit(self, event: str, **fields) -> None:
        """Best effort; never raises.  OFF -> immediate return."""
        if not self._enabled or self._root is None or event not in EVENTS:
            return
        try:
            bodies = self._bodies
            record: dict = {}
            for key, value in fields.items():
                if value is None:
                    continue
                if key in TEXT_FIELDS:
                    if not isinstance(value, str):
                        continue
                    record[f"{key}_chars"] = len(value)
                    if bodies:
                        record[key] = value[:MAX_TEXT_CHARS]
                        if len(value) > MAX_TEXT_CHARS:
                            record[f"{key}_truncated"] = True
                    continue
                kind = FIELDS.get(key)
                if kind is None:
                    continue
                cleaned = _clean(kind, value)
                if cleaned is not None:
                    record[key] = cleaned
            with self._lock:
                self._seq += 1
                head = {"schema_version": SCHEMA_VERSION, "event": event, "origin": self._origin, "seq": self._seq,
                        "at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                        "capture": MODE_FULL if bodies else MODE_METADATA}
                line = (json.dumps({**head, **record}, ensure_ascii=False, separators=(",", ":"),
                                   allow_nan=False) + "\n").encode("utf-8")
                self._append(line)
        except Exception:
            self._failed("emit_failed")

    # -- sink --------------------------------------------------------------------------------------------------------
    def _failed(self, code: str) -> None:
        self.dropped += 1
        self.problem = code
        self._blocked_until = time.monotonic() + RETRY_AFTER_FAILURE
        log.warning("Conversation diagnostics could not be written; events are being dropped")  # content-free

    def _append(self, line: bytes) -> None:
        if time.monotonic() < self._blocked_until:
            self.dropped += 1
            return
        if len(line) > self._max_file:
            self.dropped += 1
            self.problem = "event_too_large"
            return
        dir_fd = file_fd = None
        try:
            dir_fd = self._open_dir()
            file_fd = self._open_file(dir_fd)
            if os.fstat(file_fd).st_size + len(line) > self._max_file:
                os.close(file_fd)
                file_fd = None
                self._rotate(dir_fd)
                file_fd = self._open_file(dir_fd)
            if os.write(file_fd, line) != len(line):
                raise OSError("short write")
            self.writes += 1
            self.problem = None
        except OSError as exc:
            self._failed("disk_full" if getattr(exc, "errno", None) == 28 else "write_failed")
        except Exception:
            self._failed("write_failed")
        finally:
            for fd in (file_fd, dir_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    @staticmethod
    def _open_file(dir_fd: int) -> int:
        fd = os.open(TRACE_FILENAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                     dir_fd=dir_fd)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1):
            os.close(fd)
            raise OSError("unsafe trace file")
        return fd

    def _rotate(self, dir_fd: int) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        os.rename(TRACE_FILENAME, f"conversation-trace.{stamp}-{self._seq}.jsonl", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        rotated = sorted(name for name in os.listdir(dir_fd) if ROTATED.match(name))
        for name in rotated[:max(0, len(rotated) - self._max_files)]:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
                os.unlink(name, dir_fd=dir_fd)

    def _open_dir(self) -> int:
        assert self._root is not None
        parent = self._root.parent
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for name in (self._root.name, CONVERSATIONS_DIRNAME):
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise OSError("unsafe trace directory")
        except Exception:
            os.close(fd)
            raise
        return fd


def read_settings(session) -> dict[str, bool]:
    from .models import RoscSetting
    values = {}
    for key in SETTING_KEYS:
        row = session.get(RoscSetting, key)
        values[key] = bool(row is not None and row.value_int == 1)
    return values


def write_settings(session, actor_id: str, changes: dict[str, bool]) -> None:
    from .models import RoscSetting
    now = datetime.now(timezone.utc)
    for key, value in changes.items():
        row = session.get(RoscSetting, key)
        if row is None:
            session.add(RoscSetting(key=key, value_int=int(value), updated_by=actor_id, updated_at=now))
        else:
            row.value_int, row.updated_by, row.updated_at = int(value), actor_id, now
