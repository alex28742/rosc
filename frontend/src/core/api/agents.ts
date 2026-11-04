import type { AgentDefinition, AgentInput, AgentUpdateInput } from '../models';
import { patch, post, request } from './http';

/** The list is global, not Project-scoped; the Registry is administrative metadata only. */
export const listAgents = () => request<AgentDefinition[]>('/agents');
export const getAgent = (agentId: string) => request<AgentDefinition>(`/agents/${agentId}`);
/** Registers a definition. It never launches a runtime or contacts a provider. */
export const registerAgent = (input: AgentInput) => post<AgentDefinition>('/agents', input);
/** Full replacement with `expected_version`; a stale version is a 409 and must be reconciled by reload. */
export const updateAgent = (agentId: string, input: AgentUpdateInput) =>
  patch<AgentDefinition>(`/agents/${agentId}`, input);
