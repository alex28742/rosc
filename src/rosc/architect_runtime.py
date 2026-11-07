"""Project Architect runtime boundary and the OpenCode adapter (P015 / M3-B).

Verified interface (OpenCode 2.2.5, ``opencode serve``, checked against the installed binary, no model call):

* ``opencode serve --hostname 127.0.0.1 --port N`` starts a loopback HTTP server. The password is read from the
  ``OPENCODE_PASSWORD`` environment variable (``OPENCODE_SERVER_PASSWORD`` is its documented fallback in the binary; if
  neither is set the server invents its own, so ROSC always sets one) and clients use HTTP Basic auth, user ``opencode``.
  Without the header every route answers 401. ROSC never puts the password in argv, a URL, a log line, the database or an
  API response.

Local server credential (P017-A): the password belongs to ROSC, not to the Owner.  ``start()`` (reached only through the
explicit Owner activation) draws a fresh 256-bit secret from the OS CSPRNG for that one runtime incarnation, hands it only
to the owned child's environment and uses the same value for ROSC's Basic-auth calls.  An ambient ``OPENCODE_PASSWORD`` in
ROSC's own environment is ignored: it is neither required nor reused, so it cannot create a credential shared between
Projects.  So a restarted backend can still probe an owned child that survived, the secret is kept in a ROSC-private file
(``<data>/architect-runtime/.secrets/<session_id>``, mode 0600, directory 0700, created exclusively without following
links) and deleted only after termination of that child is confirmed.  A missing, foreign-owned, group/world-accessible or
malformed file means "no credential": probes answer False (the persisted ``starting`` blocker stays), ``ask``/``lookup``
refuse, and ``stop`` still works because it relies on /proc identity, not on the password.  This is a local-only
credential for a loopback server; it is not the OpenCode Go / provider credential.

Provider credential (P017.2): OpenCode 2.2.5 keeps interactive ``auth login`` credentials in the same SQLite database
as its sessions (``<XDG_DATA_HOME>/opencode/opencode.db``; ``OPENCODE_DB`` only relocates that whole database), the
legacy ``auth.json`` is NOT read, and there is no auth-only/read-only hook.  Pointing every Architect at one
enrolled profile would therefore share one mutable session store across Projects, so ROSC never does that.  The only
source-verified channel that carries a provider credential without a database is the ``OPENCODE_API_KEY`` environment
variable (it makes the ``opencode-go`` provider available with source "environment").  The Owner opts in with
``ROSC_OPENCODE_PROVIDER_KEY_FILE`` (absolute path of a private 0600 file holding only the key).  The file is
inspected (metadata only) for configuration status and read solely inside ``start()`` - i.e. after the explicit Owner
activation - then handed to that one child's environment, and only for provider ``opencode-go``.  It is never copied,
stored, logged, sent to an API/UI, or inherited by another process; the ambient ``OPENCODE_API_KEY`` of ROSC's own
environment is not inherited.  Each child still has its own empty ``data`` directory, so sessions never mix.
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
import errno
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from .architect_latency import DISABLED, LatencyTrace
from .architect_tools import PERMISSION_PATTERN, SERVER_NAME, TOOL_NAMES, ToolEndpoint

ENV_PASSWORD = "OPENCODE_PASSWORD"
ENV_BINARY = "ROSC_OPENCODE_BIN"
ENV_APPROVED_MODEL = "ROSC_OPENCODE_APPROVED_MODEL"  # "provider/model" the Owner approved for outbound content
ENV_MARKER = "ROSC_ARCHITECT_OWNER"
ENV_PROVIDER_KEY_FILE = "ROSC_OPENCODE_PROVIDER_KEY_FILE"  # opt-in: absolute path of a private file holding the key
ENV_PROVIDER_KEY = "OPENCODE_API_KEY"  # child-only; the variable OpenCode 2.2.5 reads for the opencode-go provider
PROVIDER_KEY_PROVIDER = "opencode-go"
MAX_PROVIDER_KEY_FILE = 4096
_PROVIDER_KEY = re.compile(r"^[\x21-\x7e]{8,512}$")  # one printable token, no whitespace/control characters
ADAPTER_NAME = "opencode"
MIN_PASSWORD = 16
SECRET_BYTES = 32  # 256 bits from the OS CSPRNG
SECRETS_DIRNAME = ".secrets"
_SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
START_TIMEOUT = 25.0
REQUEST_TIMEOUT = 15.0
MAX_RESPONSE_BYTES = 4_000_000  # control calls and ordinary 200-message lookup pages only; never a reply-size limit
LOOKUP_PAGE_SIZE = 200
SINGLE_MESSAGE_PAGE = 1  # fallback page size: one message at a time, so a huge reply never drags unrelated ones along
READ_CHUNK_BYTES = 1 << 20
MAX_LOOKUP_PAGES = 25
MAX_LOOKUP_MESSAGES = 5_000
RUNTIME_AGENT = "plan"
DENY_ALL = [{"action": "*", "resource": "*", "effect": "deny"}]
RESTRICTED_ACTIONS = ("shell", "edit", "read", "webfetch", "subagent")
# P019: the ROSC-owned tool server's tools (``rosc_*``) are the ONLY thing allowed on top of DENY_ALL (last rule wins,
# verified against the installed 2.2.5 permission evaluator).  Native shell/edit/read/webfetch/subagent stay denied.
ALLOW_ROSC_TOOLS = {"action": PERMISSION_PATTERN, "resource": "*", "effect": "allow"}
TOOLS_PERMISSIONS = DENY_ALL + [ALLOW_ROSC_TOOLS]
MCP_CONNECT_TIMEOUT = 15.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the request carries the local server credential, so a 3xx must not move it anywhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # the 3xx then surfaces as an HTTPError and is refused by ``_call``


# No environment proxy either (the credential must go to the loopback server only); no redirect following.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


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


class RuntimeStartUnresolved(RuntimeProblem):
    """Startup failed and the spawned process could not be proven stopped.

    The handle is intentionally persisted by the service so the Project remains blocked until an explicit Owner
    deactivation can prove termination.  It contains process identity only, never credentials.
    """

    def __init__(self, code: str, handle: "RuntimeHandle") -> None:
        super().__init__(code)
        self.handle = handle


@dataclass(frozen=True)
class RuntimeHandle:
    pid: int
    ticks: int
    port: int
    runtime_session_id: str
    rosc_session_id: str = ""  # ROSC's ArchitectSession id: selects this incarnation's local server credential


@dataclass(frozen=True)
class ReplyLookup:
    status: str  # "reply" | "in_progress" | "absent" | "uncertain"
    text: str | None = None


class ArchitectRuntime(Protocol):
    name: str

    def configuration_problem(self, provider_id: str, model_id: str) -> str | None: ...
    def start(self, session_id: str, provider_id: str, model_id: str,
              tools: ToolEndpoint | None = None) -> RuntimeHandle: ...
    def probe(self, session_id: str, handle: RuntimeHandle) -> bool: ...
    def ask(self, handle: RuntimeHandle, runtime_message_id: str, text: str, timeout: float) -> str: ...
    def lookup(self, handle: RuntimeHandle, runtime_message_id: str) -> ReplyLookup: ...
    def stop(self, session_id: str, handle: RuntimeHandle) -> bool: ...


def _outcome(exc: BaseException) -> str:
    """Closed-vocabulary outcome for diagnostics: a ROSC error code, never an exception string."""
    return exc.code if isinstance(exc, RuntimeProblem) else "exception"


# -- process identity (Linux /proc) -----------------------------------------------------------------------------
def process_ticks(pid: int) -> int | None:
    """Start time (clock ticks since boot) of ``pid`` or None. Field 22 of /proc/<pid>/stat."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return int(raw[raw.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def process_group_identity(pid: int) -> tuple[int, int, int] | None:
    """Return ``(start_ticks, process_group, session)`` without exposing argv or environment values."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rindex(")") + 2:].split()
        return int(fields[19]), int(fields[2]), int(fields[3])
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


def owned_group_leader(pid: int, ticks: int, session_id: str) -> bool:
    """Prove the original process is still the leader of the new session/process group ROSC created."""
    identity = process_group_identity(pid)
    return (identity == (ticks, pid, pid) and process_has_marker(pid, session_id))


class OpenCodeRuntime:
    name = ADAPTER_NAME
    supports_tools = True  # the service passes a ToolEndpoint only to runtimes that declare this

    def __init__(self, data_dir: Path, environ: Mapping[str, str] | None = None) -> None:
        self._root = Path(data_dir) / "architect-runtime"
        self._environ = environ if environ is not None else os.environ
        self._children: dict[str, subprocess.Popen] = {}
        self._passwords: dict[str, str] = {}
        self._diag: LatencyTrace = DISABLED  # P024: opt-in, content-free latency diagnostics (off by default)
        self._tl = threading.local()  # per-thread byte counter, only touched while diagnostics are enabled

    def attach_diagnostics(self, trace: LatencyTrace) -> None:
        self._diag = trace

    # -- configuration ----------------------------------------------------------------------------------------
    def _binary(self) -> str | None:
        configured = self._environ.get(ENV_BINARY)
        path = configured or shutil.which("opencode", path=self._environ.get("PATH"))
        if not path or not os.path.isabs(path) or os.path.basename(path) != "opencode":
            return None
        return path if os.access(path, os.X_OK) and os.path.isfile(path) else None

    # -- local server credential (owned by ROSC, one per runtime incarnation) ---------------------------------------
    @property
    def _secrets(self) -> Path:
        return self._root / SECRETS_DIRNAME

    def _secret_path(self, session_id: str) -> Path | None:
        # Only a canonical UUID may name a file: no separators, dots or traversal can reach this path.
        return self._secrets / session_id if _SESSION_ID.match(session_id or "") else None

    def _provision_secret(self, session_id: str) -> str:
        path = self._secret_path(session_id)
        if path is None:
            raise RuntimeUnavailable("secret_provisioning_failed")
        fd: int | None = None
        created: tuple[int, int] | None = None  # (st_dev, st_ino) of the file THIS attempt exclusively created
        try:
            secret = secrets.token_urlsafe(SECRET_BYTES)
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._root.chmod(0o700)
            self._secrets.mkdir(exist_ok=True, mode=0o700)
            info = os.lstat(self._secrets)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise OSError(errno.EPERM, "unsafe secret directory")
            self._secrets.chmod(0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)  # never overwrites
            made = os.fstat(fd)
            created = (made.st_dev, made.st_ino)
            data = secret.encode()
            if os.write(fd, data) != len(data):
                raise OSError(errno.EIO, "short write")
        except (OSError, NotImplementedError):
            # Failing before/at the exclusive create (EEXIST, unsafe path, CSPRNG, directory setup) owns nothing:
            # an existing file or another incarnation's in-memory credential is never touched.  After a successful
            # create only that very file (same device/inode) is removed, so no partial secret stays reusable.
            if created is not None:
                self._remove_created(path, created)
            raise RuntimeUnavailable("secret_provisioning_failed") from None
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._passwords[session_id] = secret
        return secret

    @staticmethod
    def _remove_created(path: Path, created: tuple[int, int]) -> None:
        try:
            current = os.lstat(path)
            if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == created:
                os.unlink(path)
        except OSError:
            pass

    def _password(self, session_id: str) -> str | None:
        """This incarnation's secret from memory, else from the private file after a strict ownership/mode check."""
        cached = self._passwords.get(session_id)
        if cached:
            return cached
        path = self._secret_path(session_id)
        if path is None:
            return None
        try:
            info = os.lstat(path)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or info.st_size > 512):
                return None
            parent = os.lstat(self._secrets)
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
                return None
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                value = os.read(fd, 513).decode()
            finally:
                os.close(fd)
        except (OSError, ValueError):
            return None
        if len(value) < MIN_PASSWORD or not value.isascii() or not value.isprintable():
            return None
        self._passwords[session_id] = value
        return value

    def _discard_secret(self, session_id: str) -> None:
        self._passwords.pop(session_id, None)
        path = self._secret_path(session_id)
        if path is not None:
            try:
                if stat.S_ISREG(os.lstat(path).st_mode):
                    os.unlink(path)
            except OSError:
                pass

    # -- provider credential (opt-in, file based, child-environment only) ---------------------------------------------
    def _provider_key_file_problem(self) -> tuple[str | None, Path | None]:
        """Metadata-only check of the configured key file (never opens or reads it): ``(code, path)``."""
        configured = self._environ.get(ENV_PROVIDER_KEY_FILE)
        if not configured:
            return "provider_key_not_configured", None
        if not os.path.isabs(configured) or "\0" in configured or os.path.normpath(configured) != configured:
            return "provider_key_unsafe", None
        path = Path(configured)
        try:
            parent = os.lstat(path.parent)
            info = os.lstat(path)
        except FileNotFoundError:
            return "provider_key_missing", None
        except OSError:
            return "provider_key_unsafe", None
        uid = os.getuid()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != uid or parent.st_mode & 0o022
                or not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077
                or info.st_nlink != 1 or os.path.realpath(path.parent) != str(path.parent)):
            return "provider_key_unsafe", None
        if not 0 < info.st_size <= MAX_PROVIDER_KEY_FILE:
            return "provider_key_invalid", None
        return None, path

    def _read_provider_key(self) -> str:
        """Read the key at activation time only.  Raises a non-secret code; the value is returned, never stored."""
        problem, path = self._provider_key_file_problem()
        if problem or path is None:
            raise RuntimeNotConfigured(problem or "provider_key_unsafe")
        fd: int | None = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or info.st_nlink != 1):
                raise RuntimeNotConfigured("provider_key_unsafe")
            raw = os.read(fd, MAX_PROVIDER_KEY_FILE + 1)
        except OSError:
            raise RuntimeNotConfigured("provider_key_unsafe") from None
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            key = raw.decode("ascii").strip()
        except UnicodeDecodeError:
            raise RuntimeNotConfigured("provider_key_invalid") from None
        if len(raw) > MAX_PROVIDER_KEY_FILE or not _PROVIDER_KEY.match(key):
            raise RuntimeNotConfigured("provider_key_invalid")
        return key

    def configuration_problem(self, provider_id: str, model_id: str) -> str | None:
        if self._binary() is None:
            return "runtime_binary_missing"
        if self._environ.get(ENV_APPROVED_MODEL) != f"{provider_id}/{model_id}":
            return "model_not_approved"
        if provider_id == PROVIDER_KEY_PROVIDER:
            return self._provider_key_file_problem()[0]
        return None

    # -- HTTP ---------------------------------------------------------------------------------------------------
    def _call(self, session_id: str, port: int, method: str, path: str, body: object | None = None,
              timeout: float = REQUEST_TIMEOUT, max_bytes: int | None = MAX_RESPONSE_BYTES) -> object:
        """``max_bytes=None`` reads one response of whatever size the server sends (only used for a single-message
        page of a completed reply).  It is read in chunks; real exhaustion of memory surfaces as
        ``RuntimeAmbiguous('reply_retrieval_resources')``, never as a completed or failed reply."""
        password = self._password(session_id)
        if password is None:
            raise RuntimeNotConfigured("runtime_secret_missing")
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(port)}{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"})
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                if max_bytes is None:
                    raw = self._read_all(response)
                else:
                    raw = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            exc.read(1024)
            if 300 <= exc.code < 400:  # never followed; the body and Location are neither read further nor reported
                raise RuntimeRefused("redirect_refused") from None
            raise RuntimeRefused(f"http_{exc.code}") from None
        except MemoryError:
            raise RuntimeAmbiguous("reply_retrieval_resources") from None
        except (TimeoutError, socket.timeout):
            raise RuntimeAmbiguous("timeout") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise RuntimeAmbiguous("timeout") from None
            raise RuntimeUnavailable("connection_failed") from None
        except OSError:
            raise RuntimeAmbiguous("connection_lost") from None
        if self._diag.enabled:
            self._tl.bytes = getattr(self._tl, "bytes", 0) + len(raw)
        if max_bytes is not None and len(raw) > max_bytes:
            raise RuntimeRefused("response_too_large")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise RuntimeRefused("malformed_response") from None
        except MemoryError:
            raise RuntimeAmbiguous("reply_retrieval_resources") from None

    @staticmethod
    def _read_all(response) -> bytes:
        chunks = bytearray()
        while True:
            chunk = response.read(READ_CHUNK_BYTES)
            if not chunk:
                return bytes(chunks)
            chunks += chunk

    @staticmethod
    def _data(payload: object) -> object:
        return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload

    # -- lifecycle ----------------------------------------------------------------------------------------------
    def start(self, session_id: str, provider_id: str, model_id: str,
              tools: ToolEndpoint | None = None) -> RuntimeHandle:
        problem = self.configuration_problem(provider_id, model_id)
        if problem:
            raise RuntimeNotConfigured(problem)
        binary = self._binary()
        provider_key = self._read_provider_key() if provider_id == PROVIDER_KEY_PROVIDER else None  # before any side effect
        workdir = self._root / session_id
        workdir.mkdir(parents=True, exist_ok=False, mode=0o700)  # empty: no repository is exposed
        self._root.chmod(0o700)
        workdir.chmod(0o700)
        isolated = {name: workdir / name for name in ("home", "config", "data", "cache", "state", "tmp")}
        for path in isolated.values():
            path.mkdir(mode=0o700)
            path.chmod(0o700)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        secret = self._provision_secret(session_id)  # fresh per incarnation; only ever reached via explicit activation
        # Never fall back to the Owner's global OpenCode config, credentials or sessions.  A future live setup must
        # provision this session-local home explicitly; until then model/provider checks fail truthfully.
        env = {key: value for key, value in self._environ.items() if key in ("PATH", "LANG", "LC_ALL")}
        env.update({"HOME": str(isolated["home"]), "XDG_CONFIG_HOME": str(isolated["config"]),
                    "XDG_DATA_HOME": str(isolated["data"]), "XDG_CACHE_HOME": str(isolated["cache"]),
                    "XDG_STATE_HOME": str(isolated["state"]), "TMPDIR": str(isolated["tmp"])})
        env[ENV_PASSWORD] = secret  # child-local only; the ambient OPENCODE_PASSWORD is deliberately not inherited
        if provider_key is not None:
            env[ENV_PROVIDER_KEY] = provider_key  # this child only; ambient OPENCODE_API_KEY is never inherited
            provider_key = None
        env[ENV_MARKER] = session_id
        try:
            child = subprocess.Popen([binary, "serve", "--hostname", "127.0.0.1", "--port", str(port)],  # type: ignore[list-item]
                                     cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        except OSError:
            self._discard_secret(session_id)
            raise RuntimeUnavailable("spawn_failed") from None
        self._children[session_id] = child
        ticks = process_ticks(child.pid)
        provisional = RuntimeHandle(child.pid, ticks or 0, port, "", session_id)
        try:
            self._await_identity(session_id, port, child)
            if ticks is None:
                raise RuntimeUnavailable("process_identity_unproven")
            self._require_model(session_id, port, provider_id, model_id)
            if tools is not None:
                self._register_tools(session_id, port, tools)
            runtime_session = self._create_session(session_id, port, provider_id, model_id, workdir,
                                                   tools is not None)
        except RuntimeProblem as exc:
            if self._terminate(session_id, provisional, child):
                self._children.pop(session_id, None)
                self._discard_secret(session_id)
                raise
            raise RuntimeStartUnresolved("start_cleanup_unconfirmed", provisional) from exc
        except Exception as exc:
            if self._terminate(session_id, provisional, child):
                self._children.pop(session_id, None)
                self._discard_secret(session_id)
                raise RuntimeUnavailable("runtime_failure") from None
            raise RuntimeStartUnresolved("start_cleanup_unconfirmed", provisional) from exc
        return RuntimeHandle(child.pid, ticks, port, runtime_session, session_id)

    def _await_identity(self, session_id: str, port: int, child: subprocess.Popen) -> None:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeUnavailable("process_exited")
            try:
                info = self._data(self._call(session_id, port, "GET", "/api/info", timeout=2.0))
            except RuntimeNotConfigured:
                raise
            except RuntimeRefused as exc:
                if exc.code == "http_401":  # our own child rejects our own secret: no point waiting
                    raise RuntimeUnavailable("server_auth_failed") from None
                time.sleep(0.25)
                continue
            except RuntimeProblem:
                time.sleep(0.25)
                continue
            if isinstance(info, dict) and info.get("pid") == child.pid:
                return
            raise RuntimeUnavailable("endpoint_identity_mismatch")
        raise RuntimeUnavailable("start_timeout")

    def _require_model(self, session_id: str, port: int, provider_id: str, model_id: str) -> None:
        models = self._data(self._call(session_id, port, "GET", "/api/model"))
        if not isinstance(models, list) or not any(
                isinstance(m, dict) and m.get("providerID") == provider_id and m.get("id") == model_id
                for m in models):
            raise RuntimeUnavailable("model_unavailable")

    def _register_tools(self, session_id: str, port: int, endpoint: ToolEndpoint) -> None:
        """Register the ROSC tool server in THIS child only and require it to answer initialize + tools/list."""
        self._call(session_id, port, "PUT", f"/api/experimental/mcp/{SERVER_NAME}", {"config": {
            "type": "remote", "url": endpoint.url, "headers": {"Authorization": f"Bearer {endpoint.token}"}}})
        deadline = time.monotonic() + MCP_CONNECT_TIMEOUT
        state = "pending"
        while time.monotonic() < deadline:
            listed = self._data(self._call(session_id, port, "GET", "/api/mcp", timeout=5.0))
            entry = next((item for item in listed if isinstance(item, dict) and item.get("name") == SERVER_NAME),
                         None) if isinstance(listed, list) else None
            status = entry.get("status") if isinstance(entry, dict) else None
            state = status.get("status") if isinstance(status, dict) else "pending"
            if state == "connected":
                return
            if state in ("failed", "needs_auth", "disabled"):
                break
            time.sleep(0.25)
        raise RuntimeUnavailable("tools_unavailable")

    def _create_session(self, session_id: str, port: int, provider_id: str, model_id: str, workdir: Path,
                        with_tools: bool = False) -> str:
        wanted = TOOLS_PERMISSIONS if with_tools else DENY_ALL
        created = self._data(self._call(session_id, port, "POST", "/api/session", {
            "title": "ROSC Project Architect", "agent": RUNTIME_AGENT,
            "model": {"id": model_id, "providerID": provider_id},
            "location": {"directory": str(workdir)}, "permissions": wanted}))
        if not isinstance(created, dict) or not isinstance(created.get("id"), str):
            raise RuntimeRefused("malformed_response")
        if created.get("permissions") != wanted:
            raise RuntimeUnavailable("restricted_mode_unconfirmed")  # never send a model request unrestricted
        for action in RESTRICTED_ACTIONS:
            decision = self._data(self._call(session_id, port, "POST", f"/api/session/{created['id']}/permission",
                                             {"action": action, "resources": ["*"]}))
            if not isinstance(decision, dict) or decision.get("effect") != "deny":
                raise RuntimeUnavailable("restricted_mode_unconfirmed")
        # The evaluator itself must agree: every ROSC tool allowed, anything else (even a lookalike) denied.
        for action, effect in [*((f"{SERVER_NAME}_{name}", "allow" if with_tools else "deny") for name in TOOL_NAMES),
                               ("other_tool", "deny"), (f"x{SERVER_NAME}_briefing", "deny")]:
            decision = self._data(self._call(session_id, port, "POST", f"/api/session/{created['id']}/permission",
                                             {"action": action, "resources": ["*"]}))
            if not isinstance(decision, dict) or decision.get("effect") != effect:
                raise RuntimeUnavailable("restricted_mode_unconfirmed")
        return created["id"]

    def probe(self, session_id: str, handle: RuntimeHandle) -> bool:
        if not owned_process(handle.pid, handle.ticks, session_id):
            return False
        if handle.rosc_session_id and handle.rosc_session_id != session_id:
            return False
        try:
            info = self._data(self._call(session_id, handle.port, "GET", "/api/info", timeout=3.0))
            if not (isinstance(info, dict) and info.get("pid") == handle.pid):
                return False
            found = self._data(self._call(session_id, handle.port, "GET", f"/api/session/{handle.runtime_session_id}",
                                          timeout=3.0))
        except RuntimeProblem:
            return False
        return isinstance(found, dict) and found.get("id") == handle.runtime_session_id

    # -- conversation ---------------------------------------------------------------------------------------------
    def _page(self, handle: RuntimeHandle, query: str, max_bytes: int | None = MAX_RESPONSE_BYTES) -> object:
        return self._call(handle.rosc_session_id, handle.port, "GET",
                          f"/api/session/{handle.runtime_session_id}/message?{query}", max_bytes=max_bytes)

    def _single_message_page(self, handle: RuntimeHandle, cursor: str | None) -> object:
        """Re-read the cursor that exceeded the control cap as a one-message page, without a byte cap: the message
        may be a complete reply of any size the provider produced.  Memory is bounded by that one message, not by a
        batch of unrelated ones; there is deliberately no fixed reply-size ceiling here.  A transport failure is
        ``RuntimeAmbiguous`` (recoverable by reconciling), never a verdict on the reply."""
        params = {"order": "asc", "limit": SINGLE_MESSAGE_PAGE} if cursor is None else {
            "cursor": cursor, "limit": SINGLE_MESSAGE_PAGE}
        try:
            return self._page(handle, urllib.parse.urlencode(params), max_bytes=None)
        except RuntimeRefused as exc:
            if exc.code.startswith("http_"):
                raise RuntimeAmbiguous("lookup_failed") from None
            raise

    def ask(self, handle: RuntimeHandle, runtime_message_id: str, text: str, timeout: float) -> str:
        sid = handle.runtime_session_id
        diag = self._diag
        started = diag.now()
        try:
            self._call(handle.rosc_session_id, handle.port, "POST", f"/api/session/{sid}/prompt",
                       {"id": runtime_message_id, "text": text}, timeout=min(timeout, 30.0))
        except Exception as exc:
            diag.emit("prompt_admitted", duration_ms=diag.since(started), outcome=_outcome(exc))
            raise
        diag.emit("prompt_admitted", duration_ms=diag.since(started), outcome="ok")
        started = diag.now()
        try:
            self._call(handle.rosc_session_id, handle.port, "POST", f"/api/experimental/session/{sid}/wait", timeout=timeout)
        except RuntimeUnavailable:
            diag.emit("wait_returned", duration_ms=diag.since(started), timeout_ms=timeout * 1000.0,
                      outcome="connection_lost")
            raise RuntimeAmbiguous("connection_lost") from None
        except Exception as exc:
            diag.emit("wait_returned", duration_ms=diag.since(started), timeout_ms=timeout * 1000.0,
                      outcome=_outcome(exc))
            raise
        diag.emit("wait_returned", duration_ms=diag.since(started), timeout_ms=timeout * 1000.0, outcome="idle")
        result = self.lookup(handle, runtime_message_id)
        if result.status == "reply" and result.text:
            return result.text
        if result.status in ("in_progress", "uncertain"):
            raise RuntimeAmbiguous("timeout")
        raise RuntimeRefused("no_reply")

    def lookup(self, handle: RuntimeHandle, runtime_message_id: str) -> ReplyLookup:
        """Traverse the verified OpenCode cursor API.  ``absent`` requires the terminal page."""
        diag = self._diag
        if not diag.enabled:
            return self._lookup(handle, runtime_message_id, None)
        stats = {"requests": 0, "messages": 0, "fallbacks": 0}
        self._tl.bytes = 0
        started = diag.now()
        outcome = "interrupted"  # a BaseException (cancellation) is reported as such, never as a finished lookup
        try:
            result = self._lookup(handle, runtime_message_id, stats)
            outcome = result.status
            return result
        except Exception as exc:
            outcome = _outcome(exc)
            raise
        finally:
            diag.emit("lookup", duration_ms=diag.since(started), requests=stats["requests"], messages=stats["messages"],
                      fallbacks=stats["fallbacks"], bytes=getattr(self._tl, "bytes", 0), outcome=outcome)

    def _lookup(self, handle: RuntimeHandle, runtime_message_id: str, stats: dict | None) -> ReplyLookup:
        cursor: str | None = None
        cursors: set[str] = set()
        seen = False
        count = 0
        pages = 0
        while pages < MAX_LOOKUP_PAGES:
            pages += 1
            if cursor is None:
                query = urllib.parse.urlencode({"order": "asc", "limit": LOOKUP_PAGE_SIZE})
            else:
                query = urllib.parse.urlencode({"cursor": cursor, "limit": LOOKUP_PAGE_SIZE})
            if stats is not None:
                stats["requests"] += 1
            try:
                payload = self._page(handle, query)
            except RuntimeRefused as exc:
                if exc.code == "response_too_large":
                    if stats is not None:
                        stats["requests"] += 1
                        stats["fallbacks"] += 1
                    # A page of 200 messages exceeded the control-call cap.  That is a transport property, not a
                    # verdict on any reply: fetch this same cursor again one message at a time, each read in full.
                    payload = self._single_message_page(handle, cursor)
                    pages -= 1  # a scan-length bound, not a reply bound: fallback pages do not consume it
                elif exc.code.startswith("http_"):
                    raise RuntimeAmbiguous("lookup_failed") from None
                else:
                    raise
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise RuntimeRefused("malformed_response")
            page_cursor = payload.get("cursor")
            if not isinstance(page_cursor, dict) or page_cursor.get("next") is not None and not isinstance(
                    page_cursor.get("next"), str):
                raise RuntimeRefused("malformed_response")
            for item in payload["data"]:
                count += 1
                if stats is not None:
                    stats["messages"] = count
                if count > MAX_LOOKUP_MESSAGES:
                    return ReplyLookup("uncertain")
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise RuntimeRefused("malformed_response")
                if item.get("id") == runtime_message_id:
                    if seen or item.get("type") != "user":
                        return ReplyLookup("uncertain")
                    seen = True
                    continue
                if seen and item.get("type") == "user":
                    # An assistant after this point could belong to the later user message.
                    return ReplyLookup("uncertain")
                if seen and item.get("type") == "assistant":
                    completed = (item.get("time") or {}).get("completed") if isinstance(item.get("time"), dict) else None
                    if completed is None and not item.get("error"):
                        return ReplyLookup("in_progress")
                    if item.get("error"):
                        raise RuntimeRefused("provider_error")  # also after earlier successful tool steps
                    if item.get("finish") == "tool-calls":
                        # A completed intermediate step: the model asked for tools and the run continues with a
                        # later assistant step.  Its text is never the answer.  If the stream ends here, the final
                        # step is simply not produced yet (reported below as in_progress, never as a reply).
                        continue
                    if item.get("finish") != "stop":
                        raise RuntimeRefused("provider_error")
                    content = item.get("content")
                    if not isinstance(content, list):
                        raise RuntimeRefused("malformed_response")
                    parts = [part.get("text") for part in content
                             if isinstance(part, dict) and part.get("type") == "text"
                             and isinstance(part.get("text"), str)]
                    text = "\n".join(part for part in parts if part).strip()
                    if not text:
                        raise RuntimeRefused("empty_reply")
                    return ReplyLookup("reply", text)
            next_cursor = page_cursor.get("next")
            if next_cursor is None:
                return ReplyLookup("in_progress" if seen else "absent")
            if not next_cursor or next_cursor in cursors:
                return ReplyLookup("uncertain")
            cursors.add(next_cursor)
            cursor = next_cursor
        return ReplyLookup("uncertain")

    # -- shutdown -------------------------------------------------------------------------------------------------
    @staticmethod
    def _group_absent(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except OSError as exc:
            return exc.errno == errno.ESRCH
        return False

    @classmethod
    def _termination_confirmed(cls, handle: RuntimeHandle) -> bool:
        """The original PID identity and its process group must both be gone (or the PID reliably reused)."""
        if not cls._group_absent(handle.pid):
            return False
        identity = process_group_identity(handle.pid)
        if identity is not None:
            return identity[0] != handle.ticks
        try:
            os.kill(handle.pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    @classmethod
    def _wait_group_absent(cls, handle: RuntimeHandle, timeout: float,
                           child: subprocess.Popen | None = None) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if child is not None:
                child.poll()  # reap a terminated direct child so its process group can disappear
            if cls._termination_confirmed(handle):
                return True
            time.sleep(0.1)
        if child is not None:
            child.poll()
        return cls._termination_confirmed(handle)

    @classmethod
    def _terminate(cls, session_id: str, handle: RuntimeHandle,
                   child: subprocess.Popen | None = None) -> bool:
        """Signal only a freshly re-proven owned group; success requires the whole group to be absent."""
        if cls._termination_confirmed(handle):
            return True
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not owned_group_leader(handle.pid, handle.ticks, session_id):
                return False
            try:
                os.killpg(handle.pid, sig)
            except ProcessLookupError:
                return cls._termination_confirmed(handle)
            except OSError:
                return False
            if cls._wait_group_absent(handle, 5.0, child):
                if child is not None:
                    try:
                        child.wait(timeout=0)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
                return True
        return False

    def stop(self, session_id: str, handle: RuntimeHandle) -> bool:
        """Stop the process only if ROSC can prove it launched it. Returns False for anything it cannot prove."""
        child = self._children.get(session_id)
        if child is not None and child.pid != handle.pid:
            return False
        stopped = self._terminate(session_id, handle, child)
        if stopped:
            self._children.pop(session_id, None)
            self._discard_secret(session_id)  # only after termination of the whole group is confirmed
        return stopped
