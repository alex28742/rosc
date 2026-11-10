import type { ArchitectStatus, ContextAccess, ConversationMessageInput, ConversationMessageResult, ConversationPage, DeliveryResult } from '../models';
import { post, request } from './http';

/** One bounded page of the durable Project Conversation; `before` asks for messages older than that ordinal. */
export const getConversation = (projectId: string, before?: number) =>
  request<ConversationPage>(`/projects/${projectId}/conversation${before === undefined ? '' : `?before=${before}`}`);

/** Records one Owner message. The client key is opaque and only makes a retry idempotent; it is not the identity. */
export const sendConversationMessage = (projectId: string, input: ConversationMessageInput) =>
  post<ConversationMessageResult>(`/projects/${projectId}/conversation/messages`, input);

/** P038: one Task's own durable conversation. Reading never creates a conversation or starts an agent. */
export const getTaskConversation = (projectId: string, taskId: string, before?: number) =>
  request<ConversationPage>(`/projects/${projectId}/tasks/${taskId}/conversation${before === undefined ? '' : `?before=${before}`}`);

/** Stores one Owner message in the Task Conversation; `recipient` is saved intent and nothing is delivered. */
export const sendTaskConversationMessage = (projectId: string, taskId: string, input: ConversationMessageInput) =>
  post<ConversationMessageResult>(`/projects/${projectId}/tasks/${taskId}/conversation/messages`, input);

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

/** Read-only effective access summary (access matrix, tool transport state, selected provider/model). */
export const getContextAccess = (projectId: string) => request<ContextAccess>(`/projects/${projectId}/architect/context-access`);
