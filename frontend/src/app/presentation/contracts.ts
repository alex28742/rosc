// The only surface a presentation package sees. Presentations render these typed view models and call these
// callbacks; they never reach the API client, the router or Core persistence.
import type { ComponentType, ReactNode } from 'react';
import type { AgentDefinition, AgentFormValues, ArchitectStatus, ContextAccess, ConversationMessage, GroupStatus, MessageRecipient, Project, PromptRevision, ProjectFormValues, RepairConfirmation, RoleAssignment, SpecRevision, SpecRevisionSummary, Task, TaskArchitectStatus, TaskAuthoring, TaskFormValues, ProjectWorker } from '../../core/models';
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

/** The five approved `Aa · Вид` steps. It is local UI state, never Core data. */
export const TEXT_SCALE_STEPS = [90, 100, 110, 120, 130] as const;
export type TextScale = (typeof TEXT_SCALE_STEPS)[number];

/** App-owned per-Work-Area text scale; the presentation only renders and reports a chosen step. */
export type TextScaleControls = {
  scale: TextScale;
  onChange: (scale: TextScale) => void;
};

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

/** Engine system notices are application/technical state only; Task/workflow notifications stay in their own UI. */
export type EngineNoticeKind = 'progress' | 'success' | 'warning' | 'error';

/**
 * One compact Engine popup card. `key` is the identity of its source, so a later state of the same source
 * replaces the existing card instead of stacking a duplicate.
 */
export type EngineNotice = {
  key: string;
  kind: EngineNoticeKind;
  /** Small uppercase source label, e.g. `ROSC · СИСТЕМА`. */
  kicker: string;
  title: string;
  description: string;
};

export type ShellProps = {
  children: ReactNode;
  /** Opens the approved global Settings / Agent Registry surface. */
  onOpenSettings: () => void;
  settingsOpen: boolean;
  /** Engine system popups (top-right); the controller owns their state and lifecycle. */
  engineNotices: readonly EngineNotice[];
  onDismissEngineNotice: (key: string) => void;
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
  error: string;
};

/**
 * P046 group conversation of one room (PC or this TC). `info` is exactly what Core reports; every mutation is an
 * explicit Owner action and resolves to an error message or null. Only `onStart` can launch provider turns, and only
 * for a round the Owner has already authorized.
 */
export type GroupControls = {
  status: LoadStatus;
  info: GroupStatus | null;
  error: string;
  /** The action currently in flight (one at a time), or null. */
  busy: string | null;
  onReload: () => void;
  onPost: (body: string, clientKey: string, to: string[]) => Promise<string | null>;
  onAuthorize: (agents: string[], quota: number, renew: boolean) => Promise<string | null>;
  onStart: () => Promise<string | null>;
  onControls: (change: { paused?: boolean; emergency_brake?: boolean }) => Promise<string | null>;
  onRelease: (agentId: string) => Promise<string | null>;
  onKeep: (agentId: string) => Promise<string | null>;
  /** Owner Send-full / Skip for a large Passive backlog; continues the authorized queue afterwards. */
  onCatchup: (turnId: string, choice: 'send_full' | 'skip_backlog') => Promise<string | null>;
  onReconcile: () => Promise<string | null>;
};

export type ConversationControls = {
  state: ConversationState;
  group: GroupControls;
  architect: ArchitectControlState;
  contextAccess: ContextAccessControlState;
  /** Explicit Owner activation of the Project Architect. Resolves to an error message or null. */
  onActivate: () => Promise<string | null>;
  onDeactivate: () => Promise<string | null>;
  /** Read-only Architect status re-probe after a failed load; never loads chat history or starts a runtime. */
  onRefreshArchitect: () => void;
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
  /** Independent left Work Area text scale; never shared with Task or Dashboard. */
  textScale: TextScaleControls;
  tab: ProjectTab;
  onSelectTab: (tab: ProjectTab) => void;
  onClose: () => void;
  onRetry: () => void;
  onOpenTask: (taskId: string) => void;
  onNewTask: () => void;
};

