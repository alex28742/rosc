# ROSC development workflow

This is repository workflow guidance. Canonical ROSC product and architecture documents, including the [implementation pipeline](https://drive.google.com/file/d/1GVaW8SGFuB8F0CaxLEmPXd-HQkuU21gH/view), are in Drive.

## Before work

1. Read `docs/README.md`, route to the relevant canonical owner documents, and check the current pipeline stage.
2. Check branch, `git status --short`, recent commit and current source. Do not overwrite unexpected user changes.
3. A task named as `p<ID>__<slug>.md` is a managed run. Run `prompt-pull <ID>` before ordinary routing, verify the printed frozen snapshot, and work only from that version. If either fails, stop.

## Managed run

Follow `docs/agent_reporting.md`. Capture full base commit before edits, create the report skeleton with the verified snapshot, declare COLD or genuinely WARM context, record documentation reads with `scripts.read_path.cli`, and commit scoped work. Record the result HEAD, attach telemetry, finalize the local report, then print the short completion line. The full report stays in its file. An optional `reports-push` is an independent retryable transport after local finalization; only a real Drive acknowledgement means `synced`.

## UX/UI gate

Stage 01 is Owner-accepted with documented deferrals (checkpoint: P003.1, commit `4ba9d0e`); ADR-0001 stays `proposed` and Stage 01F is not started. The canonical decisions are in the Drive addendum [Stage 01 acceptance and UI delivery rules](https://drive.google.com/file/d/1NcHJRHeBK6daFNyGu22fxQqlD-BIy9qC/view); this section is the operational summary, not a second source of truth.

For UI work:

1. Open the actual interactive `ROSC_Dashboard_Project_Task_Work_Area_v06.html` in a browser and exercise its flows. Check that the file's SHA-256 is `21950ed08df67caafbcb747b05cf1eedc7e02163c718f10d85bb304ee0754c80`. The Owner keeps a local copy; its private path is not recorded in this repository. If the file cannot be opened or verified, mark the UX gate BLOCKED/UNVERIFIED and do not invent a layout.
2. Implement the approved layout and flows. A state missing from the prototype (new error or empty state) reuses existing patterns. A meaningful redesign or an entirely new UX state needs explicit Owner agreement.
3. Compare the integrated result with the reference in a browser and record deviations in the report.
4. Keep demo state (prototype data) distinct from backend facts; never present one as the other.
5. Stage 01F is desktop-first, with v0.6 as the desktop baseline. At 390 px only functional safety is checked: navigation, close controls, important forms, visible errors, no clipping or horizontal overflow. Polished mobile parity is not required. A separately approved mobile presentation comes later and its UX is not yet approved; do not build a mobile package in 01F. The current `alternative` presentation test is an architecture proof, not a mobile template.

## Prompt split and executors

- Where practical, write separate managed prompts for backend/Core/API/data and for frontend/UI/presentation, split by responsibility. Each states shared typed contracts, dependencies and handoffs, and integration acceptance. A combined cross-layer prompt is allowed when splitting would be counterproductive.
- The Owner chooses the executor for each task before dispatch. Any eligible agent (Claude, DeepSeek, Codex or others) may take backend or frontend work; the repository assigns no provider to a discipline.
- Do not run concurrent prompts that modify the same files.
- No agent redesigns the approved UX or bypasses Owner approval.

## Implementation rules

- Each new Engine mechanism needs a minimal observable UI check in its slice. Terminal/API checks are useful intermediate evidence.
- Follow the accepted Dashboard, Project and Task UX baseline; do not redesign it implicitly during implementation.
- A visible UI change needs the Owner-approved flow and concrete states, including failure behavior.
- Track document impact: paths reviewed, changed, and any drift found. Link canonical Drive decisions rather than silently copying them into competing documents.
- Keep secrets, runtime artifacts, private project sources and raw telemetry out of Git.

## Completion

Run scoped checks, `git diff --check`, review the exact changed-file list and report result, remaining risk, commit, worktree state and documentation impact. Local report finalization is the completion boundary; Drive archival is retryable and does not rewrite the report.
