"""P042A: tiny, stdlib-only CLI-side acknowledgement, run by Claude Code's own ``SessionStart`` hook.

It is invoked as ``python3 <this file>`` (never imported by the CLI, no ROSC package or Engine needed, so the Worker CLI
keeps running when the Engine is stopped).  It proves exactly one thing: the interactive CLI process that ROSC started
has *initialised a session* for the incarnation nonce in its environment.  It does not prove the model accepted any task
(that is a separate, later Run acknowledgement) and it makes no provider call.

Writes one small JSON file atomically to the path named by ``ROSC_WORKER_ACK_FILE``; every other input is ignored.
"""
from __future__ import annotations

import json
import os
import sys
import time

NONCE_ENV = "ROSC_WORKER_INCARNATION"
FILE_ENV = "ROSC_WORKER_ACK_FILE"
MAX_ANCESTORS = 32


def ancestors(pid: int) -> list[int]:
    """Parent chain of ``pid`` from /proc (Linux); empty when unreadable."""
    chain: list[int] = []
    while pid > 1 and len(chain) < MAX_ANCESTORS:
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                raw = handle.read().decode("utf-8", "replace")
            pid = int(raw.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        chain.append(pid)
    return chain


def main() -> int:
    target = os.environ.get(FILE_ENV, "")
    nonce = os.environ.get(NONCE_ENV, "")
    if not target or not nonce:
        return 0  # not a ROSC-managed session: do nothing, never fail the CLI's startup
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        event = {}
    if not isinstance(event, dict):
        event = {}
    payload = {
        "incarnation": nonce,
        "event": str(event.get("hook_event_name", ""))[:32],
        "source": str(event.get("source", ""))[:32],
        "cli_session_id": str(event.get("session_id", ""))[:64],
        "ancestors": ancestors(os.getpid()),
        "acked_at": time.time(),
    }
    temp = f"{target}.{os.getpid()}.tmp"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
