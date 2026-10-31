// The only surface a presentation package sees. Presentations render these typed view models and call these
// callbacks; they never reach the API client, the router or Core persistence.
import type { ComponentType, ReactNode } from 'react';
import type { Project, ProjectFormValues, RepairConfirmation, Task, TaskFormValues } from '../../core/models';
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
  feedback: Feedback;
  /** True while an overlay covers the Dashboard: it must not take focus or clicks. */
  inert: boolean;
  onOpenProject: (projectId: string) => void;
  onRegisterProject: () => void;
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
  onClose: () => void;
  onRetry: () => void;
  onOpenTask: (taskId: string) => void;
  onNewTask: () => void;
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
