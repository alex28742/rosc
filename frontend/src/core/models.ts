// Typed mirror of the ROSC Core JSON contract (src/rosc/app.py). Only facts the API returns live here:
// there is no Agent or Run model in Engine Slice 01.

export type WorkspaceState = 'ready' | 'needs_reconciliation';

/** `pending` = DB record not yet marked ready; the rest are manifest inspection results. */
export type WorkspaceProblem = 'pending' | 'missing' | 'oversized' | 'foreign' | 'unsafe' | 'corrupt' | 'drift';

export type Workspace = { state: WorkspaceState; problem: WorkspaceProblem | null };

export type Project = {
  id: string;
  name: string;
  description: string;
  root_path: string;
  mode: 'new' | 'existing';
  docs_entrypoint: string | null;
  workspace_state: WorkspaceState;
  workspace_problem: WorkspaceProblem | null;
  architect: string;
  active_workers: number;
  created_at: string;
};

export type Task = {
  id: string;
  project_id: string;
  title: string;
  description: string;
  acceptance_criteria: string;
  status: string;
  workspace_state: WorkspaceState;
  workspace_problem: WorkspaceProblem | null;
  worker: string;
  created_at: string;
};

export type ProjectInput = {
  name: string;
  description: string;
  root_path: string;
  mode: 'new' | 'existing';
  docs_entrypoint: string | null;
  no_external_agent_confirmed: boolean;
};

export type TaskInput = { title: string; description: string; acceptance_criteria: string };

/** Raw form strings; normalising them into a payload is Core-side (see toProjectInput). */
export type ProjectFormValues = { name: string; description: string; root_path: string; mode: string; docs_entrypoint: string; no_external_agent_confirmed: boolean };
export type TaskFormValues = { title: string; description: string; acceptance_criteria: string };

export type RepairRecord = { backup_id: string };
export type RepairResult<T> = T & { repair: RepairRecord | null };

/** Durable Project Conversation (P014). A participant is attribution history, never liveness or authority. */
export type ConversationParticipant = {
  participant_id: string;
  kind: 'owner' | 'architect' | 'worker';
  display_name: string;
  actor_id: string;
  assurance: string;
  agent_id: string | null;
  /** P046: registered display name of an Agent sender (Worker replies); absent/null for the Owner. */
  agent_display_name?: string | null;
};

/** P046: immutable P043B group envelope of a message. `to`/`audience` hold stable Agent ids or `owner`. */
export type MessageGroup = {
  to: string[];
  audience: string[];
  reply_to: string | null;
  reply_to_ordinal: number | null;
  round_id: string | null;
  turn_id: string | null;
  sender_agent_id: string | null;
};

/** Delivery of one Owner message to the Project Architect (P015). `not_sent` means it was only stored in ROSC. */
export type DeliveryState = 'not_sent' | 'pending' | 'delivered' | 'failed' | 'unknown';
export type MessageDelivery = {
  state: DeliveryState;
  error_code: string | null;
  reply_message_id: string | null;
  can_retry: boolean;
  can_reconcile: boolean;
};

export type ConversationMessage = {
  message_id: string;
  conversation_id: string;
  ordinal: number;
  body: string;
  created_at: string;
  sender: ConversationParticipant;
  /** Present on Owner messages only. */
  delivery?: MessageDelivery;
  /** P038: the addressee the Owner chose in a Task Conversation. Stored intent only, never a delivery. */
  recipient?: MessageRecipient;
  /** P046: group addressing (null for a legacy message, whose addressing is `recipient`). */
  group?: MessageGroup | null;
};
export type MessageRecipient = 'architect' | null;

