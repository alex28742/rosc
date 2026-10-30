"""Minimal local sync-state manifest.

Tracks known prompt/report artifacts, their recorded SHA-256, their Drive file
ID, their per-artifact sync status, and one mechanism-wide Drive transport-health
fact. The manifest is the single authoritative home for *current* transport
state -- it is never written into an artifact body.

The manifest is explicitly non-authoritative relative to the prompt/report files
it describes: an unreadable or malformed ``state.json`` must never block writing
an authoritative artifact. :func:`load_state` degrades to an empty manifest
(after preserving the corrupt file for diagnosis when that is cheap and safe)
instead of propagating a parse error into a caller.

A report entry carries a ``finalized_at`` marker (``None`` while a draft). The
draft -> final transition records the SHA-256 of the *final* bytes; later
transport-status changes never touch artifact bytes or the recorded hash.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_VERSION = 1
_VALID_TYPES = {"prompt", "report"}
_VALID_SYNC_STATUS = {"pending", "synced", "failed"}
_VALID_TRANSPORT_STATUS = {"ok", "auth_required", "unavailable"}


def _empty_state() -> dict:
    return {"version": STATE_VERSION, "entries": {}}


def _quarantine_corrupt_state(state_path: Path, exc: Exception) -> None:
    """Warn and move an unreadable manifest aside so a fresh one can be written.

    Preservation is best-effort only: if the rename itself fails, the caller
    still continues with an empty manifest -- the authoritative prompt/report
    artifact must never be blocked by manifest damage.
    """

    logger.warning(
        "Ignoring unreadable sync-state manifest %s (%s); starting from an empty "
        "manifest.",
        state_path,
        exc,
    )
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantine = state_path.with_name(state_path.name + f".corrupt-{stamp}")
        state_path.replace(quarantine)
        logger.warning("Preserved the unreadable manifest as %s for diagnosis.", quarantine)
    except OSError:
        logger.warning(
            "Could not preserve the unreadable manifest; continuing with empty state."
        )


def load_state(state_path: Path) -> dict:
    """Return the manifest dict, or an empty manifest when it is missing or
    unreadable. Never raises on missing/corrupt/invalid-structure content."""

    if not state_path.exists():
        return _empty_state()
    try:
        raw = state_path.read_text(encoding="utf-8")
        state = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _quarantine_corrupt_state(state_path, exc)
        return _empty_state()
    if not isinstance(state, dict) or not isinstance(state.get("entries"), dict):
        _quarantine_corrupt_state(state_path, ValueError("unexpected manifest structure"))
        return _empty_state()
    state.setdefault("version", STATE_VERSION)
    return state


def artifact_entry(state_path: Path, local_path: str) -> dict | None:
    """The raw manifest entry for ``local_path``, or ``None`` when untracked."""

    entry = load_state(state_path).get("entries", {}).get(local_path)
    return entry if isinstance(entry, dict) else None


def recorded_sha256(state_path: Path, local_path: str) -> str | None:
    """The SHA-256 recorded for ``local_path``, or ``None`` when unrecorded."""

    entry = artifact_entry(state_path, local_path)
    value = entry.get("sha256") if entry else None
    return value if isinstance(value, str) and value else None


def finalized_at(state_path: Path, local_path: str) -> str | None:
    """The finalization timestamp for a report entry, or ``None`` when the entry
    is missing/not finalized (i.e. still an editable draft)."""

    entry = artifact_entry(state_path, local_path)
    value = entry.get("finalized_at") if entry else None
    return value if isinstance(value, str) and value else None


def sync_status_for(state_path: Path, local_path: str) -> str | None:
    """The current (live) sync status recorded for ``local_path``, or ``None``
    when the artifact is not tracked. This is the manifest-is-authoritative
    source the completion line reads from -- never a report body."""

    entry = artifact_entry(state_path, local_path)
    value = entry.get("sync_status") if entry else None
    return value if isinstance(value, str) and value else None


def write_state(state_path: Path, state: dict) -> None:
    """Atomically replace ``state_path`` (temp-file-then-rename)."""

    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_name(state_path.name + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(state_path)


def record_artifact(
    state_path: Path,
    *,
    local_path: str,
    artifact_type: str,
    sha256: str,
    created_at: str,
    drive_id: str | None = None,
    sync_status: str = "pending",
) -> dict:
    """Add or refresh one entry (``artifact_type`` is ``"prompt"`` or
    ``"report"``).

    ``drive_id``/``sync_status`` default to the local-only state (``None`` /
    ``"pending"``); a pull passes a known Drive draft ID and ``"synced"`` because
    a pull is itself the sync.
    """

    if artifact_type not in _VALID_TYPES:
        raise ValueError(f"Unknown artifact_type: {artifact_type!r}")
    if sync_status not in _VALID_SYNC_STATUS:
        raise ValueError(f"Unknown sync_status: {sync_status!r}")
    state = load_state(state_path)
    state.setdefault("version", STATE_VERSION)
    entries = state.setdefault("entries", {})
    entries[local_path] = {
        "type": artifact_type,
        "sha256": sha256,
        "drive_id": drive_id,
        "sync_status": sync_status,
        "created_at": created_at,
        "last_sync_attempt": None,
        "last_error": None,
        "finalized_at": None,
    }
    write_state(state_path, state)
    return state


def mark_report_finalized(
    state_path: Path,
    *,
    local_path: str,
    sha256: str,
    finalized_at: str,
) -> dict:
    """Record the *final* bytes' SHA-256 and the finalization marker for an
    already-tracked report entry.

    Raises ``KeyError`` if the entry is missing (finalization only applies to
    managed reports created by ``report-skeleton``). Only ``sha256`` and
    ``finalized_at`` change -- artifact bytes are never touched here.
    """

    state = load_state(state_path)
    entries = state.setdefault("entries", {})
    entry = entries.get(local_path)
    if not isinstance(entry, dict):
        raise KeyError(f"No tracked artifact entry for {local_path!r}")
    entry["sha256"] = sha256
    entry["finalized_at"] = finalized_at
    write_state(state_path, state)
    return state


def set_sync_status(
    state_path: Path,
    *,
    local_path: str,
    sync_status: str,
    error: str | None = None,
) -> dict:
    """Update only the live sync status of a tracked entry.

    This is the mutable transport state the contract keeps external to the
    report body; it never rewrites the artifact file or its recorded SHA-256.
    ``error`` records the last transport error (cleared on success) so a
    ``failed`` state is diagnosable and retryable.
    """

    if sync_status not in _VALID_SYNC_STATUS:
        raise ValueError(f"Unknown sync_status: {sync_status!r}")
    state = load_state(state_path)
    entries = state.setdefault("entries", {})
    entry = entries.get(local_path)
    if not isinstance(entry, dict):
        raise KeyError(f"No tracked artifact entry for {local_path!r}")
    entry["sync_status"] = sync_status
    entry["last_sync_attempt"] = datetime.now(timezone.utc).isoformat()
    entry["last_error"] = error
    write_state(state_path, state)
    return state


def set_drive_id(state_path: Path, *, local_path: str, drive_id: str) -> dict:
    """Record the Drive file identity for a tracked entry.

    Once known, the Drive file ID is the primary transport identity: retries
    reconcile against it rather than creating a second file.
    """

    state = load_state(state_path)
    entries = state.setdefault("entries", {})
    entry = entries.get(local_path)
    if not isinstance(entry, dict):
        raise KeyError(f"No tracked artifact entry for {local_path!r}")
    entry["drive_id"] = drive_id
    write_state(state_path, state)
    return state


def record_transport_health(
    state_path: Path,
    *,
    status: str,
    detail: str | None = None,
) -> dict:
    """Record the outcome of the most recent attempt to open the Drive transport
    itself, independent of any single prompt/report artifact.

    This is deliberately *not* per-artifact: opening the transport either
    succeeds or fails as one fact about current credentials, regardless of which
    artifact triggered the attempt. It is the only source ``completion-line``
    consults to avoid printing a bare, healthy-looking ``sync=synced`` when the
    last known attempt to reach Drive required interactive reauth or otherwise
    failed. This function only *records* an outcome; it never attempts to reach
    Drive.
    """

    if status not in _VALID_TRANSPORT_STATUS:
        raise ValueError(f"Unknown transport status: {status!r}")
    state = load_state(state_path)
    state.setdefault("version", STATE_VERSION)
    state["transport"] = {
        "status": status,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "detail": detail,
    }
    write_state(state_path, state)
    return state


def transport_health(state_path: Path) -> dict | None:
    """The last known Drive transport health fact (``status``, ``checked_at``,
    ``detail``), or ``None`` when no transport attempt has ever been recorded.

    Never a live probe -- purely a manifest read. A missing/corrupt manifest
    degrades to ``None`` (the same "unknown, not unhealthy" default used
    elsewhere).
    """

    entry = load_state(state_path).get("transport")
    return entry if isinstance(entry, dict) else None

