# ROSC

Relay Orchestration & Supervision Center. The Engine foundation is provisional; the managed development workflow is being established first.

The working architecture and [implementation pipeline](https://drive.google.com/file/d/1GVaW8SGFuB8F0CaxLEmPXd-HQkuU21gH/view) live in the ROSC Drive folder. Repository `docs/` files provide local agent bootstrap and a versioned implementation copy. Start with [docs/README.md](docs/README.md).

## Current behavior

- Register a new or existing local Project without starting an agent.
- Create a Draft Task with a title, description and acceptance criteria.
- Persist both in SQLite and provision a separate ROSC Workspace manifest.
- Inspect and create them in a small React UI; closing and reopening the UI does not change agent state.

The [accepted interactive UX reference](https://drive.google.com/file/d/17OZ90HLDmNwRuG8UpSA6FBdbaLl3eQZ7/view) is a mock browser simulation. The [implementation pipeline](https://drive.google.com/file/d/1GVaW8SGFuB8F0CaxLEmPXd-HQkuU21gH/view) is maintained in Drive.

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

Open the Vite URL on localhost. API requests are proxied to `127.0.0.1:8000`. Keep `.local-data/` and `.venv/` out of Git. To run backend checks: `python -m unittest discover -s tests -v`.

## Current limits

There is no Worker launch, Architect session, Run approval, event import, attachment service or production chat yet. No UI action in this slice starts an agent. Existing Project registration assumes no external agent currently writes the checkout; it does not adopt external sessions. ROSC Workspace artifacts are separate from the project's application source.

Canonical architecture and scenarios remain in the ROSC Google Drive folder; this public repository contains implementation code and a mock UX reference, not copies of private project documentation.
