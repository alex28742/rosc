"""Naming, hashing, integrity, and report-skeleton helpers for local agent
artifacts.

Self-contained: the only external process invoked here is ``git`` (for
branch/HEAD/worktree facts a report records). No third-party module is imported;
the optional Google Drive transport lives in ``drive.py`` and is imported
lazily by callers that need it.

Local artifact bytes are hashed with SHA-256. A frozen prompt's freeze-time hash
and a finalized report's final hash are recorded in the sync-state manifest
(``state.py``), never embedded in the artifact itself (that would be a
self-reference problem).
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import state as state_mod

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
TELEMETRY_ROOT = REPO_ROOT / ".agent-telemetry"
PROMPTS_DIR = TELEMETRY_ROOT / "prompts"
REPORTS_DIR = TELEMETRY_ROOT / "reports"
STATE_PATH = TELEMETRY_ROOT / "agent-sync" / "state.json"

_SLUG_COLLAPSE_RE = re.compile(r"[^a-z0-9]+")

# The `__r<NN>` suffix is optional on *parse* only, for backward compatibility
# with snapshots placed before the naming contract existed -- a missing suffix
# parses as an implicit r01. Every NEW filename this module builds always
# includes the explicit suffix. `prompt_id` is any run of non-underscore
# characters, which supports dotted IDs such as `003.1` and `003.2`.
_FROZEN_PROMPT_RE = re.compile(
    r"^p(?P<prompt_id>[^_]+)__(?P<slug>.+?)(?:__r(?P<run>\d+))?\.md$"
)
_REPORT_RE = re.compile(
    r"^p(?P<prompt_id>[^_]+)__(?P<timestamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2})"
    r"__(?P<model>[^_]+)__r(?P<run>\d+)\.md$"
)


def slugify(text: str) -> str:
    """Lowercase, hyphen-joined, filesystem-safe slug.

    Collapses any run of characters outside ``[a-z0-9]`` into one hyphen and
    strips leading/trailing hyphens. Never returns an empty string.
    """

    slug = _SLUG_COLLAPSE_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "untitled"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FrozenPromptName:
    prompt_id: str
    slug: str
    run: int

    @property
    def filename(self) -> str:
        return f"p{self.prompt_id}__{self.slug}__r{self.run:02d}.md"


def parse_frozen_prompt_filename(filename: str) -> FrozenPromptName | None:
    """Parse a frozen-prompt filename, or return ``None`` when it does not match
    the naming contract at all (a missing ``__r<NN>`` suffix still parses, as an
    implicit r01)."""

    match = _FROZEN_PROMPT_RE.match(filename)
    if not match:
        return None
    run = int(match.group("run")) if match.group("run") else 1
    return FrozenPromptName(
        prompt_id=match.group("prompt_id"), slug=match.group("slug"), run=run
    )


def next_frozen_prompt_path(
    prompt_id: str, slug: str, *, directory: Path | None = None
) -> Path:
    """Resolve the path a NEW frozen snapshot for ``(prompt_id, slug)`` should
    use, bumping the run number when one already exists -- an executed snapshot
    is never silently overwritten.

    ``directory`` defaults to the module-level ``PROMPTS_DIR``, looked up at
    call time (not bound at import time) so callers/tests can redirect it by
    assigning ``artifacts.PROMPTS_DIR``.
    """

    directory = directory if directory is not None else PROMPTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    run = 1
    while True:
        candidate = FrozenPromptName(prompt_id=prompt_id, slug=slugify(slug), run=run)
        path = directory / candidate.filename
        if not path.exists():
            return path
        run += 1


def parse_report_filename(filename: str) -> dict | None:
    match = _REPORT_RE.match(filename)
    if not match:
        return None
    data = match.groupdict()
    data["run"] = int(data["run"])
    return data


def report_filename(
    prompt_id: str, *, model_slug: str, run: int = 1, when: datetime | None = None
) -> str:
    when = when or datetime.now()
    stamp = when.strftime("%Y-%m-%d_%H-%M")
    return f"p{prompt_id}__{stamp}__{slugify(model_slug)}__r{run:02d}.md"


def next_report_path(
    prompt_id: str,
    *,
    model_slug: str,
    when: datetime | None = None,
    directory: Path | None = None,
) -> Path:
    """``directory`` defaults to ``REPORTS_DIR``, looked up at call time -- see
    :func:`next_frozen_prompt_path` for why."""

    directory = directory if directory is not None else REPORTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    run = 1
    while True:
        name = report_filename(prompt_id, model_slug=model_slug, run=run, when=when)
        path = directory / name
        if not path.exists():
            return path
        run += 1


def _exclusive_create(path: Path, content: str) -> None:
    """Create ``path`` and write ``content``, atomically refusing to overwrite.

    ``open("x")`` is the exclusive-creation primitive: it fails with
    :class:`FileExistsError` when the path already exists, closing the
    check-then-write race that ``next_*_path()`` alone leaves open. Historical
    artifacts are therefore never silently overwritten.
    """

    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)


def create_frozen_prompt(
    prompt_id: str,
    slug: str,
    content: str,
    *,
    directory: Path | None = None,
    read_only: bool = True,
) -> Path:
    """Atomically create the next free frozen-prompt snapshot for
    ``(prompt_id, slug)``.

    When the chosen ``r<NN>`` path is taken between resolution and creation (a
    concurrent freeze), the exclusive create raises ``FileExistsError`` and the
    next revision is tried -- the loser becomes a new visible revision rather
    than an overwrite.
    """

    directory = directory if directory is not None else PROMPTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    run = 1
    while True:
        candidate = FrozenPromptName(prompt_id=prompt_id, slug=slugify(slug), run=run)
        path = directory / candidate.filename
        try:
            _exclusive_create(path, content)
        except FileExistsError:
            run += 1
            continue
        if read_only:
            make_read_only(path)
        return path


def create_report(
    prompt_id: str,
    *,
    model_slug: str,
    content: str,
    when: datetime | None = None,
    directory: Path | None = None,
) -> Path:
    """Atomically create the next free report for ``(prompt_id, model_slug)``.

    Same exclusive-create-with-retry contract as :func:`create_frozen_prompt`: a
    report is an authoritative historical artifact and is never silently
    overwritten. ``when`` is resolved once so every retry shares one timestamp.
    """

    directory = directory if directory is not None else REPORTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    when = when or datetime.now()
    run = 1
    while True:
        name = report_filename(prompt_id, model_slug=model_slug, run=run, when=when)
        path = directory / name
        try:
            _exclusive_create(path, content)
        except FileExistsError:
            run += 1
            continue
        return path


def make_read_only(path: Path) -> bool:
    """Best-effort accidental-write guard for a frozen artifact.

    On POSIX this sets mode ``0o444`` (on Windows the same call maps to the NTFS
    read-only attribute). **This is not a security boundary** -- a file owner can
    always restore write access to their own file, so it only blocks a naive
    ``open(path, "w")``/``write_text()``, never a deliberate ``chmod +w`` edit.
    Returns ``True`` on success and ``False`` (with a warning) on any
    :class:`OSError`; it never raises, and freeze/verification logic must not
    depend on it succeeding.
    """

    try:
        path.chmod(0o444)
        return True
    except OSError as exc:
        logger.warning("Could not mark %s read-only (%s); continuing.", path, exc)
        return False


class FrozenPromptIntegrityError(RuntimeError):
    """Raised when a frozen prompt cannot be verified as frozen: the file is
    missing/unreadable, no freeze-time SHA-256 is recorded, or the recorded hash
    does not match the current bytes. Fail closed -- never silently enroll,
    re-hash, repair, or rewrite."""


@dataclass(frozen=True)
class IntegrityResult:
    path: Path
    sha256: str
    recorded_sha256: str

    @property
    def verified(self) -> bool:
        return True


def verify_frozen_prompt(
    path: Path,
    *,
    state_path: Path | None = None,
    repo_root: Path | None = None,
) -> IntegrityResult:
    """Managed-run verification of a frozen prompt: **fail closed**.

    A frozen prompt is trusted only when all of the following hold:

    - the file exists and is a regular file;
    - it is readable;
    - the sync-state manifest records an expected freeze-time SHA-256 for that
      exact artifact;
    - the current file SHA-256 equals that recorded value.

    A missing expected hash is treated as "not verifiably frozen" and rejected
    (never silently enrolled/re-hashed). A mismatch is rejected. The file is
    never rewritten or repaired. Explicit enrollment is a separate, deliberate
    operation (``freeze-write``), never part of managed-run verification.
    """

    state_path = state_path if state_path is not None else STATE_PATH
    repo_root = repo_root if repo_root is not None else REPO_ROOT
    if not path.exists() or not path.is_file():
        raise FrozenPromptIntegrityError(f"Frozen prompt not found or not a file: {path}")
    try:
        current = sha256_file(path)
    except OSError as exc:
        raise FrozenPromptIntegrityError(
            f"Frozen prompt is not readable: {path} ({exc})"
        ) from exc
    try:
        local_path = str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        local_path = str(path)
    recorded = state_mod.recorded_sha256(state_path, local_path)
    if recorded is None:
        raise FrozenPromptIntegrityError(
            f"Frozen prompt {local_path} has no recorded freeze-time SHA-256 in "
            f"{state_path}; refusing to treat it as verifiably frozen."
        )
    if recorded != current:
        raise FrozenPromptIntegrityError(
            f"Frozen prompt {local_path} SHA-256 mismatch: recorded {recorded}, "
            f"found {current}."
        )
    return IntegrityResult(path=path, sha256=current, recorded_sha256=recorded)


class ReportTailError(ValueError):
    """Raised when a report does not end in a valid recognized standard tail
    (absent, incomplete, or displaced/followed by nonstandard content), so it
    cannot be safely normalized/finalized."""


class ReportProvenanceError(ValueError):
    """Raised when managed report metadata does not match verified prompt and
    Git state."""


STANDARD_TAIL_HEADINGS = ("## Sync state", "## Telemetry")
_HEADING_RE = re.compile(r"(?m)^## .*$")
_ANY_HEADING_RE = re.compile(r"(?m)^#{1,6}[ \t].*$")


def _h2_sections(text: str) -> list[tuple[str, int, int]]:
    """``(heading, start, end)`` for every level-2 heading, in order."""

    matches = list(_HEADING_RE.finditer(text))
    sections: list[tuple[str, int, int]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(0).strip(), match.start(), end))
    return sections


def normalize_report_tail(text: str) -> str:
    """Validate and normalize a report's recognized *trailing* standard tail.

    A managed report must end in a complete standard trailing block: a maximal
    trailing run of H2 sections whose headings are ``## Sync state`` and
    ``## Telemetry``, containing both, with no other heading (of any level)
    appearing after the run begins. Only that trailing run is normalized -- to
    exactly one of each in canonical order, keeping the **last** occurrence's
    body -- while all earlier content (including any earlier, legitimate section
    that happens to share a standard heading name) is preserved byte-for-byte.

    Raises :class:`ReportTailError` when the canonical tail is absent,
    incomplete, or not terminal (displaced/followed by nonstandard content).
    Finalization must reject such a report rather than guess. Deterministic and
    idempotent for valid input.
    """

    sections = _h2_sections(text)
    run_start = len(sections)
    while run_start > 0 and sections[run_start - 1][0] in STANDARD_TAIL_HEADINGS:
        run_start -= 1
    if run_start == len(sections):
        raise ReportTailError(
            "canonical standard tail is missing or not terminal (the report must "
            "end with '## Sync state' followed by '## Telemetry')"
        )
    tail_start = sections[run_start][1]
    # Nothing but the recognized standard headings may appear from the start of
    # the trailing run onward; any other heading means the tail is displaced or
    # followed by nonstandard content.
    for match in _ANY_HEADING_RE.finditer(text, tail_start):
        if match.group(0).strip() not in STANDARD_TAIL_HEADINGS:
            raise ReportTailError("nonstandard content follows the canonical standard tail")
    bodies: dict[str, str] = {}
    for heading, start, end in sections[run_start:]:
        block = text[start:end]
        body = block.split("\n", 1)[1] if "\n" in block else ""
        bodies[heading] = body
    missing = [heading for heading in STANDARD_TAIL_HEADINGS if heading not in bodies]
    if missing:
        raise ReportTailError(
            "canonical standard tail is incomplete; missing " + ", ".join(missing)
        )
    parts = []
    for heading in STANDARD_TAIL_HEADINGS:
        body = bodies[heading].strip("\n")
        parts.append(f"{heading}\n\n{body}\n")
    return text[:tail_start] + "\n".join(parts)


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def resolve_head_sha(repo_root: Path | None = None) -> str:
    """Full HEAD SHA, or ``"none"`` when it cannot be resolved."""

    repo_root = repo_root if repo_root is not None else REPO_ROOT
    result = _git(repo_root, "rev-parse", "HEAD")
    if result.returncode != 0:
        return "none"
    return result.stdout.strip() or "none"


def current_branch_name(repo_root: Path | None = None) -> str | None:
    """The local branch name, or ``None`` when HEAD is detached."""

    repo_root = repo_root if repo_root is not None else REPO_ROOT
    result = _git(repo_root, "symbolic-ref", "--short", "-q", "HEAD")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_worktree_clean(repo_root: Path | None = None) -> bool:
    """True when ``git status --porcelain`` reports no changes."""

    repo_root = repo_root if repo_root is not None else REPO_ROOT
    result = _git(repo_root, "status", "--porcelain")
    return result.returncode == 0 and not result.stdout.strip()


def git_context(repo_root: Path | None = None) -> dict:
    """Branch/HEAD/worktree facts a report records."""

    repo_root = repo_root if repo_root is not None else REPO_ROOT
    return {
        "head": resolve_head_sha(repo_root),
        "branch": current_branch_name(repo_root),
        "worktree_clean": is_worktree_clean(repo_root),
    }


_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def git_commit_exists(commit: str, repo_root: Path | None = None) -> bool:
    """Whether ``commit`` is a full SHA naming a local commit object."""

    if not _FULL_SHA_RE.fullmatch(commit):
        return False
    repo_root = repo_root if repo_root is not None else REPO_ROOT
    result = _git(repo_root, "cat-file", "-e", f"{commit}^{{commit}}")
    return result.returncode == 0


def git_is_ancestor(
    ancestor: str, descendant: str, repo_root: Path | None = None
) -> bool:
    """Whether two verified commits have the required ancestry relation."""

    repo_root = repo_root if repo_root is not None else REPO_ROOT
    if not git_commit_exists(ancestor, repo_root) or not git_commit_exists(
        descendant, repo_root
    ):
        return False
    return _git(repo_root, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def validate_base_commit(
    base_commit: str, *, current_head: str, repo_root: Path | None = None
) -> None:
    """Validate an explicitly captured managed-run baseline.

    Report creation may happen after the implementation commit, so the base is
    allowed to be the current HEAD or an ancestor of it, but never an unknown,
    abbreviated, future, or unrelated revision.
    """

    repo_root = repo_root if repo_root is not None else REPO_ROOT
    if not git_commit_exists(base_commit, repo_root):
        raise ReportProvenanceError(
            "base commit must be an existing full 40-character commit SHA"
        )
    if not git_is_ancestor(base_commit, current_head, repo_root):
        raise ReportProvenanceError(
            "base commit is not the current HEAD or one of its ancestors"
        )


def _report_metadata_value(text: str, label: str) -> str | None:
    match = re.search(rf"(?m)^- {re.escape(label)}: (.+)$", text)
    return match.group(1).strip() if match else None


def validate_managed_report_provenance(
    text: str,
    *,
    repo_root: Path | None = None,
    state_path: Path | None = None,
) -> None:
    """Fail closed when a managed report claims unverifiable provenance.

    Legacy/bootstrap reports without the standard run-metadata block remain
    finalizable. A standard block whose prompt snapshot is ``(none)`` is an
    explicit ad-hoc report and is likewise outside this managed validation.
    """

    snapshot_value = _report_metadata_value(text, "Prompt snapshot")
    if snapshot_value is None or snapshot_value == "(none)":
        return
    repo_root = repo_root if repo_root is not None else REPO_ROOT
    state_path = state_path if state_path is not None else STATE_PATH
    required = {
        label: _report_metadata_value(text, label)
        for label in (
            "Prompt ID",
            "Prompt revision/run",
            "Prompt SHA-256",
            "Base commit",
            "Result commit",
        )
    }
    missing = [label for label, value in required.items() if value is None]
    if missing:
        raise ReportProvenanceError(
            "managed report metadata is incomplete; missing " + ", ".join(missing)
        )

    snapshot = Path(snapshot_value)
    if not snapshot.is_absolute():
        snapshot = repo_root / snapshot
    try:
        snapshot.resolve().relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ReportProvenanceError(
            "managed prompt snapshot must be inside the repository workspace"
        ) from exc
    try:
        integrity = verify_frozen_prompt(
            snapshot, state_path=state_path, repo_root=repo_root
        )
    except FrozenPromptIntegrityError as exc:
        raise ReportProvenanceError(str(exc)) from exc
    parsed = parse_frozen_prompt_filename(snapshot.name)
    expected_revision = f"r{parsed.run:02d}" if parsed else None
    if parsed is None or parsed.prompt_id != required["Prompt ID"]:
        raise ReportProvenanceError(
            "managed report prompt ID does not match its verified snapshot"
        )
    if required["Prompt revision/run"] != expected_revision:
        raise ReportProvenanceError(
            "managed report prompt revision does not match its verified snapshot"
        )
    if required["Prompt SHA-256"] != integrity.sha256:
        raise ReportProvenanceError(
            "managed report prompt SHA-256 does not match its verified snapshot"
        )

    base_commit = required["Base commit"]
    result_commit = required["Result commit"]
    current_head = resolve_head_sha(repo_root)
    if not git_commit_exists(result_commit, repo_root):
        raise ReportProvenanceError(
            "result commit must be an existing full 40-character commit SHA"
        )
    validate_base_commit(base_commit, current_head=result_commit, repo_root=repo_root)
    if result_commit != current_head:
        raise ReportProvenanceError(
            "result commit does not match the current repository HEAD"
        )


REPORT_TEMPLATE = """# Prompt {prompt_id} — {title}

