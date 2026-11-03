"""Fail-closed local managed-run start: report skeleton + read-path run, once.

Operates **after** a verified ``prompt-pull``. It re-verifies the frozen snapshot
against the local manifest, requires the repository HEAD to equal the declared
base commit exactly (a changed HEAD is a ``LATE_START``, never retro-enrolled),
then creates the managed report skeleton and initialises the matching read-path
run -- before any ``docs/`` read. It performs no Drive/OAuth access and cannot
certify what the model actually read; it only guarantees ordering of the two
telemetry artifacts relative to the declared base.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from scripts.read_path import trace

from . import artifacts
from . import state as state_mod


class ManagedStartError(Exception):
    """A refused or incomplete start; ``code`` is a stable diagnostic keyword."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StartResult:
    run_id: str
    report: str
    raw: str
    mode: str
    base_commit: str
    branch: str
    resumed: bool = False


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _matching_unfinished_reports(
    *, reports_dir: Path, prompt_id: str, snapshot: str, model: str, prompt_run: int,
    sha256: str, branch: str, base_commit: str, state_path: Path, repo_root: Path,
) -> list[Path]:
    """Existing unfinalized reports of this exact start (same prompt revision,
    model and base)."""

    found: list[Path] = []
    slug = artifacts.slugify(model)
    for path in sorted(reports_dir.glob(f"p{prompt_id}__*.md")) if reports_dir.is_dir() else []:
        parsed = artifacts.parse_report_filename(path.name)
        if not parsed or parsed["prompt_id"] != prompt_id or parsed["model"] != slug:
            continue
        if state_mod.finalized_at(state_path, _rel(path, repo_root)):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if (
            artifacts._report_metadata_value(text, "Prompt ID") == prompt_id
            and artifacts._report_metadata_value(text, "Prompt snapshot") == snapshot
            and artifacts._report_metadata_value(text, "Prompt revision/run") == f"r{prompt_run:02d}"
            and artifacts._report_metadata_value(text, "Prompt SHA-256") == sha256
            and artifacts._report_metadata_value(text, "Agent/model") == model
            and artifacts._report_metadata_value(text, "Branch") == branch
            and artifacts._report_metadata_value(text, "Base commit") == base_commit
        ):
            found.append(path)
    return found


