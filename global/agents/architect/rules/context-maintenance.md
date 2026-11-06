# Architect — Durable Context Maintenance Rules

Status: OWNER ACCEPTED r01 · Core events and typed Tools pending implementation.

Maintain concise recoverable project-scoped context in the ROSC-owned Handoff, linked to canonical Core Pipeline, Task History, accepted decisions and verified source evidence. Handoff is not an alternate Project state database or a shadow Pipeline. Update meaningfully during work, not only at session exit. On fresh activation, verify project binding, rule version and actual Core state before trusting old checkpoint text.

Use typed Handoff actions: `get_snapshot`, `get_section`, `update_checkpoint` (conceptual names) with revision precondition, source references, next_actor/action, outstanding approvals, blockers and verified vs claimed facts. Store detailed scratch research as optional notes and source refs, not an ever-growing startup prompt. If a resource update fails or the source changed, retain a pending reconciliation and report the failure; never silently mark current.

When a Worker Run reports terminal status: Core retains the event and immutable Run/Report refs; verify claim vs evidence, add typed attributable append-only Task History review as permitted, refresh compact Handoff pointers, inspect Pipeline only for materially changed dependencies/scope/milestones. Do not auto-accept a Task/milestone from a Worker COMPLETED claim. When Owner closes a Task, reconcile Task History, Handoff and Pipeline; 'no Pipeline change' is a valid explicit result.

On restart or offline interval, process pending scoped events idempotently by event ID and expected revision; new agent incarnation does not erase Project context. Prefer small reads via semantic Tools (R-14). `current-state.md`, `milestones.md`, `decisions-index.md`, `open-questions.md`, `sources.md`, `work-log.md` and per-Task dossier files are not all mandatory on greenfield bootstrap. Preserve any historically existing files; do not fabricate new duplicates or delete old notes during an upgrade.

The pipeline's active state belongs to Core after appropriate approved implementation/cutover. A Markdown export is read-only; no private copy of the accepted roadmap can be made authoritative by updating Handoff. External Project source edits and cross-Project reads remain separately gated.
