import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, coreApi, describeError, type CoreApi } from '../core/api';
import { toAgentInput, toAgentUpdateInput, toProjectInput, toTaskInput, type AgentDefinition, type AgentFormValues, type ArchitectStatus, type ContextAccess, type ConversationMessage, type Project, type ProjectFormValues, type RepairConfirmation, type RepairResult, type RoleAssignment, type Task, type TaskFormValues } from '../core/models';
import type { Navigation } from './navigation';
import type { AgentRegistryState, AssignmentIntent, Feedback, LoadStatus, ProjectAssignmentsState, ProjectTab } from './presentation/contracts';
import { assignmentBaseline, ROLE_REQUIRED_CAPABILITIES } from './presentation/contracts';
import { projectIdOf } from './route';

export type Layer = 'dashboard' | 'project' | 'task' | 'modal' | 'settings';

type TaskCache = { projectId: string | null; items: Task[]; status: LoadStatus };
const NO_TASKS: TaskCache = { projectId: null, items: [], status: 'loading' };
const NO_FEEDBACK: Feedback = { error: '', notice: '' };

type ChatCache = {
  projectId: string | null;
  status: LoadStatus;
  conversationId: string | null;
  messages: ConversationMessage[];
  hasMore: boolean;
  nextBefore: number | null;
  error: string;
  sending: boolean;
  olderLoading: boolean;
};

const NO_CHAT: ChatCache = {
  projectId: null, status: 'loading', conversationId: null, messages: [], hasMore: false, nextBefore: null,
  error: '', sending: false, olderLoading: false,
};

/** Union by message_id in stable ordinal order; the server-confirmed record wins over a duplicate. */
const mergeMessages = (known: ConversationMessage[], incoming: ConversationMessage[]): ConversationMessage[] => {
  const byId = new Map<string, ConversationMessage>();
  for (const message of known) byId.set(message.message_id, message);
  for (const message of incoming) byId.set(message.message_id, message);
  return [...byId.values()].sort((a, b) => a.ordinal - b.ordinal);
};

type AccessCache = { projectId: string | null; status: LoadStatus; info: ContextAccess | null; error: string };
const NO_ACCESS: AccessCache = { projectId: null, status: 'loading', info: null, error: '' };

type ArchitectCache = { projectId: string | null; status: LoadStatus; info: ArchitectStatus | null; busy: boolean; error: string };
const NO_ARCHITECT: ArchitectCache = { projectId: null, status: 'loading', info: null, busy: false, error: '' };
const architectForProject = (projectId: string | null): ArchitectCache => ({ ...NO_ARCHITECT, projectId });
/** While any Owner message awaits the Architect's reply the history is refreshed on this interval. */
const DELIVERY_POLL_MS = 2000;

const chatForProject = (projectId: string | null): ChatCache => ({ ...NO_CHAT, projectId, status: 'loading' });

const repairNotice = (done: RepairResult<unknown>) => done.repair
  ? `Манифест восстановлен. Копия сохранена в карантин: ${done.repair.backup_id}`
  : 'Манифест уже в порядке.';

type RegistryCache = { status: LoadStatus; agents: AgentDefinition[]; error: string; notice: string; busy: boolean };
const NO_REGISTRY: RegistryCache = { status: 'loading', agents: [], error: '', notice: '', busy: false };

type AssignmentsCache = {
  projectId: string | null;
  status: LoadStatus;
  agents: AgentDefinition[];
  assignments: RoleAssignment[];
  architect: ArchitectStatus | null;
  architectKnown: boolean;
  error: string;
  notice: string;
  busy: boolean;
  unresolved: string[];
};

const NO_ASSIGNMENTS: AssignmentsCache = {
  projectId: null, status: 'loading', agents: [], assignments: [], architect: null, architectKnown: false,
  error: '', notice: '', busy: false, unresolved: [],
};
const assignmentsForProject = (projectId: string): AssignmentsCache => ({ ...NO_ASSIGNMENTS, projectId });

const assignmentLabel = (kind: 'revoke' | 'create', role: 'architect' | 'worker', agentId: string) =>
  `${kind === 'revoke' ? 'отзыв' : 'назначение'} ${role === 'architect' ? 'Architect' : 'Worker'} ${agentId}`;