/** P046 group conversation (Core-reported state of one PC/TC room). Nothing here is inferred by the browser. */
export type GroupRosterEntry = {
  agent_id: string;
  display_name: string;
  icon_label: string | null;
  role: 'architect' | 'worker';
  runtime_adapter: string;
  model_id: string;
  /** True only when a real group transport exists for this Agent (v1.0: Claude Code Workers). */
  group_supported: boolean;
  unsupported_reason: string | null;
};
export type GroupTurnState = 'queued' | 'reserved' | 'submitted' | 'accepted' | 'reply_captured' | 'published'
  | 'deferred' | 'failed' | 'unknown' | 'cancelled';
export type GroupTurn = {
  turn_id: string; round_id: string; agent_id: string; state: GroupTurnState; pass: number; position: number;
  attempt: number; lane_id: string | null; error_code: string | null; catchup_choice: string | null;
  input_after_ordinal: number | null; input_through_ordinal: number | null; reply_message_id: string | null;
};
export type GroupRound = {
  round_id: string; round_no: number; state: 'open' | 'awaiting_owner' | 'closed'; per_agent_quota: number;
  current_pass: number; grants: Record<string, { quota: number; published: number; failed: boolean }>; turns: GroupTurn[];
};
export type GroupMember = {
  agent_id: string; role: 'architect' | 'worker'; state: 'active' | 'passive' | 'released'; epoch: number;
  applied_through: number; considered_through: number; omitted_ranges: number[][]; missed: number;
  release_offer: { missed: number; step: number; threshold: number } | null;
  lane: { lane_id: string; native_session_id: string; root_binding_version: number } | null;
};
export type GroupCatchup = { turn_id: string; agent_id: string; from: number; through: number; chars: number; limit: number; count: number };
/** `cli_confirmed`: the CLI's own init event named this lane's session and the process is alive; never inferred from tmux. */
export type GroupActivity = 'cli_confirmed' | 'awaiting_confirmation' | null;
export type GroupStatus = {
  conversation_id: string | null;
  roster: GroupRosterEntry[];
  names: Record<string, string>;
  settings: Record<string, number>;
  has_owner_message: boolean;
  controls: { paused: boolean; emergency_brake: boolean };
  round: GroupRound | null;
  holding_turn: GroupTurn | null;
  activity: GroupActivity;
  answering: string | null;
  waiting: string[];
  failed: GroupTurn[];
  members: GroupMember[];
  catchup: GroupCatchup | null;
  driver: { running: boolean; last: { state: string; reason?: string; agent_id?: string | null; error_code?: string | null; at: string } | null };
};
export type GroupMessageInput = { client_key: string; body: string; to: string[] };

/** Verified Project Architect runtime state. `ready` is reported by Core only after a fresh liveness probe. */
export type ArchitectState = 'not_configured' | 'stopped' | 'starting' | 'ready' | 'error';
export type ArchitectStatus = {
  project_id: string;
  state: ArchitectState;
  detail: string | null;
  can_activate: boolean;
  can_deactivate: boolean;
  agent: { agent_id: string; display_name: string; provider_id: string; model_id: string } | null;
  session: { session_id: string; state: string; verified_at: string | null } | null;
  /** P029: ROSC Tools are reported separately; a live session stays `ready` whether or not they are connected. */
  tools: { state: 'active' | 'inactive' | 'not_running' | 'not_available'; detail: string | null };
  /** P029: one-time model onboarding of a NEW native session. `legacy_unverified` = created before P029. */
  onboarding: { state: OnboardingState | null; detail: string | null };
};
/** P040 r03: verified state of ONE Task's native Architect session. `active` only after Core's runtime probe; the Project host being ready is a different fact. */
export type TaskArchitectState = 'inactive' | 'active' | 'error';
export type TaskArchitectStatus = {
  project_id: string;
  task_id: string;
  state: TaskArchitectState;
  detail: string | null;
  can_activate: boolean;
};
export type OnboardingState = 'legacy_unverified' | 'initializing' | 'ready' | 'blocked' | 'unknown';

