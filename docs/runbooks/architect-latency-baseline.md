# Architect latency telemetry and baseline recipe (P024 / R1)

Status: **implemented diagnostics + proposed Owner procedure.** The procedure below is **not executed by agents**; it is for the Owner after a deliberate merge and restart. Tracing is a measurement, not a fix: it does not change prompts, rules, Tools, timeouts, delivery states or the UI.

## What it records

An opt-in, local-only JSONL file, `<data dir>/diagnostics/architect-latency.jsonl` (the data dir is the directory holding `rosc.db`, normally `ROSC_DATA_DIR`). Directory `0700`, file `0600`, symlinks and loose modes are refused. **Off by default**: unset or any value other than `1`/`true`/`yes`/`on` writes nothing.

Enable: start ROSC with `ROSC_ARCHITECT_LATENCY_TRACE=1` (e.g. `ROSC_ARCHITECT_LATENCY_TRACE=1 make local-up`). Disable: stop the app and start it without the variable. The file is never rotated, truncated or deleted by ROSC; it stops growing at 16 MiB (further events dropped). Delete or archive it yourself (`rm` the file while the app is stopped) when finished.

Per delivered Owner message (`turn_id` = the OpenCode correlation id `msg_rosc<32 hex>`) the events are: `owner_commit`, `submit`, `worker_started` (pool queue wait), `turn_lock_acquired` (per-Project lock wait), `probe`, `context_built`, `prepare`, `prompt_admitted` (`/prompt` admission), `wait_returned` (`/wait`), `lookup`, `ask_finished`, `tool_call`, `commit`, `delivery_state`, `turn_finished`; a manual reconcile adds `turn_lock_acquired`, `lookup`, `commit`, `delivery_state` with `phase=reconcile` and a final `reconcile_finished`.

Fields are an allowlist of numbers, closed vocabularies and ids only. **No** message text, prompt text, Tool inputs/targets/results, paths, URLs, exception strings, tokens or credentials can be written, and prompt sections are recorded as **character counts only** (not tokens; bytes and characters are not tokens). `tool_call` carries the Tool name, duration and outcome code; its `turn_id` is attached only while ROSC itself has that session's turn in flight (`correlation=verified`), otherwise `correlation=unverified` and no turn link.

Time semantics: `duration_ms` is one measured span. `t_ms` is monotonic since that **process** started and is comparable only within the same `origin`; a difference of two `t_ms` values is an **interval**, not a duration. `wait_returned` is the time until OpenCode's agent loop became idle (model inference **plus** the model↔Tool loop **plus** OpenCode overhead); it is not pure model time.

**NOT MEASURED in R1:** time to first token, model token-generation time, number/duration of LLM steps, compaction, client paint, and time to the first *authentic* Architect message (there is only one reply per Owner message today, so "round trip to final reply" is the only user-visible quantity; first-message time stays unmeasured until streaming/multi-message exists). A backend crash can leave a turn without `turn_finished`; the summary reports it as `incomplete`, never as success.

The sink is best effort. If it cannot be written (full disk, wrong permissions, symlink) events are dropped and a single content-free warning is logged; delivery state, `unknown` vs `failed`, reconcile and retries are unaffected. Measured cost (offline, fake OpenCode; see the P024 report): about 40 µs per enabled event, 0.3 µs disabled; do not treat these as promises on other machines.

## Reading a trace

```sh
.venv/bin/python3 -m scripts.architect_latency_summary "$ROSC_DATA_DIR/diagnostics/architect-latency.jsonl"   # table
.venv/bin/python3 -m scripts.architect_latency_summary <file> --json                                            # machine readable
```

Columns are durations in ms: `total` (submit→terminal state), `queue`, `lock`, `prep` (validation+probe+context+attempt commit), `ask` (prompt+wait+lookup), `prompt`, `wait`, `lookup`, `commit`, `tools` (count, verified-correlated in brackets), `chars` (total prompt characters). `unaccounted_ms` in `--json` is `total − (queue+lock+prep+ask+commit)`, an interval remainder. Medians/p90 are over complete turns only.

