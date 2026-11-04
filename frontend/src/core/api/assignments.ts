import type { AssignmentInput, RoleAssignment } from '../models';
import { post, request } from './http';

/** Active assignments by default; P016 never requests revoked history. */
export const listAssignments = (projectId: string) =>
  request<RoleAssignment[]>(`/projects/${projectId}/assignments`);
/** Records Project-scoped intent only (`task_id: null`); authorization and eligibility stay Core-side. */
export const createAssignment = (projectId: string, input: AssignmentInput) =>
  post<RoleAssignment>(`/projects/${projectId}/assignments`, input);
/** Version-checked revocation; Core keeps the historical record. */
export const revokeAssignment = (projectId: string, assignmentId: string, expectedVersion: number) =>
  post<RoleAssignment>(`/projects/${projectId}/assignments/${assignmentId}/revoke`, {
    expected_version: expectedVersion,
  });
