# ROSC documentation router

The [ROSC Google Drive folder](https://drive.google.com/drive/folders/1o5V94xdEBz11xo3uhPW9MZ3EccLhu2w3) owns the working product and architecture documents, including the [implementation pipeline](https://drive.google.com/file/d/1GVaW8SGFuB8F0CaxLEmPXd-HQkuU21gH/view) and [managed agent workflow](https://drive.google.com/file/d/1-1R0DAJQfQUF7gD7_RwxNNtt5VDsmbRc/view). Repository files under `docs/` describe the agent workflow and give local read-path telemetry a stable bootstrap. If a local copy disagrees with a newer canonical Drive decision, stop and resolve the drift before implementing.

## Read order

1. `docs/development_workflow.md` for repository changes and managed runs.
2. For prompt/report transport or telemetry, `docs/agent_reporting.md`.
3. For Engine behavior, ROSC [02 Architecture Notes](https://docs.google.com/document/d/1F1lUXIU5Y9FI6nFTABZ4JjtCWGDbobOR6RJHy36weUk/edit) and the specific relevant section; for UX, [08 Scenario 01](https://docs.google.com/document/d/18lc0I_A0ToHw7IFXXAX4nvsovNTTimd85aLoK0jOXmI/edit) and the approved HTML prototype.
4. For any UI or presentation work, read the UX sources in this precedence order: the [Stage 01 acceptance and UI delivery rules](https://drive.google.com/file/d/1NcHJRHeBK6daFNyGu22fxQqlD-BIy9qC/view) (`ROSC__Stage01_Acceptance_and_UI_Delivery_Rules__2026-10-31.md`), the [Frontend Foundation & Presentation Boundary](https://drive.google.com/file/d/1P5Khp6JSHYdyJPIKAXk-z5SNGpjYoalY/view), then the approved interactive [HTML v0.6](https://drive.google.com/file/d/17OZ90HLDmNwRuG8UpSA6FBdbaLl3eQZ7/view) (SHA-256 `21950ed08df67caafbcb747b05cf1eedc7e02163c718f10d85bb304ee0754c80`) as the visual and behavioral baseline. Record only what was actually opened; an inaccessible source is a stated limitation, not content to assume. Follow the UX gate in `docs/development_workflow.md`.
5. For current state, [03 Session Handoff](https://docs.google.com/document/d/1S9Qyk5MfKJfk7bQR8nKTRv0DpFPYN24_4xZU2RM-RmA/edit). Check the implementation pipeline before choosing the next slice.

Read triggered dependencies only. Search is not a substitute for the routing document. Keep `contract`, `decision`, `proposal`, `implemented`, and `verified` distinct.
