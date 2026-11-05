// The only surface a presentation package sees. Presentations render these typed view models and call these
// callbacks; they never reach the API client, the router or Core persistence.
import type { ComponentType, ReactNode } from 'react';
import type { AgentDefinition, AgentFormValues, ArchitectStatus, ContextAccess, DisclosureCategory, ConversationMessage, Project, ProjectFormValues, RepairConfirmation, RoleAssignment, Task, TaskFormValues } from '../../core/models';
import type { ScrollRegions } from '../scroll';

export type LoadStatus = 'loading' | 'ready' | 'failed';

/** Messages for this layer only; empty strings when the message belongs to a different layer. */
export type Feedback = { error: string; notice: string };

/** The closed, provider-neutral capability vocabulary of the Core (declarations only). */
export const AGENT_CAPABILITIES: ReadonlyArray<{ id: string; label: string; optional: boolean }> = [
  { id: 'project.context.read', label: 'Чтение контекста проекта', optional: false },
  { id: 'task.context.read', label: 'Чтение контекста Task', optional: false },
  { id: 'architect.document.read', label: 'Чтение документов Architect', optional: false },
  { id: 'architect.document.write', label: 'Редактирование документов Architect', optional: false },
  { id: 'task.artifact.publish', label: 'Публикация Task-артефактов (опционально)', optional: true },
];

/** Capabilities a role assignment must request; the same set Core enforces. */
export const ROLE_REQUIRED_CAPABILITIES: Record<'architect' | 'worker', readonly string[]> = {
  architect: ['project.context.read', 'architect.document.read', 'architect.document.write'],
  worker: ['project.context.read', 'task.context.read'],
};

/** Safe glyph mapping for the prototype icon keys; an unknown Core label falls back to a neutral dot. */
export const AGENT_ICON_GLYPHS: Readonly<Record<string, string>> = {
  diamond: '◆', star: '✳', command: '⌘', robot: '🤖', gear: '⚙', brain: '🧠',
};

export const AGENT_RUNTIME_OPTIONS: ReadonlyArray<{ id: string; label: string }> = [
  { id: 'opencode', label: 'OpenCode' },
  { id: 'claude-code', label: 'Claude Code' },
  { id: 'codex-cli', label: 'Codex CLI' },
  { id: 'other', label: 'Другой / не интегрирован' },
];

export const agentIcon = (iconLabel: string | null): string =>
  (iconLabel ? AGENT_ICON_GLYPHS[iconLabel] : undefined) ?? '·';

export const runtimeLabel = (runtimeAdapter: string): string =>
  AGENT_RUNTIME_OPTIONS.find(option => option.id === runtimeAdapter)?.label ?? runtimeAdapter;

/** An Agent may take a role only when it declares every required capability (Core still decides). */
export const roleEligible = (agent: AgentDefinition, role: 'architect' | 'worker'): boolean => {
  const declared = new Set(agent.capabilities.map(item => item.capability));
  return ROLE_REQUIRED_CAPABILITIES[role].every(capability => declared.has(capability));
};

export type CapabilityPreset = 'none' | 'architect' | 'worker' | 'both' | 'custom';

export function capabilityPreset(capabilities: readonly string[]): CapabilityPreset {
  const names = [...new Set(capabilities)].sort().join(',');
  if (!names) return 'none';
  const matches = (role: 'architect' | 'worker') => [...ROLE_REQUIRED_CAPABILITIES[role]].sort().join(',') === names;
  if (matches('architect')) return 'architect';
  if (matches('worker')) return 'worker';
  const both = [...new Set([...ROLE_REQUIRED_CAPABILITIES.architect, ...ROLE_REQUIRED_CAPABILITIES.worker])].sort().join(',');
  return names === both ? 'both' : 'custom';
}

export type ProjectTab = 'tasks' | 'settings';

/** Explicit Owner intent for one Project save; the controller computes the minimal delta against fresh Core records. */
export type AssignmentIntent = {
  architectAgentId: string | null;
  workerAgentIds: string[];
  /** `assignmentBaseline` of the authoritative set the Owner's selection was made against. */
  baseline: string;
};

/**
 * Order-independent fingerprint of the active Project-scoped assignments (Task-scoped records are never part of
 * Project edit intent). A save is only allowed when fresh Core state still has the exact fingerprint the Owner saw.
 */
export function assignmentBaseline(list: RoleAssignment[]): string {
  return list
    .filter(item => item.state === 'active' && item.scope === 'project')
    .map(item => [item.assignment_id, item.agent_id, item.role, item.scope, item.version, item.readiness].join(':'))
    .sort()
    .join('|');
}

export type AgentRegistryState = {
  status: LoadStatus;
  agents: AgentDefinition[];
  /** Load or mutation failure; a mutation failure never carries a success notice. */
  error: string;
  notice: string;
  busy: boolean;
};

export type SettingsViewProps = {
  state: AgentRegistryState;
  /** Loaded Projects are the only ones a truthful assignment link may point at. */
  projects: Project[];
  onRetry: () => void;
  /** Resolves to an error message or null; the view keeps the draft on failure. */
  onSaveAgent: (agentId: string | null, values: AgentFormValues) => Promise<string | null>;
  onOpenProjectSettings: (projectId: string) => void;
  onClose: () => void;
};

