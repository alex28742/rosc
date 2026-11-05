#!/usr/bin/env python3
"""ROSC local developer launcher: ``make local-up | local-down | local-status | local-logs``.

Starts/stops ONLY the ROSC developer web pair (uvicorn backend + Vite dev server) on loopback. It is not the ROSC
Worker/Run runtime and starts no agent. Linux only (needs /proc). Python standard library only.

Safety rules (see README "Local development launcher"):
* never creates, migrates or resets a database; it only reads the existing SQLite file read-only;
* never kills by port, name or pattern; it signals a process group only while the leader's recorded identity
  (pid + /proc start time + command line + cwd + own process group) is still proven;
* a foreign process on a required port is refused, never adopted or killed;
* anything it cannot prove is reported as UNKNOWN and left alone.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOOPBACK = "127.0.0.1"
DEFAULT_BACKEND_PORT = 8010
DEFAULT_FRONTEND_PORT = 5173
# Frontend ports the backend Origin allowlist (src/rosc/app.py LOOPBACK_PORTS) accepts. Do not widen here.
ALLOWED_FRONTEND_PORTS = (5173, 4173)
MIN_NODE_MAJOR = 24

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
EXIT_STOPPED, EXIT_DEGRADED, EXIT_UNKNOWN = 3, 4, 5

RUNNING, STOPPED, DEGRADED, UNKNOWN = "RUNNING", "STOPPED", "DEGRADED", "UNKNOWN"
STATUS_EXIT = {RUNNING: EXIT_OK, STOPPED: EXIT_STOPPED, DEGRADED: EXIT_DEGRADED, UNKNOWN: EXIT_UNKNOWN}


class LauncherError(Exception):
    """An actionable failure; ``code`` is the process exit status."""

    def __init__(self, message: str, code: int = EXIT_FAIL):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------------------------- configuration

def resolve_data_dir(environ: dict[str, str] | None = None, root: Path = ROOT) -> Path:
    """Same rule as ``rosc.config.resolve_data_dir`` (pinned by a parity test) and, like it, WITHOUT any mkdir:
    a non-empty ``ROSC_DATA_DIR`` wins, else ``<checkout>/data``. The launcher always runs from a checkout."""
    env = os.environ if environ is None else environ
    explicit = (env.get("ROSC_DATA_DIR") or "").strip()
    return (Path(explicit).expanduser() if explicit else Path(root) / "data").resolve()


@dataclass
class Config:
    root: Path = ROOT
    data_dir: Path = field(default_factory=resolve_data_dir)
    backend_port: int = DEFAULT_BACKEND_PORT
    frontend_port: int = DEFAULT_FRONTEND_PORT
    ready_timeout: float = 45.0
    stop_timeout: float = 10.0
    # Which values the user asked for explicitly (flag or env). Implicit defaults never conflict with a running pair.
    backend_explicit: bool = False
    frontend_explicit: bool = False
    data_dir_explicit: bool = False

    @property
    def state_dir(self) -> Path:
        return self.root / ".local-dev"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "lock"

    def log_file(self, name: str) -> Path:
        return self.state_dir / f"{name}.log"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "rosc.db"

    @property
    def python(self) -> Path:
        return self.root / ".venv" / "bin" / "python3"

    @property
    def frontend_dir(self) -> Path:
        return self.root / "frontend"

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.frontend_port}"

    @property
    def backend_url(self) -> str:
        return f"http://{LOOPBACK}:{self.backend_port}"


def validate_ports(backend: int, frontend: int) -> None:
    """The backend port is free-form (unprivileged range); the frontend port is restricted by the Origin allowlist."""
    if not 1024 <= backend <= 65535:
        raise LauncherError(f"Backend port {backend} is outside the allowed range 1024..65535. "
                            f"Example: local-app up -p {DEFAULT_BACKEND_PORT}", EXIT_USAGE)
    if frontend not in ALLOWED_FRONTEND_PORTS:
        raise LauncherError(f"Frontend port {frontend} is not allowed: the backend Origin allowlist only accepts "
                            f"{' or '.join(map(str, ALLOWED_FRONTEND_PORTS))}. The launcher will not weaken that policy.",
                            EXIT_USAGE)
    if backend == frontend:
        raise LauncherError(f"Backend port {backend} collides with the frontend port {frontend}. "
                            f"Choose another backend port, e.g. local-app up -p {DEFAULT_BACKEND_PORT}", EXIT_USAGE)


def parse_port(value: str, what: str = "port") -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise LauncherError(f"Invalid {what} {value!r}: expected an integer 1024..65535", EXIT_USAGE) from None
    if not 1024 <= port <= 65535:
        raise LauncherError(f"Invalid {what} {port}: expected 1024..65535", EXIT_USAGE)
    return port


def effective_config(cfg: Config, state: dict) -> Config:
    """The managed pair's RECORDED ports/data dir win over the invoking shell's defaults (status/down/idempotent up)."""
    rec = state.get("config") if isinstance(state, dict) else None
    if not isinstance(rec, dict):
        return cfg
    b, f, d = rec.get("backend_port"), rec.get("frontend_port"), rec.get("data_dir")
    if not (isinstance(b, int) and isinstance(f, int) and isinstance(d, str)):
        return cfg
    return replace(cfg, backend_port=b, frontend_port=f, data_dir=Path(d))


# ----------------------------------------------------------------------------------- occupied-port diagnostics
# Best effort, Linux /proc only, strictly read-only. It never prints environment variables or full command lines,
# never claims ownership from a matching name, and never adopts or signals anything.
_TCP_LISTEN = "0A"
_LOOPBACK_OR_ANY = {"0100007F", "00000000", "00000000000000000000000001000000", "00000000000000000000000000000000"}
_ROSC_BACKEND_MARKERS = ("uvicorn", "rosc.app:app")


def _listener_inodes(proc: Path, port: int) -> set[str]:
    inodes: set[str] = set()
    for table in ("net/tcp", "net/tcp6"):
        try:
            lines = (proc / table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != _TCP_LISTEN:
                continue
            address, _, hex_port = fields[1].rpartition(":")
            if hex_port.upper() == f"{port:04X}" and address.upper() in _LOOPBACK_OR_ANY:
                inodes.add(fields[9])
    return inodes


def _pids_for_inodes(proc: Path, inodes: set[str]) -> tuple[list[int], int]:
    """Matching pids plus how many process fd tables could not be read (other users, hidepid)."""
    found: list[int] = []
    unreadable = 0
    wanted = {f"socket:[{inode}]" for inode in inodes}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        targets: set[str] = set()
        try:
            descriptors = list((entry / "fd").iterdir())
        except OSError:
            unreadable += 1
            continue
        for descriptor in descriptors:
            with contextlib.suppress(OSError):  # an fd can close between listing and reading it
                targets.add(os.readlink(descriptor))
        if targets & wanted:
            found.append(int(entry.name))
    return sorted(found), unreadable


def _describe_process(proc: Path, pid: int) -> str:
    """Bounded identification: executable name and a coarse resemblance tag. No argv values, no environment."""
    try:
        comm = (proc / str(pid) / "comm").read_text().strip()[:32] or "?"
    except OSError:
        return f"pid {pid} (details not readable)"
    try:
        argv = (proc / str(pid) / "cmdline").read_bytes()[:4096].split(b"\0")
    except OSError:
        return f"pid {pid}, process name {comm!r}, command line not readable: identity UNKNOWN"
    words = {part.decode(errors="replace") for part in argv}
    if all(any(marker in word for word in words) for marker in _ROSC_BACKEND_MARKERS):
        verdict = ("resembles a ROSC backend (uvicorn rosc.app:app) - NOT verified as launched by this launcher; "
                   "it may be an old backend")
    else:
        verdict = "does not resemble a ROSC backend: identity UNKNOWN"
    return f"pid {pid}, process name {comm!r}; {verdict}"


def listener_diagnostics(port: int, proc: Path = Path("/proc")) -> str:
    """Read-only explanation of who listens on ``port``; empty string when /proc offers nothing."""
    try:
        inodes = _listener_inodes(proc, port)
        if not inodes:
            return ""
        pids, unreadable = _pids_for_inodes(proc, inodes)
    except OSError:
        return ""
    if pids:
        return "\n  Listener (read-only /proc inspection): " + "; ".join(_describe_process(proc, pid) for pid in pids)
    note = f" ({unreadable} processes could not be inspected)" if unreadable else ""
    return f"\n  A listener exists on port {port} but its process is not visible to this user{note}; inspect it manually."


def occupied_message(label: str, port: int, diagnostics: str = "") -> str:
    if label == "backend":
        retry = f"local-app up -p {'8051' if port == 8050 else '8050'}   (any free port 1024..65535)"
    else:
        other = [p for p in ALLOWED_FRONTEND_PORTS if p != port]
        allowed = ", ".join(map(str, ALLOWED_FRONTEND_PORTS))
        retry = (f"local-app up --frontend-port {other[0]}   (allowed frontend ports: {allowed})" if other
                 else f"free port {port} (the only allowed frontend port: {allowed})")
    return (f"The {label} port {port} is already in use by a process this launcher does not own. It was not touched "
            f"and nothing was started.{diagnostics}\n  Retry with: {retry}")


def service_commands(cfg: Config) -> dict[str, dict]:
    """Argv/cwd/env additions for each service. No secrets are placed in argv."""
    # `npm` is a node script; exec node on its real path directly so the command line is stable from the first
    # instant (a `#!/usr/bin/env node` hop would change it). npm later rewrites its own title to "npm run dev ...".
    node = shutil.which("node") or "node"
    npm = os.path.realpath(shutil.which("npm") or "npm")
    return {
        "backend": {
            "argv": [str(cfg.python), "-m", "uvicorn", "rosc.app:app", "--host", LOOPBACK, "--port",
                     str(cfg.backend_port), "--reload", "--reload-dir", str(cfg.root / "src")],
            "cwd": str(cfg.root),
            "env": {"ROSC_DATA_DIR": str(cfg.data_dir)},
        },
        "frontend": {
            "argv": [node, npm, "run", "dev", "--", "--host", LOOPBACK, "--port", str(cfg.frontend_port), "--strictPort"],
            "cwd": str(cfg.frontend_dir),
            "title": "npm",
            "env": {"ROSC_API_TARGET": cfg.backend_url},
        },
    }


# ------------------------------------------------------------------------------------------ /proc identity

def read_stat(pid: int) -> dict | None:
    """pid, starttime (clock ticks since boot), pgrp, session from /proc/<pid>/stat; None if gone/unreadable."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    # comm may contain spaces/parens: split after the LAST ')'.
    rest = raw[raw.rfind(")") + 1:].split()
    try:
        return {"state": rest[0], "pgrp": int(rest[2]), "session": int(rest[3]), "starttime": int(rest[19])}
    except (IndexError, ValueError):
        return None


