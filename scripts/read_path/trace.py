"""Raw read-path event stream and routing analysis (ROSC adaptation).

Evidence model, stated plainly: an event exists only because the agent (or a
wrapper) ran the recorder after reading a document. The recorder adds a tool-side
timestamp and the SHA-256 of the file as it is on disk at that moment, so an event
proves *the document existed with that content when it was recorded* -- not that
the agent actually consumed it. DOUTLAB's reference mechanism has the same limit
(a hand-kept table plus a hash ledger); no harness read hook exists to do better.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import ledger

REPO_ROOT = Path(__file__).resolve().parents[2]
TELEMETRY_ROOT = REPO_ROOT / ".agent-telemetry"
RUNS_DIR = TELEMETRY_ROOT / "read-path"
LEDGER_PATH = TELEMETRY_ROOT / "read-path-state.json"

SCHEMA = 1
CATEGORIES = ("BOOTSTRAP", "OWNER", "DEPENDENCY", "DETOUR")
OUTCOMES = ("full", "partial", "failed")
CONTEXT_MODES = ("COLD", "WARM")
# Routing entry points named by AGENTS.md / CLAUDE.md; reading them is never a miss.
BOOTSTRAP_ROOTS = ("docs/README.md", "docs/development_workflow.md")
# The only non-docs files that may be registered: the repository bootstrap pair, and
# only as BOOTSTRAP. Nothing else outside docs/ is ever a documentation read.
BOOTSTRAP_FILES = ("AGENTS.md", "CLAUDE.md")

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


class TraceError(ValueError):
    """Invalid recorder input or run state."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_path(run_id: str, runs_dir: Path = RUNS_DIR) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        raise TraceError(f"invalid run id: {run_id!r}")
    return runs_dir / f"{run_id}.jsonl"


def doc_rel(repo_root: Path, path: str) -> str:
    """Normalize a documentation path to repo-relative form, or refuse it.

    Only canonical Markdown under ``docs/`` is a documentation read; code,
    tests, searches and the bootstrap files are deliberately not logged.
    """

    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else repo_root / candidate).resolve()
    try:
        rel = resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        raise TraceError(f"path is outside the repository: {path}") from None
    if rel not in BOOTSTRAP_FILES and not (rel.startswith("docs/") and rel.endswith(".md")):
        raise TraceError(f"not a documentation read (docs/**/*.md or {', '.join(BOOTSTRAP_FILES)}): {rel}")
    return rel


def append(file: Path, event: dict) -> dict:
    events, _ = read_events(file)
    event = {"seq": len(events) + 1, "ts": now(), **event}
    with file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")
    return event


def read_events(file: Path) -> tuple[list[dict], int]:
    """Return ``(events, corrupt_line_count)``; a missing file is empty."""

    events: list[dict] = []
    corrupt = 0
    try:
        text = file.read_text(encoding="utf-8")
    except OSError:
        return events, corrupt
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            corrupt += 1
            continue
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            events.append(value)
        else:
            corrupt += 1
    return events, corrupt


def init_run(
    *, repo_root: Path, runs_dir: Path, run_id: str, prompt_id: str, report: str,
    context_mode: str, owners: list[str], head: str, branch: str,
) -> Path:
    if context_mode not in CONTEXT_MODES:
        raise TraceError(f"context mode must be one of {CONTEXT_MODES}")
    file = run_path(run_id, runs_dir)
    if file.exists():
        raise TraceError(f"run already exists, not overwritten: {file.name}")
    owner_rels = [doc_rel(repo_root, owner) for owner in owners]
    runs_dir.mkdir(parents=True, exist_ok=True)
    append(file, {
        "type": "run_start", "schema": SCHEMA, "run_id": run_id, "prompt_id": prompt_id,
        "report": report, "context_mode": context_mode, "owners": owner_rels,
        "head": head, "branch": branch,
    })
    return file


def record_read(
    *, repo_root: Path, file: Path, state_path: Path, path: str, category: str,
    outcome: str = "full", reason: str | None = None,
) -> dict:
    if category not in CATEGORIES:
        raise TraceError(f"category must be one of {CATEGORIES}")
    if outcome not in OUTCOMES:
        raise TraceError(f"outcome must be one of {OUTCOMES}")
    start = _require_open_run(file)
    rel = doc_rel(repo_root, path)
    if rel in BOOTSTRAP_FILES and category != "BOOTSTRAP":
        raise TraceError(f"{rel} may only be recorded as BOOTSTRAP")
    sha = None
    size = None
    try:
        data = (repo_root / rel).read_bytes()
        sha, size = ledger.content_id(repo_root / rel), len(data)
    except OSError:
        outcome = "failed"  # a missing file can never be recorded as a successful read
    event = append(file, {
        "type": "doc_read", "path": rel, "category": category, "outcome": outcome,
        "sha256": sha, "bytes": size, "reason": reason, "source": "agent-recorded",
    })
    if outcome == "full" and sha:
        ledger.confirm(rel_path=rel, sha256=sha, state_path=state_path, provenance=start["run_id"])
    return event


