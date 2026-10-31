# ROSC agent bootstrap

Before a repository change, read `docs/README.md` and follow `docs/development_workflow.md` to the relevant owner context. Canonical project decisions and the implementation pipeline live in the ROSC Google Drive folder; local routing files are operational copies.

If the task names a managed draft `p<ID>__<slug>.md`, first run `.venv/bin/python3 -m scripts.agent_sync.cli prompt-pull <ID>`, then `verify-prompt` on the exact printed snapshot. Execute only that frozen revision. A failed pull or verification stops the managed run. Do not replace it with copied chat text.

UX/UI work starts from the actual approved interactive reference `ROSC_Dashboard_Project_Task_Work_Area_v06.html` (SHA-256 `21950ed08df67caafbcb747b05cf1eedc7e02163c718f10d85bb304ee0754c80`, [Drive](https://drive.google.com/file/d/17OZ90HLDmNwRuG8UpSA6FBdbaLl3eQZ7/view)) and the [Stage 01 acceptance and UI delivery rules](https://drive.google.com/file/d/1NcHJRHeBK6daFNyGu22fxQqlD-BIy9qC/view): open it in a browser and exercise it; do not design from memory or old wireframes. If it is unavailable, mark the UX gate BLOCKED/UNVERIFIED instead of inventing a layout. A new UX state or redesign needs explicit Owner agreement. Details: `docs/development_workflow.md`.

Do not print or commit credentials, tokens, `.env`, `.agent-telemetry/`, agent transcripts or private project source. Keep ROSC code separate from NeoPlatform and DOUTLAB. Opening a Project or Task does not launch an agent; a Worker Run requires explicit Owner authorization.
