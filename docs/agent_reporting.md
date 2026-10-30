# Managed prompt, report and read-path contract

This is the ROSC adaptation of the existing NeoPlatform/DOUTLAB agent workflow. It governs repository-changing managed runs named by a Drive draft `p<ID>__<slug>.md`. It is separate from ROSC's future in-product Task/Run/Prompt service: a managed development prompt must never be mistaken for a ROSC Task's approved execution contract.

## Ownership and locations

| Artifact | Authoritative location | Direction |
| --- | --- | --- |
| Editable managed draft | ROSC Drive `Agent Prompts / Drafts` | Drive → local freeze |
| Executed revision | `.agent-telemetry/prompts/p<ID>__<slug>__rNN.md` | Immutable local history |
| Report | `.agent-telemetry/reports/p<ID>__YYYY-MM-DD_HH-MM__<model>__rNN.md` | Finalize locally → Drive archive |
| Current transport state | `.agent-telemetry/agent-sync/state.json` | Local manifest |
| Raw read-path events | `.agent-telemetry/read-path/*.jsonl` | Local only |

The two Drive folders belong under the ROSC folder. The prompt draft is an ordinary raw UTF-8 Markdown file, not a Google Doc, PDF or HTML export. The local config `docs/agent_sync.local.yml` holds ROSC-only folder IDs and is ignored by Git; the tracked example has blanks. The OAuth client and token are ignored too. No folder IDs or credentials are hardcoded in the public repository.

Only the Owner/coordinator edits a Drive draft before execution. An executed local revision is never rewritten or pushed back. A changed Drive draft explicitly produces the next `rNN`; an unchanged verified revision is reused. The frozen bytes are identified by SHA-256 in the state manifest. `chmod 444` is an accidental-write guard, not isolation. Verification fails closed on missing hash, missing file or changed bytes.

## Agent run sequence

1. The coordinator and Owner agree on a draft; the coordinator stores `p<ID>__<slug>.md` in `Agent Prompts / Drafts` and gives the agent its exact name/ID.
2. Agent runs `.venv/bin/python3 -m scripts.agent_sync.cli prompt-pull <ID>` before docs routing. It runs `verify-prompt` for the exact snapshot printed by pull. No manual copy or fallback to a chat paraphrase.
3. Agent records the full base Git commit, creates a report skeleton referencing snapshot path and SHA-256, declares COLD/WARM honestly, routes through `docs/README.md`, and records actual documentation reads with `scripts.read_path.cli`.
4. Agent works, validates, commits, records actual result HEAD and documentation impact, attaches the read-path summary, and runs `finalize-report`.
5. Agent prints one completion line: `PROMPT <ID> <STATUS> | report=<path> | commit=<sha|none> | sync=<synced|pending|failed> [| transport=<auth_required|unavailable>]`. Full report stays in the file.
6. `reports-push <report>` archives the finalized report in ROSC Drive. The coordinator reads that archive or the local authoritative report and reviews evidence and code. Sync failure leaves a completed local report intact and retryable.

The report records prompt ID, frozen revision/hash, model, branch, base and result commits, status, dates, result, validation, outstanding items, documentation impact, historical sync state and telemetry summary. It references rather than duplicates the full prompt. Finalization seals the report hash and timestamp; later transport changes only the manifest, never the report body. Re-finalizing unchanged bytes is a no-op; mutating a finalized report is refused.

## CLI commands

```sh
make agent-sync-check
make read-path-check
.venv/bin/python3 -m scripts.agent_sync.cli prompt-pull 001
.venv/bin/python3 -m scripts.agent_sync.cli verify-prompt .agent-telemetry/prompts/p001__example__r01.md
.venv/bin/python3 -m scripts.agent_sync.cli status --prompt 001
.venv/bin/python3 -m scripts.agent_sync.cli reports-push .agent-telemetry/reports/<actual-report-name>.md
```

Use the CLI help for exact flags to `report-skeleton`, `finalize-report`, `completion-line`, and `scripts.read_path.cli`. Do not guess an ID, revision, snapshot or commit. Live transport requires optional `requirements-agent-sync.txt`, the ROSC folder IDs in ignored local config, and a **human** one-time interactive `make agent-sync-auth`. An agent or automated task must not initiate OAuth. CLI local commands and offline tests do not need credentials or network.

`prompt-pull` accepts exactly one matching raw Markdown draft ID in the configured folder. Zero or multiple matches, an unsupported MIME type or invalid UTF-8 fail closed. Pull is idempotent by hash. `reports-push` validates local finalization and hash before upload, adopts/reconciles a matching remote archive by artifact properties, refuses a missing or conflicting recorded remote ID, and never creates a duplicate on retry. Current transport health (`ok`, `auth_required`, `unavailable`) is separate from an artifact's last confirmed `synced` status.

## Documentation telemetry

`scripts.read_path.cli` is a diagnostic adapted from DOUTLAB: `init` before a COLD run's first docs read, then `read` after actual full/partial reads, `obligation` when a routing rule requires a document, `end`, and `attach` on the draft report before finalization. The tool timestamps and hashes recorded files. It cannot prove that the agent understood them or that unrecorded reads did not happen. WARM reuse records hash freshness plus an unverified residency claim; uncertainty means COLD. Raw events stay local; the report carries a compact analysis and pointer. Token counts and cost remain unavailable unless a reliable runtime source is added.

## Separation from product Engine

This repository workflow makes development auditable before ROSC Engine is mature. It does **not** authorize an in-product Worker Run, create a production Task Prompt Draft, or supply Agent telemetry for the product UI. Those contracts are built in later implementation slices under Owner authorization.