/**
 * Owns Core data loading, mutations, messages and the registration modal. Rules enforced here, not in views:
 * - a Task list for Project A is never exposed while the route points at Project B;
 * - messages belong to the layer/route where the action started, and are dropped when the user navigates away;
 * - feedback has three owners that never share a store: route layers (`error/notice`), the registration modal (`modalError`)
 *   and the Dashboard Project list (`projectsError`, which lives as long as the list is failed). A background list request
 *   can therefore never write into a foreground modal, and opening/closing the modal never touches Dashboard feedback;
 * - a success notice is only set after the API call succeeded (a 409/503 never produces one);
 * - one mutation at a time; Draft Task creation and repair stay explicit user actions.
 */
export function useWorkspaceController({ route, routeKey, go }: Navigation, api: CoreApi = coreApi) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsStatus, setProjectsStatus] = useState<LoadStatus>('loading');
  const [tasks, setTasks] = useState<TaskCache>(NO_TASKS);
  const [error, setError] = useState('');
  const [projectsError, setProjectsError] = useState('');
  const [modalError, setModalError] = useState('');
  const [notice, setNotice] = useState('');
  const [registrationOpen, setRegistrationOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [chat, setChat] = useState<ChatCache>(NO_CHAT);
  const [registry, setRegistry] = useState<RegistryCache>(NO_REGISTRY);
  const [assignments, setAssignments] = useState<AssignmentsCache>(NO_ASSIGNMENTS);

  const routeRef = useRef(route);
  routeRef.current = route;
  const epoch = useRef(0);
  const modalEpoch = useRef(0);
  const projectsSeq = useRef(0);
  const tasksSeq = useRef(0);
  const chatSeq = useRef(0);
  /** Project whose POST is in flight; closes the click/keyboard window before React state catches up. */
  const sendingRef = useRef<string | null>(null);
  const busyRef = useRef(false);
  const chatRef = useRef(chat);
  chatRef.current = chat;
  const architectRef = useRef<ArchitectCache>(NO_ARCHITECT);
  const registrySeq = useRef(0);
  const registryBusyRef = useRef(false);
  const registryRef = useRef(registry);
  registryRef.current = registry;
  const assignmentsSeq = useRef(0);
  const assignmentsBusyRef = useRef(false);
  const assignmentsRef = useRef(assignments);
  assignmentsRef.current = assignments;

  /** The list failure is owned by the Project list itself (not by whichever layer is visible) and stays until a load succeeds. */
  const loadProjects = useCallback(async () => {
    const seq = ++projectsSeq.current;
    try {
      const list = await api.listProjects();
      if (seq !== projectsSeq.current) return;
      setProjects(list);
      setProjectsStatus('ready');
      setProjectsError('');
    } catch (e) {
      if (seq !== projectsSeq.current) return;
      setProjectsStatus('failed');
      setProjectsError(describeError(e));
    }
  }, [api]);

  /** Registry read path. Core is the only source; nothing here implies a runtime check. */
  const loadAgents = useCallback(async () => {
    const seq = ++registrySeq.current;
    setRegistry(prev => ({ ...prev, status: 'loading', error: '' }));
    try {
      const agents = await api.listAgents();
      if (seq !== registrySeq.current) return;
      setRegistry({ status: 'ready', agents, error: '', notice: '', busy: false });
    } catch (e) {
      if (seq !== registrySeq.current) return;
      setRegistry(prev => ({ ...prev, status: 'failed', error: describeError(e) }));
    }
  }, [api]);

  /** One version-guarded Agent mutation at a time; a failure never produces a success notice. */
  const saveAgent = useCallback(async (agentId: string | null, values: AgentFormValues): Promise<string | null> => {
    if (registryBusyRef.current) return 'Сохранение уже выполняется.';
    registryBusyRef.current = true;
    setRegistry(prev => ({ ...prev, busy: true, error: '', notice: '' }));
    try {
      if (agentId === null) {
        await api.registerAgent(toAgentInput(values));
      } else {
        const known = registryRef.current.agents.find(agent => agent.agent_id === agentId);
        if (!known) throw new Error('Agent отсутствует в загруженном списке. Обновите Registry и повторите.');
        await api.updateAgent(agentId, toAgentUpdateInput(values, known.version));
      }
      const done = agentId === null
        ? 'Agent Definition зарегистрирован. Runtime не проверялся.'
        : 'Agent Definition сохранён. Runtime не проверялся.';
      try {
        const agents = await api.listAgents();
        setRegistry({ status: 'ready', agents, error: '', notice: done, busy: false });
      } catch (e) {
        setRegistry(prev => ({ ...prev, busy: false, notice: done, error: `Список не обновился: ${describeError(e)}` }));
      }
      return null;
    } catch (e) {
      const message = describeError(e);
      setRegistry(prev => ({ ...prev, busy: false, error: message }));
      // A stale PATCH is never retried blindly: reload the authoritative list so the Owner can reconcile the draft.
      if (e instanceof ApiError && e.isConflict) {
        void api.listAgents()
          .then(agents => setRegistry(prev => ({ ...prev, agents, status: 'ready' })))
          .catch(() => undefined);
      }
      return message;
    } finally {
      registryBusyRef.current = false;
    }
  }, [api]);

  const loadTasks = useCallback(async (projectId: string, quiet = false) => {
    const seq = ++tasksSeq.current;
    const started = epoch.current;
    const current = () => seq === tasksSeq.current && projectIdOf(routeRef.current) === projectId;
    try {
      const items = await api.listTasks(projectId);
      if (current()) setTasks({ projectId, items, status: 'ready' });
    } catch (e) {
      if (!current()) return;
      setTasks(prev => ({ projectId, items: prev.projectId === projectId ? prev.items : [], status: 'failed' }));
      if (!quiet && epoch.current === started) setError(describeError(e));
    }
  }, [api]);

  const projectId = projectIdOf(route);

  /** Applies a change only while the cache still belongs to this Project; a late result cannot repaint another one. */
  const patchChat = useCallback((id: string, patch: Partial<ChatCache>) => {
    setChat(prev => (prev.projectId === id ? { ...prev, ...patch } : prev));
  }, []);

  const loadConversation = useCallback(async (id: string) => {
    const seq = ++chatSeq.current;
    try {
      const page = await api.getConversation(id);
      if (seq !== chatSeq.current || projectIdOf(routeRef.current) !== id) return;
      // Merge, never replace: a message confirmed by Core while this GET was in flight must not disappear.
      setChat(prev => {
        if (prev.projectId !== id) return prev;
        const olderKnown = prev.messages.length > 0 && prev.messages[0].ordinal < (page.messages[0]?.ordinal ?? Infinity);
        return {
          ...prev, status: 'ready', conversationId: page.conversation?.conversation_id ?? prev.conversationId,
          messages: mergeMessages(prev.messages, page.messages),
          hasMore: olderKnown ? prev.hasMore : page.page.has_more,
          nextBefore: olderKnown ? prev.nextBefore : page.page.next_before, error: '',
        };
      });
    } catch (e) {
      if (seq !== chatSeq.current || projectIdOf(routeRef.current) !== id) return;
      patchChat(id, { status: 'failed', error: describeError(e) });
    }
  }, [api, patchChat]);

  const reloadConversation = useCallback(() => {
    const id = projectIdOf(routeRef.current);
    if (!id) return;
    // Confirmed messages stay visible; sending is blocked until the history is ready again.
    patchChat(id, { status: 'loading', error: '' });
    void loadConversation(id);
  }, [loadConversation, patchChat]);

  const loadOlderMessages = useCallback(async () => {
    const id = projectIdOf(routeRef.current);
    const current = chatRef.current;
    if (!id || current.projectId !== id || !current.hasMore || current.nextBefore === null || current.olderLoading) return;
    const cursor = current.nextBefore;
    patchChat(id, { olderLoading: true });
    try {
      const page = await api.getConversation(id, cursor);
      // Only the Project check matters: merging by id is safe against any reload or send that overlapped.
      setChat(prev => {
        if (prev.projectId !== id) return prev;
        const advance = prev.nextBefore === null || cursor <= prev.nextBefore;
        return { ...prev, messages: mergeMessages(prev.messages, page.messages), olderLoading: false,
          hasMore: advance ? page.page.has_more : prev.hasMore, nextBefore: advance ? page.page.next_before : prev.nextBefore };
      });
    } catch (e) {
      patchChat(id, { olderLoading: false, error: describeError(e) });
    }
  }, [api, patchChat]);

  const [architect, setArchitect] = useState<ArchitectCache>(NO_ARCHITECT);
  const architectSeq = useRef(0);
  architectRef.current = architect;

  const [access, setAccess] = useState<AccessCache>(NO_ACCESS);
  const accessSeq = useRef(0);
  const patchAccess = useCallback((id: string, patch: Partial<AccessCache>) => {
    setAccess(prev => (prev.projectId === id ? { ...prev, ...patch } : prev));
  }, []);
  /** Read-only effective access summary; never starts anything and sends nothing. */
  const loadAccess = useCallback(async (id: string) => {
    const seq = ++accessSeq.current;
    try {
      const info = await api.getContextAccess(id);
      if (seq === accessSeq.current) patchAccess(id, { status: 'ready', info, error: '' });
    } catch (e) {
      if (seq === accessSeq.current) patchAccess(id, { status: 'failed', error: describeError(e) });
    }
  }, [api, patchAccess]);

  const patchArchitect = useCallback((id: string, patch: Partial<ArchitectCache>) => {
    setArchitect(prev => (prev.projectId === id ? { ...prev, ...patch } : prev));
  }, []);

  /** Read-only status. Core re-probes liveness here but never starts a runtime. */
  const loadArchitect = useCallback(async (id: string) => {
    const seq = ++architectSeq.current;
    try {
      const info = await api.getArchitect(id);
      if (seq === architectSeq.current) patchArchitect(id, { status: 'ready', info, error: '' });
      void loadAccess(id);
    } catch (e) {
      if (seq === architectSeq.current) patchArchitect(id, { status: 'failed', error: describeError(e) });
    }
  }, [api, patchArchitect, loadAccess]);

  const changeArchitect = useCallback(async (action: 'activate' | 'deactivate'): Promise<string | null> => {
    const id = projectIdOf(routeRef.current);
    if (!id) return 'Проект не открыт.';
    if (architectRef.current.projectId === id && architectRef.current.busy) return 'Запрос уже выполняется.';
    patchArchitect(id, { busy: true, error: '' });
    const seq = ++architectSeq.current;
    try {
      const info = await (action === 'activate' ? api.activateArchitect(id) : api.deactivateArchitect(id));
      if (seq === architectSeq.current) patchArchitect(id, { info, status: 'ready', busy: false, error: '' });
      else patchArchitect(id, { busy: false });
      void loadAccess(id);
      return null;
    } catch (e) {
      const message = describeError(e);
      patchArchitect(id, { busy: false, error: message });
      return message;
    }
  }, [api, patchArchitect, loadAccess]);

  const patchAssignments = useCallback((id: string, patch: Partial<AssignmentsCache>) => {
    setAssignments(prev => (prev.projectId === id ? { ...prev, ...patch } : prev));
  }, []);

  /** Read-only Architect probe: gates an administrative change, never starts or stops a session. */
  const loadArchitectProbe = useCallback(async (id: string, seq: number) => {
    try {
      const info = await api.getArchitect(id);
      if (seq === assignmentsSeq.current) patchAssignments(id, { architect: info, architectKnown: true });
    } catch {
      if (seq === assignmentsSeq.current) patchAssignments(id, { architect: null, architectKnown: false });
    }
  }, [api, patchAssignments]);

  const loadAssignments = useCallback(async (id: string) => {
    const seq = ++assignmentsSeq.current;
    patchAssignments(id, { status: 'loading', error: '', notice: '' });
    try {
      const [agents, list] = await Promise.all([api.listAgents(), api.listAssignments(id)]);
      if (seq !== assignmentsSeq.current) return;
      patchAssignments(id, { status: 'ready', agents, assignments: list, error: '' });
    } catch (e) {
      if (seq !== assignmentsSeq.current) return;
      patchAssignments(id, { status: 'failed', error: describeError(e) });
    }
    void loadArchitectProbe(id, seq);
  }, [api, loadArchitectProbe, patchAssignments]);

  /**
   * Applies a minimal assignment delta against fresh Core records, one mutation at a time. The prototype's
   * «Сохранить назначения» is a UI intent, not an atomic endpoint: a partial failure is reported as such, is never
   * rolled back automatically, and the authoritative state is re-read. No activate/deactivate call is made.
   */
  const saveAssignments = useCallback(async (intent: AssignmentIntent, confirmArchitectChange: boolean): Promise<string | null> => {
    const id = projectIdOf(routeRef.current);
    if (!id) return 'Проект не открыт.';
    if (assignmentsRef.current.projectId !== id) return 'Настройки проекта ещё не загружены.';
    if (assignmentsBusyRef.current) return 'Сохранение уже выполняется.';
    assignmentsBusyRef.current = true;
    patchAssignments(id, { busy: true, error: '', notice: '', unresolved: [] });

    const confirmed: string[] = [];
    const unresolved: string[] = [];
    let failure: string | null = null;
    let conflict: string | null = null;
    try {
      try {
        const [fresh, probe] = await Promise.all([
          api.listAssignments(id),
          api.getArchitect(id).then(info => ({ ok: true as const, info })).catch(() => ({ ok: false as const })),
        ]);
        if (projectIdOf(routeRef.current) !== id) {
          failure = 'Настройки проекта закрыты до завершения сохранения.';
        } else {
          patchAssignments(id, { architect: probe.ok ? probe.info : null, architectKnown: probe.ok });

          if (assignmentBaseline(fresh) !== intent.baseline) {
            conflict = 'Назначения проекта изменились после открытия формы. Ничего не сохранено: проверьте актуальное состояние и повторите выбор.';
          }

          const currentArchitect = fresh.find(item => item.role === 'architect' && item.state === 'active') ?? null;
          const currentWorkers = fresh.filter(item => item.role === 'worker' && item.state === 'active' && item.scope === 'project');
          const architectChanging = (currentArchitect?.agent_id ?? null) !== intent.architectAgentId;
          const desiredWorkers = new Set(intent.workerAgentIds);
          const workersToRevoke = currentWorkers.filter(item => !desiredWorkers.has(item.agent_id));
          const currentWorkerIds = new Set(currentWorkers.map(item => item.agent_id));
          const workersToCreate = [...desiredWorkers].filter(agentId => !currentWorkerIds.has(agentId));

          if (conflict) {
            failure = conflict;
          } else if (architectChanging && currentArchitect !== null && !confirmArchitectChange) {
            failure = 'Смена или отзыв текущего Architect требует явного подтверждения Owner.';
          } else if (architectChanging) {
            // First assignment (no current Architect) needs no replacement confirmation but keeps the safety gate.
            if (!probe.ok) {
              failure = 'Не удалось проверить состояние Architect. Смена назначения заблокирована; проверьте состояние проекта.';
            } else if (!(probe.info.state === 'not_configured' || probe.info.state === 'stopped') || probe.info.can_deactivate) {
              failure = 'Architect не подтверждённо остановлен. Сначала явно деактивируйте его и дождитесь остановки.';
            }
          }

          if (!failure) {
            const steps: Array<{ label: string; run: () => Promise<unknown> }> = [];
            if (architectChanging && currentArchitect) {
              steps.push({
                label: assignmentLabel('revoke', 'architect', currentArchitect.agent_id),
                run: () => api.revokeAssignment(id, currentArchitect.assignment_id, currentArchitect.version),
              });
            }
            if (architectChanging && intent.architectAgentId) {
              const agentId = intent.architectAgentId;
              steps.push({
                label: assignmentLabel('create', 'architect', agentId),
                run: () => api.createAssignment(id, { agent_id: agentId, role: 'architect', task_id: null,
                  capabilities: [...ROLE_REQUIRED_CAPABILITIES.architect] }),
              });
            }
            for (const item of workersToRevoke) {
              steps.push({
                label: assignmentLabel('revoke', 'worker', item.agent_id),
                run: () => api.revokeAssignment(id, item.assignment_id, item.version),
              });
            }
            for (const agentId of workersToCreate) {
              steps.push({
                label: assignmentLabel('create', 'worker', agentId),
                run: () => api.createAssignment(id, { agent_id: agentId, role: 'worker', task_id: null,
                  capabilities: [...ROLE_REQUIRED_CAPABILITIES.worker] }),
              });
            }

            if (steps.length === 0) {
              patchAssignments(id, { busy: false, notice: 'Назначения не изменились.' });
              return null;
            }

            for (let index = 0; index < steps.length; index += 1) {
              if (projectIdOf(routeRef.current) !== id) {
                failure = 'Настройки проекта закрыты до завершения сохранения.';
                unresolved.push(...steps.slice(index).map(step => step.label));
                break;
              }
              try {
                await steps[index]!.run();
                confirmed.push(steps[index]!.label);
              } catch (e) {
                failure = describeError(e);
                unresolved.push(...steps.slice(index).map(step => step.label));
                break;
              }
            }
          }
        }
      } catch (e) {
        failure = describeError(e);
      }

      // Authoritative reload after any attempt: partial results are never presented as a full save.
      try {
        const [agents, list] = await Promise.all([api.listAgents(), api.listAssignments(id)]);
        patchAssignments(id, { agents, assignments: list });
        void loadArchitectProbe(id, assignmentsSeq.current);
      } catch (e) {
        failure = `${failure ? `${failure} ` : ''}Не удалось перечитать назначения: ${describeError(e)}`;
      }

      if (failure) {
        const parts = [conflict ? failure : `Не все изменения подтверждены: ${failure}`];
        if (confirmed.length) parts.push(`Подтверждено: ${confirmed.join('; ')}.`);
        if (unresolved.length) parts.push(`Не подтверждено: ${unresolved.join('; ')}. Требуется решение Owner.`);
        const message = parts.join(' ');
        patchAssignments(id, { busy: false, error: message, notice: '', unresolved });
        return message;
      }
      patchAssignments(id, { busy: false, error: '', notice: 'Назначения сохранены.', unresolved: [] });
      return null;
    } finally {
      assignmentsBusyRef.current = false;
    }
  }, [api, loadArchitectProbe, patchAssignments]);

  const changeDelivery = useCallback(async (messageId: string, action: 'reconcile' | 'retry'): Promise<string | null> => {
    const id = projectIdOf(routeRef.current);
    if (!id) return 'Проект не открыт.';
    try {
      await (action === 'reconcile' ? api.reconcileDelivery(id, messageId) : api.retryDelivery(id, messageId));
      await loadConversation(id);
      return null;
    } catch (e) {
      return describeError(e);
    }
  }, [api, loadConversation]);

  const sendConversationMessage = useCallback(async (body: string, clientKey: string): Promise<string | null> => {
    const id = projectIdOf(routeRef.current);
    if (!id) return 'Проект не открыт.';
    const current = chatRef.current;
    if (current.projectId !== id || current.status !== 'ready') return 'История проекта ещё не загружена.';
    if (current.sending || sendingRef.current === id) return 'Предыдущее сообщение ещё сохраняется.';
    sendingRef.current = id;
    patchChat(id, { sending: true, error: '' });
    try {
      const result = await api.sendConversationMessage(id, { client_key: clientKey, body });
      // A message confirmed by Core is merged whatever happened to the history requests meanwhile.
      setChat(prev => prev.projectId !== id ? prev
        : { ...prev, conversationId: result.message.conversation_id, messages: mergeMessages(prev.messages, [result.message]) });
      return null;
    } catch (e) {
      const message = describeError(e);
      patchChat(id, { error: message });
      return message;
    } finally {
      if (sendingRef.current === id) sendingRef.current = null;
      patchChat(id, { sending: false });
    }
  }, [api, patchChat]);

  // Effect order matters: the first one starts a new message epoch before the loaders capture it.
  useEffect(() => { epoch.current++; setError(''); setNotice(''); }, [routeKey]);
  useEffect(() => { void loadProjects(); }, [loadProjects]);
  useEffect(() => {
    if (projectId) void loadTasks(projectId);
    else { tasksSeq.current++; setTasks(NO_TASKS); }
  }, [projectId, routeKey, loadTasks]);
  useEffect(() => {
    if (projectId) { chatSeq.current++; setChat(chatForProject(projectId)); void loadConversation(projectId); }
    else { chatSeq.current++; setChat(NO_CHAT); }
  }, [projectId, loadConversation]);
  useEffect(() => {
    if (projectId) {
      architectSeq.current++; setArchitect(architectForProject(projectId));
      accessSeq.current++; setAccess({ ...NO_ACCESS, projectId });
      void loadArchitect(projectId);
    } else { architectSeq.current++; setArchitect(NO_ARCHITECT); accessSeq.current++; setAccess(NO_ACCESS); }
  }, [projectId, loadArchitect]);
  const settingsRoute = route.kind === 'settings';
  useEffect(() => {
    if (settingsRoute) void loadAgents();
    else registrySeq.current++;
  }, [settingsRoute, loadAgents]);
  const projectTab: ProjectTab = route.kind === 'project' ? (route.tab ?? 'tasks') : 'tasks';
  useEffect(() => {
    if (projectId && projectTab === 'settings') {
      assignmentsSeq.current++;
      setAssignments(assignmentsForProject(projectId));
      void loadAssignments(projectId);
    } else if (!projectId) {
      assignmentsSeq.current++;
      setAssignments(NO_ASSIGNMENTS);
    }
  }, [projectId, projectTab, loadAssignments]);
  const awaitingReply = chat.projectId === projectId && chat.messages.some(m => m.delivery?.state === 'pending');
  useEffect(() => {
    if (!projectId || !awaitingReply) return;
    const timer = setInterval(() => { void loadConversation(projectId); }, DELIVERY_POLL_MS);
    return () => clearInterval(timer);
  }, [projectId, awaitingReply, loadConversation]);

  const project = projects.find(p => p.id === projectId) ?? null;
  const taskList = tasks.projectId === projectId && projectId ? tasks : NO_TASKS;
  const task = route.kind === 'task' ? taskList.items.find(t => t.id === route.taskId) ?? null : null;
  // A cache for another Project is never exposed, not even for one render while the next page loads.
  const chatCache = projectId && chat.projectId === projectId ? chat : chatForProject(projectId);
  const conversation = {
    state: {
      status: chatCache.status, conversationId: chatCache.conversationId, messages: chatCache.messages,
      hasMore: chatCache.hasMore, nextBefore: chatCache.nextBefore, error: chatCache.error,
      sending: chatCache.sending, olderLoading: chatCache.olderLoading,
    },
    architect: {
      status: architect.projectId === projectId ? architect.status : 'loading' as LoadStatus,
      info: architect.projectId === projectId ? architect.info : null,
      busy: architect.projectId === projectId && architect.busy,
      error: architect.projectId === projectId ? architect.error : '',
    },
    contextAccess: {
      status: access.projectId === projectId ? access.status : 'loading' as LoadStatus,
      info: access.projectId === projectId ? access.info : null,
      error: access.projectId === projectId ? access.error : '',
    },
    onActivate: () => changeArchitect('activate'),
    onDeactivate: () => changeArchitect('deactivate'),
    onReconcile: (messageId: string) => changeDelivery(messageId, 'reconcile'),
    onRetryDelivery: (messageId: string) => changeDelivery(messageId, 'retry'),
    onReload: reloadConversation,
    onLoadOlder: () => void loadOlderMessages(),
    onSend: sendConversationMessage,
  };

  const layer: Layer = registrationOpen ? 'modal'
    : route.kind === 'settings' ? 'settings'
      : (route.kind === 'task' || route.kind === 'new-task') && project ? 'task'
        : route.kind !== 'dashboard' ? 'project' : 'dashboard';

  const feedbackFor = (target: Layer): Feedback => {
    if (target === 'modal') return layer === 'modal' ? { error: modalError, notice: '' } : NO_FEEDBACK;
    if (target === 'dashboard') return layer === 'dashboard' ? { error: projectsError, notice: '' } : NO_FEEDBACK;
    if (target === 'settings') return NO_FEEDBACK;
    if (layer !== target) return NO_FEEDBACK;
    // The Project drawer also reports a failed Project list, since it cannot render without it.
    return { error: error || (target === 'project' ? projectsError : ''), notice };
  };

  /** Runs one mutation. `live()` is false once the user navigated or closed the modal, so late results do not yank the UI. */
  const mutate = useCallback(async (run: (live: () => boolean) => Promise<void>, scope: 'route' | 'modal' = 'route') => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    if (scope === 'modal') setModalError(''); else { setError(''); setNotice(''); }
    const started = epoch.current, startedModal = modalEpoch.current;
    try { await run(() => epoch.current === started && (scope === 'route' || modalEpoch.current === startedModal)); }
    finally { busyRef.current = false; setBusy(false); }
  }, []);

  const fail = (live: () => boolean, e: unknown, scope: 'route' | 'modal' = 'route') => {
    if (live()) (scope === 'modal' ? setModalError : setError)(describeError(e));
  };

  const registerProject = (values: ProjectFormValues) => mutate(async live => {
    try {
      const created = await api.createProject(toProjectInput(values));
      await loadProjects();
      // `live()` is false once this modal was dismissed or replaced; the Project stays created and cached, but the UI is not touched.
      if (live()) { setRegistrationOpen(false); go({ kind: 'project', projectId: created.id }); }
    } catch (e) {
      fail(live, e, 'modal');
      void loadProjects();
    }
  }, 'modal');

  const createTask = (values: TaskFormValues) => mutate(async live => {
    const at = routeRef.current;
    if (at.kind !== 'new-task') return;
    try {
      const created = await api.createTask(at.projectId, toTaskInput(values));
      await loadTasks(at.projectId);
      if (live()) go({ kind: 'task', projectId: at.projectId, taskId: created.id });
    } catch (e) {
      fail(live, e);
      void loadTasks(at.projectId, true);
    }
  });

  const reconcileProject = () => mutate(async live => {
    const id = projectIdOf(routeRef.current);
    if (!id) return;
    try { await api.reconcileProject(id); await loadProjects(); }
    catch (e) { fail(live, e); void loadProjects(); }
  });

  const repairProject = (_confirmation: RepairConfirmation) => mutate(async live => {
    const id = projectIdOf(routeRef.current);
    if (!id) return;
    try {
      const done = await api.repairProject(id);
      await loadProjects();
      if (live()) setNotice(repairNotice(done));
    } catch (e) {
      fail(live, e);
      void loadProjects();
    }
  });

  const reconcileTask = () => mutate(async live => {
    const at = routeRef.current;
    if (at.kind !== 'task') return;
    try { await api.reconcileTask(at.projectId, at.taskId); await loadTasks(at.projectId); }
    catch (e) { fail(live, e); void loadTasks(at.projectId, true); }
  });

  const repairTask = (_confirmation: RepairConfirmation) => mutate(async live => {
    const at = routeRef.current;
    if (at.kind !== 'task') return;
    try {
      const done = await api.repairTask(at.projectId, at.taskId);
      await loadTasks(at.projectId);
      if (live()) setNotice(repairNotice(done));
    } catch (e) {
      fail(live, e);
      void loadTasks(at.projectId, true);
    }
  });

  const retry = async () => {
    setError('');
    setNotice('');
    setProjectsError('');
    await loadProjects();
    if (projectId) await loadTasks(projectId);
  };

  /** Dashboard Retry: one `listProjects` request, no mutation. Cached Projects stay visible while it runs. */
  const retryProjects = async () => {
    setProjectsError('');
    setProjectsStatus('loading');
    await loadProjects();
  };

  const registryState: AgentRegistryState = {
    status: registry.status, agents: registry.agents, error: registry.error, notice: registry.notice, busy: registry.busy,
  };
  const assignmentCache = projectId && assignments.projectId === projectId ? assignments : assignmentsForProject(projectId ?? '');
  const assignmentState: ProjectAssignmentsState = {
    status: assignmentCache.status, agents: assignmentCache.agents, assignments: assignmentCache.assignments,
    architect: assignmentCache.architect, architectKnown: assignmentCache.architectKnown,
    error: assignmentCache.error, notice: assignmentCache.notice, busy: assignmentCache.busy,
    unresolved: assignmentCache.unresolved,
  };

  return {
    layer, projects, projectsStatus, project, tasks: taskList.items, tasksStatus: taskList.status, task, busy, registrationOpen,
    feedbackFor, conversation, registry: registryState, projectTab,
    registryControls: {
      state: registryState,
      onRetry: () => void loadAgents(),
      onSaveAgent: saveAgent,
    },
    assignments: {
      state: assignmentState,
      onRetry: () => { if (projectId) void loadAssignments(projectId); },
      onSave: saveAssignments,
      onOpenRegistry: () => go({ kind: 'settings' }),
    },
    actions: {
      retry, retryProjects,
      openSettings: () => go({ kind: 'settings' }),
      // Each modal instance gets its own epoch and starts and ends with clean, modal-owned feedback only.
      openRegistration: () => { modalEpoch.current++; setModalError(''); setRegistrationOpen(true); },
      closeRegistration: () => { modalEpoch.current++; setModalError(''); setRegistrationOpen(false); },
      registerProject, createTask, reconcileProject, repairProject, reconcileTask, repairTask,
    },
  };
}
