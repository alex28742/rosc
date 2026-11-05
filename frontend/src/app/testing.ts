// Test fixtures shared by the application tests. Not imported by production code.
import { vi } from 'vitest';
import type { CoreApi } from '../core/api';
import type { AgentDefinition, ArchitectStatus, ContextAccess, ConversationMessage, ConversationPage, ConversationParticipant, ConversationMessageInput, Project, RoleAssignment, Task } from '../core/models';

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

export const contextAccess = (over: Partial<ContextAccess> = {}): ContextAccess => ({
  project_id: 'P0001',
  matrix: [
    { resource: 'global rules/skills/tools', operations: 'list, read', mode: 'read_only' },
    { resource: "this Project's ROSC-private documents", operations: 'list, search, read, create, update, delete (recoverable)', mode: 'read_write' },
    { resource: "other registered Projects' ROSC-private documents", operations: 'list, search, read', mode: 'read_only' },
    { resource: "this Project's bound source checkout", operations: 'list, search, read, git head/status/log', mode: 'read_only' },
    { resource: 'other Projects\' source, secrets, host directories, shell, publishing', operations: 'none', mode: 'denied' },
  ],
  tools: { transport: 'not_running', names: ['rosc_briefing'] },
  source: { bound: true, binding_version: 1, docs_entrypoint: 'README.md', access: 'read_only' },
  disclosure: { default: 'none', categories: ['project_docs_own', 'project_docs_other', 'project_source'],
    provider_id: 'prov', model_id: 'model-a', scopes: [], note: '' },
  publication: { automatic_sync: false, architect_git_operations: false, note: '' }, ...over,
});

const declared = (capabilities: string[]) => capabilities.map(capability => ({ capability, status: 'declared' as const }));

export const agentDefinition = (id = 'agent-1', over: Partial<AgentDefinition> = {}): AgentDefinition => ({
  agent_id: id, display_name: `Agent ${id}`, icon_label: 'robot', runtime_adapter: 'opencode',
  provider_id: 'prov', model_id: 'model-a',
  capabilities: declared(['project.context.read', 'architect.document.read', 'architect.document.write']),
  runtime_state: 'unverified', version: 1, created_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-01T00:00:00Z', ...over,
});

export const roleAssignment = (id: string, over: Partial<RoleAssignment> = {}): RoleAssignment => ({
  assignment_id: id, agent_id: 'agent-1', project_id: 'P0001', task_id: null, role: 'worker', scope: 'project',
  state: 'active', readiness: 'unverified', capabilities: ['project.context.read', 'task.context.read'],
  runtime_effect: 'none', version: 1, created_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-01T00:00:00Z',
  revoked_at: null, created_by: 'local-owner', created_by_assurance: 'local_unverified',
  revoked_by: null, revoked_by_assurance: null, ...over,
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
    listAgents: vi.fn(async () => [] as AgentDefinition[]),
    getAgent: vi.fn(async () => agentDefinition()),
    registerAgent: vi.fn(async () => agentDefinition('agent-2')),
    updateAgent: vi.fn(async () => agentDefinition()),
    listAssignments: vi.fn(async () => [] as RoleAssignment[]),
    createAssignment: vi.fn(async () => roleAssignment('assignment-1')),
    revokeAssignment: vi.fn(async () => roleAssignment('assignment-1', { state: 'revoked', version: 2 })),
    getArchitect: vi.fn(async () => architectStatus()),
    activateArchitect: vi.fn(async () => architectStatus({ state: 'ready', can_activate: false, can_deactivate: true })),
    deactivateArchitect: vi.fn(async () => architectStatus()),
    getContextAccess: vi.fn(async () => contextAccess()),
    setContextDisclosure: vi.fn(async () => contextAccess()),
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
