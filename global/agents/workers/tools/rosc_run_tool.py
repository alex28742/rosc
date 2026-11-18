#!/usr/bin/env python3
"""ROSC Worker Artifact Tool (rosc.run-tool 1.0.0) - the ONLY writer of a formal Run's local artifacts.

A byte-exact copy of this file is pinned in every Run package that makes stages E1-E6 operative
(``<run dir>/package/rosc-run-tool.py``).  Invoke it from the Project root as::

    python3 <run dir>/package/rosc-run-tool.py <command> [options]

It needs only the Python standard library and the files of its own Run directory.  It never imports ROSC, opens a
database, uses the network, calls a model provider or talks to ROSC Core: it keeps working when ROSC is stopped.
Every artifact path is derived from the package location; no option can name an output path.

Run directory layout (``<run dir>`` = parent of ``package/``)::

    package/                 read-only Run contract (written by ROSC Core, never by this tool)
    plan/draft.json          helper-managed working draft of the next Plan revision (not a published fact)
    plan/v001.json ...       immutable published Plan revisions (linked by the previous revision's sha256)
    journal/events.jsonl     append-only, hash-chained typed events (one JSON object per line)
    checkpoints/latest.json  atomically replaced continuation snapshot (cursor = journal seq + event hash)
    evidence/                bounded test logs
    reports/final.md         immutable finalized Run Report
    terminal.json            correlated terminal record written by ``run finish`` (after report + journal close)

Commands (each prints one JSON object; exit 0 = ok, 2 = refused, nothing changed on refusal)::

    status                                       current Plan, Step states (two axes), blockers, cursor
    run start                                    E1: record that execution of this Run started
    plan add-step --id S01 --title T [--description D] [--depends S00,..] [--expect E] [--test-intent I]
    plan remove-step --id S01
    plan show                                    the working draft
    plan publish [--reason R]                    E2: publish the FIRST revision (before material edits)
    plan revise --reason R                       publish the next revision of the draft
    step start|produced|blocked|failed|uncertain --id S01 [--note N] [--evidence REF]
    test begin --id T01 --steps S01,S02
    test record --id T01 --steps S01,S02 --command "CMD" --exit N --outcome passed|failed|inconclusive
                [--log-file PATH | --log-stdin] [--limitations L]
    note --kind decision|action --text TEXT
    blocker record --id B01 --summary S [--step S01] [--next-action A] [--owner-decision]
    blocker resolve --id B01 --note N
    checkpoint save [--note N] [--current-step S01]
    report finalize --outcome completed|partial|blocked|failed (--body-file PATH | --body-stdin)
    run finish                                   close the Journal and write terminal.json (after report finalize)
    journal recover                              preserve a torn tail (crash mid-append) and record it

Retrying an identical command is a no-op; reusing an id (test, blocker, step) with different content is refused.
Output and files are Worker-produced evidence only: never an Owner authorization or an independent verification.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time

TOOL_VERSION = "1.0.0"
ARTIFACT_FORMAT = "rosc.run-artifacts.v1"
PLAN_SCHEMA = "rosc.run-plan.v1"
EVENT_SCHEMA = "rosc.run-event.v1"
CHECKPOINT_SCHEMA = "rosc.run-checkpoint.v1"
TERMINAL_SCHEMA = "rosc.run-terminal.v1"
FULL_STAGES = ["E0", "E1", "E2", "E3", "E4", "E5", "E6"]

MAX_MANIFEST = 262_144
MAX_PLAN = 524_288
MAX_JOURNAL = 8_388_608
MAX_EVENTS = 5000
MAX_LINE = 65_536
MAX_STEPS = 300
MAX_TEXT = 2000
MAX_TITLE = 200
MAX_COMMAND = 1000
MAX_LOG_KEEP = 65_536
MAX_LOG_READ = 16_777_216
MAX_REPORT_BODY = 262_144
MAX_REFS = 20
ID = re.compile(r"[A-Z][A-Z0-9._-]{0,31}")
TEST_ID = re.compile(r"T[A-Za-z0-9._-]{1,31}")
BLOCKER_ID = re.compile(r"B[A-Za-z0-9._-]{1,31}")

IMPL = ("PENDING", "IN_PROGRESS", "PRODUCED", "BLOCKED", "FAILED", "UNCERTAIN")
TESTS = ("NOT_TESTED", "TESTING", "PASSED", "FAILED", "INCONCLUSIVE", "STALE")
STEP_COMMANDS = {"start": "IN_PROGRESS", "produced": "PRODUCED", "blocked": "BLOCKED", "failed": "FAILED",
                 "uncertain": "UNCERTAIN"}
ALLOWED = {"PENDING": {"IN_PROGRESS", "BLOCKED"},
           "IN_PROGRESS": {"PRODUCED", "BLOCKED", "FAILED", "UNCERTAIN"},
           "BLOCKED": {"IN_PROGRESS", "FAILED"}, "FAILED": {"IN_PROGRESS"}, "UNCERTAIN": {"IN_PROGRESS", "PRODUCED"},
           "PRODUCED": {"IN_PROGRESS"}}
OUTCOMES = ("completed", "partial", "blocked", "failed")
TEST_OUTCOMES = {"passed": "PASSED", "failed": "FAILED", "inconclusive": "INCONCLUSIVE"}


class Refused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# -- filesystem primitives -------------------------------------------------------------------------------------------
def read_bounded(path: str, limit: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise Refused("unsafe_path", f"{os.path.basename(path)} is a symlink") from None
        raise
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise Refused("unsafe_path", f"{os.path.basename(path)} is not a regular file")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise Refused("too_large", f"{os.path.basename(path)} exceeds {limit} bytes")
    return data


def fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_temp(directory: str, name: str, data: bytes) -> str:
    temp = os.path.join(directory, f".{name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return temp


def publish_once(directory: str, name: str, data: bytes) -> None:
    """Atomic exclusive publish of an immutable file (link never replaces an existing target)."""
    target = os.path.join(directory, name)
    temp = write_temp(directory, name, data)
    try:
        os.chmod(temp, 0o400)
        os.link(temp, target)
    finally:
        os.unlink(temp)
    fsync_dir(directory)


def replace_atomic(directory: str, name: str, data: bytes) -> None:
    target = os.path.join(directory, name)
    if os.path.islink(target):
        raise Refused("unsafe_path", f"{name} is a symlink")
    temp = write_temp(directory, name, data)
    os.replace(temp, target)
    fsync_dir(directory)


class Run:
    """The Run this pinned copy belongs to, derived from the tool's own location (never from an argument)."""

    def __init__(self, tool_path: str) -> None:
        self.package = os.path.dirname(tool_path)
        self.root = os.path.dirname(self.package)
        if os.path.basename(self.package) != "package":
            raise Refused("not_in_package", "The tool must be run from its Run package directory")
        for path in (self.root, self.package):
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise Refused("unsafe_path", "The Run directory is not a private real directory of this user")
        if os.path.realpath(self.root) != os.path.abspath(self.root):
            raise Refused("unsafe_path", "The Run directory path traverses a symlink")
        raw = read_bounded(os.path.join(self.package, "run.json"), MAX_MANIFEST)
        self.manifest_sha256 = sha(raw)
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Refused("manifest_invalid", "run.json is not valid JSON") from None
        if (not isinstance(manifest, dict) or manifest.get("format") != "rosc.run.v1"
                or manifest.get("run_id") != os.path.basename(self.root)
                or manifest.get("operative_stages") != FULL_STAGES):
            raise Refused("manifest_invalid", "run.json does not describe this Run with operative E1-E6")
        artifacts, tool = manifest.get("artifacts"), manifest.get("tool")
        if not isinstance(artifacts, dict) or artifacts.get("format") != ARTIFACT_FORMAT or not isinstance(tool, dict):
            raise Refused("manifest_invalid", "run.json has no supported artifact contract")
        own = read_bounded(tool_path, 1_048_576)
        if tool.get("sha256") != sha(own) or tool.get("version") != TOOL_VERSION:
            raise Refused("tool_mismatch", "This tool's bytes do not match the pinned digest in run.json")
        for item in manifest.get("contents", []):
            if isinstance(item, dict) and item.get("file") == os.path.basename(tool_path) and item.get("sha256") != sha(own):
                raise Refused("tool_mismatch", "This tool's bytes do not match the package contents")
        self.manifest = manifest
        self.identity = {"run_id": manifest["run_id"], "project_id": manifest["project"]["id"],
                         "task_id": manifest["task"]["id"], "agent_id": manifest["worker"]["agent_id"],
                         "incarnation_ref": manifest["worker"].get("incarnation_ref"),
                         "manifest_sha256": self.manifest_sha256, "protocol_sha256": manifest["protocol"]["sha256"]}

    def sub(self, name: str) -> str:
        path = os.path.join(self.root, name)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            os.mkdir(path, 0o700)
            info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise Refused("unsafe_path", f"{name}/ is a symlink or not a directory")
        return path

    def exists(self, *parts: str) -> bool:
        return os.path.lexists(os.path.join(self.root, *parts))


