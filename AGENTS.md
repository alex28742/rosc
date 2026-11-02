# ROSC agent bootstrap

Before a repository change, read `docs/README.md` and follow `docs/development_workflow.md`. The Owner-approved managed prompt is the execution contract for the task; task-specific requirements, decisions and references belong there, not in these files. Determine the current implementation state from the repository source, tests and migrations.

If the task names a managed draft `p<ID>__<slug>.md`, first run `.venv/bin/python3 -m scripts.agent_sync.cli prompt-pull <ID>`, then `verify-prompt` on the exact printed snapshot. Execute only that frozen revision. A failed pull or verification stops the managed run. Do not replace it with copied chat text.

You are not expected to open Google Drive documents. A Drive URL is a reference, not proof of access or review. If essential task context is missing or unreadable, report it and stop the affected work; do not invent it. UI work uses only an Owner-approved reference supplied or made accessible for the task; otherwise mark the UX reference BLOCKED/UNVERIFIED instead of guessing a layout.

Do not print or commit credentials, tokens, `.env`, `.agent-telemetry/`, agent transcripts or private project source. Keep ROSC code separate from NeoPlatform and DOUTLAB. Opening a Project or Task does not launch an agent; a Worker Run requires explicit Owner authorization. Commit, push and merge only as the current prompt or Owner explicitly authorizes.
