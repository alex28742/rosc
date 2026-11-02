# ROSC

Relay Orchestration & Supervision Center. The Engine foundation is provisional; the managed development workflow is being established first.

The working architecture and [implementation pipeline](https://drive.google.com/file/d/1GVaW8SGFuB8F0CaxLEmPXd-HQkuU21gH/view) live in the ROSC Drive folder. Repository `docs/` files provide local agent bootstrap and a versioned implementation copy. Start with [docs/README.md](docs/README.md).

## Current behavior

- Register a new or existing local Project without starting an agent.
- Create a Draft Task with a title, description and acceptance criteria.
- Register a provider-neutral Agent and record scoped `architect`/`worker` assignments for a Project or Task; neither action starts a runtime, session or Run, and neither grants an Agent Core authority.
- Persist both in SQLite and provision a separate ROSC Workspace manifest.
- Inspect and create them in a small React UI; closing and reopening the UI does not change agent state.

The [accepted interactive UX reference](https://drive.google.com/file/d/17OZ90HLDmNwRuG8UpSA6FBdbaLl3eQZ7/view) is a mock browser simulation. The [implementation pipeline](https://drive.google.com/file/d/1GVaW8SGFuB8F0CaxLEmPXd-HQkuU21gH/view) is maintained in Drive.

Stage 01 (Foundation) is Owner-accepted with documented deferrals; ADR-0001 remains `proposed` and Stage 01F is not started. UI work follows the UX gate in [docs/development_workflow.md](docs/development_workflow.md), starting from the approved reference above and the Drive [Stage 01 acceptance and UI delivery rules](https://drive.google.com/file/d/1NcHJRHeBK6daFNyGu22fxQqlD-BIy9qC/view).

## Managed agent workflow

The coordinator and Owner prepare an editable Markdown draft in the ROSC Drive `Agent Prompts / Drafts` folder. An agent receives its filename, pulls and verifies an immutable local revision, works, commits, finalizes a local report, and archives that report in `Agent Reports / Archive`. See [docs/agent_reporting.md](docs/agent_reporting.md). Raw telemetry and credentials stay local and ignored by Git. This workflow manages development agents; it is separate from future in-product ROSC Task Runs.

Run `make agent-sync-check` and `make read-path-check` for offline verification. Live Drive transport needs `pip install -r requirements-agent-sync.txt`, a gitignored `docs/agent_sync.local.yml` based on the example, and a human interactive `make agent-sync-auth` on the actual development machine.

## Local development

Requirements: Python 3.11+, Node.js 24+, npm. Use a disposable data directory while testing.

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

The launcher uses the same data directory rule as the backend: `ROSC_DATA_DIR`, else `~/.local/share/rosc` (the database is `<dir>/rosc.db`). It prints the resolved path before starting and never silently substitutes another. If the database is missing or not at head it refuses and prints the command to run yourself, e.g. `ROSC_DATA_DIR=<dir> .venv/bin/alembic upgrade head` (back up first). It only reads the schema version read-only.

Exit codes: `0` success/RUNNING, `1` failure, `2` usage, `3` STOPPED, `4` DEGRADED, `5` UNKNOWN. `up` is idempotent: a healthy managed pair is reported, not restarted, and a plain `local-app up` accepts a pair that was started with `-p 8050`. An explicit different port or data dir while a pair is running is refused without changing anything (run `down` first). `status` and `down` always use the ports and data dir recorded when the pair was started, not the current shell's defaults.

Ports: the backend port is flexible (any free port 1024..65535, including 8000 when genuinely free). The frontend port can only be `5173` or `4173` (`--frontend-port`) because the backend Origin allowlist accepts only those; the launcher does not weaken Host/Origin policy. If a required port is held by any other process, `up` fails naming the port and a retry command; it never kills, adopts or reuses the holder and never starts a half pair.

State, pid identities and logs live in the git-ignored `.local-dev/`. Stopping signals a process group only while the leader's recorded identity (pid, `/proc` start time, command line, working directory, own process group) still matches; the identity check narrows but cannot fully eliminate OS races.

Recovering from UNKNOWN/stale state: `local-app status` shows which service is unproven. If a recorded process is gone, `local-app down` clears the stale record. If a process group is alive but its leader cannot be proven (for example the leader exited and descendants remain), the launcher leaves it alone: inspect it with `ps -eo pid,pgid,cmd | grep <pgid>`, stop what is verifiably yours manually, then run `local-app down` (or delete `.local-dev/state.json` once nothing owned remains). `local-app check launcher` includes a live smoke against a disposable temporary database.

Workspace manifests (`project.yaml`, `task.yaml`) are projections of SQLite. Ordinary reconciliation only rebuilds a *missing* manifest and never replaces an existing file. A corrupt or drifted manifest needs the separate, Owner-confirmed repair action, which first copies the exact old bytes into `workspace/quarantine/`; a manifest that belongs to another record is never overwritten and needs manual resolution.

`alembic downgrade` drops tables together with their data. Back up `rosc.db` and the `workspace/` directory first and never run it against a real data directory.

## Current limits

There is no Worker launch, Architect session, Run approval, event import, attachment service or production chat yet. No UI action in this slice starts an agent. Existing Project registration assumes no external agent currently writes the checkout; it does not adopt external sessions. ROSC Workspace artifacts are separate from the project's application source.

Registered Agents and role assignments are administrative records only. A registered Agent is not a session, credential or runtime; an assignment is bounded intent, not activation or an effective permission. Capability readiness is reported as `declared`/`unverified` or `blocked` and is never `verified`; agent actors remain denied by the Core policy until a later authorized Run slice exists.

Canonical architecture and scenarios remain in the ROSC Google Drive folder; this public repository contains implementation code and a mock UX reference, not copies of private project documentation.
