# R-14 — Agent Intelligence Is for Reasoning, Not Context Plumbing

Status: **OWNER APPROVED FOUNDATIONAL PRODUCT PRINCIPLE**, 2026-11-04. Implementation contracts, APIs, storage adapters and release sequence remain **PROPOSED / NOT IMPLEMENTED**.

> Интеллект агента должен расходоваться на анализ, проектирование и принятие рабочих решений, а не на поиск файлов, чтение лишних страниц и механическую сборку контекста.
>
> Agent intelligence should be spent on analysis, design, review and decisions — not on locating files, reading irrelevant pages or mechanically assembling context.

## Core invariant

ROSC is responsible for preparing **the smallest sufficient, current, verifiable, authorized context** for an Agent's actual operation. An Agent should ask for a logical resource or a precise slice of information; ROSC resolves the current assigned Project/Task/Run, authoritative source, location, revision and access checks. No agent should need to rediscover routine paths, Drive IDs, attachment directories or document locations in every session.

**Minimum context is not an excuse to omit required evidence.** Agents must be able to expand from a brief view to exact source content, surrounding context and full artifacts when required to make or verify a consequential conclusion.

## Product implications

1. **Semantic access over location discovery.** Expose project-scoped operations for `project.get_briefing`, `pipeline.get_current`, `task.get_context`, `task.history.read`, `runs.get_report`, `docs.search`, `docs.read_section`, `docs.patch_section`, `attachments.get`. Names are illustrative interface proposals, not adopted API contracts.
2. **Progressive disclosure.** Return compact, typed responses with explicit source, revision, evidence pointers, freshness and truncation/coverage indicators. Support field selection, result limits, pagination and targeted drill-down. Never silently pretend a summary contains the entire source.
3. **Selective document reads and writes.** Use sections or stable logical anchors when an adapter can verify them. A patch must specify expected revision/preconditions, avoid rewriting unrelated content and report conflicts/unsupported operations accurately. Fallback to full-document handling is permitted where necessary; never fabricate a safe fine-grained patch capability.
4. **Tools vs Skills vs Rules.** Tools perform authorized operations; Skills describe reusable workflows and are loaded when needed; short global Role Rules state responsibility. Avoid stuffing a large catalog of tool schemas, all Skills or all project records into every prompt.
5. **Core-managed context resolution.** The agent is given a trusted active Project/Task binding. ROSC maps logical identifiers to DB records, project-owned documents, authorized third-party sources and private attachments. Physical storage paths, document backends and retrieval mechanics are implementation details, not agent-authored arguments.
6. **Preserve authority.** Core owns its Pipeline, Task History, Run and conversation state. Existing project-owned ADR/spec/README and other canonical documents remain authoritative in their own sources; search indexes and summaries are derived caches. This principle does not grant direct file modification, override Project-Owned Knowledge or erase existing external workflows.
7. **Session-independent recovery.** Durable Core state and Project-specific working context permit a newly started Architect/Worker to request a small briefing, inspect latest Task/Run evidence and progressively drill down without replaying weeks of chat history.
8. **Security and provenance at the tool boundary.** Enforce Project/Task scoping and operation authority in ROSC, not solely in agent instructions. Return clear `not_found`, `not_authorized`, `conflict`, `stale`, `unsupported`, or `partial` outcomes. Never silently read another Project or claim unverified data was checked.

## Quality bar and measurement

Assess the outcome per completed, **verified** work unit: context-input tokens, token budget consumed by tool schemas/results, retrieval/tool round trips, wall time, source freshness, missed evidence, write conflicts and Owner correction rate. Lower token count alone is not sufficient if correctness, completeness or recoverability suffers. Prefer small, reliable vertical workflows before elaborate semantic search/vector infrastructure.

## Documentation / rollout status

This file establishes an Owner-approved design invariant. `design/context-access-layer.md` develops the architectural direction; operational tool definitions, adapters, exact schemas, quotas, caching, indexing, authentication and code remain subject to subsequent engineering design and implementation approval. The current Drive workflow is not replaced by creating this document.
