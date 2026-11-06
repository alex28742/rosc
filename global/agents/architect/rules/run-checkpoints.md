# Architect — Run Checkpoints & Task History Continuity

Status: OWNER-APPROVED PRODUCT DIRECTION / GLOBAL ROLE RULE PROPOSAL v0.7, NOT IMPLEMENTED

## Principle

Tasks outlive Runs and Architect/Worker sessions. Use **ROSC Core Task History**, not required `working-context/tasks/Txxxx.md` files, to preserve the full multi-Run timeline, compact current-state summary, outstanding findings and next action. Worker machine receipts, immutable reports and telemetry remain linked by Run ID rather than duplicated into the Task History body. This replaces the former v0.6 Task-dossier file proposal.

## After each Run terminal event

1. Receive a durable Project/Task/Run-scoped event (`WorkerRunReported` or equivalent) with stable IDs. If offline, Core must retain the event for later delivery.
2. Confirm reported vs observed results, receipt, exact Run ID, report/telemetry references, branch/base/result commit, actual Git state and approvals. Do not infer correctness from a short chat message or Worker self-report.
3. Read recent Core Task History/current Task summary; add a **typed, attributable, append-only** Architect review/progress entry, linking the Run and relevant evidence. Update derived current summary: scope remaining, unresolved blockers, review findings, next actor/action and current branch/revision. Core owns event identity and idempotency.
4. Update brief Project Handoff pointers to active Task IDs/current next steps, not copies of complete Run logs.
5. Read current Project Pipeline revision. Check whether accepted milestone, sequencing, dependencies or scope materially changed. If not, leave Pipeline unchanged. If material change is needed, prepare a structured proposal with rationale and request Owner approval; do not use Pipeline as a per-Run log.
6. Report concise review outcome in Project Conversation and make detailed evidence discoverable through the Task UI. An incoming `COMPLETED` is a Worker claim; independent verification and Owner Task acceptance remain separate states.
7. Record actual completion/partial/failure of the reconciliation. Do not swallow failed writes or generate duplicate entries on retries.

## On pause / archive / restart

Project lifecycle changes do not destroy Task History or Pipeline. On resume, load verified project identity and role rules → Project Handoff → current Core Pipeline/Task states → outstanding Task History events and selected full Run reports → actual Git/evidence. Revalidate pending Owner approvals before continuing; never revive them from a memory note.

## Task closed by Owner

Owner closure is a separate event, not a Run terminal event. Reconcile final Task History/acceptance, Project Handoff and any affected Pipeline items; do not automatically claim an entire milestone is accepted.

See `global/agents/architect/rules/pipeline-management.md` and `design/project-pipeline-core.md`. Actual tools, APIs, persistence and dispatcher are later implementation work.
