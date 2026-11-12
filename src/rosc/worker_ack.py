"""P042A: tiny, stdlib-only CLI-side evidence writer, run by Claude Code's own hooks (``SessionStart``,
``PostToolUse`` for ``Read`` and ``Stop``), installed per launch through ``--settings``.

Modes (``python3 worker_ack.py <mode>``):

* ``session``  - SessionStart: the CLI initialised a session (the r01 acknowledgement, below).
* ``read``     - PostToolUse(Read): append the path of a file the CLI really read through its native Read tool, but only
  while an Owner-requested bootstrap request exists.  This is evidence recorded by the CLI's own hook, not a claim.
* ``submit``   - UserPromptSubmit: the CLI itself reports that a prompt carrying the request's token was submitted
  (proof of delivery; typing keys into tmux proves nothing).
* ``stop``     - Stop: when the finished turn's last message carries the request's correlated marker line, write the
  small bootstrap acknowledgement (token, claimed outcome, observed reads).  Anything else is ignored.

Nothing here talks to the Engine or a model; every mode is a no-op outside a ROSC-managed session.

Original r01 description of the ``session`` mode:

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


def _sibling(target: str, suffix: str) -> str:
    base = target[:-len(".ack.json")] if target.endswith(".ack.json") else target
    return base + suffix


def _atomic_write(target: str, payload: dict) -> None:
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


def _load(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _session(target: str, nonce: str, event: dict) -> None:
    _atomic_write(target, {
        "incarnation": nonce,
        "event": str(event.get("hook_event_name", ""))[:32],
        "source": str(event.get("source", ""))[:32],
        "cli_session_id": str(event.get("session_id", ""))[:64],
        "ancestors": ancestors(os.getpid()),
        "acked_at": time.time(),
    })


def _read(target: str, nonce: str, event: dict) -> None:
    request = _load(_sibling(target, ".request.json"))
    tool_input = event.get("tool_input")
    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not request or request.get("incarnation") != nonce or not isinstance(path, str) or event.get("tool_name") != "Read":
        return
    try:
        with open(_sibling(target, ".reads.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"token": request.get("token"), "path": path[:1024]}) + "\n")
    except OSError:
        pass


def _submit(target: str, nonce: str, event: dict) -> None:
    request = _load(_sibling(target, ".request.json"))
    prompt = event.get("prompt")
    token = request.get("token") if request else None
    if not request or request.get("incarnation") != nonce or not isinstance(prompt, str) or not isinstance(token, str) or not token:
        return
    if token in prompt:
        _atomic_write(_sibling(target, ".submitted.json"), {"incarnation": nonce, "token": token, "acked_at": time.time()})


def _stop(target: str, nonce: str, event: dict) -> None:
    request = _load(_sibling(target, ".request.json"))
    done = _sibling(target, ".bootstrap.json")
    message = event.get("last_assistant_message")
    if not request or request.get("incarnation") != nonce or not isinstance(message, str) or os.path.exists(done):
        return
    token = request.get("token")
    if not isinstance(token, str) or not token:
        return
    claimed = None
    for line in message.splitlines():
        parts = line.strip().split()
        if len(parts) == 3 and parts[0] == "ROSC-BOOTSTRAP" and parts[1] == token:
            claimed = parts[2].upper()[:16]
    if claimed is None:
        return  # an unrelated or interrupted turn: stays unconfirmed, never counted as an acknowledgement
    reads: list[str] = []
    try:
        with open(_sibling(target, ".reads.jsonl"), "r", encoding="utf-8") as handle:
            for raw in handle.readlines()[-200:]:
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("token") == token and isinstance(item.get("path"), str):
                    reads.append(item["path"])
    except OSError:
        pass
    _atomic_write(done, {"incarnation": nonce, "token": token, "claimed": claimed, "observed_reads": sorted(set(reads)),
                         "ancestors": ancestors(os.getpid()), "acked_at": time.time()})


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    mode = args[0] if args else "session"
    target = os.environ.get(FILE_ENV, "")
    nonce = os.environ.get(NONCE_ENV, "")
    if not target or not nonce:
        return 0  # not a ROSC-managed session: do nothing, never fail the CLI
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        event = {}
    if not isinstance(event, dict):
        event = {}
    try:
        {"session": _session, "read": _read, "submit": _submit, "stop": _stop}.get(mode, lambda *_: None)(target, nonce, event)
    except Exception:  # a hook must never break the CLI
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
