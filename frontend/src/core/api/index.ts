import * as agents from './agents';
import * as assignments from './assignments';
import * as authoring from './authoring';
import * as conversations from './conversations';
import * as projects from './projects';
import * as tasks from './tasks';
import * as workers from './workers';

export { ApiError, describeError } from './http';

/** The operations the application controller needs; tests replace this whole object with a stub. */
export const coreApi = { ...projects, ...tasks, ...conversations, ...agents, ...assignments, ...authoring, ...workers };
export type CoreApi = typeof coreApi;