/** Effective Architect access to context (P019). The matrix is Core's statement of what the ROSC Tools allow. */
export type ContextAccess = {
  project_id: string;
  matrix: { resource: string; operations: string; mode: 'read_only' | 'read_write' | 'denied' }[];
  tools: { transport: 'active' | 'inactive' | 'not_running' | 'not_available'; names: string[] };
  source: { bound: boolean; binding_version: number; docs_entrypoint: string | null; access: 'read_only' };
  provider: { provider_id: string | null; model_id: string | null; note: string };
  publication: { automatic_sync: false; architect_git_operations: false; note: string };
};

export type ProjectConversation = {
  conversation_id: string;
  kind: string;
  project_id: string;
  task_id: string | null;
  created_at: string;
  participants: ConversationParticipant[];
};

export type ConversationPage = {
  project_id: string;
  task_id?: string | null;
  conversation: ProjectConversation | null;
  messages: ConversationMessage[];
  page: { limit: number; has_more: boolean; next_before: number | null };
};

export type ConversationMessageInput = { client_key: string; body: string; recipient?: MessageRecipient };
export type ConversationMessageResult = { message: ConversationMessage; replayed: boolean };
export type DeliveryResult = { delivery: MessageDelivery };

/** Can only be produced by an explicit user confirmation; the repair endpoint requires it. */
export type RepairConfirmation = { readonly confirmed: true };

/** Agent Definition (P011): administrative metadata. `runtime_state` is always `unverified` here. */
export type AgentCapabilityDeclaration = { capability: string; status: 'declared' };

export type AgentDefinition = {
  agent_id: string;
  display_name: string;
  icon_label: string | null;
  runtime_adapter: string;
  provider_id: string;
  model_id: string;
  capabilities: AgentCapabilityDeclaration[];
  runtime_state: 'unverified';
  version: number;
  created_at: string;
  updated_at: string;
};

/** Full registration metadata; `capabilities` are declarations, never runtime proof or tool permissions. */
export type AgentInput = {
  display_name: string;
  icon_label: string | null;
  runtime_adapter: string;
  provider_id: string;
  model_id: string;
  capabilities: string[];
};

/** The Core `PATCH` is a full metadata replacement guarded by the last seen version. */
export type AgentUpdateInput = AgentInput & { expected_version: number };

/** Raw form strings; the controller builds the Core payload through `toAgentInput`. */
export type AgentFormValues = {
  display_name: string;
  icon_label: string;
  runtime_adapter: string;
  provider_id: string;
  model_id: string;
  capabilities: string[];
};

/** Bounded Project/Task intent. `readiness` and `runtime_effect` are Core facts, not liveness. */
export type RoleAssignment = {
  assignment_id: string;
  agent_id: string;
  project_id: string;
  task_id: string | null;
  role: 'architect' | 'worker';
  scope: 'project' | 'task';
  state: 'active' | 'revoked';
  readiness: 'unverified' | 'blocked';
  capabilities: string[];
  runtime_effect: 'none';
  version: number;
  created_at: string;
  updated_at: string;
  revoked_at: string | null;
  created_by: string;
  created_by_assurance: string;
  revoked_by: string | null;
  revoked_by_assurance: string | null;
};

/** Project-scope assignment only: P016 never assigns a Task-scoped role. */
export type AssignmentInput = {
  agent_id: string;
  role: 'architect' | 'worker';
  task_id: null;
  capabilities: string[];
};

export function toProjectInput(values: ProjectFormValues): ProjectInput {
  return {
    name: values.name, description: values.description, root_path: values.root_path,
    mode: values.mode === 'new' ? 'new' : 'existing',
    docs_entrypoint: values.docs_entrypoint.trim() || null,
    no_external_agent_confirmed: values.no_external_agent_confirmed,
  };
}

export function toTaskInput(values: TaskFormValues): TaskInput {
  return { title: values.title, description: values.description, acceptance_criteria: values.acceptance_criteria };
}