# -- journal ---------------------------------------------------------------------------------------------------------
def event_hash(event: dict) -> str:
    return sha(canonical({k: v for k, v in event.items() if k != "hash"}))


def read_journal(run: Run) -> tuple[list[dict], int, bytes]:
    """(valid events, byte length of the valid prefix, torn tail bytes).  A corrupt line before the end refuses."""
    path = os.path.join(run.root, "journal", "events.jsonl")
    if not os.path.lexists(path):
        return [], 0, b""
    raw = read_bounded(path, MAX_JOURNAL)
    events: list[dict] = []
    offset = 0
    previous = None
    while offset < len(raw):
        end = raw.find(b"\n", offset)
        if end < 0:
            return events, offset, raw[offset:]  # torn tail: an interrupted append, never silently dropped
        line = raw[offset:end]
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Refused("journal_corrupt", f"journal line {len(events) + 1} is not valid JSON") from None
        if (not isinstance(event, dict) or event.get("schema") != EVENT_SCHEMA or event.get("seq") != len(events) + 1
                or event.get("prev_hash") != previous or event.get("hash") != event_hash(event)
                or any(event.get(k) != v for k, v in run.identity.items())):
            raise Refused("journal_corrupt", f"journal event {len(events) + 1} fails its sequence/hash/identity check")
        previous = event["hash"]
        events.append(event)
        offset = end + 1
    return events, offset, b""


def append_event(run: Run, events: list[dict], kind: str, data: dict, *, step_id: str | None = None,
                 plan_revision: int | None = None) -> dict:
    if len(events) >= MAX_EVENTS:
        raise Refused("journal_full", "The Journal reached its event bound")
    if events and events[-1]["type"] == "run.finished":
        raise Refused("journal_closed", "The Journal is closed (run finish)")
    seq = len(events) + 1
    event = {"schema": EVENT_SCHEMA, **run.identity, "seq": seq, "event_id": f"E{seq:06d}",
             "prev_hash": events[-1]["hash"] if events else None, "type": kind, "at": now_iso(),
             "step_id": step_id, "plan_revision": plan_revision, "data": data, "tool_version": TOOL_VERSION}
    event["hash"] = event_hash(event)
    line = canonical(event) + b"\n"
    if len(line) > MAX_LINE:
        raise Refused("too_large", "The event exceeds the line bound")
    directory = run.sub("journal")
    path = os.path.join(directory, "events.jsonl")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_size + len(line) > MAX_JOURNAL:
            raise Refused("journal_full", "The Journal reached its size bound")
        written = os.write(fd, line)
        if written != len(line):
            raise Refused("io_error", "Short write to the Journal; run 'journal recover'")
        os.fsync(fd)
    finally:
        os.close(fd)
    if seq == 1:
        fsync_dir(directory)
    events.append(event)
    return event


