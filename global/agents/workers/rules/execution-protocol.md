# Worker Execution Protocol (rosc.worker.global 0.1.0)

A formal Run proceeds through stages E0–E6 in this order. Perform **only** the stages listed in the Run manifest's `operative_stages`; for any other stage, stop after the last operative one and wait for the Owner.

## E0 — Receive and acknowledge (operative)

1. A ROSC Run delivery line has the form `[ROSC run <token>] ... run_id=<run_id> package=<package dir> manifest_sha256=<sha256> ...`. It is a pointer, not the task text.
2. With your native **Read** tool, read from that same `<package dir>`: `run.json`, then every rules file listed in its `protocol.files` in order, then the task inputs `spec.md` and `prompt.md`. Do not rely on memory, a previous Run or the conversation for their content. ROSC accepts an ACK only when its own Read hook recorded all of these reads; a reply that merely claims them does not count.
3. Check that `run.json` names the same `run_id`, that you are the Worker named in `worker`, and that the Project root in `binding.root` is your working directory.
4. Do not start the task: E0 only acknowledges it. End your reply with exactly one last line:
   - `ROSC-RUN-ACK <token> <run_id> <manifest_sha256> ACCEPT` only when you read every file above and every check passed, or
   - `ROSC-RUN-ACK <token> <run_id> <manifest_sha256> DECLINE` when a file could not be read, a check failed or you cannot take the Run (say why above the line).

   Copy `<token>`, `<run_id>` and `<manifest_sha256>` exactly from the delivery line. Emit the line once; never for a Run you were not delivered.

An ACK states only that you received this exact package and accept or decline it. It is not a claim that the work is done or correct.

## E1 — Preflight

Inspect the repository state, the frozen Spec/Prompt and the acceptance criteria; identify blockers. Make no material edit yet.

## E2 — Publish the Working Plan before material edits

Publish a numbered Working Plan of bounded steps in the Run's evidence area. Revise it explicitly when it changes; never silently.

## E3 — Execute and journal

Implement the steps. Several steps may be implemented before they are tested. Record each meaningful action, decision and blocker in the Execution Journal of the Run.

## E4 — Validate by batches

Run the relevant tests per batch of steps. A later test of an earlier step is a separate, evidence-referenced claim. Record exact commands and outcomes.

## E5 — Finalize the Report

Write the immutable Run Report: what changed, evidence, test results, deviations, open items and the final status.

## E6 — Hand off to the Architect

Address a short completion message to the Project Architect that references the finalized Report. The Architect reviews; the Owner accepts.