def record_reuse(
    *, repo_root: Path, file: Path, state_path: Path, path: str, note: str | None = None,
    residency_claimed: bool = False,
) -> dict:
    """Log reuse of resident context (WARM only).

    ``freshness`` is tool-computed (SHA-256 vs ledger). ``residency`` is only what the
    agent *claims* about its own context; a matching hash proves the file is unchanged,
    never that its text is present in the agent's context, so it is never "verified".
    """

    start = _require_open_run(file)
    if start.get("context_mode") != "WARM":
        raise TraceError("context reuse is only permitted in a WARM run; a COLD run must read fresh")
    rel = doc_rel(repo_root, path)
    classification, sha = ledger.classify(repo_root=repo_root, rel_path=rel, state_path=state_path)
    return append(file, {
        "type": "doc_reuse", "path": rel, "classification": classification, "sha256": sha,
        "fresh_read_required": classification != ledger.REUSED_VALID, "note": note,
        "residency": "agent-claimed" if residency_claimed else "not-claimed", "residency_verified": False,
    })


def record_obligation(*, repo_root: Path, file: Path, path: str, trigger: str) -> dict:
    _require_open_run(file)
    if not trigger.strip():
        raise TraceError("an obligation must cite the router trigger that requires it")
    return append(file, {"type": "obligation", "path": doc_rel(repo_root, path), "trigger": trigger.strip()})


def end_run(*, file: Path, head: str) -> dict:
    _require_open_run(file)
    return append(file, {"type": "run_end", "head": head})


def _require_open_run(file: Path) -> dict:
    events, _ = read_events(file)
    if not events or events[0].get("type") != "run_start":
        raise TraceError(f"no initialised run at {file.name}")
    if any(e.get("type") == "run_end" for e in events):
        raise TraceError("run is already ended")
    return events[0]


def links_from(repo_root: Path, rel: str) -> set[str]:
    """Markdown documents (repo-relative) that ``rel`` links to; unreadable -> empty."""

    try:
        text = (repo_root / rel).read_text(encoding="utf-8")
    except OSError:
        return set()
    found: set[str] = set()
    for target in _LINK_RE.findall(text):
        target = target.split("#", 1)[0]
        if not target or "://" in target or not target.endswith(".md"):
            continue
        resolved = ((repo_root / rel).parent / target).resolve()
        try:
            found.add(resolved.relative_to(repo_root.resolve()).as_posix())
        except ValueError:
            continue
    return found