def _ensure_resumed_report_tracked(
    *, report: Path, state_path: Path, repo_root: Path, prompt_id: str,
    snapshot: str, prompt_run: int, prompt_sha256: str, model: str,
    branch: str, base_commit: str,
) -> None:
    """Restore a missing draft manifest entry, or validate an existing one.

    Report bytes are authoritative after exclusive creation.  Registration is
    deliberately completed before read-path initialisation so a failed recovery
    never leaves a misleading ``run_start`` behind.
    """

    local = _rel(report, repo_root)
    text = report.read_text(encoding="utf-8")
    current_sha = artifacts.sha256_text(text)
    entry = state_mod.artifact_entry(state_path, local)
    if entry is None:
        heading = re.match(rf"\A# Prompt {re.escape(prompt_id)} — (.+)\n", text)
        start = artifacts._report_metadata_value(text, "Start")
        expected = artifacts.render_report_skeleton(
            prompt_id=prompt_id,
            title=heading.group(1) if heading else "",
            prompt_snapshot=snapshot,
            prompt_run=prompt_run,
            prompt_sha256=prompt_sha256,
            model=model,
            branch=branch,
            base_commit=base_commit,
            start=start,
        ) if heading and start else None
        if text != expected:
            raise ManagedStartError(
                "RESUME_INVALID",
                f"untracked report {local} is not the intact managed-start draft; "
                "nothing was overwritten",
            )
        try:
            state_mod.record_artifact(
                state_path,
                local_path=local,
                artifact_type="report",
                sha256=current_sha,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        except (OSError, ValueError, TypeError) as exc:
            raise ManagedStartError(
                "RESUME_INVALID",
                f"report {local} is intact but could not be re-registered "
                f"({exc.__class__.__name__}); no read-path run was created",
            ) from exc
        return
    if (
        entry.get("type") != "report"
        or entry.get("sha256") != current_sha
        or entry.get("finalized_at")
        or entry.get("sync_status") not in ("pending", "failed")
        or entry.get("drive_id") is not None
    ):
        raise ManagedStartError(
            "RESUME_INVALID",
            f"sync-state entry for {local} contradicts the intact unfinalized report; "
            "nothing was overwritten",
        )


def managed_start(
    *, prompt_id: str, snapshot: Path, model: str, base_commit: str, mode: str,
    owners: list[str], title: str | None, repo_root: Path, state_path: Path,
    reports_dir: Path, runs_dir: Path, resume_report: Path | None = None,
) -> StartResult:
    if mode not in trace.CONTEXT_MODES:
        raise ManagedStartError("BAD_MODE", f"context mode must be one of {trace.CONTEXT_MODES}")
    try:
        integrity = artifacts.verify_frozen_prompt(snapshot, state_path=state_path, repo_root=repo_root)
    except artifacts.FrozenPromptIntegrityError as exc:
        raise ManagedStartError("SNAPSHOT_INVALID", str(exc)) from exc
    parsed = artifacts.parse_frozen_prompt_filename(snapshot.name)
    if parsed is None or parsed.prompt_id != prompt_id:
        raise ManagedStartError(
            "SNAPSHOT_INVALID", "prompt ID does not match the verified snapshot filename"
        )
    if not artifacts.git_commit_exists(base_commit, repo_root):
        raise ManagedStartError("BASE_INVALID", "base commit must be an existing full 40-character SHA")
    head = artifacts.resolve_head_sha(repo_root)
    if head != base_commit:
        raise ManagedStartError(
            "LATE_START",
            f"HEAD {head} differs from declared base {base_commit}; a run cannot be "
            "enrolled after the repository moved. Start from the intended base or "
            "get an Owner-approved base.",
        )
    if not artifacts.is_worktree_clean(repo_root):
        raise ManagedStartError("DIRTY_WORKTREE", "worktree has uncommitted changes; refusing to start")
    branch = artifacts.current_branch_name(repo_root) or "(detached)"
    snapshot_rel = _rel(snapshot, repo_root)
    try:
        owner_rels = [trace.doc_rel(repo_root, owner) for owner in owners]
    except trace.TraceError as exc:
        raise ManagedStartError("BAD_OWNER", str(exc)) from exc

    existing = _matching_unfinished_reports(
        reports_dir=reports_dir, prompt_id=prompt_id, snapshot=snapshot_rel,
        model=model, prompt_run=parsed.run, sha256=integrity.sha256, branch=branch,
        base_commit=base_commit, state_path=state_path, repo_root=repo_root,
    )
    resuming = resume_report is not None
    if resuming:
        target = resume_report.resolve()
        candidates = [p.resolve() for p in existing if not trace.run_path(p.stem, runs_dir).exists()]
        if target not in candidates:
            raise ManagedStartError(
                "RESUME_INVALID",
                "--resume-report must name an unfinalized report of this exact start "
                "that has no read-path run yet",
            )
        report = target
        _ensure_resumed_report_tracked(
            report=report, state_path=state_path, repo_root=repo_root,
            prompt_id=prompt_id, snapshot=snapshot_rel, prompt_run=parsed.run,
            prompt_sha256=integrity.sha256, model=model, branch=branch,
            base_commit=base_commit,
        )
    else:
        for path in existing:
            raw = trace.run_path(path.stem, runs_dir)
            if raw.exists():
                raise ManagedStartError(
                    "ALREADY_STARTED",
                    f"run {path.stem} already started (report={_rel(path, repo_root)}); not overwritten",
                )
            raise ManagedStartError(
                "INCOMPLETE_START",
                f"report {_rel(path, repo_root)} exists without a read-path run; nothing was "
                f"overwritten. Recover with: managed-start ... --resume-report {_rel(path, repo_root)}",
            )
        content = artifacts.render_report_skeleton(
            prompt_id=prompt_id, title=title or prompt_id,
            prompt_snapshot=snapshot_rel, prompt_run=parsed.run,
            prompt_sha256=integrity.sha256, model=model, branch=branch, base_commit=base_commit,
        )
        report = artifacts.create_report(prompt_id, model_slug=model, content=content, directory=reports_dir)
        try:
            state_mod.record_artifact(
                state_path, local_path=_rel(report, repo_root), artifact_type="report",
                sha256=artifacts.sha256_text(content),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        except (OSError, ValueError, TypeError) as exc:
            raise ManagedStartError(
                "INCOMPLETE_START",
                f"report {_rel(report, repo_root)} created but sync state could not be "
                f"updated ({exc.__class__.__name__}); it was left intact.",
            ) from exc

    rel_report = _rel(report, repo_root)
    try:
        raw = trace.init_run(
            repo_root=repo_root, runs_dir=runs_dir, run_id=report.stem, prompt_id=prompt_id,
            report=rel_report, context_mode=mode, owners=owner_rels, head=head, branch=branch,
            origin="managed-start",
        )
    except (trace.TraceError, OSError) as exc:
        raise ManagedStartError(
            "INCOMPLETE_START",
            f"report {rel_report} created but the read-path run could not start ({exc}); "
            f"it was left intact. Recover with: managed-start ... --resume-report {rel_report}",
        ) from exc
    return StartResult(
        run_id=report.stem, report=rel_report, raw=_rel(raw, repo_root), mode=mode,
        base_commit=base_commit, branch=branch, resumed=resuming,
    )
