# ROSC development workflow

This file defines stable repository-work rules for development agents. It is not a product specification, implementation pipeline, architectural decision record, or current-state handoff. Task-specific requirements, accepted decisions, reference artifacts, stage constraints and acceptance criteria belong in the Owner-approved managed prompt. Existing source code and tests establish the current implemented behavior, not requirements for unimplemented behavior.

## Source of authority and access boundaries

1. The verified, frozen managed prompt is the execution contract for the current task. Do not expand scope, infer missing approvals, or treat a referenced but unreadable document as understood.
2. Inspect the relevant repository source, tests, migrations and local documentation to understand actual behavior and dependencies. Distinguish **implemented**, **tested**, **approved**, **proposed** and **unknown**. Do not treat existing code as authority for new product decisions.
3. An external URL, including a Google Drive link, is only a reference, **not proof of access or review**. Agents are not expected to fetch additional canonical Drive documents. If a required decision or reference is not contained in the prompt or available in an explicitly identified accessible artifact, record the missing dependency and stop the affected work for Owner clarification.
4. If the prompt conflicts with existing code, local instructions or another supplied artifact, identify the conflict and ask for an Owner decision. Do not silently resolve it by assumption or by copying potentially stale documentation.

## Before making changes

1. If the task names a managed draft `p<ID>__<slug>.md`, first use the existing `scripts.agent_sync.cli prompt-pull <ID>` and verify the exact frozen snapshot returned by the command. If retrieval or verification fails, stop. Do not substitute copied chat text or another prompt revision.
2. Verify the intended branch in the single local checkout, that `git status --short` is clean, and capture the full base commit (`git rev-parse HEAD`) **before** any `docs/` read. Preserve unexpected user changes. Do not work on the wrong branch or silently reset, rebase, discard or overwrite work.
3. For a managed run, start telemetry with `scripts.agent_sync.cli managed-start` (see [`docs/agent_reporting.md`](agent_reporting.md)) **before** reading `docs/README.md`. The initial `AGENTS.md` exposure is a bootstrap exception: describe it, do not backfill it as a temporally observed read. A refused start (`LATE_START`, `SNAPSHOT_INVALID`, `DIRTY_WORKTREE`, `INCOMPLETE_START`...) is `BLOCKED`; do not bypass it by running the old commands late.
4. Then read the local routing instructions (`docs/README.md`, this file, and [`docs/agent_reporting.md`](agent_reporting.md) where applicable), recording each read right after it happens. Follow only relevant local dependencies; do not regard a Drive URL in a routing file as an implicit permission or obligation to fetch it. Review relevant source and recent commits.
4. Identify the task's required inputs, acceptance criteria and authorized actions. If an essential input is missing or cannot be verified, report the blocker rather than inventing it.

## Single local checkout

ROSC has one active local checkout, `/home/als/Документы/ROSC`, and one active modifying agent at a time. Change branches sequentially inside it. Do not create a second Git worktree, a sibling project directory or a parallel source copy for ordinary managed runs, and do not delete an existing one as part of unrelated work. The clean-tree and exact full-base-commit checks before `managed-start` stay mandatory; an unexpected dirty tree, branch drift or an existing target branch at an unexpected SHA stops the run with the facts, never a reset, rebase or stash.

## Implementation boundaries

- Implement only the approved task. Keep changes tightly scoped and avoid unrelated cleanups, architecture expansion or dependency additions.
- Preserve security, ownership and authorization boundaries. Administrative records, UI operations and development tooling do not implicitly authorize an Agent Run or access to a Workspace.
- Separate product/backend facts from demo, mock or prototype state. Do not represent simulated outcomes as verified behavior.
- For UI/presentation tasks, use only an Owner-approved reference explicitly supplied or made accessible for that task. When a specific interactive artifact or checksum is required, verify the artifact and exercise it in a browser where possible. If unavailable, mark the UX reference **BLOCKED/UNVERIFIED**; do not implement a guessed layout.
- New UX flows, material redesigns, significant new behavior or authority changes require explicit Owner approval unless already specified in the frozen prompt.
- Do not run parallel modifying tasks against overlapping files without coordinated ownership. Keep secrets, private source artifacts, credentials and raw telemetry out of Git.

## Validation and evidence

- Test the changed behavior at the appropriate level (Core, API, frontend, integration and/or real smoke), including relevant negative and failure cases. For visible behavior, provide observable evidence; a mock-only test is not automatically proof of integrated behavior.
- Run applicable existing regression checks, inspect the changed-file list, run `git diff --check` and report any skipped checks or environment limitations. Never claim independent or live verification when only an executor report or simulation exists.
- Confirm that the implementation stays within the frozen prompt's scope and that no unintended changes were made to data, files, migrations or running processes.
- Record documentation impact: which local files or explicitly supplied task references were checked, which documents changed, and any unresolved drift or missing context. Do not create a competing specification from memory.

## Managed reporting and completion

Follow [`docs/agent_reporting.md`](agent_reporting.md) for the existing prompt/report lifecycle. Record the full base commit, reference the verified frozen prompt and its SHA-256, declare COLD/WARM context accurately, and record actual documentation reads through the existing read-path tooling. Attach the read-path summary **before** report finalization.

After implementation and validation, record the exact result commit, test evidence, known limitations and outstanding decisions. Finalize the report locally as an immutable historical artifact (finalization fills a pending `End` once and requires a terminal `Status`), then return only the standard completion receipt from `completion-line`: one plain-text line, optionally preceded by a single `OWNER ATTENTION:` line when the finalized report explicitly records `Severity: serious`. Do not reread or reassess the report to decide this; full facts live in the report. The existing `reports-push` transport may archive that finalized report to Drive; transport failure does not invalidate the local report and may be retried without rewriting it.

A local commit, push, merge, force-push or other external action is permitted only to the extent explicitly authorized by the current Owner-approved prompt or a subsequent Owner instruction. If not authorized, stop at the approved boundary. Never claim Owner acceptance or integration merely because tests passed or a review branch was pushed.

### Guarded review-branch push

When the frozen prompt names an authorized `review/*` branch and its exact source base, a successful managed run may perform one mechanical push to that same branch without a further Owner approval. All of these must hold, and any failure means `push=not-pushed` with the reason in the local report:

1. The required tests and privacy checks passed and the correct changes are committed; the working tree and index are clean (`git status --short` empty).
2. The local branch name equals the prompt's `review/*` branch, its HEAD descends from the named base, and the remote ref (if it exists) is an ancestor of HEAD, so the push is a normal fast-forward.
3. The push is one plain `git push origin <review-branch>`: never `main`, never `--force`/`--force-with-lease`, tags, deletion, branch-protection changes or history rewriting, and never a merge. A permission error, non-fast-forward, branch drift or an integrity concern blocks the push and is reported, not worked around.
4. Afterwards the remote ref is read back independently (`git ls-remote origin refs/heads/<review-branch>`). Only a SHA equal to the reported HEAD allows `push=pushed:<full SHA>` in the completion receipt.

This does not authorize integration into `main`; the Owner reviews and merges separately. It does not build a push daemon or grant any other write right.