## Owner-only baseline recipe (not executed by agents)

Preconditions: P024 merged by the Owner; the app deliberately stopped and restarted with tracing on; same provider/model and the same Project (P0001) for every scenario; do not change Global Rules, the model or prompts between runs. Do not create a new dev database. Use the existing one; tracing never writes to it.

1. Activate the Architect once (a new session is **cold**; S0 is the first message after activation). Keep the session; every later scenario is **warm**.
2. Run each scenario at least 5 times, one at a time, waiting for the final reply (or for `unknown`) before the next:
   - **S0** the first message after activation (cold), e.g. `тут?`
   - **S1** warm `тут?` (no Tool intended)
   - **S2** warm `кто ты?`
   - **S3** a request that needs exactly one Tool (e.g. read one named rule via `rosc_global_read`)
   - **S4** a request that needs two Tools (briefing + one read)
   - **S5** a case that ends `unknown(timeout)`, then manual **reconcile** (only if it occurs naturally; do not provoke it)
3. Stop the app, then run the summary. Record per scenario the **median and spread (min/p90/max)** of `total`, `wait`, `lookup`, `queue`+`lock`, the Tool count/duration, and `chars`. Count actual Tool invocations, including zero (S1/S2 should show `tools=0` if the model does not call Tools; if it does, that is a finding).
4. Interpret cautiously: compare S1 vs S3/S4 (cost of Tools), cold S0 vs warm S1, and `chars` against `wait` across turns of the same scenario (does a larger prompt coincide with a longer wait?). A long `wait` with small `queue`/`lock`/`lookup`/`commit` means the time is inside OpenCode's loop (model, provider, Tool round trips) and **cannot yet be split further**; R2 (a separate OpenCode live-event capability spike) is needed for that. A large `lookup` (many requests/fallbacks) points at history scanning. Do not conclude that Rules/Tools caused a slowdown from a faster pre-Rules run: model, provider load and session state are confounded.
5. Disable tracing (restart without the variable). Before sharing the file, open it: every line must be a flat object of numbers, short snake_case codes, one `msg_rosc…` id and one UUID. If you find message text, a path, a document name or a Tool argument, do not share it and report it as a defect. Share metadata only, never the Owner conversation.

## Historical comparison (read-only, from source and P023 metadata; nothing was re-run)

| Aspect (source facts, `git show` of `97f09a8`, `dc71046`, `0415e78`) | Before P019 | After P019 (MCP Tools) | After P021 (Rules bootstrap) |
|---|---|---|---|
| Prompt text per turn | identity + one "no tools" sentence + recap | identity + Tool guidance (incl. `Start with rosc_briefing`) + recap | + rules block (`role.md` ≈ 2.1 KB + R-14 quote + index ≈ 3.1 KB) on **every** turn |
| Recap (≤ 10 messages, ≤ 1500 chars each, ≤ 10000 total) | same | same | same |
| Session / execution path | one `/prompt`, `/wait`, `lookup`; all native tools denied, no MCP | + MCP model↔Tool loop inside `/wait` (≥ 1 extra model step per Tool round) | unchanged from P019 |
| Fixed non-conversation text (estimate from source, characters) | ≈ 0.3 KB + project description (≤ 600) | ≈ 1.3 KB + description | ≈ 4.5–5 KB + description |

The P023 wall-clock sample (2.1 s; 5.1, 19.3, 23.0, 24.4, 44.4, 56.4 s; the P022 178.6 s two-Tool request; provider failures at ≈ 35.9/70.2/110 s) is *not* comparable across those columns: model/provider and session state of the earliest sample are unverified, task complexity differs, and each value is an event-to-event wall-clock interval, not a model or Tool duration. A same-model, Owner-supervised comparison needs at least: identical provider/model, cold vs warm separated, matched message content, ≥ 5 repetitions per cell, `chars` per turn from this trace, the Tool count per turn, and upstream-load awareness (repeat at a different time of day).