## Run metadata

- Prompt ID: {prompt_id}
- Prompt snapshot: {prompt_snapshot}
- Prompt revision/run: r{prompt_run:02d}
- Prompt SHA-256: {prompt_sha256}
- Agent/model: {model}
- Branch: {branch}
- Base commit: {base_commit}
- Result commit: {result_commit}
- Status: {status}
- Start: {start}
- End: {end}

## Result

(concise summary of work completed)

## Validation

(tests/checks run, pass/fail state)

## Outstanding items

(unresolved issues, caveats, skipped scope, follow-up needs)

## Documentation Impact Check

- Reviewed: (exact canonical document paths considered because this task's changed
  behavior/structure/decision/operations surface could plausibly affect them, each
  with a concise outcome — updated / no change needed and why. If no canonical
  document is plausibly affected, say so explicitly instead of inventing entries.)
- Updated: (exact document paths actually changed by this task, with a one-line
  summary of what changed; "none" if the Reviewed outcomes required no changes.)
- New drift/debt found (optional): (stale documentation discovered during the check
  but not caused by this task, with its likely canonical owner/path; omit if none.)

## Sync state

sync: pending

(Historical finalization-time snapshot. This line is never rewritten after the
report is finalized; current/live sync status lives in
`.agent-telemetry/agent-sync/state.json`, which the completion line reads.)

