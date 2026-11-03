"""Project Architect runtime boundary and the OpenCode adapter (P015 / M3-B).

Verified interface (OpenCode 2.2.5, ``opencode serve``, checked against the installed binary, no model call):

* ``opencode serve --hostname 127.0.0.1 --port N`` starts a loopback HTTP server. The password is read from the
  ``OPENCODE_PASSWORD`` environment variable and clients use HTTP Basic auth, user ``opencode``. Without the header every
  route answers 401. ROSC never puts the password in argv, a URL, a log line, the database or an API response.
* ``GET /api/info`` reports the server ``pid``; that is how ROSC proves the endpoint is the process it launched.
* ``GET /api/model`` lists provider/model pairs; ``POST /api/session`` accepts ``agent``, ``model``, ``location`` and a
  ``permissions`` rule list; a deny-all rule is enforced by the server (``POST /api/session/{id}/permission`` returns
  ``effect=deny`` for shell/edit/read/webfetch/subagent), so tools are restricted by configuration, not by prompt text.
* ``POST /api/session/{id}/prompt`` takes a caller-chosen ``msg_*`` id (used here as the delivery correlation id),
  ``POST /api/experimental/session/{id}/wait`` blocks until the agent loop is idle and
  ``GET /api/session/{id}/message`` returns the projected messages including ``assistant`` text.

NOT verified live: an actual model reply, and whether a repeated prompt id is deduplicated by the provider. ROSC
therefore never re-sends an ambiguous attempt; it only looks the id up again.

Only a process whose identity ROSC can prove (pid + /proc start time + a per-session marker in its environment) is ever
signalled. A foreign process, including the Owner's background OpenCode service, is never adopted or stopped.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

ENV_PASSWORD = "OPENCODE_PASSWORD"
ENV_BINARY = "ROSC_OPENCODE_BIN"
ENV_APPROVED_MODEL = "ROSC_OPENCODE_APPROVED_MODEL"  # "provider/model" the Owner approved for outbound content
ENV_MARKER = "ROSC_ARCHITECT_OWNER"
ADAPTER_NAME = "opencode"
MIN_PASSWORD = 16
START_TIMEOUT = 25.0
REQUEST_TIMEOUT = 15.0
RUNTIME_AGENT = "plan"
DENY_ALL = [{"action": "*", "resource": "*", "effect": "deny"}]


class RuntimeProblem(Exception):
    """Base class. ``code`` is a short machine-readable, non-secret reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RuntimeNotConfigured(RuntimeProblem):
    """The Owner has not configured/approved this runtime (nothing was started)."""


class RuntimeUnavailable(RuntimeProblem):
    """The runtime could not be reached or started; nothing was delivered."""


class RuntimeRefused(RuntimeProblem):
    """The provider answered but refused or produced no usable reply."""


class RuntimeAmbiguous(RuntimeProblem):
    """The prompt may or may not have been received (timeout or broken connection after sending)."""


@dataclass(frozen=True)
class RuntimeHandle:
    pid: int
    ticks: int
    port: int
    runtime_session_id: str


@dataclass(frozen=True)
class ReplyLookup:
    status: str  # "reply" | "in_progress" | "absent"
    text: str | None = None


class ArchitectRuntime(Protocol):
    name: str

    def configuration_problem(self, provider_id: str, model_id: str) -> str | None: ...
    def start(self, session_id: str, provider_id: str, model_id: str) -> RuntimeHandle: ...
    def probe(self, session_id: str, handle: RuntimeHandle) -> bool: ...
    def ask(self, handle: RuntimeHandle, runtime_message_id: str, text: str, timeout: float) -> str: ...
    def lookup(self, handle: RuntimeHandle, runtime_message_id: str) -> ReplyLookup: ...
    def stop(self, session_id: str, handle: RuntimeHandle) -> bool: ...


