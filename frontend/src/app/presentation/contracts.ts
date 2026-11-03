// The only surface a presentation package sees. Presentations render these typed view models and call these
// callbacks; they never reach the API client, the router or Core persistence.
import type { ComponentType, ReactNode } from 'react';
import type { ArchitectStatus, ConversationMessage, Project, ProjectFormValues, RepairConfirmation, Task, TaskFormValues } from '../../core/models';
import type { ScrollRegions } from '../scroll';

export type LoadStatus = 'loading' | 'ready' | 'failed';

/** Messages for this layer only; empty strings when the message belongs to a different layer. */
export type Feedback = { error: string; notice: string };

/** Workspace manifest actions shared by Project and Task. `onRepair` is only reachable with an explicit confirmation. */
export type WorkspaceControls = {
  busy: boolean;
  onReconcile: () => void;
  onRepair: (confirmation: RepairConfirmation) => void;
};

export type ShellProps = { children: ReactNode };

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

export type ConversationControls = {
  state: ConversationState;
  architect: ArchitectControlState;
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
};
