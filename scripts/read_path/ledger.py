"""Local freshness ledger for confirmed documentation reads.

Adapted from the DOUTLAB reference ``scripts/read_path_telemetry.py`` (content
SHA-256 + provenance, ``reused-valid`` / ``stale`` / ``unknown/unproven``). The
ROSC adaptation keys entries by repository-relative path and adds
``missing`` for a deleted or renamed document, so such a document can never be
treated as reusable.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

REUSED_VALID = "reused-valid"
STALE = "stale"
UNKNOWN = "unknown/unproven"
MISSING = "missing"

_REQUIRED = ("content_id", "read_at", "provenance")


def content_id(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(state_path: Path) -> dict:
    """Return the ledger; missing, unreadable or malformed state is empty."""

    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"documents": {}}
    if not isinstance(value, dict) or not isinstance(value.get("documents"), dict):
        return {"documents": {}}
    return value


def classify(*, repo_root: Path, rel_path: str, state_path: Path) -> tuple[str, str | None]:
    """Classify ``rel_path`` against the ledger; returns ``(classification, current_sha)``.

    Only ``reused-valid`` allows skipping a fresh read. Everything else --
    stale, unknown, missing -- requires one.
    """

    document = repo_root / rel_path
    try:
        current = content_id(document)
    except OSError:
        return MISSING, None
    entry = load(state_path)["documents"].get(rel_path)
    if not isinstance(entry, dict) or not all(
        isinstance(entry.get(key), str) and entry[key] for key in _REQUIRED
    ):
        return UNKNOWN, current
    return (REUSED_VALID if entry["content_id"] == current else STALE), current


def confirm(*, rel_path: str, sha256: str, state_path: Path, provenance: str) -> dict:
    """Record a fresh, complete read. Never called for mentions or searches."""

    state = load(state_path)
    state["documents"][rel_path] = {
        "content_id": sha256,
        "read_at": datetime.now(timezone.utc).isoformat(),
        "provenance": provenance,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(f"{state_path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(state_path)
    return state["documents"][rel_path]

