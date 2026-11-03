# ROSC documentation router

Files under `docs/` hold stable, general repository-work rules for development agents and give local read-path telemetry a stable bootstrap. They are not a product specification, implementation pipeline or current-state handoff. Task-specific requirements, accepted decisions, reference artifacts and acceptance criteria come from the Owner-approved managed prompt. The repository source, tests and migrations show what is actually implemented. Agents are not expected to access Google Drive; a Drive link is a reference, not proof of access or review. Missing essential context is reported, not invented.

## Read order

1. [`docs/development_workflow.md`](development_workflow.md) for repository changes and managed runs.
2. For prompt/report transport or telemetry, [`docs/agent_reporting.md`](agent_reporting.md).
3. The relevant source, tests and migrations for the area being changed.

Read triggered dependencies only. Search is not a substitute for the routing document. Keep `contract`, `decision`, `proposal`, `implemented`, and `verified` distinct. If a managed prompt conflicts with local instructions or code, identify the conflict and ask for an Owner decision.
