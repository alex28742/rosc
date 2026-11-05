# `global/` — canonical home for versioned global definitions

**Status: scaffold only. Nothing in this directory is an approved, loaded or operative rule.**

This is the Git-tracked root for ROSC's future *global* definitions, kept apart from private runtime data (`data/`,
git-ignored). The intended shape is:

```text
global/
  principles/                 # cross-cutting principles
  agents/architect/{rules,templates}/
  agents/workers/rules/
  skills/                     # future, on demand
  tools/                      # future definitions; a definition file is NOT a capability grant
```

What exists today: the Project Architect's read-only `rosc_global_list` / `rosc_global_read` Tools can list and read
definition files (`.md .txt .yaml .yml .json`) below `principles/`, `agents/`, `skills/` and `tools/`. They do not load
rules automatically, do not make a listed file executable and do not change any Tool right. Symlinks, secret-named files,
oversized files and everything outside those four directories are not served. This README itself is intentionally not
served either.

What does **not** exist: Role Rule snapshotting/loading/onboarding, semantic Docs APIs, a Pipeline, Task History and
Skill execution. The design reference (a Drive "Project Workspace & Agent Rules Blueprint") is a specimen, not an
installed implementation; its files have not been imported here. Add real, Owner-approved definitions only through a
reviewed change.
