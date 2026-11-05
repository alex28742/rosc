import type { ArchitectStatus, ContextAccess, DisclosureCategory, ConversationMessageInput, ConversationMessageResult, ConversationPage, DeliveryResult } from '../models';
import { post, put, request } from './http';

/** One bounded page of the durable Project Conversation; `before` asks for messages older than that ordinal. */
export const getConversation = (projectId: string, before?: number) =>
  request<ConversationPage>(`/projects/${projectId}/conversation${before === undefined ? '' : `?before=${before}`}`);

/** Records one Owner message. The client key is opaque and only makes a retry idempotent; it is not the identity. */
export const sendConversationMessage = (projectId: string, input: ConversationMessageInput) =>
  post<ConversationMessageResult>(`/projects/${projectId}/conversation/messages`, input);

/** Read-only: Core probes liveness but never starts anything. */
export const getArchitect = (projectId: string) => request<ArchitectStatus>(`/projects/${projectId}/architect`);

/** The only call that starts a Project Architect runtime: an explicit Owner action, never implied by opening a Project. */
export const activateArchitect = (projectId: string) => post<ArchitectStatus>(`/projects/${projectId}/architect/activate`);
export const deactivateArchitect = (projectId: string) => post<ArchitectStatus>(`/projects/${projectId}/architect/deactivate`);

/** Looks an uncertain delivery up at the provider; never re-sends. */
export const reconcileDelivery = (projectId: string, messageId: string) =>
  post<DeliveryResult>(`/projects/${projectId}/conversation/messages/${messageId}/reconcile`);
/** Re-sends only when Core says the previous attempt provably never reached the provider. */
export const retryDelivery = (projectId: string, messageId: string) =>
  post<DeliveryResult>(`/projects/${projectId}/conversation/messages/${messageId}/retry`);

/** Read-only effective access summary (access matrix, tool transport state, standing disclosure scope). */
export const getContextAccess = (projectId: string) => request<ContextAccess>(`/projects/${projectId}/architect/context-access`);
/** Owner-only: enable or revoke disclosure of one private-content category to the active Architect's provider/model. */
export const setContextDisclosure = (projectId: string, category: DisclosureCategory, enabled: boolean) =>
  put<ContextAccess>(`/projects/${projectId}/architect/context-access/disclosure`, { category, covered: 'all', enabled });