def read_cmdline(pid: int) -> list[str] | None:
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [a.decode(errors="replace") for a in data.split(b"\0") if a]


def read_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def cmdline_matches(actual: list[str] | None, expected: list[str], title: str | None = None) -> bool:
    """Exact argv, or the argv after a program rewrote its own process title (npm does: it drops the `--`).

    The rewritten form must still start with the same program name and contain only tokens from the original argv,
    so it can never match an unrelated command."""
    if actual is None:
        return False
    if actual == expected:
        return True
    tokens = " ".join(actual).split()
    allowed = set(expected[1:])
    return bool(tokens) and tokens[0] in {Path(expected[0]).name, title} and all(t in allowed for t in tokens[1:]) \
        and len(tokens) > 1


def snapshot_identity(pid: int, argv: list[str], cwd: str, title: str | None = None) -> dict:
    stat = read_stat(pid)
    if stat is None:
        raise LauncherError(f"Process {pid} vanished immediately after start", EXIT_FAIL)
    return {"pid": pid, "starttime": stat["starttime"], "pgid": stat["pgrp"], "argv": argv, "cwd": cwd, "title": title}


def identity_matches(rec: dict) -> bool:
    """True only if /proc still shows the exact process we started: same start time, command, cwd, own group."""
    pid = rec.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    stat = read_stat(pid)
    if stat is None or stat["state"] == "Z":
        return False
    return (stat["starttime"] == rec.get("starttime")
            and stat["pgrp"] == pid == rec.get("pgid") and stat["session"] == pid
            and cmdline_matches(read_cmdline(pid), rec.get("argv") or [""], rec.get("title"))
            and read_cwd(pid) == rec.get("cwd"))


