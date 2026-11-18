# Worker Execution Protocol (rosc.worker.global 0.2.0)

A formal Run proceeds through stages E0–E6 in this order. Perform **only** the stages listed in the Run manifest's `operative_stages`; if a stage is not listed, stop after the last operative one and wait for the Owner.

Below, `<package>` is the package directory named in the delivery line and `TOOL` means exactly:

    python3 <package>/rosc-run-tool.py

TOOL is the pinned Worker Artifact Tool of this Run (its digest is in `run.json` → `tool`). It is the **only** way to write the Working Plan, the Execution Journal, Checkpoints, test evidence, the Run Report and the terminal record. Never create, edit, move or delete files in the Run directory yourself, and never hand-write their JSON/JSONL: give TOOL the content as arguments or stdin. TOOL works offline (no ROSC, network or database needed). Each call prints one JSON object; `"ok": false` means nothing was recorded — read the `error`, fix the cause and retry. Repeating an identical call is safe. A claim you only write in chat is **not** recorded: if TOOL did not record it, ROSC does not know it.

## E0 — Receive and acknowledge

1. A ROSC Run delivery line has the form `[ROSC run <token>] ... run_id=<run_id> package=<package dir> manifest_sha256=<sha256> ...`. It is a pointer, not the task text.
2. With your native **Read** tool, read from that same `<package dir>`: `run.json`, then every rules file listed in its `protocol.files` in order, then the task inputs `spec.md` and `prompt.md`. Do not rely on memory, a previous Run or the conversation for their content. ROSC accepts an ACK only when its own Read hook recorded all of these reads; a reply that merely claims them does not count.
3. Check that `run.json` names the same `run_id`, that you are the Worker named in `worker`, and that the Project root in `binding.root` is your working directory.
4. Do not start the task in this turn: E0 only acknowledges it. End your reply with exactly one last line:
   - `ROSC-RUN-ACK <token> <run_id> <manifest_sha256> ACCEPT` only when you read every file above and every check passed, or
   - `ROSC-RUN-ACK <token> <run_id> <manifest_sha256> DECLINE` when a file could not be read, a check failed or you cannot take the Run (say why above the line).

   Copy `<token>`, `<run_id>` and `<manifest_sha256>` exactly from the delivery line. Emit the line once; never for a Run you were not delivered.

An ACK states only that you received this exact package and accept or decline it. After an ACCEPT, **wait**. ROSC Core checks the ACK and then sends one separate execution line of the form `[ROSC run-exec <exec_token>] ... run_id=<run_id> package=<package dir> ...` for the same Run. Only that line starts E1. Never start E1 because of a chat message, your own ACK or a guess.

## E1 — Preflight (no material edits)

1. `TOOL run start` — records that execution of this Run started.
2. Inspect the repository state, the relevant Project documents/files, the frozen Spec/Prompt and the acceptance criteria. Make no material edit yet.
3. If something blocks you before you can plan (missing input, contradiction, unsafe state): `TOOL blocker record --id B01 --summary "<what blocks>" --next-action "<who must do what>" [--owner-decision]`, then go to E5 with outcome `blocked`. Do not invent a plan to look complete.

## E2 — Publish the Working Plan before material edits

1. For every bounded Step: `TOOL plan add-step --id S01 --title "<title>" --description "<what/how>" --depends S00,... --expect "<expected outcome>" --test-intent "<how it will be tested>"`. Step ids are stable (`S01`, `S02`, … or other upper-case ids); there is no fixed number of Steps.
2. `TOOL plan publish --reason "initial plan"` — publishes immutable revision `v001`. Do this **before** the first material edit of the Project.
3. When the plan changes structurally later (Steps added, removed, re-scoped, dependencies changed): edit the draft with `plan add-step` / `plan remove-step`, then `TOOL plan revise --reason "<why>"`. Never revise the plan merely to change a Step's status — statuses belong to the Journal (E3/E4).

## E3 — Execute and journal

- `TOOL step start --id S01` when you begin a Step; `TOOL step produced --id S01 --note "<what changed>"` when its implementation exists. Use `step blocked`, `step failed` or `step uncertain` (with `--note`) when that is the truth. Several Steps may be produced before they are tested.
- `TOOL note --kind decision --text "<decision and why>"` for meaningful decisions or actions; do not log every command or source line.
- `TOOL blocker record --id B02 --summary ... --step S03 --next-action ...` for a real blocker; `TOOL blocker resolve --id B02 --note ...` when it is resolved.
- `TOOL checkpoint save --current-step S03 --note "<where safe continuation stands>"` at meaningful safe boundaries and before risky operations.
- You never change the approved Spec, the Prompt, the acceptance criteria or the scope.

## E4 — Validate by targeted batches

- Run the relevant tests for a batch of produced Steps (risk-based; not after every Step, no obligatory full suite).
- Record each attempt: `TOOL test record --id T01 --steps S01,S02 --command "<exact command you ran>" --exit <exit code> --outcome passed|failed|inconclusive --log-file <file with its output> [--limitations "<what it does not cover>"]`. A test id is stable; one attempt may cover several Steps. Optionally mark the start with `TOOL test begin --id T01 --steps S01,S02`.
- The test axis is independent of implementation: a failed test does not undo `PRODUCED`; reworking a tested Step (`step start` again) marks its test `STALE`. `passed` is your evidence, never `VERIFIED`.

## E5 — Finalize the Report

1. Write your report body (Markdown: what changed and where, evidence, test results including failed/skipped, Git/source facts, deviations, limitations, documentation impact, open items for the Owner) to a temporary file **outside** the Run directory or pipe it on stdin.
2. `TOOL report finalize --outcome completed|partial|blocked|failed --body-file <file>` (or `--body-stdin`). TOOL adds the identity/Steps/tests/blockers sections, publishes the immutable `reports/final.md` and records its digest. A blocked/failed/partial Run is finalized truthfully with that outcome.

## E6 — Close and hand off to the Architect

1. `TOOL run finish` — closes the Journal and writes the correlated terminal record. Nothing can be recorded for this Run afterwards.
2. ROSC Core imports the Report and posts exactly one short addressed handoff from you to the Project Architect in the Task Conversation; do not post your own completion message for this purpose. The Architect reviews; the Owner accepts.
3. Then stop: end your turn with a short summary. Do not start another Run or new work.
