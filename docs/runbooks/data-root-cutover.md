# Runbook (DRY-RUN DOCUMENT): move the live ROSC data root to the repo-local `data/`

**Status: PROPOSAL, NOT EXECUTED, NOT AUTHORIZED.** Written in P020 Stage 1 (code-only). Stage 1 changed source, tests and
docs; it touched no process, database, secret, Workspace or `.agent-telemetry` byte. Stage 2 runs only after Coordinator
code review **and** explicit Owner approval of every step marked 🔴. Re-verify all facts below on the day: the Stage 0
audit was a snapshot, and PIDs in it are historical observations, never identities to signal.

Legend: 🟢 read-only check (safe to run) · 🟡 creates a new file/dir only in the *new* location or a backup · 🔴 stops a
process, writes the live data, migrates a schema, or changes configuration (needs explicit Owner approval).

Placeholders: `OLD` = the data root recorded by the running launcher (Stage 0: `~/.local/share/rosc`; read it from
`.local-dev/state.json`, do not assume), `NEW` = `<checkout>/data`, `BK` = a new, empty, 0700 backup directory on the
same filesystem as `NEW`'s parent.

## 0. What Stage 1 already provides (and its limits)

- Code default `ROSC_DATA_DIR` unset → `<checkout>/data` (backend, Alembic via `rosc.config`, launcher via its stdlib
  copy; parity is test-pinned). Explicit non-empty `ROSC_DATA_DIR` always wins; an empty value counts as unset.
- A running pair keeps its **recorded** `data_dir` for `status`/`down`/idempotent `up`; an implicit default never
  conflicts with it. A fresh `up` with a missing/unmigrated `NEW` refuses; it never creates or migrates a database.
- Non-editable installs have **no** default: `ROSC_DATA_DIR` is mandatory (`DataRootError`); the global library is
  then absent. Not papered over; Stage 1 does not package `global/`.
- `rosc.app` no longer builds the default application at import (it is created on first `app` access, as
  `uvicorn rosc.app:app` does). Importing the module cannot create `data/`.
- SourceRead excludes `data/` (when it is the configured root), `.local-dev/`, `.local-data/`, `stail/` of this checkout
  and, for any bound root that verifies as a ROSC checkout, those names; secret names and `.agent-telemetry` stay
  name-protected. `.gitignore` is not a boundary.
- Migration `0008_project_context_access` is in the tree but **not applied to any real database**.

## 1. Preflight (all 🟢; stop on any mismatch → BLOCKED, no repair attempts)

1. Branch/commit: single checkout, reviewed commit of the P020 branch merged by the Owner; `git status --short` empty.
2. Read the recorded launcher config: `cat .local-dev/state.json` (do not edit). Record `data_dir`, ports, pids.
3. `.venv/bin/python3 scripts/local_app.py status` (read-only). Record RUNNING/DEGRADED/STOPPED/UNKNOWN per service.
4. Identity of every ROSC-owned process, by **pid + /proc start time + cmdline + cwd + process group** (the launcher's
   own proof). For `opencode serve` children also the per-session environment marker. Anything unproven is UNKNOWN and is
   left alone. List the independent `opencode serve --service` separately and mark it **do not touch**.
5. Ports 8010/5173 (or recorded) and their holders (`ss -ltnp`, read-only).
6. Inventory `OLD`: `find OLD -maxdepth 3 -printf '%M %u %s %p\n'`, sizes, free space on target filesystem ≥ 2× tree size,
   `OLD` and `NEW` on the same filesystem (needed for an atomic rename) — otherwise BLOCKED, ask the Owner.
7. `sqlite3 'file:OLD/rosc.db?mode=ro' 'PRAGMA integrity_check; PRAGMA journal_mode; SELECT version_num FROM alembic_version;'`
   expect `ok`, and `0007_architect_reply_unbounded_body`. Note any `rosc.db-wal`/`-shm` (Stage 0: none).
8. Same read-only check on `OLD/opencode-auth/**/*.db` (it has live WAL/SHM, so only `mode=ro`; do not open writable).
9. Project manifest inventory: list `OLD/workspace/projects/P*/` with file counts and sha256 of every file (`sha256sum`
   to a list stored in `BK`, not in Git). Count Projects/Tasks/Architect sessions from the DB read-only.
10. `NEW` must not exist (or be empty and Owner-acknowledged). `data/` is git-ignored; confirm `git check-ignore data`.
11. Persisted-path audit (🟢): `grep -rl "OLD-absolute-path" OLD --include='*.yaml' --include='*.json'` and
    `sqlite3 ... "SELECT ... LIKE '%OLD%'"` on the text columns of `architect_*`/`project*` tables. Known: manifests
    `rosc.project.v3` hold no paths; legacy `v1/v2` hold the external *source* `root_path` (unaffected by the move);
    Architect runtime rows hold pid + start ticks + a work dir under `OLD/architect-runtime/<session>`; Project
    registration paths point at external source checkouts. Any path found under `OLD` must be listed and decided before
    copying.

## 2. Quiescence (🔴 each step needs approval; never by port/name/pattern, never the independent service)

1. Owner deactivates every active Architect through ROSC (the product action) so children stop and `.secrets/<session>`
   files are removed after termination is confirmed. A session that cannot be proven stopped keeps its secret and blocks
   the cutover (do not delete it by hand).
2. `scripts/local_app.py down` — it signals only a process group whose recorded identity is still proven. If it reports
   UNKNOWN, stop here; resolve per README "Recovering from UNKNOWN".
