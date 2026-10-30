# ROSC agent bootstrap

Before a repository change, read `docs/README.md` and follow `docs/development_workflow.md` to the relevant owner context. Canonical project decisions and the implementation pipeline live in the ROSC Google Drive folder; local routing files are operational copies.

If the task names a managed draft `p<ID>__<slug>.md`, first run `.venv/bin/python3 -m scripts.agent_sync.cli prompt-pull <ID>`, then `verify-prompt` on the exact printed snapshot. Execute only that frozen revision. A failed pull or verification stops the managed run. Do not replace it with copied chat text.

Do not print or commit credentials, tokens, `.env`, `.agent-telemetry/`, agent transcripts or private project source. Keep ROSC code separate from NeoPlatform and DOUTLAB. Opening a Project or Task does not launch an agent; a Worker Run requires explicit Owner authorization.
