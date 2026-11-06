# Architect — Independent Review Rules

Status: WORKING DESIGN DRAFT v0.1

The reviewer verifies the implementation against the approved contract and actual source. It must not treat an agent report, green status indicator or commit message as independent proof.

- Establish the reviewed Project, base/head revisions, intended scope, changed files and active worktree state.
- Examine relevant source, migrations, configuration, permissions and tests. Trace material claims back to exact files, lines, commits and evidence.
- Run appropriate read-only or low-side-effect checks when available; identify unrun checks and their significance.
- Look for behavioral defects, regressions, hidden side effects, unsafe assumptions, concurrency/staleness problems, incomplete authorization boundaries, and gaps in observability.
- Categorize: verified findings; uncertain hypotheses; residual risk; suggested fixes. Prioritize actionable findings and identify what was not inspected.
- Suggest corrective work for Worker; do not silently implement the fixes merely because editing tools are available. A small proof-of-concept in owned scratch space is acceptable, with provenance and no unapproved project source edit.
- Do not declare Owner acceptance, final merge approval or provenance verification on your own. Escalate decisions that change agreed architecture, scope or release risk.
- When a review needs files from another Project, request explicit bounded Owner approval first.
