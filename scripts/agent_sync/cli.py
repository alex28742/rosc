"""CLI for local agent prompt/report artifact helpers and the optional Drive
transport.

See ``docs/agent_reporting.md`` for the full contract. Local-only subcommands:
``hash``, ``freeze-write``, ``verify-prompt``, ``report-skeleton``,
``finalize-report``, ``completion-line``, ``status``. Drive transport
subcommands: ``prompt-pull``, ``reports-push``, ``auth`` (interactive-only).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import artifacts, errors, sync
from . import config as config_mod
from . import drive
from . import state as state_mod


def _display_path(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(artifacts.REPO_ROOT.resolve()))
    except ValueError:
        return str(path)


def _record_or_warn(
    artifact_path: Path,
    *,
    artifact_type: str,
    sha256: str,
    created_at: str,
) -> bool:
    """Persist one artifact entry, but never let bookkeeping failure destroy an
    already-created authoritative artifact: warn and continue instead."""

    local_path = _display_path(artifact_path)
    try:
        state_mod.record_artifact(
            artifacts.STATE_PATH,
            local_path=local_path,
            artifact_type=artifact_type,
            sha256=sha256,
            created_at=created_at,
        )
        return True
    except (OSError, ValueError, TypeError) as exc:
        print(
            f"WARNING: created {local_path} but could not update sync state "
            f"({exc.__class__.__name__}: {exc}). The artifact was left untouched and "
            "is authoritative; re-establish sync state before managed integrity "
            "verification will accept it.",
            file=sys.stderr,
        )
        return False


def cmd_hash(args: argparse.Namespace) -> int:
    print(artifacts.sha256_file(Path(args.file)))
    return 0


def cmd_freeze_write(args: argparse.Namespace) -> int:
    """Atomically create a frozen-prompt snapshot from a local source file.

    Local-only legacy/bootstrap tool: the source is an ordinary local file (not a
    Drive pull). Uses exclusive creation so a concurrent freeze can never
    overwrite an executed snapshot, marks the new file read-only as a best-effort
    write guard, and records its SHA-256 in the sync-state manifest.
    """

    source = Path(args.source)
    if not source.exists() or not source.is_file():
        print(f"Source file not found: {source}", file=sys.stderr)
        return 1
    content = source.read_text(encoding="utf-8")
    path = artifacts.create_frozen_prompt(args.prompt, args.slug, content)
    _record_or_warn(
        path,
        artifact_type="prompt",
        sha256=artifacts.sha256_file(path),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    print(_display_path(path))
    return 0


def cmd_verify_prompt(args: argparse.Namespace) -> int:
    """Managed verification of a frozen prompt: fail closed on any mismatch."""

    path = Path(args.path)
    try:
        integrity = artifacts.verify_frozen_prompt(path)
    except artifacts.FrozenPromptIntegrityError as exc:
        print(f"VERIFY FAILED | {exc}", file=sys.stderr)
        return 1
    print(
        f"PROMPT VERIFIED | snapshot={_display_path(path)} "
        f"| sha256={integrity.sha256} | recorded={integrity.recorded_sha256}"
    )
    return 0


def cmd_report_skeleton(args: argparse.Namespace) -> int:
    # Resolve to an absolute path before comparing it against REPO_ROOT: a
    # relative path (e.g. ".agent-telemetry/prompts/...") must be resolved against
    # the current working directory the way a shell would interpret it.
    prompt_snapshot = Path(args.prompt_snapshot).resolve() if args.prompt_snapshot else None
    ctx = artifacts.git_context()
    if prompt_snapshot is not None:
        if args.ad_hoc:
            print("--ad-hoc cannot be combined with --prompt-snapshot.", file=sys.stderr)
            return 1
        if not args.base_commit:
            print(
                "Managed report creation requires --base-commit captured at run start.",
                file=sys.stderr,
            )
            return 1
        # An explicitly supplied snapshot must be a real, readable, verifiably
        # frozen prompt. It is never silently downgraded to "(none)" and the
        # expected hash is never silently enrolled.
        if not prompt_snapshot.exists() or not prompt_snapshot.is_file():
            print(
                f"Prompt snapshot not found or not a file: {prompt_snapshot}",
                file=sys.stderr,
            )
            return 1
        try:
            integrity = artifacts.verify_frozen_prompt(prompt_snapshot)
        except artifacts.FrozenPromptIntegrityError as exc:
            print(f"Refusing to trust frozen prompt: {exc}", file=sys.stderr)
            return 1
        prompt_sha256 = integrity.sha256
        prompt_snapshot_display = _display_path(prompt_snapshot)
        parsed = artifacts.parse_frozen_prompt_filename(prompt_snapshot.name)
        if (
            parsed is None
            or parsed.prompt_id != args.prompt
            or parsed.run != args.prompt_run
        ):
            print(
                "Managed report prompt ID/revision does not match the verified "
                "snapshot filename.",
                file=sys.stderr,
            )
            return 1
        try:
            artifacts.validate_base_commit(
                args.base_commit,
                current_head=ctx["head"],
            )
        except artifacts.ReportProvenanceError as exc:
            print(f"Invalid managed-run baseline: {exc}", file=sys.stderr)
            return 1
        base_commit = args.base_commit
    else:
        if not args.ad_hoc:
            print(
                "A report without --prompt-snapshot must explicitly use --ad-hoc.",
                file=sys.stderr,
            )
            return 1
        prompt_sha256 = "(unknown)"
        prompt_snapshot_display = "(none)"
        base_commit = args.base_commit or ctx["head"]

    if args.result_commit != "none":
        if not artifacts.git_commit_exists(args.result_commit):
            print("Result commit must be an existing full commit SHA.", file=sys.stderr)
            return 1
        if args.result_commit != ctx["head"]:
            print("Result commit must match the current repository HEAD.", file=sys.stderr)
            return 1
    content = artifacts.render_report_skeleton(
        prompt_id=args.prompt,
        title=args.title or args.prompt,
        prompt_snapshot=prompt_snapshot_display,
        prompt_run=args.prompt_run,
        prompt_sha256=prompt_sha256,
        model=args.model,
        branch=ctx["branch"] or "(detached)",
        base_commit=base_commit,
        result_commit=args.result_commit,
        status=args.status,
    )
    report_path = artifacts.create_report(args.prompt, model_slug=args.model, content=content)

    # The report is authoritative the moment it is created; a later manifest
    # write failure must not hide that (see _record_or_warn).
    _record_or_warn(
        report_path,
        artifact_type="report",
        sha256=artifacts.sha256_text(content),
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    print(_display_path(report_path))
    return 0


def cmd_finalize_report(args: argparse.Namespace) -> int:
    """The single controlled draft -> final transition for a managed report.

    Normalizes only the recognized trailing standard sections, computes the
    report's SHA-256 from the **final** bytes, records that hash plus a
    ``finalized_at`` marker in ``state.json``, and thereafter treats the report
    as an immutable historical artifact. A second call on an unchanged finalized
    report is a no-op; a call after post-finalization mutation is rejected
    without touching the file.
    """

    path = Path(args.path)
    if not path.exists() or not path.is_file():
        print(f"Report not found or not a file: {path}", file=sys.stderr)
        return 1
    local_path = _display_path(path)
    entry = state_mod.artifact_entry(artifacts.STATE_PATH, local_path)
    if entry is None or entry.get("type") != "report":
        print(
            f"Not a managed report tracked in sync state: {local_path} "
            "(create it with report-skeleton first).",
            file=sys.stderr,
        )
        return 1

    if state_mod.finalized_at(artifacts.STATE_PATH, local_path):
        if artifacts.sha256_file(path) == entry.get("sha256"):
            print(f"{local_path} (already finalized; unchanged)")
            return 0
        print(
            f"Refusing to modify finalized report {local_path}: content changed "
            "after finalization.",
            file=sys.stderr,
        )
        return 1

    text = path.read_text(encoding="utf-8")
    try:
        artifacts.validate_managed_report_provenance(text)
    except artifacts.ReportProvenanceError as exc:
        print(
            f"Refusing to finalize {local_path}: managed provenance invalid ({exc}).",
            file=sys.stderr,
        )
        return 1
    try:
        normalized = artifacts.normalize_report_tail(text)
    except artifacts.ReportTailError as exc:
        print(f"Refusing to finalize {local_path}: {exc}", file=sys.stderr)
        return 1
    if normalized != text:
        path.write_text(normalized, encoding="utf-8")

    final_sha256 = artifacts.sha256_file(path)
    try:
        state_mod.mark_report_finalized(
            artifacts.STATE_PATH,
            local_path=local_path,
            sha256=final_sha256,
            finalized_at=datetime.now(timezone.utc).isoformat(),
        )
    except (OSError, ValueError, KeyError) as exc:
        print(
            f"WARNING: normalized {local_path} but could not record finalization "
            f"({exc.__class__.__name__}: {exc}); it remains a draft and can be "
            "finalized again.",
            file=sys.stderr,
        )
        return 1
    print(local_path)
    return 0


def cmd_completion_line(args: argparse.Namespace) -> int:
    # Current state is authoritative in the manifest; a report body is a
    # historical snapshot and is never read for either value below.
    sync_status = args.sync
    transport_suffix = args.transport
    if args.sync is None:
        sync_status = state_mod.sync_status_for(artifacts.STATE_PATH, args.report) or "pending"
        if args.transport is None:
            # A bare `sync=synced` must not be printed when the last known attempt
            # to reach Drive needed reauth or otherwise failed; see
            # docs/agent_reporting.md's transport-health rules. An explicit
            # `--sync` override skips this; an explicit `--transport` override is
            # used verbatim.
            health = state_mod.transport_health(artifacts.STATE_PATH)
            if health is not None and health.get("status") != "ok":
                transport_suffix = health.get("status")
    print(
        artifacts.completion_line(
            prompt_id=args.prompt,
            status=args.status,
            report_path=args.report,
            commit=args.commit,
            sync=sync_status,
            transport=transport_suffix,
        )
    )
    return 0


def _record_transport_health_or_warn(*, status: str, detail: str | None = None) -> None:
    """Best-effort: a manifest write failure here must never turn a real transport
    outcome into an uncaught error -- warn and continue."""

    try:
        state_mod.record_transport_health(artifacts.STATE_PATH, status=status, detail=detail)
    except (OSError, ValueError, TypeError) as exc:
        print(
            f"WARNING: could not record Drive transport health "
            f"({exc.__class__.__name__}: {exc}).",
            file=sys.stderr,
        )


_SCOPE_INSUFFICIENT_REASON = "ACCESS_TOKEN_SCOPE_INSUFFICIENT"
_SCOPE_INSUFFICIENT_MESSAGE_MARKER = "insufficient authentication scopes"


def _http_status(exc: Exception) -> int | None:
    """The HTTP status of a Drive/API error, duck-typed.

    Real ``googleapiclient.errors.HttpError`` exposes ``.resp.status``; using
    duck typing here keeps classification testable and dependency-free when the
    optional Google client libraries are absent. Anything without an integer
    ``resp.status`` is not an HTTP error.
    """

    status = getattr(getattr(exc, "resp", None), "status", None)
    return status if isinstance(status, int) else None


def _http_403_is_auth_required(exc) -> bool:
    """Conservative allowlist for a post-open Drive ``HttpError`` 403.

    Drive/API 403s cover many non-auth conditions -- file permission, quota,
    per-user/per-app rate limiting, storage limit, and others -- where telling
    the Owner to reauthenticate would be wrong. Only a 403 that *unambiguously*
    names the OAuth token's own scope as the problem may become
    ``auth_required``: the standard ``ACCESS_TOKEN_SCOPE_INSUFFICIENT``
    error-details reason, or the literal "insufficient authentication scopes"
    message. Anything else -- a recognized non-auth reason, an unrecognized
    reason, or a missing reason -- falls through to ``unavailable``.
    """

    message = (getattr(exc, "reason", "") or "").lower()
    if _SCOPE_INSUFFICIENT_MESSAGE_MARKER in message:
        return True
    for detail in getattr(exc, "error_details", None) or []:
        if isinstance(detail, dict) and detail.get("reason") == _SCOPE_INSUFFICIENT_REASON:
            return True
    return False


def _classify_transport_failure(exc: Exception) -> str:
    """Classify a *post-open* Drive/API failure into ``auth_required`` or
    ``unavailable``.

    ``HttpError`` 401 (invalid/expired credential) always classifies as
    ``auth_required``; 403 only does so when :func:`_http_403_is_auth_required`
    recognizes a genuine scope/credential reason. Anything else (network error,
    5xx, timeout, unrecognized failure, or an unrecognized/missing-reason 403) is
    ``unavailable``.
    """

    if isinstance(exc, drive.AuthRequiredError):
        return "auth_required"
    status = _http_status(exc)
    if status == 401:
        return "auth_required"
    if status == 403 and _http_403_is_auth_required(exc):
        return "auth_required"
    return "unavailable"


def _record_post_open_transport_failure(exc: Exception) -> None:
    """Record a post-open Drive/API failure as mechanism-wide transport health:
    the client opened ``ok``, but the real API call the caller needed then
    failed, so the last-known-health fact must be downgraded. Never called for a
    pre-flight ``AgentSyncError`` (no transport was attempted against those)."""

    _record_transport_health_or_warn(
        status=_classify_transport_failure(exc), detail=f"{exc.__class__.__name__}: {exc}"
    )


def _transport_or_fail(config):
    """Build the Drive transport, converting any auth/network failure into a
    clear message + ``None`` rather than an uncaught traceback.

    This is the one chokepoint ``prompt-pull``/``reports-push`` share, so it is
    also the single place that records current Drive transport health -- success
    or failure, independent of whether the caller's own artifact ends up
    ``synced``/``pending``/``failed``.
    """

    try:
        transport = drive.open_transport(config)
    except drive.AuthRequiredError as exc:
        _record_transport_health_or_warn(status="auth_required", detail=str(exc))
        print(f"Drive authentication required: {exc}", file=sys.stderr)
        print(
            "Run `python3 -m scripts.agent_sync.cli auth` yourself (interactive "
            "browser sign-in) to grant the Drive scope; see docs/agent_reporting.md.",
            file=sys.stderr,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - any Drive/network failure is reported, not raised
        _record_transport_health_or_warn(
            status="unavailable", detail=f"{exc.__class__.__name__}: {exc}"
        )
        print(
            f"Drive transport unavailable ({exc.__class__.__name__}: {exc}).",
            file=sys.stderr,
        )
        return None
    _record_transport_health_or_warn(status="ok")
    return transport


def cmd_prompt_pull(args: argparse.Namespace) -> int:
    """Pull exactly one Drive draft by Prompt ID and freeze it locally."""

    prompt_id = args.prompt_id
    config = config_mod.load_config()
    transport = _transport_or_fail(config)
    if transport is None:
        return 1
    try:
        result = sync.pull_prompt(prompt_id=prompt_id, transport=transport, config=config)
    except errors.AgentSyncError as exc:
        print(f"PROMPT {prompt_id} PULL FAILED | {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - transport failure, nothing local was written
        _record_post_open_transport_failure(exc)
        print(
            f"PROMPT {prompt_id} PULL FAILED | transport error "
            f"({exc.__class__.__name__}: {exc}).",
            file=sys.stderr,
        )
        return 1
    verb = "REUSED" if result.reused else "PULLED"
    print(
        f"PROMPT {prompt_id} {verb} | snapshot={result.local_path} "
        f"| revision=r{result.revision:02d} | sha256={result.sha256} "
        f"| drive_id={result.drive_id}"
    )
    return 0


def cmd_reports_push(args: argparse.Namespace) -> int:
    """Push one finalized local report to the Drive archive (idempotent)."""

    config = config_mod.load_config()
    try:
        target = sync.validate_report_for_push(report_path=args.report)
    except errors.AgentSyncError as exc:
        print(f"REPORT PUSH REFUSED | {exc}", file=sys.stderr)
        return 1
    transport = _transport_or_fail(config)
    if transport is None:
        sync.mark_push_failed(local_path=target.local_path, error="Drive transport unavailable")
        print(
            f"REPORT {target.prompt_id or '?'} SYNC FAILED | report={target.local_path} "
            "| sync=failed",
            file=sys.stderr,
        )
        return 1
    try:
        result = sync.push_report(target=target, transport=transport, config=config)
    except errors.SyncConfigError as exc:
        print(f"REPORT PUSH REFUSED | {exc}", file=sys.stderr)
        return 1
    except errors.ReportPushError as exc:
        # The Drive mechanism answered successfully; this artifact could not be
        # reconciled. Keep transport health at ``ok`` while marking only the
        # artifact retryable/failed.
        sync.mark_push_failed(
            local_path=target.local_path,
            error=f"{exc.__class__.__name__}: {exc}",
        )
        print(
            f"REPORT {target.prompt_id or '?'} SYNC FAILED | report={target.local_path} "
            f"| sync=failed | {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - any Drive/API/network failure is retryable
        sync.mark_push_failed(
            local_path=target.local_path, error=f"{exc.__class__.__name__}: {exc}"
        )
        _record_post_open_transport_failure(exc)
        print(
            f"REPORT {target.prompt_id or '?'} SYNC FAILED | report={target.local_path} "
            f"| sync=failed | {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    suffix = ""
    if result.noop:
        suffix = " (reconciled)" if result.reconciled else " (no-op)"
    print(
        f"REPORT {target.prompt_id or '?'} SYNCED | report={result.local_path} "
        f"| drive_id={result.drive_id} | sha256={result.sha256}{suffix}"
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Concise, script-friendly current sync state from the manifest."""

    rows = sync.status_rows(prompt_id=args.prompt, local_path=args.path)
    if not rows:
        print("(no matching artifacts)")
    else:
        for row in rows:
            parts = [
                f"type={row.artifact_type}",
                f"prompt={row.prompt_id or '-'}",
                f"revision={row.revision or '-'}",
                f"sync={row.sync_status or '-'}",
                f"path={row.local_path}",
                f"sha256={row.sha256 or '-'}",
                f"drive_id={row.drive_id or '-'}",
                f"finalized_at={row.finalized_at or '-'}",
            ]
            if row.last_error:
                parts.append(f"last_error={row.last_error}")
            print(" ".join(parts))
    # Transport health is a mechanism-wide fact, independent of any one
    # artifact/filter, so it prints once regardless of --prompt/--path.
    health = state_mod.transport_health(artifacts.STATE_PATH)
    if health is not None:
        transport_parts = [
            f"status={health.get('status')}",
            f"checked_at={health.get('checked_at')}",
        ]
        if health.get("detail"):
            transport_parts.append(f"detail={health['detail']}")
        print("transport: " + " ".join(transport_parts))
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    """Interactive-only: renew the agent-sync Drive token with the required
    scope. Never run by an agent/automated task."""

    config = config_mod.load_config()
    print("Starting Google OAuth authorization for agent prompt/report sync...")
    print("A browser window will open -- sign in and grant Drive access.")
    try:
        creds = drive.run_interactive_authorization(
            config.credentials_path, config.token_path, scopes=drive.SCOPES
        )
    except drive.AuthRequiredError as exc:
        print(f"Authorization could not start: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - any OAuth/browser/cancellation failure
        print(
            f"Authorization failed or was cancelled ({exc.__class__.__name__}).",
            file=sys.stderr,
        )
        print("The existing token, if any, was left untouched.", file=sys.stderr)
        return 1
    try:
        drive.write_token(creds, config.token_path)
    except OSError as exc:
        print(
            "Authorization succeeded, but the token could not be stored safely "
            f"({exc.__class__.__name__}).",
            file=sys.stderr,
        )
        print("The existing token, if any, was left intact.", file=sys.stderr)
        return 1
    print("")
    print("Agent-sync Google Drive authorization updated successfully.")
    print(f"{config.token_path} refreshed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.agent_sync.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_hash = sub.add_parser("hash", help="Print the SHA-256 of a file.")
    p_hash.add_argument("file")
    p_hash.set_defaults(func=cmd_hash)

    p_freeze_write = sub.add_parser(
        "freeze-write",
        help="Atomically write a frozen-prompt snapshot from a local source file (no Drive fetch).",
    )
    p_freeze_write.add_argument("--prompt", required=True)
    p_freeze_write.add_argument("--slug", required=True)
    p_freeze_write.add_argument("--source", required=True)
    p_freeze_write.set_defaults(func=cmd_freeze_write)

    p_verify = sub.add_parser(
        "verify-prompt",
        help="Verify a frozen prompt against its recorded freeze-time SHA-256 (fail closed).",
    )
    p_verify.add_argument("path")
    p_verify.set_defaults(func=cmd_verify_prompt)

    p_report = sub.add_parser(
        "report-skeleton", help="Create a report skeleton with metadata pre-filled."
    )
    p_report.add_argument("--prompt", required=True)
    p_report.add_argument("--title", default=None)
    p_report.add_argument("--model", required=True)
    p_report.add_argument("--prompt-snapshot", default=None)
    p_report.add_argument("--prompt-run", type=int, default=1)
    p_report.add_argument(
        "--base-commit",
        default=None,
        help="Full commit SHA captured at managed-run start (required with --prompt-snapshot).",
    )
    p_report.add_argument(
        "--ad-hoc",
        action="store_true",
        help="Explicitly create a non-managed report without a frozen prompt snapshot.",
    )
    p_report.add_argument("--result-commit", default="none")
    p_report.add_argument("--status", default="IN PROGRESS")
    p_report.set_defaults(func=cmd_report_skeleton)

    p_finalize = sub.add_parser(
        "finalize-report",
        help="Finalize a managed report: normalize the standard tail and record its final SHA-256.",
    )
    p_finalize.add_argument("path")
    p_finalize.set_defaults(func=cmd_finalize_report)

    p_line = sub.add_parser("completion-line", help="Print the standard completion line.")
    p_line.add_argument("--prompt", required=True)
    p_line.add_argument("--status", required=True)
    p_line.add_argument("--report", required=True)
    p_line.add_argument("--commit", default="none")
    p_line.add_argument(
        "--sync",
        default=None,
        help="Override the sync status; by default it is read from the sync-state manifest.",
    )
    p_line.add_argument(
        "--transport",
        default=None,
        help=(
            "Override the trailing transport-health segment verbatim. By default "
            "(when both --sync and --transport are omitted) it is read from the "
            "manifest's last known Drive transport check and shown only when that "
            "status is not 'ok'."
        ),
    )
    p_line.set_defaults(func=cmd_completion_line)

    p_pull = sub.add_parser(
        "prompt-pull",
        help="Pull exactly one Drive prompt draft by Prompt ID and freeze it locally.",
    )
    p_pull.add_argument("prompt_id", help="Prompt ID, e.g. 003.1.")
    p_pull.set_defaults(func=cmd_prompt_pull)

    p_push = sub.add_parser(
        "reports-push",
        help="Push one finalized local report to the Drive archive (idempotent).",
    )
    p_push.add_argument("report", help="Path to a finalized managed report.")
    p_push.set_defaults(func=cmd_reports_push)

    p_status = sub.add_parser(
        "status", help="Show current sync state for one artifact or Prompt ID."
    )
    p_status.add_argument("--prompt", default=None, help="Filter by Prompt ID, e.g. 003.1.")
    p_status.add_argument("--path", default=None, help="Show exactly this tracked local path.")
    p_status.set_defaults(func=cmd_status)

    p_auth = sub.add_parser(
        "auth",
        help="Interactive-only: renew the agent-sync Drive OAuth token (human at a browser).",
    )
    p_auth.set_defaults(func=cmd_auth)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

