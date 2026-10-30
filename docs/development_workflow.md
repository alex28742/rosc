# ROSC development workflow

This is repository workflow guidance. Canonical ROSC product and architecture documents, including the [implementation pipeline](https://drive.google.com/file/d/1GVaW8SGFuB8F0CaxLEmPXd-HQkuU21gH/view), are in Drive.

## Before work

1. Read `docs/README.md`, route to the relevant canonical owner documents, and check the current pipeline stage.
2. Check branch, `git status --short`, recent commit and current source. Do not overwrite unexpected user changes.
3. A task named as `p<ID>__<slug>.md` is a managed run. Run `prompt-pull <ID>` before ordinary routing, verify the printed frozen snapshot, and work only from that version. If either fails, stop.

## Managed run

Follow `docs/agent_reporting.md`. Capture full base commit before edits, create the report skeleton with the verified snapshot, declare COLD or genuinely WARM context, record documentation reads with `scripts.read_path.cli`, and commit scoped work. Record the result HEAD, attach telemetry, finalize the local report, then print the short completion line. The full report stays in its file. An optional `reports-push` is an independent retryable transport after local finalization; only a real Drive acknowledgement means `synced`.

## Implementation rules

- Each new Engine mechanism needs a minimal observable UI check in its slice. Terminal/API checks are useful intermediate evidence.
- Follow the accepted Dashboard, Project and Task UX baseline; do not redesign it implicitly during implementation.
- A visible UI change needs the Owner-approved flow and concrete states, including failure behavior.
- Track document impact: paths reviewed, changed, and any drift found. Link canonical Drive decisions rather than silently copying them into competing documents.
- Keep secrets, runtime artifacts, private project sources and raw telemetry out of Git.

## Completion

Run scoped checks, `git diff --check`, review the exact changed-file list and report result, remaining risk, commit, worktree state and documentation impact. Local report finalization is the completion boundary; Drive archival is retryable and does not rewrite the report.
