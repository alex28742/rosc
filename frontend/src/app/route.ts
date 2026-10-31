// Route ownership: URL <-> Route. Pure functions only; history/popstate wiring lives in navigation.ts.

export type Route =
  | { kind: 'dashboard' }
  | { kind: 'project'; projectId: string }
  | { kind: 'new-task'; projectId: string }
  | { kind: 'task'; projectId: string; taskId: string };

const DASHBOARD: Route = { kind: 'dashboard' };

function decode(piece: string): string | null {
  try { return decodeURIComponent(piece); } catch { return null; }
}

/**
 * Supported: /, /projects/:id, /projects/:id/new-task, /projects/:id/tasks/:taskId (one trailing slash tolerated).
 * Anything else falls back to the Dashboard instead of being coerced into a Project or Task view.
 */
export function parseRoute(pathname: string): Route {
  const pieces = pathname.replace(/\/$/, '').split('/').slice(1);
  if (pieces.length === 1 && pieces[0] === '') return DASHBOARD;
  if (pieces[0] !== 'projects' || pieces.length < 2 || pieces.length > 4 || pieces.includes('')) return DASHBOARD;
  const ids = pieces.slice(1).map(decode);
  if (ids.includes(null)) return DASHBOARD;
  const projectId = ids[0]!;
  if (pieces.length === 2) return { kind: 'project', projectId };
  if (pieces.length === 3 && pieces[2] === 'new-task') return { kind: 'new-task', projectId };
  if (pieces.length === 4 && pieces[2] === 'tasks') return { kind: 'task', projectId, taskId: ids[2]! };
  return DASHBOARD;
}

export function routePath(route: Route): string {
  switch (route.kind) {
    case 'dashboard': return '/';
    case 'project': return `/projects/${encodeURIComponent(route.projectId)}`;
    case 'new-task': return `/projects/${encodeURIComponent(route.projectId)}/new-task`;
    case 'task': return `/projects/${encodeURIComponent(route.projectId)}/tasks/${encodeURIComponent(route.taskId)}`;
  }
}

export const projectIdOf = (route: Route): string | null => (route.kind === 'dashboard' ? null : route.projectId);

/** The route one layer up: Task and new Task close to their Project, a Project closes to the Dashboard. */
export function parentRoute(route: Route): Route {
  return route.kind === 'dashboard' ? route : route.kind === 'project' ? DASHBOARD : { kind: 'project', projectId: route.projectId };
}

export const sameRoute = (a: Route, b: Route): boolean => routePath(a) === routePath(b);
