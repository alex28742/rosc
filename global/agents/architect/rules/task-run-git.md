# Architect — Task / Run Git Branch Strategy

Status: WORKING DESIGN DRAFT v0.5 · GLOBAL ARCHITECT ROLE RULE · NOT IMPLEMENTED

## Stable identity vs readable name

Project, Task, Run and Agent each have separate **canonical ROSC Core identities**. Git branch names are human-readable projections, never unique authority or sufficient proof of the executing Agent. Branch labels cannot reliably capture every Run of a long-lived Task.

**Default: one stable integration branch per Task.** All sequential Worker Runs contribute commits to this branch, and the Task remains open across correction/continuation Runs until Owner accepts/closes it. Different agents may work on the same Task over time, without renaming its branch. One modifying writer at a time unless a deliberately approved concurrency policy safely changes that rule.

### Suggested naming convention (future in-product Task/Run)

Primary Task branch:

`task/<project-slug>-p<project-number>-t<task-number>[-<short-topic>]`

Example: `task/rosc-p0001-t0042-runtime-adapter`.

- `<project-slug>`: stable, sanitized lowercase mnemonic, not the sole identity. Include `Pxxxx` / `Txxxx` because Task numbers can repeat across Projects and display names can change.
- No Run/Agent in **primary Task branch**: both may change while the same branch remains the Task's continuity point.
- Optional topic helps humans but must not contain secrets or unstable timestamps.

For an explicitly justified Run-specific or parallel sub-branch:

`run/<project-slug>-p<project-number>-t<task-number>-r<task-local-sequence>-<agent-slug>[-<yyyymmdd>]`

Example: `run/rosc-p0001-t0042-r0003-codex-20261104`.

The `run/` branch is optional, not the standard every-Run path. A multi-branch Task may instead use named workstreams (e.g. `task/rosc-p0001-t0042-ui`) if the Owner approves a coherent integration plan. A run sequence must come from trusted Task-local Core state and **must not be guessed from Git naming**; Run UUID remains authoritative. Agent slug may represent the worker identity/label for readability, while ROSC records exact AgentDefinition and assignment IDs. The date is optional for human convenience; it is not an identity or ordering guarantee.

### Commit, report and event attribution

Each Run records Project ID, Task ID, Run UUID, Task-local sequence, AgentDefinition/assignment, prompt revision/hash, approved branch and base, actual start/result commit, changed files/diff, test evidence, report ID and provenance in ROSC. Use project-appropriate commit messages such as:

`[P0001/T0042 R0003 codex] Fix runtime adapter error mapping`

Where available, record structured commit trailers (`ROSC-Project`, `ROSC-Task`, `ROSC-Run`, `ROSC-Agent`) in addition to Core provenance. Never equate a Git author string or branch slug with independently verified authorship.

## Multi-branch exception

Architect may propose multiple branches for technically justified independent workstreams, large isolated experiments or parallel development with controlled ownership. The proposal specifies branch names, bases, dependency/merge order, shared-file collision risks, test/integration strategy, final Task branch, who reconciles results, and which Owner decisions are required. No automatic parallel writers, extra worktrees, force pushes, merges or branch deletion based solely on an Architect recommendation. Preserve existing dirty work and historical branch evidence.

## Run continuation and recovery

Before each Run, re-check branch existence, exact base/result SHA, current HEAD, clean/dirty state, untracked files, ownership and any unresolved/unknown prior Run. A new Run must not assume the branch is pristine; correction Runs should normally start from the **last verified Task branch HEAD** rather than the original Task base. If a Run outcome or write is uncertain, reconcile source and durable records before retry. On branch drift or conflicting work, stop and ask Owner rather than silently reset, rebase, stash or force-push.

Only the explicitly authorized operation contract permits a particular commit/push/merge. Owner retains final Task acceptance and protected-branch integration rights. For ROSC's **existing managed repository-development workflow**, `docs/development_workflow.md` and the specific approved prompt remain authoritative (`review/*` naming, single checkout and guarded push); do not retroactively rename those branches or treat this proposed convention as implemented.