def group_members(pgid: int) -> list[int]:
    members = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            stat = read_stat(int(entry.name))
            if stat and stat["pgrp"] == pgid and stat["state"] != "Z":
                members.append(int(entry.name))
    return sorted(members)


def signal_group(pgid: int, sig: int) -> None:
    if pgid <= 1 or pgid == os.getpgrp():
        raise LauncherError(f"Refusing to signal process group {pgid}", EXIT_FAIL)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, sig)


# --------------------------------------------------------------------------------------------- state, lock

@contextlib.contextmanager
def state_lock(cfg: Config):
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise LauncherError("Another local-up/local-down is already running; wait for it to finish.") from None
            raise
        yield
    finally:
        os.close(fd)  # closing releases the flock


def load_state(cfg: Config) -> dict:
    try:
        data = json.loads(cfg.state_file.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise LauncherError(f"{cfg.state_file} is unreadable. Inspect it, then delete it only after confirming no "
                            f"ROSC dev process owns it (see README recovery).", EXIT_UNKNOWN) from None
    return data if isinstance(data, dict) else {}


def save_state(cfg: Config, state: dict) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    tmp = cfg.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, cfg.state_file)


def clear_state(cfg: Config) -> None:
    with contextlib.suppress(FileNotFoundError):
        cfg.state_file.unlink()