3. Re-run preflight 3–5: backend and Vite absent, ports free, no ROSC-owned `opencode serve`, no `rosc.db-wal` activity
   (`PRAGMA wal_checkpoint(TRUNCATE)` only if a WAL exists and only on the quiesced DB, with Owner approval).
4. Snapshot `.local-dev/state.json` into `BK` (🟡) before anything clears it.

## 3. Backup (🟡, into `BK`, originals untouched)

1. Archive the **entire** `OLD` tree preserving modes/mtimes (`tar --numeric-owner -C OLD/.. -cpf BK/rosc-data.tar rosc`),
   including `.secrets/` and `opencode-auth`, then `sha256sum`. Directories 0700, files 0600 (`umask 077`).
2. `opencode-auth` has a live WAL/SHM held by a process that must **not** be stopped. A raw file copy of a live WAL
   database is not a consistent backup. Take its copy with the SQLite online-backup API (`sqlite3 SRC '.backup DST'`
   from a read-only handle) and record that a consistent point-in-time copy was made while the owner stays alive. Decision
   needed from the Owner: whether `opencode-auth` is part of the move at all, or stays at `OLD` (the independent service's
   XDG path points there and would break if it were moved). ROSC does not read it (README: provider key bridge).
3. Verify: restore the tar into a scratch directory, compare file lists and checksums, run `PRAGMA integrity_check` on the
   restored `rosc.db`. Keep `BK` until the Owner releases it.

## 4. Staged copy (🟡 new location only)

1. `umask 077; mkdir -m 700 NEW.staging` beside `NEW` (same filesystem). Copy `OLD/rosc.db` via `sqlite3 .backup` (never
   copy `-wal`/`-shm` raw), and `workspace/`, `architect-runtime/` (skeleton only if no live sessions), `backups/` with
   `cp -a --no-preserve=ownership` / `rsync -a --checksum`, no `--delete`, no overwrite.
2. Verify: file list + sha256 equals the preflight inventory; modes 0700/0600; `integrity_check` = `ok`;
   `alembic_version` still `0007...`; Project/Task counts equal; the manifest inventory matches.
3. Atomic placement: `mv NEW.staging NEW` (same filesystem rename). `OLD` is **not** removed, renamed or modified; it is
   the rollback copy. No destructive source removal is part of this runbook.

## 5. Migration 0008 (🔴, only after steps 3–4 are verified and approved)

1. Run on the **copy only**: `ROSC_DATA_DIR=NEW .venv/bin/alembic upgrade head` (re-confirm `NEW` in the printed URL
   source: `migrations/env.py` takes it from `rosc.config`, not from `alembic.ini`).
2. Verify `alembic_version = 0008_project_context_access`, `integrity_check`, counts. If anything fails: abandon `NEW`
   (rename aside), `OLD` is untouched, nothing else happened.

## 6. Controlled launcher cutover (🔴)

1. State is empty (step 2.2 cleared it). To avoid recorded-state ambiguity do not hand-edit `.local-dev/state.json`.
2. Start from a shell with an explicit `ROSC_DATA_DIR=NEW` for the first start (`scripts/local_app.py up`) so the recorded
   `data_dir` is unambiguous; afterwards the unset default resolves to the same path (verify with `status`, which prints
   `data dir`/`database`). Restart policy for any persistent shell/service wrapper that exports an old `ROSC_DATA_DIR`:
   find and update it (🟢 grep of shell profiles and unit files) — a stale export would silently re-select `OLD`.
3. Smoke (🟢 after start): `/api/health`, UI loads, Project list equals the inventory, one read-only Architect tool call
   path only with Owner-approved test Project; no provider call.
4. Re-activation of Architects is an Owner action; new runtime sessions are created under `NEW/architect-runtime`.

## 7. Rollback (🔴)

Trigger: any failed verification, UNKNOWN process, divergent counts, or Owner request.

- Before `up` on `NEW` (steps 1–5): nothing to roll back; remove or rename `NEW.staging`/`NEW`; restart the old pair with
  `ROSC_DATA_DIR=OLD scripts/local_app.py up` (explicit, so the recorded config is `OLD`).
- After `up` on `NEW` and writes happened there: **do not** simply switch back (data divergence). Stop with
  `local_app.py down`, take a fresh backup of `NEW`, and ask the Owner which side is authoritative; reconcile by
  exporting/copying the specific records, never by overwriting either tree. If no writes happened (verify by
  `rosc.db` mtime/`PRAGMA data_version` and manifest checksums vs. step 4) restoring `OLD` as the active root is clean:
  `down`, then `ROSC_DATA_DIR=OLD ... up`, and put the saved `.local-dev/state.json` back only if no pair is running.
- The migration `0008` is additive but is applied to `NEW` only; `OLD` stays at `0007`, so an old-code rollback needs no
  schema downgrade.

## 8. Identity and path hazards to resolve before Stage 2

- Runtime session rows keep `pid` + start ticks; after a restart they are only trusted if the process still proves
  its marker. Old children cannot be migrated: deactivate first.
- The independent OpenCode service and any other consumer of `OLD/opencode-auth` keep using `OLD`.
- If the ROSC repository is registered as a ROSC Project, its binding encloses `NEW`: the dynamic private-root exclusion
  already hides it, but re-check with a SourceRead of `data/` after the cutover (expect `path_excluded`).
- `stail/`, `.local-data/`, `.local-dev/`, `.agent-telemetry/` stay in place and are not part of the data move.
- Open Owner decisions: `opencode-auth` in or out; whether to retire `OLD` and when; where the persistent `ROSC_DATA_DIR`
  environment is exported today.
