import type { RunJournalPage, RunReportPage, RunView } from '../models';
import { post, request } from './http';

/** P054B: managed Runs of one Task (Core-recorded lifecycle). Read-only: never sends, imports or starts anything. */
export const listTaskRuns = (projectId: string, taskId: string) => request<RunView[]>(`/projects/${projectId}/tasks/${taskId}/runs`);
/** Full read-only view of one Run, including the imported Working Plan / Step axes / Report reference. */
export const getRun = (projectId: string, runId: string) => request<RunView>(`/projects/${projectId}/runs/${runId}`);
/** «Запустить Run»: the ONE explicit Owner start. The same `launch_key` for a retry of the same click never starts a second Run. */
export const launchRun = (projectId: string, taskId: string, agentId: string, launchKey: string) =>
  post<RunView & { created: boolean }>(`/projects/${projectId}/tasks/${taskId}/runs`, { agent_id: agentId, launch_key: launchKey });
/** Bounded page of the imported Execution Journal (read-only). */
export const getRunJournal = (projectId: string, runId: string, after = 0, limit = 100) =>
  request<RunJournalPage>(`/projects/${projectId}/runs/${runId}/journal?after=${after}&limit=${limit}`);
/** The registered Worker Run Report (digest re-verified by Core on every read). */
export const getRunReport = (projectId: string, runId: string) => request<RunReportPage>(`/projects/${projectId}/runs/${runId}/report`);