# -- plan ------------------------------------------------------------------------------------------------------------
def plan_revisions(run: Run) -> list[dict]:
    """Published revisions v001.. (contiguous, linked, identity-checked); a gap or tamper refuses."""
    directory = os.path.join(run.root, "plan")
    if not os.path.lexists(directory):
        return []
    names = sorted(n for n in os.listdir(directory) if re.fullmatch(r"v\d{3}\.json", n))
    plans: list[dict] = []
    previous = None
    for index, name in enumerate(names, start=1):
        if name != f"v{index:03d}.json":
            raise Refused("plan_corrupt", "Plan revisions are not contiguous")
        raw = read_bounded(os.path.join(directory, name), MAX_PLAN)
        try:
            plan = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise Refused("plan_corrupt", f"{name} is not valid JSON") from None
        if (not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA or plan.get("revision") != index
                or plan.get("previous") != previous or any(plan.get(k) != v for k, v in run.identity.items())):
            raise Refused("plan_corrupt", f"{name} fails its revision/link/identity check")
        plan["_sha256"] = sha(raw)
        previous = {"revision": index, "sha256": plan["_sha256"]}
        plans.append(plan)
    return plans


def check_plan_events(plans: list[dict], events: list[dict]) -> None:
    published = [e for e in events if e["type"] == "plan.published"]
    if len(published) > len(plans) or any(
            e["data"].get("revision") != i + 1 or e["data"].get("sha256") != plans[i]["_sha256"]
            for i, e in enumerate(published)):
        raise Refused("plan_corrupt", "A published Plan revision differs from its Journal record")