def analyze(*, repo_root: Path, file: Path) -> dict:
    """Derive the routing report from the raw stream. Never raises on bad data."""

    events, corrupt = read_events(file)
    start = next((e for e in events if e.get("type") == "run_start"), None)
    if start is None:
        return {"verdict": "UNAVAILABLE", "reason": "no readable run_start event", "corrupt_lines": corrupt,
                "events": len(events)}
    reads = [e for e in events if e.get("type") == "doc_read"]
    reuses = [e for e in events if e.get("type") == "doc_reuse"]
    done = [r for r in reads if r.get("outcome") in ("full", "partial") and isinstance(r.get("path"), str)]

    link_cache: dict[str, set[str]] = {}

    def links(path: str) -> set[str]:
        return link_cache.setdefault(path, links_from(repo_root, path))

    def link_state(read: dict, path: str) -> tuple[bool, bool]:
        """``(fully_read, unchanged_since_read)`` for a recorded read of ``path``."""

        recorded = read.get("sha256")
        try:
            unchanged = bool(recorded) and ledger.content_id(repo_root / path) == recorded
        except OSError:
            unchanged = False
        return read.get("outcome") == "full", unchanged

    def vouches(state: tuple[bool, bool], source: str, target: str) -> str:
        """verified / uncertain / no: may ``source`` be relied on as routing to ``target``?"""

        full, unchanged = state
        if not unchanged:
            return "uncertain"  # content differs from what was read (or file gone): links unknowable
        if target not in links(source):
            return "no"
        return "verified" if full else "uncertain"  # linked, but agent only partly read it

    # Routing: confident only through an earlier fully-read router whose content still
    # matches the hash recorded at read time. Otherwise the route is uncertain, not a miss.
    chain, misses, uncertain, seen = [], [], [], []
    states: dict[str, tuple[bool, bool]] = {}
    for read in done:
        path = read["path"]
        verified = next((s for s in seen if vouches(states[s], s, path) == "verified"), None)
        unsure = [s for s in seen if vouches(states[s], s, path) == "uncertain"]
        if path in BOOTSTRAP_ROOTS or path in BOOTSTRAP_FILES:
            route, via = "bootstrap", "bootstrap"
        elif verified:
            route, via = "verified", verified
        elif unsure:
            route, via = "uncertain", None
            uncertain.append(path)
        else:
            route, via = "none", None
            if path not in seen:
                misses.append(path)
        chain.append({"seq": read["seq"], "path": path, "category": read.get("category"), "via": via, "route": route})
        new = link_state(read, path)
        if path not in states or new[0]:
            states[path] = new
        seen.append(path)

    duplicates, cycles, last = [], [], {}
    for index, read in enumerate(done):
        path = read["path"]
        if path in last:
            prev_index, prev = last[path]
            justified = bool(read.get("reason")) or read.get("sha256") != prev.get("sha256")
            item = {"seq": read["seq"], "path": path, "justified": justified, "between": index - prev_index - 1}
            duplicates.append(item)
            if not justified and item["between"] >= 1:
                cycles.append(item)
        last[path] = (index, read)

    owners = [o for o in start.get("owners", []) if isinstance(o, str)]
    declared = {e["path"]: e.get("trigger") for e in events if e.get("type") == "obligation" and isinstance(e.get("path"), str)}
    obligations: dict[str, dict] = {}
    for path in (*BOOTSTRAP_ROOTS, *owners):
        obligations[path] = {"basis": "bootstrap" if path in BOOTSTRAP_ROOTS else "declared-owner"}
    for path, trigger in declared.items():
        obligations.setdefault(path, {"basis": f"router trigger: {trigger}"})
    for path, info in obligations.items():
        outcomes = {r.get("outcome") for r in reads if r.get("path") == path}
        reuse = [r for r in reuses if r.get("path") == path and not r.get("fresh_read_required")]
        if "full" in outcomes:
            info["status"] = "full"
        elif reuse:
            # Freshness only: the file is unchanged. Residency in context is the agent's claim.
            info["status"] = "reused-valid-claimed" if any(r.get("residency") == "agent-claimed" for r in reuse) else "reused-valid-unclaimed"
        elif "partial" in outcomes:
            info["status"] = "partial"
        else:
            info["status"] = "not-recorded"
        if path in BOOTSTRAP_ROOTS:
            info["router_cited"] = True
        else:
            votes = {vouches(states[s], s, path) for s in seen}
            # None = cannot say: a read router changed since it was read, or was only partly read.
            info["router_cited"] = True if "verified" in votes else (None if "uncertain" in votes else False)

    full = sum(1 for i in obligations.values() if i["status"] == "full")
    reused = sum(1 for i in obligations.values() if i["status"].startswith("reused-valid"))
    partial = sum(1 for i in obligations.values() if i["status"] == "partial")
    absent = sum(1 for i in obligations.values() if i["status"] == "not-recorded")
    counts = {c: sum(1 for r in done if r.get("category") == c) for c in CATEGORIES}
    findings = {
        "router_misses": misses,
        "detours": counts["DETOUR"],
        "unexplained_repeats": [d for d in duplicates if not d["justified"]],
        "potential_cycles": cycles,
        "not_recorded_obligations": [p for p, i in obligations.items() if i["status"] == "not-recorded"],
        "partial_obligations": [p for p, i in obligations.items() if i["status"] == "partial"],
        "uncited_obligations": [p for p, i in obligations.items() if p in declared and i["router_cited"] is False],
        "failed_reads": [r.get("path") for r in reads if r.get("outcome") == "failed"],
        "reuse_needing_fresh_read": [r["path"] for r in reuses if r.get("fresh_read_required")],
    }
    uncertainties = {
        "route_uncertain": uncertain,
        "obligation_citation_uncertain": [p for p, i in obligations.items() if i["router_cited"] is None],
        "corrupt_lines": corrupt,
    }
    ended = any(e.get("type") == "run_end" for e in events)
    if not done and not reuses:
        verdict = "UNAVAILABLE"
    elif not ended:
        verdict = "INCOMPLETE"
    elif any(bool(v) for v in findings.values()):
        verdict = "ATTENTION"
    elif any(bool(v) for v in uncertainties.values()):
        verdict = "INCONCLUSIVE"
    else:
        verdict = "NO-FINDINGS"
    return {
        "verdict": verdict, "run_id": start.get("run_id"), "prompt_id": start.get("prompt_id"),
        "context_mode": start.get("context_mode"), "report": start.get("report"),
        "head_start": start.get("head"), "head_end": next((e.get("head") for e in events if e.get("type") == "run_end"), None),
        "started": start.get("ts"), "ended": next((e.get("ts") for e in events if e.get("type") == "run_end"), None),
        "reads": len(done), "reuses": len(reuses), "categories": counts, "chain": chain,
        "duplicates": duplicates, "obligations": obligations,
        "coverage": f"{full} full, {reused} freshness-only reuse, {partial} partial, {absent} not-recorded of {len(obligations)} obligations",
        "confidence": "agent-recorded, hash-verified on disk; not harness-instrumented",
        "corrupt_lines": corrupt, "findings": findings, "uncertainties": uncertainties,
    }