# -- process identity (Linux /proc) -----------------------------------------------------------------------------
def process_ticks(pid: int) -> int | None:
    """Start time (clock ticks since boot) of ``pid`` or None. Field 22 of /proc/<pid>/stat."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return int(raw[raw.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def process_has_marker(pid: int, session_id: str) -> bool:
    """True only if the process environment carries ROSC's per-session marker. Only the marker is compared; the rest
    of the environment (which contains the password) is never stored or returned."""
    try:
        data = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    return f"{ENV_MARKER}={session_id}".encode() in data.split(b"\0")


def owned_process(pid: int | None, ticks: int | None, session_id: str) -> bool:
    return (pid is not None and ticks is not None and process_ticks(pid) == ticks
            and process_has_marker(pid, session_id))


class OpenCodeRuntime:
    name = ADAPTER_NAME

    def __init__(self, data_dir: Path, environ: Mapping[str, str] | None = None) -> None:
        self._root = Path(data_dir) / "architect-runtime"
        self._environ = environ if environ is not None else os.environ
        self._children: dict[str, subprocess.Popen] = {}

    # -- configuration ----------------------------------------------------------------------------------------
    def _binary(self) -> str | None:
        configured = self._environ.get(ENV_BINARY)
        path = configured or shutil.which("opencode", path=self._environ.get("PATH"))
        if not path or not os.path.isabs(path) or os.path.basename(path) != "opencode":
            return None
        return path if os.access(path, os.X_OK) and os.path.isfile(path) else None

    def _password(self) -> str | None:
        value = self._environ.get(ENV_PASSWORD)
        return value if value and len(value) >= MIN_PASSWORD else None

    def configuration_problem(self, provider_id: str, model_id: str) -> str | None:
        if self._binary() is None:
            return "runtime_binary_missing"
        if self._password() is None:
            return "runtime_credential_missing"
        if self._environ.get(ENV_APPROVED_MODEL) != f"{provider_id}/{model_id}":
            return "model_not_approved"
        return None

    # -- HTTP ---------------------------------------------------------------------------------------------------
    def _call(self, port: int, method: str, path: str, body: object | None = None,
              timeout: float = REQUEST_TIMEOUT) -> object:
        password = self._password()
        if password is None:
            raise RuntimeNotConfigured("runtime_credential_missing")
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(port)}{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(4_000_000)
        except urllib.error.HTTPError as exc:
            exc.read(1024)
            raise RuntimeRefused(f"http_{exc.code}") from None
        except (TimeoutError, socket.timeout):
            raise RuntimeAmbiguous("timeout") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise RuntimeAmbiguous("timeout") from None
            raise RuntimeUnavailable("connection_failed") from None
        except OSError:
            raise RuntimeAmbiguous("connection_lost") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise RuntimeRefused("malformed_response") from None

    @staticmethod
    def _data(payload: object) -> object:
        return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload

    # -- lifecycle ----------------------------------------------------------------------------------------------
    def start(self, session_id: str, provider_id: str, model_id: str) -> RuntimeHandle:
        problem = self.configuration_problem(provider_id, model_id)
        if problem:
            raise RuntimeNotConfigured(problem)
        binary = self._binary()
        workdir = self._root / session_id
        workdir.mkdir(parents=True, exist_ok=False)  # an empty ROSC-owned directory: no repository is exposed
        (self._root / "logs").mkdir(parents=True, exist_ok=True)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = {key: value for key, value in self._environ.items()
               if key in ("HOME", "PATH", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
                          "LANG", "LC_ALL", "TMPDIR")}
        env[ENV_PASSWORD] = self._password()  # type: ignore[assignment]
        env[ENV_MARKER] = session_id
        log = open(self._root / "logs" / f"{session_id}.log", "ab")
        try:
            child = subprocess.Popen([binary, "serve", "--hostname", "127.0.0.1", "--port", str(port)],  # type: ignore[list-item]
                                     cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True)
        except OSError:
            raise RuntimeUnavailable("spawn_failed") from None
        finally:
            log.close()
        self._children[session_id] = child
        ticks = process_ticks(child.pid)
        try:
            self._await_identity(port, child)
            if ticks is None:
                raise RuntimeUnavailable("process_identity_unproven")
            self._require_model(port, provider_id, model_id)
            runtime_session = self._create_session(port, provider_id, model_id, workdir)
        except RuntimeProblem:
            self._terminate(child)
            self._children.pop(session_id, None)
            raise
        return RuntimeHandle(child.pid, ticks, port, runtime_session)

    def _await_identity(self, port: int, child: subprocess.Popen) -> None:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeUnavailable("process_exited")
            try:
                info = self._data(self._call(port, "GET", "/api/info", timeout=2.0))
            except RuntimeNotConfigured:
                raise
            except RuntimeProblem:
                time.sleep(0.25)
                continue
            if isinstance(info, dict) and info.get("pid") == child.pid:
                return
            raise RuntimeUnavailable("endpoint_identity_mismatch")
        raise RuntimeUnavailable("start_timeout")

    def _require_model(self, port: int, provider_id: str, model_id: str) -> None:
        models = self._data(self._call(port, "GET", "/api/model"))
        if not isinstance(models, list) or not any(
                isinstance(m, dict) and m.get("providerID") == provider_id and m.get("id") == model_id
                for m in models):
            raise RuntimeUnavailable("model_unavailable")

    def _create_session(self, port: int, provider_id: str, model_id: str, workdir: Path) -> str:
        created = self._data(self._call(port, "POST", "/api/session", {
            "title": "ROSC Project Architect", "agent": RUNTIME_AGENT,
            "model": {"id": model_id, "providerID": provider_id},
            "location": {"directory": str(workdir)}, "permissions": DENY_ALL}))
        if not isinstance(created, dict) or not isinstance(created.get("id"), str):
            raise RuntimeRefused("malformed_response")
        if created.get("permissions") != DENY_ALL:
            raise RuntimeUnavailable("restricted_mode_unconfirmed")  # never send a model request unrestricted
        decision = self._data(self._call(port, "POST", f"/api/session/{created['id']}/permission",
                                         {"action": "shell", "resources": ["*"]}))
        if not isinstance(decision, dict) or decision.get("effect") != "deny":
            raise RuntimeUnavailable("restricted_mode_unconfirmed")
        return created["id"]

    def probe(self, session_id: str, handle: RuntimeHandle) -> bool:
        if not owned_process(handle.pid, handle.ticks, session_id):
            return False
        try:
            info = self._data(self._call(handle.port, "GET", "/api/info", timeout=3.0))
            if not (isinstance(info, dict) and info.get("pid") == handle.pid):
                return False
            found = self._data(self._call(handle.port, "GET", f"/api/session/{handle.runtime_session_id}",
                                          timeout=3.0))
        except RuntimeProblem:
            return False
        return isinstance(found, dict) and found.get("id") == handle.runtime_session_id

    # -- conversation ---------------------------------------------------------------------------------------------
    def ask(self, handle: RuntimeHandle, runtime_message_id: str, text: str, timeout: float) -> str:
        sid = handle.runtime_session_id
        self._call(handle.port, "POST", f"/api/session/{sid}/prompt",
                   {"id": runtime_message_id, "text": text}, timeout=min(timeout, 30.0))
        try:
            self._call(handle.port, "POST", f"/api/experimental/session/{sid}/wait", timeout=timeout)
        except RuntimeUnavailable:
            raise RuntimeAmbiguous("connection_lost") from None
        result = self.lookup(handle, runtime_message_id)
        if result.status == "reply" and result.text:
            return result.text
        if result.status == "in_progress":
            raise RuntimeAmbiguous("timeout")
        raise RuntimeRefused("no_reply")

    def lookup(self, handle: RuntimeHandle, runtime_message_id: str) -> ReplyLookup:
        payload = self._data(self._call(handle.port, "GET",
                                        f"/api/session/{handle.runtime_session_id}/message?order=asc&limit=200"))
        if not isinstance(payload, list):
            raise RuntimeRefused("malformed_response")
        seen = False
        for item in payload:
            if not isinstance(item, dict):
                continue
            if item.get("id") == runtime_message_id:
                seen = True
            elif seen and item.get("type") == "assistant":
                if item.get("error") or item.get("finish") not in ("stop",):
                    if (item.get("time") or {}).get("completed") is None and not item.get("error"):
                        return ReplyLookup("in_progress")
                    raise RuntimeRefused("provider_error")
                parts = [c.get("text") for c in item.get("content") or []
                         if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)]
                text = "\n".join(p for p in parts if p).strip()
                if not text:
                    raise RuntimeRefused("empty_reply")
                return ReplyLookup("reply", text)
        return ReplyLookup("in_progress" if seen else "absent")

    # -- shutdown -------------------------------------------------------------------------------------------------
    @staticmethod
    def _terminate(child: subprocess.Popen) -> None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def stop(self, session_id: str, handle: RuntimeHandle) -> bool:
        """Stop the process only if ROSC can prove it launched it. Returns False for anything it cannot prove."""
        if not owned_process(handle.pid, handle.ticks, session_id):
            return False
        child = self._children.pop(session_id, None)
        if child is not None and child.pid == handle.pid:
            self._terminate(child)
            return True
        try:
            os.kill(handle.pid, signal.SIGTERM)
            for _ in range(50):
                if process_ticks(handle.pid) != handle.ticks:
                    return True
                time.sleep(0.1)
            if owned_process(handle.pid, handle.ticks, session_id):
                os.kill(handle.pid, signal.SIGKILL)
        except OSError:
            pass
        return True