# ------------------------------------------------------------------------------------------------- probes

def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        if sock.connect_ex((LOOPBACK, port)) == 0:
            return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # TIME_WAIT is not "in use"; a listener still is
        try:
            sock.bind((LOOPBACK, port))
        except OSError:
            return True
    return False


def http_get(url: str, timeout: float = 2.0) -> tuple[int, str] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - loopback http only
            return resp.status, resp.read(65536).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (OSError, ValueError):
        return None


def is_rosc_health(body: str) -> bool:
    try:
        return json.loads(body).get("service") == "rosc"
    except (ValueError, AttributeError):
        return False


def backend_ready(cfg: Config) -> bool:
    got = http_get(f"{cfg.backend_url}/api/health")
    return bool(got and got[0] == 200 and is_rosc_health(got[1]))


def frontend_ready(cfg: Config) -> bool:
    page = http_get(f"{cfg.url}/")
    proxied = http_get(f"{cfg.url}/api/health")
    return bool(page and page[0] == 200 and proxied and proxied[0] == 200 and is_rosc_health(proxied[1]))


READY = {"backend": backend_ready, "frontend": frontend_ready}


# ------------------------------------------------------------------------------------------- prerequisites

def alembic_head(root: Path) -> str:
    """Single Alembic head parsed from migrations/versions (stdlib only)."""
    revisions, downs = set(), set()
    for path in (root / "migrations" / "versions").glob("*.py"):
        text = path.read_text()
        rev = re.search(r"^revision(?:\s*:\s*[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
        down = re.search(r"^down_revision(?:\s*:\s*[^=]+)?\s*=\s*(?:['\"]([^'\"]+)['\"]|None)", text, re.M)
        if rev:
            revisions.add(rev.group(1))
        if down and down.group(1):
            downs.add(down.group(1))
    heads = revisions - downs
    if len(heads) != 1:
        raise LauncherError(f"Expected exactly one Alembic head, found {sorted(heads)}")
    return heads.pop()


def db_version(db_path: Path) -> str | None:
    """Current Alembic revision via a READ-ONLY connection (never creates or modifies the file)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def check_prerequisites(cfg: Config) -> None:
    problems = []
    if sys.version_info < (3, 11):
        problems.append("Python 3.11+ is required to run the launcher.")
    if not cfg.python.exists():
        problems.append(f"{cfg.python} is missing. Create it: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'")
    else:
        probe = subprocess.run([str(cfg.python), "-c",
                                "import importlib.util as u,sys;sys.exit(0 if u.find_spec('uvicorn') and "
                                "u.find_spec('rosc') else 1)"], cwd=cfg.root, capture_output=True)
        if probe.returncode != 0:
            problems.append("uvicorn/rosc are not installed in .venv. Run: .venv/bin/pip install -e '.[dev]'")
    node = shutil.which("node")
    major = None
    if node:
        out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
        match = re.match(r"v(\d+)\.", out)
        major = int(match.group(1)) if match else None
    if major is None or major < MIN_NODE_MAJOR:
        problems.append(f"Node.js >= {MIN_NODE_MAJOR} is required (found {'none' if major is None else major}).")
    if not shutil.which("npm"):
        problems.append("npm is not on PATH.")
    if not (cfg.frontend_dir / "node_modules" / ".bin" / "vite").exists():
        problems.append("frontend/node_modules is missing. Run once: (cd frontend && npm install)")
    if problems:
        raise LauncherError("Prerequisites not met:\n  - " + "\n  - ".join(problems))
    check_database(cfg)


def check_database(cfg: Config) -> None:
    fix = f"ROSC_DATA_DIR='{cfg.data_dir}' .venv/bin/alembic upgrade head"
    if not cfg.data_dir.is_dir() or not cfg.db_path.is_file():
        raise LauncherError(f"No existing database at {cfg.db_path}. The launcher never creates one. If this is the "
                            f"intended data directory, set it up yourself first:\n  mkdir -p '{cfg.data_dir}' && {fix}\n"
                            f"or point ROSC_DATA_DIR at an existing data directory.")
    head = alembic_head(cfg.root)
    current = db_version(cfg.db_path)
    if current != head:
        raise LauncherError(f"Database {cfg.db_path} is at revision {current!r}, expected {head!r}. The launcher never "
                            f"migrates. Back it up, then run yourself:\n  {fix}")


# ----------------------------------------------------------------------------------------- process control

def rotate_log(path: Path, limit: int = 5_000_000) -> None:
    with contextlib.suppress(OSError):
        if path.stat().st_size > limit:
            os.replace(path, path.with_suffix(".log.1"))


def spawn(cfg: Config, name: str, spec: dict) -> dict:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    log = cfg.log_file(name)
    rotate_log(log)
    env = dict(os.environ)
    env.update(spec["env"])
    with open(log, "ab") as out:
        out.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} starting {name}: {' '.join(spec['argv'])}\n".encode())
        out.flush()
        proc = subprocess.Popen(spec["argv"], cwd=spec["cwd"], env=env, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
    return snapshot_identity(proc.pid, spec["argv"], spec["cwd"], spec.get("title"))


def classify(rec: dict | None) -> str:
    """'absent' (no record / nothing left), 'owned' (leader proven), 'unknown' (group alive, leader unproven)."""
    if not rec:
        return "absent"
    if identity_matches(rec):
        return "owned"
    pgid = rec.get("pgid")
    if isinstance(pgid, int) and pgid > 1 and group_members(pgid):
        return "unknown"
    return "absent"


def stop_service(cfg: Config, rec: dict) -> str:
    """Stop one service. Returns 'stopped', 'stale' (nothing left) or 'unknown' (could not prove/clear)."""
    kind = classify(rec)
    if kind == "absent":
        return "stale"
    if kind == "unknown":
        return "unknown"
    pgid = rec["pgid"]
    # Leader identity was proven just above; the process group id cannot be reused while any member lives.
    signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + cfg.stop_timeout
    while time.monotonic() < deadline and group_members(pgid):
        time.sleep(0.1)
    if group_members(pgid):
        signal_group(pgid, signal.SIGKILL)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and group_members(pgid):
            time.sleep(0.1)
    return "unknown" if group_members(pgid) else "stopped"


def stop_all(cfg: Config, state: dict) -> dict[str, str]:
    results = {}
    for name in ("frontend", "backend"):  # reverse of start order
        if name in state.get("services", {}):
            results[name] = stop_service(cfg, state["services"][name])
    return results


def wait_ready(cfg: Config, name: str, rec: dict) -> None:
    deadline = time.monotonic() + cfg.ready_timeout
    while time.monotonic() < deadline:
        if not identity_matches(rec):
            raise LauncherError(f"{name} exited during startup. See: make local-logs")
        if READY[name](cfg):
            return
        time.sleep(0.3)
    raise LauncherError(f"{name} was not ready within {cfg.ready_timeout:.0f}s. See: make local-logs")


def describe(cfg: Config) -> list[str]:
    return [f"data dir : {cfg.data_dir}", f"database : {cfg.db_path}"]


# --------------------------------------------------------------------------------------------- commands

def evaluate(cfg: Config, state: dict) -> tuple[str, dict[str, dict]]:
    """Overall RUNNING/STOPPED/DEGRADED/UNKNOWN plus per-service facts (no side effects)."""
    detail = {}
    for name in ("backend", "frontend"):
        rec = state.get("services", {}).get(name)
        kind = classify(rec)
        ready = READY[name](cfg) if kind == "owned" else False
        detail[name] = {"identity": kind, "ready": ready, "rec": rec}
    kinds = {d["identity"] for d in detail.values()}
    if "unknown" in kinds:
        return UNKNOWN, detail
    if kinds == {"absent"}:
        return STOPPED, detail
    if all(d["identity"] == "owned" and d["ready"] for d in detail.values()):
        return RUNNING, detail
    return DEGRADED, detail


def cmd_up(cfg: Config) -> int:
    validate_ports(cfg.backend_port, cfg.frontend_port)
    with state_lock(cfg):
        state = load_state(cfg)
        running_cfg = effective_config(cfg, state)
        overall, _ = evaluate(running_cfg, state)
        if overall == RUNNING:
            conflicts = []
            if cfg.backend_explicit and cfg.backend_port != running_cfg.backend_port:
                conflicts.append(f"backend port {cfg.backend_port} (running: {running_cfg.backend_port})")
            if cfg.frontend_explicit and cfg.frontend_port != running_cfg.frontend_port:
                conflicts.append(f"frontend port {cfg.frontend_port} (running: {running_cfg.frontend_port})")
            if cfg.data_dir_explicit and cfg.data_dir != running_cfg.data_dir:
                conflicts.append(f"data dir {cfg.data_dir} (running: {running_cfg.data_dir})")
            if conflicts:
                raise LauncherError("A managed pair is already running with a different configuration: "
                                    + "; ".join(conflicts) + ". Nothing was changed. Run `local-app down` first.")
            print("\n".join(describe(running_cfg)))
            print(f"RUNNING (already up, not restarted)\nbackend  : {running_cfg.backend_url}\n"
                  f"frontend : {running_cfg.url}\nopen     : {running_cfg.url}")
            return EXIT_OK
        print("\n".join(describe(cfg)))
        if overall == UNKNOWN:
            raise LauncherError("State is UNKNOWN: a recorded process group is alive but its identity cannot be "
                                "proven. Not touching it. See README 'Recovering from UNKNOWN'.", EXIT_UNKNOWN)
        if overall == DEGRADED:
            raise LauncherError("A managed service is running but the pair is not healthy (DEGRADED). "
                                "Run `local-app down`, then `local-app up`.", EXIT_DEGRADED)
        clear_state(cfg)  # STOPPED: any record was stale (nothing alive)
        check_prerequisites(cfg)
        for label, port in (("backend", cfg.backend_port), ("frontend", cfg.frontend_port)):
            if port_in_use(port):
                raise LauncherError(occupied_message(label, port, listener_diagnostics(port)))
        commands = service_commands(cfg)
        state = {"config": {"backend_port": cfg.backend_port, "frontend_port": cfg.frontend_port,
                            "data_dir": str(cfg.data_dir)}, "services": {}}
        try:
            for name in ("backend", "frontend"):
                state["services"][name] = spawn(cfg, name, commands[name])
                save_state(cfg, state)
                wait_ready(cfg, name, state["services"][name])
        except BaseException as exc:
            results = stop_all(cfg, state)
            if any(v == "unknown" for v in results.values()):
                print(f"UNKNOWN: startup failed and cleanup could not be confirmed ({results}). State kept; "
                      f"inspect manually (README).", file=sys.stderr)
                raise LauncherError(str(exc), EXIT_UNKNOWN) from exc
            clear_state(cfg)
            if isinstance(exc, KeyboardInterrupt):
                print("Interrupted; partial startup rolled back.", file=sys.stderr)
                return 130
            raise
        print(f"RUNNING\nbackend  : {cfg.backend_url} (pid {state['services']['backend']['pid']})\n"
              f"frontend : {cfg.url} (pid {state['services']['frontend']['pid']})\nopen     : {cfg.url}")
        return EXIT_OK


def cmd_down(cfg: Config) -> int:
    with state_lock(cfg):
        state = load_state(cfg)
        if not state.get("services"):
            clear_state(cfg)
            print("STOPPED (nothing to stop)")
            return EXIT_OK
        results = stop_all(cfg, state)
        if any(v == "unknown" for v in results.values()):
            unresolved = {k: v for k, v in results.items() if v == "unknown"}
            print(f"UNKNOWN: could not prove or confirm termination for {sorted(unresolved)}. Nothing was force-killed "
                  f"without proof. State kept; see README 'Recovering from UNKNOWN'.", file=sys.stderr)
            return EXIT_UNKNOWN
        clear_state(cfg)
        print("STOPPED " + ", ".join(f"{k}={v}" for k, v in results.items()))
        return EXIT_OK


def cmd_status(cfg: Config) -> int:
    """Read-only: probes the RECORDED pair (not this shell's defaults) and writes nothing."""
    state = load_state(cfg)
    eff = effective_config(cfg, state)
    overall, detail = evaluate(eff, state)
    print(overall)
    print(f"data dir : {eff.data_dir}\ndatabase : {eff.db_path}")
    ports = {"backend": eff.backend_port, "frontend": eff.frontend_port}
    for name, info in detail.items():
        extra = ""
        if info["identity"] == "absent" and port_in_use(ports[name]):
            extra = " (port held by another process)"
        pid = f" pid {info['rec']['pid']}" if info["rec"] else ""
        print(f"{name:9}: {LOOPBACK}:{ports[name]} {info['identity']}{pid} ready={info['ready']}{extra}")
    if overall == RUNNING:
        print(f"open     : {eff.url}")
    return STATUS_EXIT[overall]


def cmd_logs(cfg: Config, which: str, lines: int, follow: bool) -> int:
    names = ["backend", "frontend"] if which == "all" else [which]
    shown = False
    for name in names:
        path = cfg.log_file(name)
        if not path.exists():
            print(f"--- {name}: no log at {path}")
            continue
        shown = True
        tail = path.read_bytes().decode(errors="replace").splitlines()[-lines:]
        print(f"--- {name} ({path})")
        print("\n".join(tail))
    if follow and shown:
        files = {n: open(cfg.log_file(n), "rb") for n in names if cfg.log_file(n).exists()}
        for f in files.values():
            f.seek(0, os.SEEK_END)
        try:
            while True:
                for n, f in files.items():
                    for line in f.read().decode(errors="replace").splitlines():
                        print(f"[{n}] {line}", flush=True)
                time.sleep(0.5)
        except KeyboardInterrupt:
            return EXIT_OK
    return EXIT_OK


# ------------------------------------------------------------------------------------ command registry / CLI

PROG = "local-app"
GROUPS = ("APPLICATION", "DIAGNOSTICS", "DEVELOPMENT CHECKS", "GENERAL")


@dataclass(frozen=True)
class Check:
    summary: str
    unittest_args: tuple[str, ...]


# `local-app check <name>` runs the already established suites; same commands as the Makefile targets.
CHECKS: dict[str, Check] = {
    "backend": Check("Backend unit tests (tests/)", ("-s", "tests", "-v")),
    "launcher": Check("This launcher's tests, incl. a live smoke on a temp DB",
                      ("-s", "scripts/local_app_tests", "-t", ".", "-p", "test_*.py")),
    "sync": Check("Managed prompt/report transport tests (offline)",
                  ("-s", "scripts/agent_sync/tests", "-t", ".", "-p", "test_*.py")),
    "read-path": Check("Documentation read-path telemetry tests",
                       ("-s", "scripts/read_path/tests", "-t", ".", "-p", "test_*.py")),
}


@dataclass(frozen=True)
class Command:
    name: str
    group: str
    summary: str
    configure: "callable"   # (argparse.ArgumentParser) -> None
    run: "callable"         # (argparse.Namespace, Config) -> int


def _configure_up(p: argparse.ArgumentParser) -> None:
    p.add_argument("-p", "--port", "--backend-port", dest="backend_port", metavar="PORT",
                   help=f"backend port on 127.0.0.1 (default {DEFAULT_BACKEND_PORT}, env ROSC_BACKEND_PORT; any free "
                        f"port 1024..65535)")
    p.add_argument("--frontend-port", metavar="PORT",
                   help=f"Vite port (default {DEFAULT_FRONTEND_PORT}; only {' or '.join(map(str, ALLOWED_FRONTEND_PORTS))} "
                        f"are accepted by the backend Origin allowlist)")
    p.epilog = ("Starts BOTH the backend and the Vite frontend. Uses the existing database in ROSC_DATA_DIR "
                "(default <checkout>/data); never creates or migrates it.")


def _configure_logs(p: argparse.ArgumentParser) -> None:
    p.add_argument("service", nargs="?", choices=["backend", "frontend", "all"], default="all")
    p.add_argument("-n", "--lines", type=int, default=50, help="lines to show (default 50)")
    p.add_argument("-f", "--follow", action="store_true", help="keep following new output (Ctrl-C to stop)")


def _configure_help(p: argparse.ArgumentParser) -> None:
    p.add_argument("topic", nargs="*", help="command (and sub-command), e.g. `up` or `check backend`")


def run_check(name: str, cfg: Config) -> int:
    check = CHECKS[name]
    python = str(cfg.python) if cfg.python.exists() else sys.executable
    argv = [python, "-m", "unittest", "discover", *check.unittest_args]
    print(f"check {name}: {check.summary}\n$ {' '.join(argv)}", flush=True)
    return subprocess.call(argv, cwd=cfg.root)


COMMANDS: tuple[Command, ...] = (
    Command("up", "APPLICATION", "Start backend + frontend dev servers (use -p PORT for the backend port)",
            _configure_up, lambda a, cfg: cmd_up(cfg)),
    Command("down", "APPLICATION", "Stop the managed pair (only provably owned processes)",
            lambda p: None, lambda a, cfg: cmd_down(cfg)),
    Command("status", "DIAGNOSTICS", "Show RUNNING/STOPPED/DEGRADED/UNKNOWN, ports, data dir (read-only)",
            lambda p: None, lambda a, cfg: cmd_status(cfg)),
    Command("logs", "DIAGNOSTICS", "Show persisted backend/frontend logs",
            _configure_logs, lambda a, cfg: cmd_logs(cfg, a.service, a.lines, a.follow)),
    Command("check", "DEVELOPMENT CHECKS", "Run an existing test suite (see the list above)",
            lambda p: None, None),  # sub-commands come from CHECKS
    Command("help", "GENERAL", "Show help for a command: `help up`, `help check backend`",
            _configure_help, None),
)


def package_version() -> str:
    try:
        text = (ROOT / "pyproject.toml").read_text()
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        if match:
            return match.group(1)
    except OSError:
        pass
    try:
        from importlib.metadata import version
        return version("rosc")
    except Exception:  # noqa: BLE001
        return "unknown"


def grouped_help() -> str:
    lines = []
    for group in GROUPS:
        lines.append(f"{group}")
        for cmd in COMMANDS:
            if cmd.group != group:
                continue
            if cmd.name == "check":
                lines.extend(f"  check {n:<10}{c.summary}" for n, c in CHECKS.items())
            elif cmd.name != "help":
                lines.append(f"  {cmd.name:<16}{cmd.summary}")
        if group == "GENERAL":
            lines.append(f"  {'help [command]':<16}Show help for a command: `help up`, `help check backend`")
            lines.append(f"  {'--help':<16}This overview")
            lines.append(f"  {'--version':<16}Print the version")
        lines.append("")
    return "\n".join(lines).rstrip()


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # usage errors exit 2 with a hint instead of a bare argparse dump
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\nTry `{PROG} --help`.\n")
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, usage=f"{PROG} [--help] [--version] <command> [options]",
                     description="ROSC local developer app launcher (Linux). Manages only the local dev web pair; "
                                 "never starts agents or runs.",
                     epilog=grouped_help(), formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"{PROG} {package_version()}")
    sub = parser.add_subparsers(dest="command", metavar="<command>", parser_class=_Parser)
    for cmd in COMMANDS:
        p = sub.add_parser(cmd.name, description=cmd.summary, prog=f"{PROG} {cmd.name}")
        cmd.configure(p)
        if cmd.name == "check":
            csub = p.add_subparsers(dest="check", metavar="<suite>", required=True, parser_class=_Parser)
            for name, check in CHECKS.items():
                csub.add_parser(name, description=check.summary, prog=f"{PROG} check {name}")
    return parser


def build_config(args: argparse.Namespace, environ: dict[str, str], root: Path) -> Config:
    backend_raw = getattr(args, "backend_port", None) or environ.get("ROSC_BACKEND_PORT")
    frontend_raw = getattr(args, "frontend_port", None) or environ.get("ROSC_FRONTEND_PORT")
    return Config(
        root=root, data_dir=resolve_data_dir(environ, root),
        backend_port=parse_port(backend_raw, "backend port") if backend_raw else DEFAULT_BACKEND_PORT,
        frontend_port=parse_port(frontend_raw, "frontend port") if frontend_raw else DEFAULT_FRONTEND_PORT,
        backend_explicit=bool(backend_raw), frontend_explicit=bool(frontend_raw),
        data_dir_explicit=bool((environ.get("ROSC_DATA_DIR") or "").strip()))


def main(argv: list[str] | None = None, *, root: Path = ROOT, environ: dict[str, str] | None = None) -> int:
    env = dict(os.environ if environ is None else environ)
    parser = build_parser()
    args = parser.parse_args(argv)  # --help/--version exit here, before any filesystem or process activity
    if args.command is None:
        parser.print_help()
        return EXIT_USAGE
    if args.command == "help":
        parser.parse_args([*args.topic, "--help"])  # prints that command's help and exits 0
        return EXIT_OK
    if not sys.platform.startswith("linux"):
        print("Unsupported platform: the launcher relies on Linux /proc and process groups.", file=sys.stderr)
        return EXIT_FAIL
    try:
        cfg = build_config(args, env, root)
        if args.command == "check":
            return run_check(args.check, cfg)
        command = next(c for c in COMMANDS if c.name == args.command)
        return command.run(args, cfg)
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
