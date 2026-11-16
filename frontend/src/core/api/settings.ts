import type { ConversationDiagnostics } from '../models';
import { request } from './http';

/** P052 r03: the two Owner switches of local Conversation Diagnostics (default OFF / metadata only). */
export const getConversationDiagnostics = () => request<ConversationDiagnostics>('/settings/conversation-diagnostics');
export const updateConversationDiagnostics = (change: { enabled?: boolean; include_bodies?: boolean }) =>
  request<ConversationDiagnostics>('/settings/conversation-diagnostics', {
    method: 'PUT', body: JSON.stringify(change),
  });
