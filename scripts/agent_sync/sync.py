"""Drive-sync orchestration for agent prompts and reports.

One-way ownership, by construction (``docs/agent_reporting.md``):

- **prompts**: Google Drive -> local only. :func:`pull_prompt` freezes a Drive
  Markdown draft into an immutable local revision; nothing ever pushes prompt
  content back to Drive.
- **reports**: local -> Google Drive only. :func:`push_report` archives an
  already-finalized local report; there is no ``reports-pull``.

The module is transport-agnostic: it takes a
:class:`~scripts.agent_sync.drive.DriveTransport` so unit tests use a fake and
never touch the network.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import artifacts, drive
from . import config as config_mod
from . import state as state_mod
from .errors import (
    DriveFormatError,
    PromptLookupError,
    ReportPushError,
    ReportValidationError,
    SyncConfigError,
)

logger = logging.getLogger(__name__)

_PROMPT_ID_RE = re.compile(r"^\d+(?:\.\d+)*$")
_DRIVE_DRAFT_RE = re.compile(
    r"^p(?P<prompt_id>\d+(?:\.\d+)*)__(?P<slug>[^_].*?)\.md$"
)
_LOCAL_REVISION_SUFFIX_RE = re.compile(r"__r\d+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _display(path: Path, repo_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(path)


def _safe_drive_metadata(value: str | None, *, limit: int = 160) -> str:
    """Single-line bounded Drive metadata for diagnostics."""

    value = value or "?"
    clean = "".join(character if character.isprintable() else "?" for character in value)
    return clean if len(clean) <= limit else clean[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Prompt pull (Drive -> local freeze)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PullResult:
    prompt_id: str
    local_path: str
    revision: int
    sha256: str
    drive_id: str
    reused: bool


def find_prompt_drafts(
    transport: drive.DriveTransport, folder_id: str, prompt_id: str
) -> list[drive.DriveFile]:
    """Every non-folder Drive file in ``folder_id`` whose name parses as a
    frozen-prompt-style name for ``prompt_id``.

    Drive permits duplicate names, so this returns *all* matches and the caller
    fails closed on anything but exactly one.
    """

    if not _PROMPT_ID_RE.fullmatch(prompt_id):
        raise PromptLookupError(
            f"Invalid prompt ID '{prompt_id}'; expected digits with optional dotted suffixes."
        )
    matches: list[drive.DriveFile] = []
    for file in transport.list_children(folder_id):
        if file.is_folder:
            continue
        match = _DRIVE_DRAFT_RE.fullmatch(file.name)
        if (
            match is not None
            and match.group("prompt_id") == prompt_id
            and not _LOCAL_REVISION_SUFFIX_RE.search(match.group("slug"))
        ):
            matches.append(file)
    return matches


def _normalize_draft_text(content: bytes) -> str:
    """The only normalization the Markdown draft format requires: strict UTF-8
    decoding and removal of a leading BOM. Everything else is preserved
    byte-for-byte so the frozen snapshot is a faithful copy of the draft."""

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DriveFormatError(
            "Drive prompt draft is not valid UTF-8; the canonical draft format is "
            "a UTF-8 Markdown (.md) file."
        ) from exc
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def _find_reusable_frozen(
    prompt_id: str,
    digest: str,
    prompts_dir: Path,
    state_path: Path,
    repo_root: Path,
) -> Path | None:
    """An already-frozen, *verifiably tracked* snapshot for ``prompt_id`` whose
    bytes hash to ``digest`` -- reused instead of creating a redundant revision.

    An untracked same-content file is deliberately not reused: it is a
    legacy/import artifact, and silently enrolling it would defeat managed
    fail-closed verification. A fresh revision is created instead.
    """

    for path in sorted(prompts_dir.glob(f"p{prompt_id}__*.md")):
        parsed = artifacts.parse_frozen_prompt_filename(path.name)
        if parsed is None or parsed.prompt_id != prompt_id:
            continue
        try:
            current = artifacts.sha256_file(path)
        except OSError:
            continue
        if current != digest:
            continue
        local_path = _display(path, repo_root)
        if state_mod.recorded_sha256(state_path, local_path) == digest:
            return path
    return None


def pull_prompt(
    *,
    prompt_id: str,
    transport: drive.DriveTransport,
    config: config_mod.SyncConfig,
    prompts_dir: Path | None = None,
    state_path: Path | None = None,
    repo_root: Path | None = None,
) -> PullResult:
    """Pull exactly one Drive draft for ``prompt_id`` and freeze it locally.

    Fails closed on zero matches and on more than one match. An exact-content
    match against an already-frozen, tracked snapshot is a deterministic no-op
    (the existing revision is reused); changed content creates the next revision.
    An existing revision is never overwritten.
    """

    prompts_dir = prompts_dir if prompts_dir is not None else artifacts.PROMPTS_DIR
    state_path = state_path if state_path is not None else artifacts.STATE_PATH
    repo_root = repo_root if repo_root is not None else artifacts.REPO_ROOT

    if not config.has_prompts_folder:
        raise SyncConfigError(
            "Agent prompt drafts folder is not configured. Set prompts_folder_id in "
            f"{config_mod.DEFAULT_CONFIG_PATH} (or AGENT_SYNC_PROMPTS_FOLDER_ID). See "
            "docs/agent_reporting.md for setup."
        )

    drafts = find_prompt_drafts(transport, config.prompts_folder_id, prompt_id)
    if not drafts:
        raise PromptLookupError(
            f"No prompt draft matching '{prompt_id}' in Drive folder "
            f"{config.prompts_folder_id}. Expected a file named "
            f"p{prompt_id}__<slug>.md."
        )
    if len(drafts) > 1:
        listing = "; ".join(
            f"{_safe_drive_metadata(f.name)} "
            f"(id={_safe_drive_metadata(f.id)}, "
            f"modified={_safe_drive_metadata(f.modified_time)})"
            for f in drafts
        )
        raise PromptLookupError(
            f"Ambiguous prompt draft lookup for '{prompt_id}': {len(drafts)} drafts "
            f"match ({listing}). Refusing to guess; rename or remove duplicates."
        )

    draft = drafts[0]
    current = transport.get_file(draft.id)
    if current is None:
        raise PromptLookupError(
            f"Drive draft '{_safe_drive_metadata(draft.name)}' disappeared after "
            "lookup; retry the pull."
        )
    if current.name != draft.name or config.prompts_folder_id not in current.parents:
        raise PromptLookupError(
            "Drive draft identity changed after lookup; refusing to download a "
            "raced or moved file. Retry the pull."
        )
    draft = current
    if draft.mime_type not in drive.MARKDOWN_COMPATIBLE_MIMES:
        raise DriveFormatError(
            f"Drive draft '{draft.name}' has unsupported MIME type "
            f"'{draft.mime_type or '(missing)'}'; expected a raw Markdown-compatible "
            "file (text/markdown, text/plain, or application/octet-stream)."
        )
    text = _normalize_draft_text(transport.download(draft.id))
    parsed = _DRIVE_DRAFT_RE.fullmatch(draft.name)
    slug = parsed.group("slug") if parsed is not None else prompt_id
    digest = artifacts.sha256_text(text)

    reusable = _find_reusable_frozen(
        prompt_id, digest, prompts_dir, state_path, repo_root
    )
    if reusable is not None:
        existing = artifacts.parse_frozen_prompt_filename(reusable.name)
        return PullResult(
            prompt_id=prompt_id,
            local_path=_display(reusable, repo_root),
            revision=existing.run if existing is not None else 1,
            sha256=digest,
            drive_id=draft.id,
            reused=True,
        )

    path = artifacts.create_frozen_prompt(prompt_id, slug, text, directory=prompts_dir)
    local_path = _display(path, repo_root)
    created = artifacts.parse_frozen_prompt_filename(path.name)
    file_digest = artifacts.sha256_file(path)
    try:
        state_mod.record_artifact(
            state_path,
            local_path=local_path,
            artifact_type="prompt",
            sha256=file_digest,
            created_at=_now(),
            drive_id=draft.id,
            sync_status="synced",
        )
    except (OSError, ValueError, TypeError) as exc:
        # The frozen snapshot is already authoritative and untouched; a manifest
        # write failure must not lose it. Managed verification stays fail-closed
        # until state is re-established.
        logger.warning(
            "Created %s but could not record its sync state (%s); the snapshot is "
            "authoritative and untouched. Re-establish sync state before managed "
            "verification will accept it.",
            local_path,
            exc,
        )
    return PullResult(
        prompt_id=prompt_id,
        local_path=local_path,
        revision=created.run if created is not None else 1,
        sha256=file_digest,
        drive_id=draft.id,
        reused=False,
    )


# ---------------------------------------------------------------------------
# Report push (local finalized -> Drive archive)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushTarget:
    local_path: str
    path: Path
    prompt_id: str | None
    revision: str | None
    sha256: str


@dataclass(frozen=True)
class PushResult:
    prompt_id: str | None
    local_path: str
    drive_id: str
    sha256: str
    noop: bool
    reconciled: bool = False
    created: bool = False


def validate_report_for_push(
    *,
    report_path,
    state_path: Path | None = None,
    repo_root: Path | None = None,
) -> PushTarget:
    """Pre-flight for ``reports-push``: the file must exist, be tracked as a
    report, be finalized, and its current bytes must still hash to the recorded
    final SHA-256. Raises :class:`ReportValidationError` otherwise -- before any
    transport state is touched."""

    state_path = state_path if state_path is not None else artifacts.STATE_PATH
    repo_root = repo_root if repo_root is not None else artifacts.REPO_ROOT

    path = Path(report_path)
    if not path.exists() or not path.is_file():
        raise ReportValidationError(f"Report not found or not a file: {path}")
    local_path = _display(path, repo_root)
    entry = state_mod.artifact_entry(state_path, local_path)
    if entry is None or entry.get("type") != "report":
        raise ReportValidationError(
            f"Not a managed report tracked in sync state: {local_path} "
            "(create it with report-skeleton first)."
        )
    if not state_mod.finalized_at(state_path, local_path):
        raise ReportValidationError(
            f"Report {local_path} is not finalized; run finalize-report first "
            "(reports-push is transport, not a finalizer)."
        )
    current = artifacts.sha256_file(path)
    recorded = entry.get("sha256")
    if current != recorded:
        raise ReportValidationError(
            f"Report {local_path} changed after finalization (recorded {recorded}, "
            f"current {current}); refusing to push a mutated report."
        )
    parsed = artifacts.parse_report_filename(path.name)
    prompt_id = parsed["prompt_id"] if parsed else None
    revision = f"r{parsed['run']:02d}" if parsed else None
    return PushTarget(
        local_path=local_path,
        path=path,
        prompt_id=prompt_id,
        revision=revision,
        sha256=current,
    )


def _report_app_properties(
    prompt_id: str | None, sha256: str, revision: str | None
) -> dict[str, str]:
    props = {"artifact_type": "report", "sha256": sha256}
    if prompt_id:
        props["prompt_id"] = prompt_id
    if revision:
        props["revision"] = revision
    return props


def _find_existing_report(
    transport: drive.DriveTransport,
    folder_id: str,
    *,
    prompt_id: str | None,
    sha256: str,
) -> list[drive.DriveFile]:
    """Reconcile-by-metadata before any create: a Drive file already carrying
    this report's ``artifact_type`` + ``sha256`` appProperties is the same
    archive (e.g. an earlier upload whose completion was uncertain), so it must
    be adopted rather than duplicated. Filename alone is never the identity."""

    matches: list[drive.DriveFile] = []
    for file in transport.list_children(folder_id):
        if file.is_folder:
            continue
        props = file.app_properties or {}
        if props.get("artifact_type") != "report":
            continue
        if props.get("sha256") != sha256:
            continue
        if prompt_id and props.get("prompt_id") not in (None, prompt_id):
            continue
        matches.append(file)
    return matches


def push_report(
    *,
    target: PushTarget,
    transport: drive.DriveTransport,
    config: config_mod.SyncConfig,
    state_path: Path | None = None,
) -> PushResult:
    """Archive a validated finalized report to Drive, idempotently.

    - same local hash + already-``synced`` Drive ID -> clean no-op (no network);
    - known Drive ID in ``pending``/``failed`` -> reconcile that identity, never
      create a second file;
    - no known ID -> adopt an existing metadata match if one exists, else create
      exactly one archive file;
    - a missing or mismatched remote archive fails closed for manual resolution
      (the local report stays authoritative and untouched).
    """

    state_path = state_path if state_path is not None else artifacts.STATE_PATH
    if not config.has_reports_folder:
        raise SyncConfigError(
            "Agent reports folder is not configured. Set reports_folder_id in "
            f"{config_mod.DEFAULT_CONFIG_PATH} (or AGENT_SYNC_REPORTS_FOLDER_ID). See "
            "docs/agent_reporting.md for setup."
        )

    entry = state_mod.artifact_entry(state_path, target.local_path) or {}
    sha = target.sha256
    drive_id = entry.get("drive_id")

    if drive_id:
        if entry.get("sync_status") == "synced" and entry.get("sha256") == sha:
            return PushResult(
                prompt_id=target.prompt_id,
                local_path=target.local_path,
                drive_id=drive_id,
                sha256=sha,
                noop=True,
            )
        remote = transport.get_file(drive_id)
        if remote is None:
            raise ReportPushError(
                f"Sync state records Drive file {drive_id} for {target.local_path}, "
                "but it no longer exists in Drive. Refusing to recreate silently; "
                "resolve manually."
            )
        if (remote.app_properties or {}).get("sha256") == sha:
            state_mod.set_sync_status(
                state_path, local_path=target.local_path, sync_status="synced"
            )
            return PushResult(
                prompt_id=target.prompt_id,
                local_path=target.local_path,
                drive_id=drive_id,
                sha256=sha,
                noop=True,
                reconciled=True,
            )
        raise ReportPushError(
            f"Drive file {drive_id} for {target.local_path} exists but does not "
            "match the finalized SHA-256; refusing to overwrite or duplicate. "
            "Resolve manually."
        )

    existing = _find_existing_report(
        transport, config.reports_folder_id, prompt_id=target.prompt_id, sha256=sha
    )
    if len(existing) > 1:
        listing = "; ".join(f"{f.name} (id={f.id})" for f in existing)
        raise ReportPushError(
            f"Ambiguous archive for {target.local_path}: {len(existing)} Drive files "
            f"match ({listing}). Resolve manually."
        )
    if len(existing) == 1:
        adopted = existing[0]
        state_mod.set_drive_id(
            state_path, local_path=target.local_path, drive_id=adopted.id
        )
        state_mod.set_sync_status(
            state_path, local_path=target.local_path, sync_status="synced"
        )
        return PushResult(
            prompt_id=target.prompt_id,
            local_path=target.local_path,
            drive_id=adopted.id,
            sha256=sha,
            noop=True,
            reconciled=True,
        )

    created = transport.create_file(
        name=target.path.name,
        content=target.path.read_bytes(),
        folder_id=config.reports_folder_id,
        mime_type=drive.MARKDOWN_MIME,
        app_properties=_report_app_properties(target.prompt_id, sha, target.revision),
    )
    state_mod.set_drive_id(state_path, local_path=target.local_path, drive_id=created.id)
    state_mod.set_sync_status(
        state_path, local_path=target.local_path, sync_status="synced"
    )
    return PushResult(
        prompt_id=target.prompt_id,
        local_path=target.local_path,
        drive_id=created.id,
        sha256=sha,
        noop=False,
        created=True,
    )


def mark_push_failed(*, local_path: str, error: str, state_path: Path | None = None) -> None:
    """Record a retryable transport failure without touching the artifact.

    Best-effort: if the manifest itself cannot be written, the local artifact is
    still authoritative and untouched (the local-success/remote-failure
    invariant holds regardless)."""

    state_path = state_path if state_path is not None else artifacts.STATE_PATH
    try:
        state_mod.set_sync_status(
            state_path, local_path=local_path, sync_status="failed", error=error
        )
    except (OSError, ValueError, KeyError):
        pass


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusRow:
    artifact_type: str
    prompt_id: str | None
    revision: str | None
    local_path: str
    sha256: str | None
    drive_id: str | None
    sync_status: str | None
    finalized_at: str | None
    last_error: str | None


def status_rows(
    *,
    state_path: Path | None = None,
    prompt_id: str | None = None,
    local_path: str | None = None,
) -> list[StatusRow]:
    """Current sync state from the manifest, optionally filtered by Prompt ID or
    exact local path. Purely local -- reads ``state.json`` only."""

    state_path = state_path if state_path is not None else artifacts.STATE_PATH
    entries = state_mod.load_state(state_path).get("entries", {})
    rows: list[StatusRow] = []
    for path, entry in sorted(entries.items()):
        if not isinstance(entry, dict):
            continue
        if local_path is not None and path != local_path:
            continue
        name = Path(path).name
        artifact_type = entry.get("type")
        parsed_prompt = artifacts.parse_frozen_prompt_filename(name)
        parsed_report = artifacts.parse_report_filename(name)
        if artifact_type == "report" and parsed_report:
            row_prompt_id: str | None = parsed_report["prompt_id"]
            revision: str | None = f"r{parsed_report['run']:02d}"
        elif parsed_prompt:
            row_prompt_id = parsed_prompt.prompt_id
            revision = f"r{parsed_prompt.run:02d}"
        else:
            row_prompt_id = None
            revision = None
        if prompt_id is not None and row_prompt_id != prompt_id:
            continue
        rows.append(
            StatusRow(
                artifact_type=str(artifact_type or "?"),
                prompt_id=row_prompt_id,
                revision=revision,
                local_path=path,
                sha256=entry.get("sha256"),
                drive_id=entry.get("drive_id"),
                sync_status=entry.get("sync_status"),
                finalized_at=entry.get("finalized_at"),
                last_error=entry.get("last_error"),
            )
        )
    return rows

