import './style.css';
import type { Presentation } from '../../app/presentation/contracts';
import { Shell } from './Shell';
import { DashboardView } from './views/DashboardView';
import { NewTaskView } from './views/NewTaskView';
import { ProjectView } from './views/ProjectView';
import { RegisterProjectView } from './views/RegisterProjectView';
import { TaskView } from './views/TaskView';

const presentation: Presentation = {
  id: 'default',
  Shell,
  Dashboard: DashboardView,
  Project: ProjectView,
  Task: TaskView,
  NewTask: NewTaskView,
  RegisterProject: RegisterProjectView,
};

export default presentation;