/** Task Conversation (P038): same state shape as the Project one, scoped to a single Task. */
export type TaskConversationControls = {
  state: ConversationState;
  group: GroupControls;
  /** P039: read-only Architect availability for this Project (never starts anything). */
  architect: { status: LoadStatus; info: ArchitectStatus | null };
  /** P040 r03: verified state of THIS Task's own native Architect session (read-only GET; never the Project host state). */
  taskArchitect: { status: LoadStatus; info: TaskArchitectStatus | null; busy: boolean };
  /** Explicit activation of this Task's native session; resolves to a failure message or null. Sends no message. */
  onActivateTaskArchitect: () => Promise<string | null>;
  /** Re-reads this Task's Architect status (and the Project host's, internally) from Core. */
  onRefreshArchitect: () => void;
  onReload: () => void;
  onLoadOlder: () => void;
  /**
   * Resolves to `null` once the message is stored, otherwise to an error message. A message addressed to the Architect
   * is also handed to it right away when Core reports a verified live Architect; otherwise it stays `not_sent`.
   */
  onSend: (body: string, clientKey: string, recipient: MessageRecipient) => Promise<string | null>;
  /** The explicit send of one saved Architect-addressed message. Resolves to an error message or null. */
  onSendToArchitect: (messageId: string) => Promise<string | null>;
  /** Uncertain delivery lookup (never re-sends) and safe retry (only after a provable non-delivery). */
  onReconcile: (messageId: string) => Promise<string | null>;
  onRetryDelivery: (messageId: string) => Promise<string | null>;
};

/** P040: Spec revisions, Prompt Draft revisions and the Owner approval of one exact Draft. Never starts a Run. */
export type TaskAuthoringControls = {
  status: LoadStatus;
  data: TaskAuthoring | null;
  specHistory: SpecRevisionSummary[];
  error: string;
  busy: boolean;
  onReload: () => void;
  /** Saves a new Spec revision from the edited rich text. Resolves to an error message or `null`; `created` is false when nothing changed. */
  onSaveSpec: (input: { expected_revision: number; title: string; scope: string; description: string; acceptance_criteria: string }) => Promise<{ error: string | null; created: boolean }>;
  onLoadSpecRevision: (revision: number) => Promise<SpecRevision | string>;
  /** Reads one stored Prompt revision with its body (read-only history view). Resolves to the revision or an error message. */
  onLoadPromptRevision: (revision: number) => Promise<PromptRevision | string>;
  /** Saves a new Prompt Draft revision for the current Spec. Resolves to an error message or `null`. */
  onSavePrompt: (body: string, summary: string) => Promise<string | null>;
  /** Approves the exact saved head Prompt revision (Owner only). Resolves to an error message or `null`. */
  onApprove: () => Promise<string | null>;
};

/** What one Worker row is doing right now. Per agent: a request for one Worker never blocks or labels another. */
export type WorkerBusy = 'check' | 'activate' | 'bootstrap';
/**
 * P042A: every active Project Worker is its own row with its own independent actions. Reads never start anything;
 * only `onActivate` (may start a CLI) and `onBootstrap` (asks an active CLI to read the entry points) are explicit.
 */
export type TaskWorkerControls = {
  status: LoadStatus;
  workers: ProjectWorker[];
  /** In-flight action per agent_id (absent = idle). */
  busy: Record<string, WorkerBusy>;
  /** Last failure per agent_id; cleared by that agent's next action. */
  errors: Record<string, string>;
  onCheck: (agentId: string) => Promise<void>;
  onActivate: (agentId: string) => Promise<void>;
  onBootstrap: (agentId: string) => Promise<void>;
};

export type TaskViewProps = {
  projectName: string;
  taskId: string;
  task: Task | null;
  tasksStatus: LoadStatus;
  feedback: Feedback;
  inert: boolean;
  scroll: ScrollRegions;
  workspace: WorkspaceControls;
  conversation: TaskConversationControls;
  authoring: TaskAuthoringControls;
  workers: TaskWorkerControls;
  /** Independent Task Work Area text scale; never shared with Project or Dashboard. */
  textScale: TextScaleControls;
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