def text(value: object, limit: int, name: str, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise Refused("invalid_argument", f"{name} is required")
        return None
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise Refused("invalid_argument", f"{name} must be text of at most {limit} characters")
    return value


def id_list(value: str | None, pattern: re.Pattern, name: str) -> list[str]:
    if not value:
        return []
    items = [part.strip() for part in value.split(",") if part.strip()]
    if len(items) > MAX_STEPS or any(not pattern.fullmatch(i) for i in items) or len(set(items)) != len(items):
        raise Refused("invalid_argument", f"{name} must be a comma-separated list of distinct ids")
    return items


def validate_steps(steps: list[dict]) -> None:
    if not steps:
        raise Refused("plan_empty", "A Plan needs at least one Step")
    if len(steps) > MAX_STEPS:
        raise Refused("too_large", f"A Plan has at most {MAX_STEPS} Steps")
    ids = [s["id"] for s in steps]
    if len(set(ids)) != len(ids):
        raise Refused("invalid_plan", "Step ids must be unique")
    known = set(ids)
    graph = {s["id"]: s["depends_on"] for s in steps}
    for step in steps:
        missing = [d for d in step["depends_on"] if d not in known or d == step["id"]]
        if missing:
            raise Refused("invalid_plan", f"Step {step['id']} depends on unknown Step(s) {','.join(missing)}")
    state: dict[str, int] = {}

    def visit(node: str) -> None:
        if state.get(node) == 1:
            raise Refused("invalid_plan", "Step dependencies contain a cycle")
        if state.get(node) == 2:
            return
        state[node] = 1
        for dep in graph[node]:
            visit(dep)
        state[node] = 2
    for node in ids:
        visit(node)


def load_draft(run: Run, plans: list[dict]) -> list[dict]:
    path = os.path.join(run.root, "plan", "draft.json")
    if os.path.lexists(path):
        draft = json.loads(read_bounded(path, MAX_PLAN).decode("utf-8"))
        if not isinstance(draft, dict) or draft.get("run_id") != run.identity["run_id"]:
            raise Refused("plan_corrupt", "The Plan draft belongs to another Run")
        return list(draft.get("steps", []))
    return [dict(s) for s in plans[-1]["steps"]] if plans else []


def save_draft(run: Run, steps: list[dict]) -> None:
    replace_atomic(run.sub("plan"), "draft.json", canonical({"run_id": run.identity["run_id"], "steps": steps}))


# -- state fold (Plan + Journal -> Step axes) ------------------------------------------------------------------------
def fold(plans: list[dict], events: list[dict]) -> dict:
    state = {"plan_revision": 0, "steps": {}, "order": [], "tests": {}, "blockers": {}, "report": None,
             "finished": None, "started": False, "checkpoint": None}
    for event in events:
        kind, data = event["type"], event["data"]
        if kind == "run.started":
            state["started"] = True
        elif kind == "plan.published":
            revision = data["revision"]
            plan = plans[revision - 1]
            old = state["steps"]
            state["steps"] = {s["id"]: old.get(s["id"], {"impl": "PENDING", "test": "NOT_TESTED", "tests": []})
                              for s in plan["steps"]}
            state["order"] = [s["id"] for s in plan["steps"]]
            state["plan_revision"] = revision
        elif kind == "step.status":
            state["steps"][event["step_id"]]["impl"] = data["to"]
            if data.get("test_axis"):
                state["steps"][event["step_id"]]["test"] = data["test_axis"]
        elif kind == "test.started":
            for step in data["steps"]:
                if step in state["steps"]:
                    state["steps"][step]["test"] = "TESTING"
        elif kind == "test.recorded":
            state["tests"][data["test_id"]] = data
            for step in data["steps"]:
                if step in state["steps"]:
                    state["steps"][step]["test"] = TEST_OUTCOMES[data["outcome"]]
                    state["steps"][step]["tests"].append(data["test_id"])
        elif kind == "blocker.recorded":
            state["blockers"][data["blocker_id"]] = {**data, "open": True}
        elif kind == "blocker.resolved":
            state["blockers"][data["blocker_id"]]["open"] = False
        elif kind == "checkpoint.saved":
            state["checkpoint"] = data
        elif kind == "report.finalized":
            state["report"] = data
        elif kind == "run.finished":
            state["finished"] = data
    return state


def git_context(run: Run) -> dict:
    root = run.manifest.get("binding", {}).get("root")
    if not isinstance(root, str):
        return {"available": False}
    try:
        head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "-C", root, "status", "--porcelain"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {"available": False}
    if head.returncode != 0:
        return {"available": False}
    lines = [line for line in dirty.stdout.splitlines() if line.strip()]
    return {"available": True, "head": head.stdout.strip()[:64], "dirty": bool(lines), "changed_paths": len(lines)}


# -- commands --------------------------------------------------------------------------------------------------------
class Context:
    def __init__(self, run: Run) -> None:
        self.run = run
        self.events, self.valid_bytes, self.torn = read_journal(run)
        self.plans = plan_revisions(run)
        check_plan_events(self.plans, self.events)
        self.state = fold(self.plans, self.events)

    def writable(self) -> None:
        if self.torn:
            raise Refused("journal_torn", "The Journal has a torn tail from an interrupted append; run 'journal recover'")
        if self.state["finished"]:
            raise Refused("journal_closed", "This Run is finished; its Journal is closed")

    def require_plan(self) -> int:
        if not self.state["plan_revision"]:
            raise Refused("plan_not_published", "Publish the Working Plan first (plan publish)")
        return self.state["plan_revision"]

    def append(self, kind: str, data: dict, **kw) -> dict:
        return append_event(self.run, self.events, kind, data, **kw)


def summary(ctx: Context) -> dict:
    st = ctx.state
    steps = [{"id": sid, "implementation": st["steps"][sid]["impl"], "test": st["steps"][sid]["test"],
              "tests": st["steps"][sid]["tests"]} for sid in st["order"]]
    produced = sum(1 for s in steps if s["implementation"] == "PRODUCED")
    return {"run_id": ctx.run.identity["run_id"], "plan_revision": st["plan_revision"], "steps": steps,
            "implementation_progress": f"{produced}/{len(steps)} PRODUCED (not verified)",
            "open_blockers": [b for b, v in st["blockers"].items() if v["open"]],
            "journal": {"events": len(ctx.events), "last_hash": ctx.events[-1]["hash"] if ctx.events else None,
                        "torn_tail_bytes": len(ctx.torn)},
            "report": st["report"], "finished": bool(st["finished"]), "started": st["started"]}


def cmd_status(ctx: Context, args) -> dict:
    return summary(ctx)


def cmd_run_start(ctx: Context, args) -> dict:
    ctx.writable()
    if ctx.state["started"]:
        return {"unchanged": True, **summary(ctx)}
    ctx.append("run.started", {"stage": "E1", "git": git_context(ctx.run)})
    return summary(ctx)


def step_record(args) -> dict:
    return {"id": args.id, "title": text(args.title, MAX_TITLE, "--title", True),
            "description": text(args.description, MAX_TEXT, "--description"),
            "depends_on": id_list(args.depends, ID, "--depends"),
            "expected_outcome": text(args.expect, MAX_TEXT, "--expect"),
            "test_intent": text(args.test_intent, MAX_TEXT, "--test-intent")}


def cmd_plan_add(ctx: Context, args) -> dict:
    ctx.writable()
    if not ID.fullmatch(args.id or ""):
        raise Refused("invalid_argument", "--id must look like S01 (upper-case letter, then letters/digits/._-)")
    step = step_record(args)
    draft = load_draft(ctx.run, ctx.plans)
    for index, existing in enumerate(draft):
        if existing["id"] == args.id:
            if existing == step:
                return {"unchanged": True, "draft_steps": len(draft)}
            draft[index] = step  # an explicit draft edit (the published revisions stay immutable)
            save_draft(ctx.run, draft)
            return {"updated": args.id, "draft_steps": len(draft)}
    if len(draft) >= MAX_STEPS:
        raise Refused("too_large", f"A Plan has at most {MAX_STEPS} Steps")
    draft.append(step)
    save_draft(ctx.run, draft)
    return {"added": args.id, "draft_steps": len(draft)}


def cmd_plan_remove(ctx: Context, args) -> dict:
    ctx.writable()
    draft = load_draft(ctx.run, ctx.plans)
    kept = [s for s in draft if s["id"] != args.id]
    if len(kept) == len(draft):
        raise Refused("unknown_step", f"Step {args.id} is not in the draft")
    if any(args.id in s["depends_on"] for s in kept):
        raise Refused("invalid_plan", f"Other Steps depend on {args.id}")
    save_draft(ctx.run, kept)
    return {"removed": args.id, "draft_steps": len(kept)}


def cmd_plan_show(ctx: Context, args) -> dict:
    return {"draft": load_draft(ctx.run, ctx.plans), "published_revision": ctx.state["plan_revision"]}


def publish_plan(ctx: Context, reason: str | None) -> dict:
    ctx.writable()
    draft = load_draft(ctx.run, ctx.plans)
    validate_steps(draft)
    if ctx.plans and ctx.plans[-1]["steps"] == draft:
        return {"unchanged": True, "revision": len(ctx.plans)}
    revision = len(ctx.plans) + 1
    previous = {"revision": revision - 1, "sha256": ctx.plans[-1]["_sha256"]} if ctx.plans else None
    plan = {"schema": PLAN_SCHEMA, **ctx.run.identity, "revision": revision, "previous": previous,
            "created_at": now_iso(), "reason": reason, "steps": draft, "tool_version": TOOL_VERSION}
    raw = json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if len(raw) > MAX_PLAN:
        raise Refused("too_large", "The Plan exceeds its size bound")
    directory = ctx.run.sub("plan")
    name = f"v{revision:03d}.json"
    if os.path.lexists(os.path.join(directory, name)):
        raise Refused("plan_conflict", f"{name} already exists without its Journal record; run status")
    publish_once(directory, name, raw)
    plan["_sha256"] = sha(raw)
    ctx.plans.append(plan)
    ctx.append("plan.published", {"revision": revision, "sha256": plan["_sha256"], "steps": len(draft),
                                  "reason": reason}, plan_revision=revision)
    ctx.state = fold(ctx.plans, ctx.events)
    return {"published": revision, "sha256": plan["_sha256"], "path": f"plan/{name}", **summary(ctx)}


def cmd_plan_publish(ctx: Context, args) -> dict:
    if ctx.plans:
        if ctx.plans[-1]["steps"] == load_draft(ctx.run, ctx.plans):
            return {"unchanged": True, "revision": len(ctx.plans)}
        raise Refused("plan_exists", "A Plan is already published; use plan revise --reason")
    return publish_plan(ctx, text(args.reason, MAX_TEXT, "--reason"))


def cmd_plan_revise(ctx: Context, args) -> dict:
    if not ctx.plans:
        raise Refused("plan_not_published", "Nothing to revise yet; use plan publish")
    return publish_plan(ctx, text(args.reason, MAX_TEXT, "--reason", True))


def cmd_step(ctx: Context, args) -> dict:
    ctx.writable()
    revision = ctx.require_plan()
    step = ctx.state["steps"].get(args.id)
    if step is None:
        raise Refused("unknown_step", f"Step {args.id} is not in Plan revision {revision}")
    target = STEP_COMMANDS[args.step_command]
    note = text(args.note, MAX_TEXT, "--note")
    refs = [text(r, 300, "--evidence") for r in (args.evidence or [])][:MAX_REFS]
    if step["impl"] == target:
        return {"unchanged": True, "step": args.id, "implementation": target}
    if target not in ALLOWED[step["impl"]]:
        raise Refused("invalid_transition", f"Step {args.id}: {step['impl']} -> {target} is not allowed")
    test_axis = None
    if step["impl"] == "PRODUCED" and target == "IN_PROGRESS" and step["test"] in ("PASSED", "FAILED", "INCONCLUSIVE"):
        test_axis = "STALE"  # reworked after a test: the earlier test no longer covers the current change
    ctx.append("step.status", {"from": step["impl"], "to": target, "note": note, "evidence": refs,
                               "test_axis": test_axis}, step_id=args.id, plan_revision=revision)
    ctx.state = fold(ctx.plans, ctx.events)
    return {"step": args.id, "implementation": target, "test": ctx.state["steps"][args.id]["test"]}


def test_steps(ctx: Context, value: str) -> list[str]:
    steps = id_list(value, ID, "--steps")
    if not steps:
        raise Refused("invalid_argument", "--steps names at least one Step")
    for step in steps:
        current = ctx.state["steps"].get(step)
        if current is None:
            raise Refused("unknown_step", f"Step {step} is not in the current Plan revision")
        if current["impl"] == "PENDING":
            raise Refused("invalid_transition", f"Step {step} has not been started")
    return steps


def cmd_test_begin(ctx: Context, args) -> dict:
    ctx.writable()
    revision = ctx.require_plan()
    if not TEST_ID.fullmatch(args.id or ""):
        raise Refused("invalid_argument", "--id must look like T01")
    steps = test_steps(ctx, args.steps)
    if args.id in ctx.state["tests"]:
        raise Refused("test_conflict", f"Test {args.id} is already recorded")
    for event in ctx.events:
        if event["type"] == "test.started" and event["data"]["test_id"] == args.id:
            if event["data"]["steps"] == steps:
                return {"unchanged": True, "test": args.id}
            raise Refused("test_conflict", f"Test {args.id} was started for other Steps")
    ctx.append("test.started", {"test_id": args.id, "steps": steps}, plan_revision=revision)
    return {"test": args.id, "steps": steps, "state": "TESTING"}


def cmd_test_record(ctx: Context, args) -> dict:
    ctx.writable()
    revision = ctx.require_plan()
    if not TEST_ID.fullmatch(args.id or ""):
        raise Refused("invalid_argument", "--id must look like T01")
    steps = test_steps(ctx, args.steps)
    command = text(args.command, MAX_COMMAND, "--command", True)
    if args.outcome not in TEST_OUTCOMES:
        raise Refused("invalid_argument", "--outcome is passed, failed or inconclusive")
    limitations = text(args.limitations, MAX_TEXT, "--limitations")
    log = None
    if args.log_file and args.log_stdin:
        raise Refused("invalid_argument", "Use --log-file or --log-stdin, not both")
    if args.log_file:
        try:
            log = read_bounded(args.log_file, MAX_LOG_READ)
        except OSError:
            raise Refused("invalid_argument", "--log-file cannot be read") from None
    elif args.log_stdin:
        log = sys.stdin.buffer.read(MAX_LOG_READ + 1)
        if len(log) > MAX_LOG_READ:
            raise Refused("too_large", "The test log exceeds its read bound")
    previous = ctx.state["tests"].get(args.id)
    core = {"test_id": args.id, "steps": steps, "command": command, "exit_code": args.exit,
            "outcome": args.outcome, "limitations": limitations}
    if previous is not None:
        if {k: previous.get(k) for k in core} == core:
            return {"unchanged": True, "test": args.id}
        raise Refused("test_conflict", f"Test {args.id} is already recorded with different content")
    evidence = None
    if log is not None:
        kept = log[-MAX_LOG_KEEP:]
        name = f"{args.id}.log"
        directory = ctx.run.sub("evidence")
        if os.path.lexists(os.path.join(directory, name)):
            existing = read_bounded(os.path.join(directory, name), MAX_LOG_KEEP)
            if existing != kept:
                raise Refused("test_conflict", f"evidence/{name} exists with other content")
        else:
            publish_once(directory, name, kept)
        evidence = {"path": f"evidence/{name}", "sha256": sha(kept), "bytes": len(kept),
                    "original_bytes": len(log), "truncated": len(kept) < len(log)}
    ctx.append("test.recorded", {**core, "evidence": evidence, "source": git_context(ctx.run)}, plan_revision=revision)
    ctx.state = fold(ctx.plans, ctx.events)
    return {"test": args.id, "outcome": args.outcome, "steps": {s: ctx.state["steps"][s]["test"] for s in steps},
            "note": "Worker test evidence; not VERIFIED"}


def cmd_note(ctx: Context, args) -> dict:
    ctx.writable()
    if args.kind not in ("decision", "action"):
        raise Refused("invalid_argument", "--kind is decision or action")
    body = text(args.text, MAX_TEXT, "--text", True)
    ctx.append("note.recorded", {"kind": args.kind, "text": body}, plan_revision=ctx.state["plan_revision"] or None)
    return {"recorded": args.kind, "seq": len(ctx.events)}


def cmd_blocker_record(ctx: Context, args) -> dict:
    ctx.writable()
    if not BLOCKER_ID.fullmatch(args.id or ""):
        raise Refused("invalid_argument", "--id must look like B01")
    step = args.step
    if step is not None and step not in ctx.state["steps"]:
        raise Refused("unknown_step", f"Step {step} is not in the current Plan revision")
    data = {"blocker_id": args.id, "summary": text(args.summary, MAX_TEXT, "--summary", True), "step_id": step,
            "next_action": text(args.next_action, MAX_TEXT, "--next-action"), "owner_decision": bool(args.owner_decision)}
    previous = ctx.state["blockers"].get(args.id)
    if previous is not None:
        if {k: previous.get(k) for k in data} == data:
            return {"unchanged": True, "blocker": args.id}
        raise Refused("blocker_conflict", f"Blocker {args.id} is already recorded with different content")
    ctx.append("blocker.recorded", data, step_id=step, plan_revision=ctx.state["plan_revision"] or None)
    return {"blocker": args.id, "open": True}


def cmd_blocker_resolve(ctx: Context, args) -> dict:
    ctx.writable()
    previous = ctx.state["blockers"].get(args.id)
    if previous is None:
        raise Refused("unknown_blocker", f"Blocker {args.id} is not recorded")
    if not previous["open"]:
        return {"unchanged": True, "blocker": args.id}
    ctx.append("blocker.resolved", {"blocker_id": args.id, "note": text(args.note, MAX_TEXT, "--note", True)},
               plan_revision=ctx.state["plan_revision"] or None)
    return {"blocker": args.id, "open": False}


def cmd_checkpoint(ctx: Context, args) -> dict:
    ctx.writable()
    if not ctx.events:
        raise Refused("nothing_to_checkpoint", "Record at least one Journal event first (run start)")
    st = ctx.state
    if args.current_step is not None and args.current_step not in st["steps"]:
        raise Refused("unknown_step", f"Step {args.current_step} is not in the current Plan revision")
    plan = ctx.plans[st["plan_revision"] - 1] if st["plan_revision"] else None
    body = {"schema": CHECKPOINT_SCHEMA, **ctx.run.identity,
            "cursor": {"seq": len(ctx.events), "hash": ctx.events[-1]["hash"]},
            "plan": {"revision": plan["revision"], "sha256": plan["_sha256"]} if plan else None,
            "current_step": args.current_step, "note": text(args.note, MAX_TEXT, "--note"),
            "steps": {sid: {"implementation": st["steps"][sid]["impl"], "test": st["steps"][sid]["test"]}
                      for sid in st["order"]},
            "uncertain": [sid for sid in st["order"] if st["steps"][sid]["impl"] == "UNCERTAIN"],
            "open_blockers": [b for b, v in st["blockers"].items() if v["open"]],
            "tests": sorted(st["tests"]), "git": git_context(ctx.run), "saved_at": now_iso(),
            "tool_version": TOOL_VERSION}
    last = ctx.events[-1]
    if (last["type"] == "checkpoint.saved" and last["data"].get("current_step") == args.current_step
            and last["data"].get("note") == body["note"]):
        return {"unchanged": True, "checkpoint": "checkpoints/latest.json", "sha256": last["data"]["sha256"],
                "cursor_seq": last["data"]["cursor_seq"]}  # an identical retry right after the same checkpoint
    raw = json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    replace_atomic(ctx.run.sub("checkpoints"), "latest.json", raw)
    ctx.append("checkpoint.saved", {"sha256": sha(raw), "cursor_seq": body["cursor"]["seq"],
                                    "cursor_hash": body["cursor"]["hash"], "current_step": args.current_step,
                                    "note": body["note"]}, plan_revision=st["plan_revision"] or None)
    return {"checkpoint": "checkpoints/latest.json", "sha256": sha(raw), "cursor_seq": body["cursor"]["seq"]}


def report_markdown(ctx: Context, outcome: str, body: str) -> bytes:
    run, st, m = ctx.run, ctx.state, ctx.run.manifest
    lines = [f"# Run Report — {m['task']['id']} · Run {run.identity['run_id']}", "",
             "> Worker-produced report. It is a claim to be reviewed by the Project Architect; it is not VERIFIED and "
             "not Owner ACCEPTED.", "",
             "## Identity", "",
             f"- Project: {m['project']['id']} · Task: {m['task']['id']} · Run: `{run.identity['run_id']}`",
             f"- Worker: {m['worker'].get('display_name') or ''} (`{m['worker']['agent_id']}`), CLI incarnation ref "
             f"`{run.identity['incarnation_ref']}`",
             f"- Spec revision {m['package']['spec_revision']} · approved Prompt revision {m['package']['prompt_revision']}",
             f"- Protocol {m['protocol']['ruleset_id']} {m['protocol']['version']} (`{m['protocol']['sha256'][:16]}…`) · "
             f"tool {TOOL_VERSION} · manifest `{run.manifest_sha256[:16]}…`",
             f"- Outcome claimed by the Worker: **{outcome}**", "",
             f"## Steps (Plan revision {st['plan_revision'] or '—'})", ""]
    if st["order"]:
        lines += ["| Step | Implementation | Test | Test attempts |", "| --- | --- | --- | --- |"]
        lines += [f"| {sid} | {st['steps'][sid]['impl']} | {st['steps'][sid]['test']} | "
                  f"{', '.join(st['steps'][sid]['tests']) or '—'} |" for sid in st["order"]]
    else:
        lines.append("No Working Plan was published.")
    lines += ["", "## Test attempts", ""]
    if st["tests"]:
        for tid, test in st["tests"].items():
            ev = test.get("evidence") or {}
            lines.append(f"- {tid}: `{test['command']}` → exit {test['exit_code']}, **{test['outcome']}**, Steps "
                         f"{', '.join(test['steps'])}; evidence {ev.get('path', 'none')}"
                         f"{' (truncated)' if ev.get('truncated') else ''}; source "
                         f"{(test.get('source') or {}).get('head', 'unknown')}")
    else:
        lines.append("No test attempt was recorded.")
    lines += ["", "## Blockers", ""]
    lines += ([f"- {bid}: {b['summary']} ({'open' if b['open'] else 'resolved'})" for bid, b in st["blockers"].items()]
              or ["None recorded."])
    lines += ["", f"## Journal", "", f"- Events before finalization: {len(ctx.events)}; last hash "
              f"`{ctx.events[-1]['hash'][:16] if ctx.events else '—'}…`", "", "## Worker report", "", body.rstrip(), ""]
    return "\n".join(lines).encode("utf-8")


def cmd_report_finalize(ctx: Context, args) -> dict:
    ctx.writable()
    if args.outcome not in OUTCOMES:
        raise Refused("invalid_argument", "--outcome is completed, partial, blocked or failed")
    if args.outcome in ("completed", "partial") and not ctx.state["plan_revision"]:
        raise Refused("plan_not_published", "A completed/partial Run needs a published Working Plan")
    if args.body_file and args.body_stdin:
        raise Refused("invalid_argument", "Use --body-file or --body-stdin, not both")
    if args.body_file:
        try:
            raw = read_bounded(args.body_file, MAX_REPORT_BODY)
        except OSError:
            raise Refused("invalid_argument", "--body-file cannot be read") from None
    elif args.body_stdin:
        raw = sys.stdin.buffer.read(MAX_REPORT_BODY + 1)
        if len(raw) > MAX_REPORT_BODY:
            raise Refused("too_large", f"The report body exceeds {MAX_REPORT_BODY} bytes")
    else:
        raise Refused("invalid_argument", "The report body is required (--body-file or --body-stdin)")
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("invalid_argument", "The report body must be UTF-8 text") from None
    if not body.strip():
        raise Refused("invalid_argument", "The report body is empty")
    body_sha = sha(raw)
    existing = ctx.state["report"]
    if existing is not None:
        if existing.get("body_sha256") == body_sha and existing.get("outcome") == args.outcome:
            return {"unchanged": True, **existing}
        raise Refused("report_conflict", "The Report is already finalized with other content")
    directory = ctx.run.sub("reports")
    if os.path.lexists(os.path.join(directory, "final.md")):
        raise Refused("report_conflict", "reports/final.md exists without its Journal record")
    data = report_markdown(ctx, args.outcome, body)
    publish_once(directory, "final.md", data)
    record = {"path": "reports/final.md", "sha256": sha(data), "bytes": len(data), "outcome": args.outcome,
              "body_sha256": body_sha}
    ctx.append("report.finalized", record, plan_revision=ctx.state["plan_revision"] or None)
    return {"finalized": True, **record}


def cmd_run_finish(ctx: Context, args) -> dict:
    if ctx.torn:
        raise Refused("journal_torn", "The Journal has a torn tail; run 'journal recover' first")
    st = ctx.state
    report = st["report"]
    if st["finished"]:
        event = ctx.events[-1]
        terminal = terminal_record(ctx, report, event)
        path = os.path.join(ctx.run.root, "terminal.json")
        try:
            current = read_bounded(path, 65_536) if os.path.lexists(path) else None
        except Refused:
            current = None
        if current is None or json.loads(current.decode("utf-8", "replace") or "null") != terminal:
            write_terminal(ctx, terminal)  # a crash after the closing event: re-derive the same record, never new facts
            return {"repaired": True, "terminal": "terminal.json", **st["finished"]}
        return {"unchanged": True, "terminal": "terminal.json", **st["finished"]}
    if report is None:
        raise Refused("report_missing", "Finalize the Report first (report finalize)")
    data = read_bounded(os.path.join(ctx.run.root, "reports", "final.md"), 1_048_576)
    if sha(data) != report["sha256"]:
        raise Refused("report_tampered", "reports/final.md no longer matches its finalized digest")
    counts = {o: sum(1 for t in st["tests"].values() if t["outcome"] == o) for o in TEST_OUTCOMES}
    finish = {"outcome": report["outcome"], "report_sha256": report["sha256"], "tests": counts}
    event = ctx.append("run.finished", finish, plan_revision=st["plan_revision"] or None)
    write_terminal(ctx, terminal_record(ctx, report, event))
    return {"finished": True, "terminal": "terminal.json", **finish}


def terminal_record(ctx: Context, report: dict, event: dict) -> dict:
    return {"schema": TERMINAL_SCHEMA, **ctx.run.identity, "outcome": report["outcome"],
            "report": {"path": report["path"], "sha256": report["sha256"], "bytes": report["bytes"]},
            "journal": {"last_seq": event["seq"], "last_hash": event["hash"]}, "tests": event["data"]["tests"],
            "finished_at": event["at"], "tool_version": TOOL_VERSION}


def write_terminal(ctx: Context, terminal: dict) -> None:
    replace_atomic(ctx.run.root, "terminal.json", json.dumps(terminal, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def cmd_journal_recover(ctx: Context, args) -> dict:
    if not ctx.torn:
        return {"unchanged": True, "torn_tail_bytes": 0}
    directory = ctx.run.sub("journal")
    name = f"torn-{ctx.valid_bytes:010d}.bin"
    if not os.path.lexists(os.path.join(directory, name)):
        publish_once(directory, name, ctx.torn)
    path = os.path.join(directory, "events.jsonl")
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    try:
        os.ftruncate(fd, ctx.valid_bytes)
        os.fsync(fd)
    finally:
        os.close(fd)
    torn = ctx.torn
    ctx.torn = b""
    ctx.append("journal.recovered", {"offset": ctx.valid_bytes, "bytes": len(torn), "sha256": sha(torn),
                                     "preserved_as": f"journal/{name}"})
    return {"recovered": True, "preserved_as": f"journal/{name}", "bytes": len(torn)}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rosc-run-tool.py", description="ROSC Worker Artifact Tool (offline, run-pinned)",
                                epilog="Paths are derived from this file's Run package; see worker-execution-protocol.md "
                                       "for the stage-by-stage usage.")
    p.add_argument("--version", action="version", version=f"rosc-run-tool {TOOL_VERSION}")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    run = sub.add_parser("run").add_subparsers(dest="run_command", required=True)
    run.add_parser("start").set_defaults(fn=cmd_run_start)
    run.add_parser("finish").set_defaults(fn=cmd_run_finish)
    plan = sub.add_parser("plan").add_subparsers(dest="plan_command", required=True)
    add = plan.add_parser("add-step")
    for flag in ("--id", "--title"):
        add.add_argument(flag, required=True)
    for flag in ("--description", "--depends", "--expect", "--test-intent"):
        add.add_argument(flag)
    add.set_defaults(fn=cmd_plan_add)
    rm = plan.add_parser("remove-step")
    rm.add_argument("--id", required=True)
    rm.set_defaults(fn=cmd_plan_remove)
    plan.add_parser("show").set_defaults(fn=cmd_plan_show)
    pub = plan.add_parser("publish")
    pub.add_argument("--reason")
    pub.set_defaults(fn=cmd_plan_publish)
    rev = plan.add_parser("revise")
    rev.add_argument("--reason", required=True)
    rev.set_defaults(fn=cmd_plan_revise)
    step = sub.add_parser("step").add_subparsers(dest="step_command", required=True)
    for name in STEP_COMMANDS:
        s = step.add_parser(name)
        s.add_argument("--id", required=True)
        s.add_argument("--note")
        s.add_argument("--evidence", action="append")
        s.set_defaults(fn=cmd_step)
    test = sub.add_parser("test").add_subparsers(dest="test_command", required=True)
    begin = test.add_parser("begin")
    begin.add_argument("--id", required=True)
    begin.add_argument("--steps", required=True)
    begin.set_defaults(fn=cmd_test_begin)
    rec = test.add_parser("record")
    rec.add_argument("--id", required=True)
    rec.add_argument("--steps", required=True)
    rec.add_argument("--command", required=True)
    rec.add_argument("--exit", type=int, required=True)
    rec.add_argument("--outcome", required=True)
    rec.add_argument("--log-file")
    rec.add_argument("--log-stdin", action="store_true")
    rec.add_argument("--limitations")
    rec.set_defaults(fn=cmd_test_record)
    note = sub.add_parser("note")
    note.add_argument("--kind", required=True)
    note.add_argument("--text", required=True)
    note.set_defaults(fn=cmd_note)
    blocker = sub.add_parser("blocker").add_subparsers(dest="blocker_command", required=True)
    br = blocker.add_parser("record")
    br.add_argument("--id", required=True)
    br.add_argument("--summary", required=True)
    br.add_argument("--step")
    br.add_argument("--next-action")
    br.add_argument("--owner-decision", action="store_true")
    br.set_defaults(fn=cmd_blocker_record)
    bv = blocker.add_parser("resolve")
    bv.add_argument("--id", required=True)
    bv.add_argument("--note", required=True)
    bv.set_defaults(fn=cmd_blocker_resolve)
    cp = sub.add_parser("checkpoint").add_subparsers(dest="checkpoint_command", required=True)
    save = cp.add_parser("save")
    save.add_argument("--note")
    save.add_argument("--current-step")
    save.set_defaults(fn=cmd_checkpoint)
    rep = sub.add_parser("report").add_subparsers(dest="report_command", required=True)
    fin = rep.add_parser("finalize")
    fin.add_argument("--outcome", required=True)
    fin.add_argument("--body-file")
    fin.add_argument("--body-stdin", action="store_true")
    fin.set_defaults(fn=cmd_report_finalize)
    journal = sub.add_parser("journal").add_subparsers(dest="journal_command", required=True)
    journal.add_parser("recover").set_defaults(fn=cmd_journal_recover)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        run = Run(os.path.abspath(__file__))
        lock_path = os.path.join(run.root, ".tool.lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # one writer at a time for this Run
            result = args.fn(Context(run), args)
        finally:
            os.close(fd)
    except Refused as exc:
        print(json.dumps({"ok": False, "error": exc.code, "message": exc.message}, ensure_ascii=False))
        return 2
    except OSError as exc:
        print(json.dumps({"ok": False, "error": "io_error", "message": type(exc).__name__}))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