export type ProjectAssignmentsState = {
  status: LoadStatus;
  agents: AgentDefinition[];
  assignments: RoleAssignment[];
  /** Read-only Core status used only as a change gate; null means it could not be established. */
  architect: ArchitectStatus | null;
  architectKnown: boolean;
  error: string;
  notice: string;
  busy: boolean;
  /** Changes whose outcome is unconfirmed after a partial failure; never reported as saved. */
  unresolved: string[];
};

export type ProjectAssignmentsControls = {
  state: ProjectAssignmentsState;
  onRetry: () => void;
  onSave: (intent: AssignmentIntent, confirmArchitectChange: boolean) => Promise<string | null>;
  onOpenRegistry: () => void;
};


/** Workspace manifest actions shared by Project and Task. `onRepair` is only reachable with an explicit confirmation. */
export type WorkspaceControls = {
  busy: boolean;
  onReconcile: () => void;
  onRepair: (confirmation: RepairConfirmation) => void;
};

export type ShellProps = {
  children: ReactNode;
  /** Opens the approved global Settings / Agent Registry surface. */
  onOpenSettings: () => void;
  settingsOpen: boolean;
};

export type DashboardViewProps = {
  projects: Project[];
  /** Whether `projects` is a loaded list: the empty copy is only truthful when `ready`. Cached rows may remain on `failed`. */
  projectsStatus: LoadStatus;
  feedback: Feedback;
  /** True while an overlay covers the Dashboard: it must not take focus or clicks. */
  inert: boolean;
  onOpenProject: (projectId: string) => void;
  onRegisterProject: () => void;
  /** Reloads the Project list only; never mutates. */
  onRetry: () => void;
};

/** Durable Project Conversation state for the open Project. `messages` are already ordered oldest first. */
export type ConversationState = {
  status: LoadStatus;
  conversationId: string | null;
  messages: ConversationMessage[];
  /** True while older messages exist beyond the loaded page; `nextBefore` is the cursor for them. */
  hasMore: boolean;
  nextBefore: number | null;
  /** Load failure; never carries message text. */
  error: string;
  sending: boolean;
  olderLoading: boolean;
};

/**
 * Conversation actions owned by the application controller. `onSend` resolves to `null` once the message is
 * persisted and to an error message otherwise, so a view can keep the draft and report failure truthfully.
 */
export type ArchitectControlState = {
  status: LoadStatus;
  info: ArchitectStatus | null;
  /** True while an explicit activate/deactivate request is in flight ("activating"). */
  busy: boolean;
  error: string;
};

export type ContextAccessControlState = {
  status: LoadStatus;
  info: ContextAccess | null;
  busy: boolean;
  error: string;
};

export type ConversationControls = {
  state: ConversationState;
  architect: ArchitectControlState;
  contextAccess: ContextAccessControlState;
  /** Owner-only standing disclosure toggle. Resolves to an error message or null. */
  onSetDisclosure: (category: DisclosureCategory, enabled: boolean) => Promise<string | null>;
  /** Explicit Owner activation of the Project Architect. Resolves to an error message or null. */
  onActivate: () => Promise<string | null>;
  onDeactivate: () => Promise<string | null>;
  /** Uncertain delivery lookup (never re-sends) and safe retry (only after a provable non-delivery). */
  onReconcile: (messageId: string) => Promise<string | null>;
  onRetryDelivery: (messageId: string) => Promise<string | null>;
  onReload: () => void;
  onLoadOlder: () => void;
  onSend: (body: string, clientKey: string) => Promise<string | null>;
};

export type ProjectViewProps = {
  projectId: string;
  /** null while loading, failed, or when the id does not exist; `status` tells which. */
  project: Project | null;
  projectStatus: LoadStatus;
  tasks: Task[];
  tasksStatus: LoadStatus;
  feedback: Feedback;
  inert: boolean;
  scroll: ScrollRegions;
  workspace: WorkspaceControls;
  conversation: ConversationControls;
  assignments: ProjectAssignmentsControls;
  tab: ProjectTab;
  onSelectTab: (tab: ProjectTab) => void;
  onClose: () => void;
  onRetry: () => void;
  onOpenTask: (taskId: string) => void;
  onNewTask: () => void;
};

export type TaskViewProps = {  projectName: string;
  taskId: string;
  task: Task | null;
  tasksStatus: LoadStatus;
  feedback: Feedback;
  inert: boolean;
  scroll: ScrollRegions;
  workspace: WorkspaceControls;
  onClose: () => void;
  onRetry: () => void;
};

export type NewTaskViewProps = {
  projectName: string;
  feedback: Feedback;
  busy: boolean;
  inert: boolean;
  onSubmit: (values: TaskFormValues) => void;
  onClose: () => void;
};

export type RegisterProjectViewProps = {
  feedback: Feedback;
  busy: boolean;
  onSubmit: (values: ProjectFormValues) => void;
  onClose: () => void;
};

/** A presentation package: logical views plus the shell that frames them. */
export type Presentation = {
  readonly id: string;
  Shell: ComponentType<ShellProps>;
  Dashboard: ComponentType<DashboardViewProps>;
  Project: ComponentType<ProjectViewProps>;
  Task: ComponentType<TaskViewProps>;
  NewTask: ComponentType<NewTaskViewProps>;
  RegisterProject: ComponentType<RegisterProjectViewProps>;
  Settings: ComponentType<SettingsViewProps>;
};
