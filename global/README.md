# `global/` — canonical home for versioned global definitions

**Status: Owner-approved Architect definitions are installed here (ruleset 0.4.0). They are operative only after the
loader verifies them and an Owner activation pins them to a session.**

This is the Git-tracked root for ROSC's *global* definitions, kept apart from private runtime data (`data/`,
git-ignored). Git is the only source of these files; never write local private Workspace content here.

```text
global/
  principles/context-economy.md          # R-14, foundation principle
  agents/architect/
    ruleset.yaml                         # trusted allowlist + version + digests (engineering manifest)
    documentation-standard.yaml          # validated schema reference (v0.4.0)
    rules/{role,onboarding,context-maintenance,pipeline-management}.md   # active bootstrap
    rules/{autonomy,research,review,communication,run-checkpoints,worker-task-authoring,task-run-git}.md  # reference, on demand
    templates/ agents/workers/ tools/ skills/   # README placeholders only: planned / not operative
```

How it applies:

* The backend loader (`src/rosc/architect_rules.py`) reads **only** the paths listed in `ruleset.yaml`, without following
  symlinks, and refuses the whole ruleset on any missing/changed/oversized/malformed file or digest mismatch.
* At an explicit Owner activation of the Project Architect the verified ruleset is pinned for that session (in process
  memory). Each Architect turn carries the four active rules and their identity (`version`, `digest`); the seven
  reference rules and R-14 are not pasted into every prompt and are read on demand with `rosc_global_read`.
* Editing a file on disk does not change a running session; a later explicit activation adopts the new bytes. A backend
  restart drops the pin, and delivery fails closed (`rules_snapshot_unavailable`) until the Owner reactivates. There is
  no durable rule snapshot yet.
* A definition file is never a Tool, a Skill or a permission. The read-only `rosc_global_list` / `rosc_global_read`
  Tools serve definition files (`.md .txt .yaml .yml .json`) below `principles/`, `agents/`, `skills/` and `tools/`;
  symlinks, secret-named and oversized files are not served. This README is intentionally not served.

What does **not** exist yet: `project.init_analyze`, `project.initialize`, typed Handoff/Pipeline Tools, Pipeline,
Task History, Worker rules, templates and Skills. The carried-forward reference rules mention some of these as future
behavior; that prose describes intent, not an installed capability.
