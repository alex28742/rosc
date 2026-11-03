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

export type ConversationMessage = {
  message_id: string;
  conversation_id: string;
  ordinal: number;
  body: string;
  created_at: string;
  sender: ConversationParticipant;
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

/** Can only be produced by an explicit user confirmation; the repair endpoint requires it. */
export type RepairConfirmation = { readonly confirmed: true };

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