export function toAgentInput(values: AgentFormValues): AgentInput {
  return {
    display_name: values.display_name.trim(),
    icon_label: values.icon_label.trim() || null,
    runtime_adapter: values.runtime_adapter.trim(),
    provider_id: values.provider_id.trim(),
    model_id: values.model_id.trim(),
    capabilities: [...new Set(values.capabilities)],
  };
}

export function toAgentUpdateInput(values: AgentFormValues, expectedVersion: number): AgentUpdateInput {
  return { ...toAgentInput(values), expected_version: expectedVersion };
}

/** P040 Task authoring. Rich text is canonical whitelist HTML produced by Core; still re-sanitised on render. */
export type AuthoringActor = { kind: string; id: string };
export type SpecRevision = {
  revision: number; title: string; scope: string; description_html: string; criteria_html: string;
  description_text: string; acceptance_criteria_text: string; digest: string; provenance: 'owner' | 'architect' | 'legacy_import';
  actor: AuthoringActor; created_at: string; current: boolean;
};
export type SpecRevisionSummary = Pick<SpecRevision, 'revision' | 'title' | 'scope' | 'digest' | 'provenance' | 'actor' | 'created_at' | 'current'>;
export type PromptState = 'unapproved' | 'stale' | 'approved' | 'superseded';
export type PromptApprovalRecord = { spec_revision: number; prompt_revision: number; digest: string; actor: AuthoringActor; created_at: string };
export type PromptRevision = {
  revision: number; source_spec_revision: number; summary: string; body?: string; digest: string; rules_ref: string | null;
  state: PromptState; actor: AuthoringActor; created_at: string; approval: PromptApprovalRecord | null; current?: boolean;
};
export type NextAction = {
  action: 'none' | 'create_prompt' | 'review_prompt' | 'update_prompt' | 'authorize_run';
  label: string; enabled: boolean; next_actor: string; hint: string; secondary?: string;
};
export type TaskAuthoring = {
  task_id: string; editable: boolean; spec: SpecRevision; prompt: PromptRevision | null; prompt_history: PromptRevision[];
  run: { authorized: boolean; label: string; worker: string }; next: NextAction;
};
export type SpecSaveInput = { expected_revision: number; title: string; scope?: string | null; description: string; acceptance_criteria: string };
export type PromptSaveInput = { expected_revision: number | null; source_spec_revision: number; body: string; summary: string };

/** P042A: observed state of one Project+Worker external CLI session. `active` means the CLI initialised a session (verified live); it is NOT "task accepted". */
export type WorkerSessionState = 'not_assigned' | 'not_connected' | 'connecting' | 'active' | 'unavailable' | 'unknown';
/** Project-context bootstrap: evidence recorded by the CLI's own hooks. `confirmed` is NOT "chat-ready" or "run-ready". */
export type WorkerBootstrapState = 'none' | 'pending' | 'confirmed' | 'failed' | 'unknown' | 'stale';
export type WorkerBootstrap = {
  state: WorkerBootstrapState;
  reason: string;
  can_bootstrap: boolean;
  /** Project-relative entry-point files that were (or would be) read. */
  sources: string[];
  confirmed_at: string | null;
};
export type WorkerSession = {
  project_id: string;
  agent_id: string;
  state: WorkerSessionState;
  /** Non-secret reason code (never a tmux/process identifier). */
  reason: string;
  can_activate: boolean;
  can_check: boolean;
  reused: boolean;
  initialised_at: string | null;
  checked_at: string;
  bootstrap: WorkerBootstrap;
  /** Server-verified attach command for this ROSC-owned session (never built in the browser); null when unverifiable. */
  terminal: { attach_command: string } | null;
  evidence: { tmux_alive: boolean | null; cli_observable: boolean | null; cli_session_initialised: boolean };
};
export type ProjectWorker = {
  agent: Pick<AgentDefinition, 'agent_id' | 'display_name' | 'icon_label' | 'runtime_adapter' | 'provider_id' | 'model_id'>;
  session: WorkerSession;
};
