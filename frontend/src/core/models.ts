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
};

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
};

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
  task_id: number | null;
  created_at: string;
  participants: ConversationParticipant[];
};

export type ConversationPage = {
  project_id: string;
  conversation: ProjectConversation | null;
  messages: ConversationMessage[];
  page: { limit: number; has_more: boolean; next_before: number | null };
};

export type ConversationMessageInput = { client_key: string; body: string };
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
