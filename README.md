# ROSC

Relay Orchestration & Supervision Center. The Engine foundation is provisional; the managed development workflow is being established first.

The working architecture and [implementation pipeline](https://drive.google.com/file/d/1GVaW8SGFuB8F0CaxLEmPXd-HQkuU21gH/view) live in the ROSC Drive folder. Repository `docs/` files provide local agent bootstrap and a versioned implementation copy. Start with [docs/README.md](docs/README.md).

## Current behavior

- Register a new or existing local Project without starting an agent.
- Create a Draft Task with a title, description and acceptance criteria.
- Register a provider-neutral Agent and record scoped `architect`/`worker` assignments for a Project or Task; neither action starts a runtime, session or Run, and neither grants an Agent Core authority.
- Read and add to a durable Owner-only Project Conversation in the Project Work Area. Messages are plain text stored in SQLite and keep their order across restart. A message is delivered to the Project Architect only if the Owner explicitly activated a verified Architect session before sending it; otherwise it is an Owner-only save.
- Explicitly activate a Project Architect (OpenCode adapter, see below) from the Project Conversation header. Opening a Project, assigning an Agent or saving a message never starts a process. Historically (P015–P018) the Architect had no tools; since P019 an activated Architect session can call only the vetted, read-mostly `rosc_*` ROSC Tools described below and has no other ROSC authority. Its text reply is stored in the same conversation as an Architect participant.
- Persist both in SQLite and provision a separate ROSC Workspace manifest.
- Inspect and create them in a small React UI; closing and reopening the UI does not change agent state.

The [accepted interactive UX reference](https://drive.google.com/file/d/17OZ90HLDmNwRuG8UpSA6FBdbaLl3eQZ7/view) is a mock browser simulation. The [implementation pipeline](https://drive.google.com/file/d/1GVaW8SGFuB8F0CaxLEmPXd-HQkuU21gH/view) is maintained in Drive.

Stage 01 (Foundation) is Owner-accepted with documented deferrals; ADR-0001 remains `proposed` and Stage 01F is not started. UI work follows the UX gate in [docs/development_workflow.md](docs/development_workflow.md), starting from the approved reference above and the Drive [Stage 01 acceptance and UI delivery rules](https://drive.google.com/file/d/1NcHJRHeBK6daFNyGu22fxQqlD-BIy9qC/view).

## Managed agent workflow

The coordinator and Owner prepare an editable Markdown draft in the ROSC Drive `Agent Prompts / Drafts` folder. An agent receives its filename, pulls and verifies an immutable local revision, works, commits, finalizes a local report, and archives that report in `Agent Reports / Archive`. See [docs/agent_reporting.md](docs/agent_reporting.md). Raw telemetry and credentials stay local and ignored by Git. This workflow manages development agents; it is separate from future in-product ROSC Task Runs.

Run `make agent-sync-check` and `make read-path-check` for offline verification. Live Drive transport needs `pip install -r requirements-agent-sync.txt`, a gitignored `docs/agent_sync.local.yml` based on the example, and a human interactive `make agent-sync-auth` on the actual development machine.

## Local development

Requirements: Python 3.11+, Node.js 24+, npm. Use a disposable data directory while testing.

**Data root.** ROSC's private runtime data lives in one directory: `ROSC_DATA_DIR` if set (non-empty), otherwise `<source checkout>/data/` (git-ignored; derived from the checkout's location, no user path embedded). Layout: `rosc.db`, `workspace/`, `architect-runtime/`, optional `backups/`. **The repo-local default is a code default only: `data/` is not operational until the separately approved Stage 2 cutover**, and an existing installation that stores its data elsewhere must keep exporting `ROSC_DATA_DIR` (or keep running its launcher-recorded pair) until then; moving data is a manual, backed-up procedure, see `docs/runbooks/data-root-cutover.md` (unexecuted proposal). A non-editable (installed) package has no checkout, so there `ROSC_DATA_DIR` is mandatory. Always use an explicit disposable `ROSC_DATA_DIR` for tests and experiments (the test suites do). Importing `rosc.app` never creates a data directory; the default `app` object is built on first access (`uvicorn rosc.app:app`).

**Global definitions.** `global/` (Git-tracked) is the canonical home of future global principles, Architect/Worker rules, skills and tool definitions (`global/README.md` states what exists: only a scaffold). The Architect's read-only `rosc_global_list` / `rosc_global_read` Tools list and read definition files below `principles/`, `agents/`, `skills/`, `tools/`; this grants no tool or right and loads no rule automatically. When the ROSC checkout itself is a registered Project, SourceRead never serves the configured data root, `.local-dev/`, `.local-data/`, `stail/` or `.agent-telemetry/` of that checkout (`.gitignore` is not a boundary).

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
export ROSC_DATA_DIR="$PWD/.local-data"
alembic upgrade head
uvicorn rosc.app:app --reload --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open the Vite URL on localhost. API requests are proxied to `127.0.0.1:8000`; set `ROSC_API_TARGET` (localhost hosts only) to use another backend port, e.g. `ROSC_API_TARGET=http://127.0.0.1:8010 npm run dev`. `frontend/package.json` declares `engines.node >= 24`; Vite 7 refuses to start its dev server on Node 18. Keep `.local-data/` and `.venv/` out of Git. To run backend checks: `python -m unittest discover -s tests -v`.

### Local development launcher (`local-app`)

A small Linux-only convenience CLI (`scripts/local_app.py`, Python standard library) starts the ROSC **developer web pair**: uvicorn on `127.0.0.1:8010` (with `--reload` limited to `src/`) **and** the Vite dev server on `127.0.0.1:5173` (`--strictPort`) proxying `/api` to the backend. It starts no Worker, agent or Run and is not a persistent service. macOS/Windows are unsupported (it relies on `/proc` and process groups). The manual commands above remain the fallback.

Prerequisites (never installed or migrated automatically): Python 3.11+ with `.venv` containing `pip install -e '.[dev]'` (this also installs the `local-app` command), Node.js 24+, npm, `cd frontend && npm install`, and an **existing** database at the Alembic head. After `. .venv/bin/activate`, `local-app` works from any directory; without activating, use `.venv/bin/local-app`, `make local-*` or `python scripts/local_app.py ...`.

```bash
local-app --help              # grouped command overview (no DB or running servers needed)
local-app up                  # backend 8010 + frontend 5173; returns when both and the proxied /api/health are ready
local-app up -p 8050          # same, backend on 8050 (aliases: --port, --backend-port, env ROSC_BACKEND_PORT)
local-app status              # RUNNING | STOPPED | DEGRADED | UNKNOWN for the RECORDED pair (read-only)
local-app logs backend -n 100 -f
local-app down                # stop only processes this launcher started and can still prove are its own
local-app check backend|launcher|sync|read-path   # run the existing test suites, exit code propagated
local-app help up             # same as: local-app up --help
```

Make shortcuts remain: `make local-up`, `local-down`, `local-status`, `local-logs`, `local-app-check`; pass options with `ARGS=`, e.g. `make local-up ARGS="-p 8050"`.

The launcher uses the same data directory rule as the backend: `ROSC_DATA_DIR`, else `<checkout>/data` (the database is `<dir>/rosc.db`; the previous default `~/.local/share/rosc` is no longer implied: a pair that was started with it keeps it, because `status`/`down` and idempotent `up` use the recorded path, but a fresh `up` without `ROSC_DATA_DIR` now looks at `<checkout>/data` and refuses if it is missing). It prints the resolved path before starting and never silently substitutes another. If the database is missing or not at head it refuses and prints the command to run yourself, e.g. `ROSC_DATA_DIR=<dir> .venv/bin/alembic upgrade head` (back up first). It only reads the schema version read-only.

Exit codes: `0` success/RUNNING, `1` failure, `2` usage, `3` STOPPED, `4` DEGRADED, `5` UNKNOWN. `up` is idempotent: a healthy managed pair is reported, not restarted, and a plain `local-app up` accepts a pair that was started with `-p 8050`. An explicit different port or data dir while a pair is running is refused without changing anything (run `down` first). `status` and `down` always use the ports and data dir recorded when the pair was started, not the current shell's defaults.

Ports: the backend port is flexible (any free port 1024..65535, including 8000 when genuinely free). The frontend port can only be `5173` or `4173` (`--frontend-port`) because the backend Origin allowlist accepts only those; the launcher does not weaken Host/Origin policy. If a required port is held by any other process, `up` fails naming the port and a retry command; it never kills, adopts or reuses the holder and never starts a half pair.

State, pid identities and logs live in the git-ignored `.local-dev/`. Stopping signals a process group only while the leader's recorded identity (pid, `/proc` start time, command line, working directory, own process group) still matches; the identity check narrows but cannot fully eliminate OS races.

Recovering from UNKNOWN/stale state: `local-app status` shows which service is unproven. If a recorded process is gone, `local-app down` clears the stale record. If a process group is alive but its leader cannot be proven (for example the leader exited and descendants remain), the launcher leaves it alone: inspect it with `ps -eo pid,pgid,cmd | grep <pgid>`, stop what is verifiably yours manually, then run `local-app down` (or delete `.local-dev/state.json` once nothing owned remains). `local-app check launcher` includes a live smoke against a disposable temporary database.

Workspace manifests (`project.yaml`, `task.yaml`) are projections of SQLite. Ordinary reconciliation only rebuilds a *missing* manifest and never replaces an existing file. A corrupt or drifted manifest needs the separate, Owner-confirmed repair action, which first copies the exact old bytes into `workspace/quarantine/`; a manifest that belongs to another record is never overwritten and needs manual resolution.

`alembic downgrade` drops tables together with their data. Back up `rosc.db` and the `workspace/` directory first and never run it against a real data directory.

### Project Architect (OpenCode) configuration

Verified against OpenCode 2.2.5 offline (no model call). Offline tests use a test-only fake server; **a live provider reply has not been verified**. The Owner does **not** set an OpenCode server password. On each explicit Owner activation ROSC draws a fresh 256-bit secret from the OS CSPRNG, gives it only to its own `opencode serve` child (environment) and uses the same value for its loopback Basic-auth calls (user `opencode`). So a restarted backend can still probe and stop a surviving child, the secret is kept in `<ROSC_DATA_DIR>/architect-runtime/.secrets/<session-id>` (mode 0600, directory 0700, never in Git, argv, a URL, the database, logs or any API) and removed only after the child's termination is confirmed; a missing or unsafe file keeps the Project blocked (`starting`) until an explicit deactivation proves the stop. An `OPENCODE_PASSWORD` already present in ROSC's own environment is ignored (never required, never inherited, never shared between Projects). This password protects only the local server; it is **not** an OpenCode Go/provider credential,; provider authorization for the isolated child is opt-in, see `ROSC_OPENCODE_PROVIDER_KEY_FILE` below (live model replies are not verified). Set, in the environment of the ROSC backend process (never in Git, argv or a URL):

- `ROSC_OPENCODE_APPROVED_MODEL=<provider>/<model>`: the model the Owner approves for outbound conversation content. It must equal the assigned Agent's `provider_id/model_id`, otherwise the state is `not_configured`.
- `ROSC_OPENCODE_PROVIDER_KEY_FILE=<absolute path>` (only for an agent whose provider is `opencode-go`; opt-in, default off): a private file holding **only** the OpenCode Go API key. Verified for OpenCode 2.2.5 offline: `opencode auth login` credentials live in the same SQLite database as sessions (`<XDG_DATA_HOME>/opencode/opencode.db`; the legacy `auth.json` is ignored), so ROSC never points Architects at an enrolled profile or shares that database between Projects, and the Owner's `~/.local/share/rosc/opencode-auth` enrollment is **not** reused or read. Instead ROSC reads the key file only at explicit activation and gives it to that one child as `OPENCODE_API_KEY` (the supported env source for the provider); each child keeps its own empty data directory. The file must be absolute and normalised, a regular single-link file owned by the ROSC user with mode `0600`, in a directory owned by that user and not group/world-writable, with no symlinks. Create it yourself, e.g. `install -m 600 /dev/null ~/.config/rosc/opencode-go.key` and paste the ROSC-dedicated key into it with an editor (never on a command line). Diagnostics (non-secret): `provider_key_not_configured`, `provider_key_missing`, `provider_key_unsafe`, `provider_key_invalid`. Setting the variable does not start anything or call a model; and `ROSC_OPENCODE_APPROVED_MODEL` (e.g. `opencode-go/deepseek-v4.1-flash`) must be in the environment of the **running backend process**, so restart ROSC from the shell that exports both. `opencode models` listing is not a live proof; a real reply still needs a separate Owner-approved smoke test.
- Optionally `ROSC_OPENCODE_BIN`: absolute path of the `opencode` executable (otherwise found on `PATH`).

Then register an Agent with `runtime_adapter: "opencode"` and assign it the Project `architect` role via the existing API. ROSC never connects to, reconfigures or stops the OpenCode background service of the Owner and never reads OpenCode's own credentials. Each activation starts a separate server on a free `127.0.0.1` port; ROSC stops only a process whose pid, start time and per-session marker it can prove. Back up `rosc.db`, then run `alembic upgrade head` (migration `0006` is additive; `0007` rebuilds `conversation_messages` to lift the reply-length CHECK, see below; `0008` adds only the content-free `architect_tool_audit` table) before starting the backend.

**Project Context Access (P019).** An Owner-activated Architect session may call a small `rosc_*` Tool namespace (native OpenCode `shell/edit/read/webfetch/subagent` stay denied). Every call re-verifies the live session, assignment, Project and Root Binding in ROSC Core. Resource matrix: global `rules/`, `skills/`, `tools/` are list/search/read only; the current Project's ROSC-private documents can be listed, searched, read, created, updated (with `expected_revision`), recoverably deleted and restored; other registered Projects' ROSC-private documents are read-only; only the current Project's bound source checkout is readable (list/search/read, `git head/status/log`) and it is never modified. Known credential files and VCS internals are excluded, and ROSC's own configured private data (database, Workspace, runtime) is never served as source even if a checkout encloses it. There is no separate per-category disclosure switch: because the Owner chose the provider/model and explicitly activated the session, results of the Tools that session may call can be sent to that provider/model during active conversations; nothing is sent when a Project is opened or at activation, and a permitted source file can still contain private data. `GET /api/projects/{id}/architect/context-access` returns the Owner-readable effective access summary. Verified offline with a fake OpenCode server only; no real model-driven Tool smoke has been run. Migration `0008` has not been applied to any real database by this change.

**Architect reply length (P018).** ROSC applies **no application-level size limit** to a completed Architect reply (no character, token or byte ceiling, no truncation, no `reply_too_large`); a generated answer is stored whole. Only real resources bound it: the provider's own output limits, host memory and disk, and SQLite (`SQLITE_MAX_LENGTH`). **Owner-authored messages keep the 4000-character limit** (API and service, enforced before any model call). Retrieval: ordinary control calls and 200-message lookup pages keep the 4 MB cap; when a lookup page exceeds it ROSC re-reads that cursor **one message at a time without a byte cap**, so unrelated messages are not batched in memory, and the reply is read in 1 MiB chunks. Storage normalizes representation only: CRLF/CR become LF, and NUL/other C0 controls (except tab/newline) and unpaired surrogates become U+FFFD instead of discarding the answer. If a generated reply cannot be fetched or stored right now (memory, disk, SQLite size error), the delivery becomes `unknown` with a non-secret reason (`reply_retrieval_resources`, `reply_storage_resources`, `lookup_failed`, `timeout`) and the model is never called again; **Check result** (reconcile) re-reads the reply by its stored OpenCode correlation and persists it exactly once. A historical delivery that failed as `reply_too_large` under P015–P017.2 offers **Restore answer**: an explicit Owner action that looks the original reply up by the stored correlation (no new model request) and works only while the same Architect session is still live and holds that reply; otherwise the row is left unchanged. Migration `0007` copies every `conversation_messages` row verbatim (ids, ordinals, attribution, keys, timestamps, constraints, sender index, immutability triggers) and aborts with rollback on any row-count or `foreign_key_check` mismatch; it is never run at application start, so back up `rosc.db` first. `alembic downgrade` to `0006` **refuses** while any stored message exceeds 4000 characters (it never truncates); restore the backup instead. Verified with synthetic/offline tests only; a genuine long-answer live smoke (refresh and backend restart, no duplicates) is a separate Owner-approved step.

## Current limits

There is no Worker launch, Run approval, event import, attachment service or Task Conversation yet; the Architect's context access is limited to the P019 ROSC Tools below. The only UI action that starts a process is the explicit Architect activation; it starts a loopback `opencode serve` restricted to deny-all permissions in an empty ROSC-owned directory, and previously saved messages are never replayed to it. Existing Project registration assumes no external agent currently writes the checkout; it does not adopt external sessions. ROSC Workspace artifacts are separate from the project's application source.

Registered Agents and role assignments are administrative records only. A registered Agent is not a session, credential or runtime; an assignment is bounded intent, not activation or an effective permission. Capability readiness is reported as `declared`/`unverified` or `blocked` and is never `verified`; agent actors remain denied by the Core policy until a later authorized Run slice exists.

Canonical architecture and scenarios remain in the ROSC Google Drive folder; this public repository contains implementation code and a mock UX reference, not copies of private project documentation.
