import * as conversations from './conversations';
import * as projects from './projects';
import * as tasks from './tasks';

export { ApiError, describeError } from './http';

/** The operations the application controller needs; tests replace this whole object with a stub. */
export const coreApi = { ...projects, ...tasks, ...conversations };
export type CoreApi = typeof coreApi;
