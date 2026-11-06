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
  memory). Each Architect turn carries the ruleset identity (`version`, `digest`), the **role rule** text, the R-14
  statement and an index (path + short hash) of the other active rules; it does **not** carry the full text of the other
  three active rules, nor the seven reference rules or the full R-14 file. Those are read on demand with
  `rosc_global_read`.
* `rosc_global_read` of any ruleset file (manifest, documentation standard, R-14, the 11 rules) is served only if the
  current bytes still equal what was verified at that session's activation; the result then names the pinned ruleset
  and file hash. If the file changed on disk it fails closed (`rules_source_changed`), and with no pin (restart, lost
  state, deactivation) it fails with `rules_snapshot_unavailable`. Other definition files stay plain, unverified
  documentation.
* Editing a file on disk does not change a running session; a later explicit activation adopts the new bytes. A backend
  restart drops the pin, and delivery fails closed (`rules_snapshot_unavailable`) until the Owner reactivates. There is
  no durable rule snapshot yet.
* A definition file is never a Tool, a Skill or a permission. The read-only `rosc_global_list` / `rosc_global_read`
  Tools serve definition files (`.md .txt .yaml .yml .json`) below `principles/`, `agents/`, `skills/` and `tools/`;
  symlinks, secret-named and oversized files are not served. This README is intentionally not served.

What does **not** exist yet: `project.init_analyze`, `project.initialize`, typed Handoff/Pipeline Tools, Pipeline,
Task History, Worker rules, templates and Skills. The carried-forward reference rules mention some of these as future
behavior; that prose describes intent, not an installed capability.
