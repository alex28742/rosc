import type { ConversationMessageInput, ConversationMessageResult, ConversationPage } from '../models';
import { post, request } from './http';

/** One bounded page of the durable Project Conversation; `before` asks for messages older than that ordinal. */
export const getConversation = (projectId: string, before?: number) =>
  request<ConversationPage>(`/projects/${projectId}/conversation${before === undefined ? '' : `?before=${before}`}`);

/** Records one Owner message. The client key is opaque and only makes a retry idempotent; it is not the identity. */
export const sendConversationMessage = (projectId: string, input: ConversationMessageInput) =>
  post<ConversationMessageResult>(`/projects/${projectId}/conversation/messages`, input);
