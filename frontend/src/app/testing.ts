// Test fixtures shared by the application tests. Not imported by production code.
import { vi } from 'vitest';
import type { CoreApi } from '../core/api';
import type { ArchitectStatus, ConversationMessage, ConversationPage, ConversationParticipant, ConversationMessageInput, Project, Task } from '../core/models';

export const project = (id: string, over: Partial<Project> = {}): Project => ({
  id, name: `Project ${id}`, description: `About ${id}`, root_path: `/tmp/${id}`, mode: 'existing', docs_entrypoint: null,
  workspace_state: 'ready', workspace_problem: null, architect: 'not_active', active_workers: 0, created_at: '2026-10-31T00:00:00Z', ...over,
});

export const task = (id: string, projectId: string, over: Partial<Task> = {}): Task => ({
  id, project_id: projectId, title: `Task ${id}`, description: `Do ${id}`, acceptance_criteria: `Done ${id}`, status: 'Draft',
  workspace_state: 'ready', workspace_problem: null, worker: 'not_assigned', created_at: '2026-10-31T00:00:00Z', ...over,
});

export const ownerParticipant = (over: Partial<ConversationParticipant> = {}): ConversationParticipant => ({
  participant_id: 'participant-owner', kind: 'owner', display_name: 'Owner', actor_id: 'local-owner',
  assurance: 'local_unverified', agent_id: null, ...over,
});

let messageSequence = 0;

export const conversationMessage = (body: string, over: Partial<ConversationMessage> = {}): ConversationMessage => {
  const ordinal = over.ordinal ?? ++messageSequence;
  return {
    message_id: `message-${ordinal}`, conversation_id: 'conversation-1', ordinal, body,
    created_at: '2026-11-03T10:00:00Z', sender: ownerParticipant(), ...over,
  };
};

export const conversationPage = (over: Partial<ConversationPage> = {}): ConversationPage => ({
  project_id: 'P0001', conversation: null, messages: [],
  page: { limit: 50, has_more: false, next_before: null }, ...over,
});

export const architectStatus = (over: Partial<ArchitectStatus> = {}): ArchitectStatus => ({
  project_id: 'P0001', state: 'stopped', detail: null, can_activate: true, can_deactivate: false,
  agent: { agent_id: 'agent-1', display_name: 'Architect One', provider_id: 'prov', model_id: 'model-a' },
  session: null, ...over,
});

export function stubApi(over: Partial<{ [K in keyof CoreApi]: ReturnType<typeof vi.fn> }> = {}) {
  const api = {
    listProjects: vi.fn(async () => [] as Project[]),
    createProject: vi.fn(async () => project('P0001')),
    reconcileProject: vi.fn(async () => project('P0001')),
    repairProject: vi.fn(async () => ({ ...project('P0001'), repair: null })),
    listTasks: vi.fn(async () => [] as Task[]),
    createTask: vi.fn(async () => task('T0001', 'P0001')),
    reconcileTask: vi.fn(async () => task('T0001', 'P0001')),
    repairTask: vi.fn(async () => ({ ...task('T0001', 'P0001'), repair: null })),
    getConversation: vi.fn(async () => conversationPage()),
    getArchitect: vi.fn(async () => architectStatus()),
    activateArchitect: vi.fn(async () => architectStatus({ state: 'ready', can_activate: false, can_deactivate: true })),
    deactivateArchitect: vi.fn(async () => architectStatus()),
    reconcileDelivery: vi.fn(async () => ({ delivery: { state: 'unknown', error_code: null, reply_message_id: null, can_retry: false, can_reconcile: true } })),
    retryDelivery: vi.fn(async () => ({ delivery: { state: 'pending', error_code: null, reply_message_id: null, can_retry: false, can_reconcile: true } })),
    sendConversationMessage: vi.fn(async (_projectId: string, input: ConversationMessageInput) => ({
      message: conversationMessage(input.body), replayed: false,
    })),
    ...over,
  };
  return api as typeof api & CoreApi;
}

export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