## Telemetry

{telemetry_section}
"""


def render_report_skeleton(
    *,
    prompt_id: str,
    title: str,
    prompt_snapshot: str,
    prompt_run: int,
    prompt_sha256: str,
    model: str,
    branch: str,
    base_commit: str,
    result_commit: str = "none",
    status: str = "IN PROGRESS",
    start: str | None = None,
    end: str = "(pending)",
) -> str:
    """Render a report skeleton with run metadata pre-filled.

    The agent fills in Result/Validation/Outstanding items before the run
    completes. The ``Sync state`` block is a historical finalization-time
    snapshot. The Telemetry block starts as an explicit "not recorded" statement;
    ``scripts.read_path.cli attach`` replaces it with a real summary (P005.1).
    """

    start = start or datetime.now(timezone.utc).isoformat()
    telemetry_section = (
        "Read-path telemetry not recorded for this run (see docs/agent_reporting.md)."
    )
    return REPORT_TEMPLATE.format(
        prompt_id=prompt_id,
        title=title,
        prompt_snapshot=prompt_snapshot,
        prompt_run=prompt_run,
        prompt_sha256=prompt_sha256,
        model=model,
        branch=branch,
        base_commit=base_commit,
        result_commit=result_commit,
        status=status,
        start=start,
        end=end,
        telemetry_section=telemetry_section,
    )


def completion_line(
    *,
    prompt_id: str,
    status: str,
    report_path: str,
    commit: str = "none",
    sync: str = "pending",
    transport: str | None = None,
) -> str:
    """The standard one-line completion status.

    ``transport`` is an optional trailing ``| transport=<...>`` segment using the
    same ``key=value`` pipe-delimited grammar as the rest of the line -- appended
    only when the caller has something to report, so a bare ``sync=synced``
    keeps meaning "no known current transport/auth problem."
    """

    line = f"PROMPT {prompt_id} {status} | report={report_path} | commit={commit} | sync={sync}"
    if transport:
        line += f" | transport={transport}"
    return line

