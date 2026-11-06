# Architect — Pipeline Management Rules

Status: OWNER ACCEPTED r01 · conceptual typed tools; no Core Pipeline implementation implied.

Every initialized ROSC-managed Project has a resolvable `pipeline/main` logical resource, initially `draft`/empty when no Owner-approved plan exists. The targeted active Pipeline canonical owner is ROSC Core. Any human-readable `pipeline.md` is a generated view, not a second writable master. Existing external Project roadmaps discovered at connection are read-only evidence and are not converted or adopted automatically. Earlier ROSC Drive Pipeline and its status addenda are historical product-development sources and remain untouched.

For new Project, Architect analyzes verified Owner goals and proposes a structured phase/milestone/step hierarchy through `pipeline.propose_change` (name provisional). Stable item IDs are independent of order/line number; statuses, parent/dependencies and allowed transitions follow the approved protocol. Material create/move/reorder/scope cancellation or acceptance changes require Owner decision; model-generated draft is not accepted. Derived indicators may update from verified Core events without changing approved milestones.

For existing Project, Architect reviews the discovered roadmap/README/history but starts new ROSC management Pipeline from a truthful baseline. Record links and uncertainty, never duplicate an existing source as a competing active master. If project-level import/authority ownership is later requested, it is a separate explicit Owner-reviewed operation; no default conversion.

At each Run checkpoint or Owner Task closure, read current pipeline revision and active Tasks, determine whether material change is warranted; do not rewrite Pipeline as an activity log. A closed Task or completed Run does not by itself accept a milestone. Keep Handoff compact and point to pipeline resource ID/revision rather than reproducing the full roadmap. Preserve provenance, proposal/acceptance history and CAS; report conflict, stale cache or missing Owner approval truthfully.
