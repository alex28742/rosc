import { describe, expect, it } from 'vitest';
import { parentRoute, parseRoute, projectIdOf, routePath, type Route } from './route';

describe('parseRoute', () => {
  it.each<[string, Route]>([
    ['/', { kind: 'dashboard' }],
    ['/projects/P0001', { kind: 'project', projectId: 'P0001' }],
    ['/projects/P0001/', { kind: 'project', projectId: 'P0001' }],
    ['/projects/P0001/new-task', { kind: 'new-task', projectId: 'P0001' }],
    ['/projects/P0001/tasks/T0007', { kind: 'task', projectId: 'P0001', taskId: 'T0007' }],
    ['/projects/P%2F1', { kind: 'project', projectId: 'P/1' }],
  ])('%s', (path, expected) => expect(parseRoute(path)).toEqual(expected));

  it.each(['/projects', '/projects/', '/projects//tasks/T1', '/projects/P1/tasks', '/projects/P1/tasks/T1/extra', '/projects/P1/bogus', '/projects/P1/new-task/x', '/other', '/projects/%E0'])(
    'falls back to the Dashboard for unsupported %s instead of coercing a Project/Task view', path => {
      expect(parseRoute(path)).toEqual({ kind: 'dashboard' });
    });
});

describe('routePath', () => {
  it('round-trips every supported route', () => {
    const routes: Route[] = [
      { kind: 'dashboard' }, { kind: 'project', projectId: 'P0001' }, { kind: 'new-task', projectId: 'P0001' },
      { kind: 'task', projectId: 'P0001', taskId: 'T0002' }, { kind: 'project', projectId: 'a b/c' },
    ];
    for (const r of routes) expect(parseRoute(routePath(r))).toEqual(r);
  });
});

describe('layer navigation', () => {
  it('closes Task and new Task to their Project, Project to the Dashboard', () => {
    expect(parentRoute({ kind: 'task', projectId: 'P1', taskId: 'T1' })).toEqual({ kind: 'project', projectId: 'P1' });
    expect(parentRoute({ kind: 'new-task', projectId: 'P1' })).toEqual({ kind: 'project', projectId: 'P1' });
    expect(parentRoute({ kind: 'project', projectId: 'P1' })).toEqual({ kind: 'dashboard' });
    expect(parentRoute({ kind: 'dashboard' })).toEqual({ kind: 'dashboard' });
  });

  it('exposes the owning project id', () => {
    expect(projectIdOf({ kind: 'dashboard' })).toBeNull();
    expect(projectIdOf({ kind: 'task', projectId: 'P1', taskId: 'T1' })).toBe('P1');
  });
});
