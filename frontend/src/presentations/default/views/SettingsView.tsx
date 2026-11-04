import { useState, type FormEvent } from 'react';
import type { AgentDefinition, AgentFormValues } from '../../../core/models';
import type { SettingsViewProps } from '../../../app/presentation/contracts';
import {
  AGENT_CAPABILITIES, AGENT_ICON_GLYPHS, AGENT_RUNTIME_OPTIONS, agentIcon, capabilityPreset,
  ROLE_REQUIRED_CAPABILITIES, runtimeLabel, type CapabilityPreset,
} from '../../../app/presentation/contracts';
import { Drawer } from '../ui/Drawer';
import { Feedback } from '../ui/Feedback';

const IDENTIFIERS = {
  runtime_adapter: /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/,
  provider_id: /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/,
  model_id: /^[A-Za-z0-9][A-Za-z0-9._:+-]{0,95}$/,
};
const SECRET_MARKERS = /(secret|password|passwd|token|api_key|apikey|private_key|private key|bearer |authorization:|credential)/i;
const SECRET_PREFIXES = /^(sk-|pk-|ghp_|gho_|ghs_|github_pat_|xoxb-|xoxp-|akia|asia)/i;

/** Mirrors the Core's bounded metadata rules; the server still decides and its rejection is shown truthfully. */
function validateAgent(values: AgentFormValues): string | null {
  const name = values.display_name.trim();
  if (!name || name.length > 80) return 'Имя должно быть от 1 до 80 символов.';
  if (SECRET_MARKERS.test(name) || name.includes('://')) return 'Имя должно быть описательным, без URL и секретов.';
  for (const [field, limit] of [['runtime_adapter', 64], ['provider_id', 64], ['model_id', 96]] as const) {
    const value = values[field].trim();
    if (!value || value.length > limit || !IDENTIFIERS[field].test(value) || SECRET_MARKERS.test(value) || SECRET_PREFIXES.test(value)) {
      return 'Runtime Adapter, Provider ID и Model ID обязательны: короткие идентификаторы без путей, URL и секретов.';
    }
  }
  if (values.capabilities.length > 8) return 'Можно объявить не более 8 capabilities.';
  return null;
}

const initialDraft = (agent: AgentDefinition | null): AgentFormValues => ({
  display_name: agent?.display_name ?? '',
  icon_label: agent?.icon_label ?? 'robot',
  runtime_adapter: agent?.runtime_adapter ?? 'opencode',
  provider_id: agent?.provider_id ?? '',
  model_id: agent?.model_id ?? '',
  capabilities: agent?.capabilities.map(item => item.capability) ?? [],
});

const presetCapabilities = (preset: CapabilityPreset): string[] => {
  if (preset === 'architect') return [...ROLE_REQUIRED_CAPABILITIES.architect];
  if (preset === 'worker') return [...ROLE_REQUIRED_CAPABILITIES.worker];
  if (preset === 'both') return [...new Set([...ROLE_REQUIRED_CAPABILITIES.architect, ...ROLE_REQUIRED_CAPABILITIES.worker])];
  return [];
};

const PRESET_LABELS: ReadonlyArray<{ id: CapabilityPreset; label: string }> = [
  { id: 'none', label: 'Пока не объявлены' },
  { id: 'architect', label: 'Для роли Architect' },
  { id: 'worker', label: 'Для роли Worker' },
  { id: 'both', label: 'Для обеих ролей' },
  { id: 'custom', label: 'Выбрать вручную' },
];

type EditorProps = {
  agent: AgentDefinition | null;
  busy: boolean;
  projects: SettingsViewProps['projects'];
  onSave: SettingsViewProps['onSaveAgent'];
  onOpenProjectSettings: SettingsViewProps['onOpenProjectSettings'];
  onCancel: () => void;
};

function AgentEditor({ agent, busy, projects, onSave, onOpenProjectSettings, onCancel }: EditorProps) {
  const [draft, setDraft] = useState(() => initialDraft(agent));
  const [preset, setPreset] = useState<CapabilityPreset>(() => capabilityPreset(initialDraft(agent).capabilities));
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  const update = (patch: Partial<AgentFormValues>) => setDraft(prev => ({ ...prev, ...patch }));
  const choosePreset = (next: CapabilityPreset) => {
    setPreset(next);
    if (next !== 'custom') update({ capabilities: presetCapabilities(next) });
  };
  const toggleCapability = (capability: string, checked: boolean) => {
    setPreset('custom');
    update({
      capabilities: checked
        ? [...draft.capabilities, capability]
        : draft.capabilities.filter(item => item !== capability),
    });
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError('');
    const invalid = validateAgent(draft);
    if (invalid) { setError(invalid); return; }
    setSaving(true);
    try {
      const failure = await onSave(agent?.agent_id ?? null, draft);
      if (failure) setError(failure);
      else onCancel();
    } finally {
      setSaving(false);
    }
  };

  const runtimeKnown = AGENT_RUNTIME_OPTIONS.some(option => option.id === draft.runtime_adapter);
  return <form className="rosc-card rosc-settings__editor" onSubmit={event => void submit(event)}>
    <div className="rosc-settings__editor-head">
      <h3>{agent ? 'Редактирование Agent Definition' : 'Новый Agent Definition'}</h3>
      <button type="button" className="rosc-btn rosc-btn--small" onClick={onCancel} disabled={saving}>Закрыть</button>
    </div>

    <div className="rosc-settings__grid">
      <label className="rosc-field"><span>Отображаемое имя</span>
        <input value={draft.display_name} maxLength={80} placeholder="Например, DeepSeek Coder"
          onChange={event => update({ display_name: event.target.value })} />
      </label>
      <label className="rosc-field"><span>Иконка</span>
        <select value={draft.icon_label} onChange={event => update({ icon_label: event.target.value })}>
          {Object.entries(AGENT_ICON_GLYPHS).map(([key, glyph]) => <option key={key} value={key}>{glyph} · {key}</option>)}
        </select>
      </label>
      <label className="rosc-field"><span>Runtime Adapter</span>
        <select value={draft.runtime_adapter} onChange={event => update({ runtime_adapter: event.target.value })}>
          {!runtimeKnown && <option value={draft.runtime_adapter}>{draft.runtime_adapter} · из Core</option>}
          {AGENT_RUNTIME_OPTIONS.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}
        </select>
      </label>
      <label className="rosc-field"><span>Provider ID</span>
        <input value={draft.provider_id} maxLength={64} placeholder="Например, deepseek"
          onChange={event => update({ provider_id: event.target.value })} />
      </label>
      <label className="rosc-field rosc-settings__full"><span>Model ID</span>
        <input value={draft.model_id} maxLength={96} placeholder="Например, deepseek-v4.1-flash"
          onChange={event => update({ model_id: event.target.value })} />
      </label>
    </div>

    <label className="rosc-field"><span>Набор declared capabilities</span>
      <select value={preset} onChange={event => choosePreset(event.target.value as CapabilityPreset)}>
        {PRESET_LABELS.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}
      </select>
    </label>

    <div className="rosc-settings__caps-head">
      <strong>Capabilities (словарь Core)</strong>
      <span className="rosc-tag">Декларации, не права на запуск</span>
    </div>
    <div className="rosc-settings__caps">
      {AGENT_CAPABILITIES.map(capability => <label key={capability.id} className="rosc-settings__cap">
        <input type="checkbox" checked={draft.capabilities.includes(capability.id)}
          disabled={preset !== 'custom'}
          onChange={event => toggleCapability(capability.id, event.target.checked)} />
        <span>{capability.label}<code>{capability.id}</code></span>
      </label>)}
    </div>

    <div className="rosc-settings__hint">
      <strong>Проверка подключения:</strong> не выполнялась. Runtime, Provider и Model — только метаданные.
      Секреты, ключи и команды здесь не запрашиваются.
    </div>
    {agent && <div className="rosc-settings__hint">
      <strong>Назначения:</strong> изменение capabilities может сделать существующие назначения <strong>blocked</strong>;
      изменение runtime/provider/model потребует отдельной проверки. Активный Architect не деактивируется автоматически.
    </div>}
    {agent && (projects.length === 1
      ? <button type="button" className="rosc-btn rosc-btn--small" onClick={() => onOpenProjectSettings(projects[0]!.id)}>
        Открыть назначения · {projects[0]!.name}
      </button>
      : <p className="rosc-hint">Назначения ролей отображаются в настройках конкретного проекта.</p>)}

    {error && <p className="rosc-alert" role="alert">{error}</p>}
    <div className="rosc-settings__editor-actions">
      <button type="submit" className="rosc-btn rosc-btn--primary rosc-btn--small" disabled={busy || saving}>
        {agent ? 'Сохранить изменения' : 'Зарегистрировать'}
      </button>
    </div>
  </form>;
}

export function SettingsView({ state, projects, onRetry, onSaveAgent, onOpenProjectSettings, onClose }: SettingsViewProps) {
  const [editingId, setEditingId] = useState<string | null | undefined>(undefined);
  const editing = editingId === undefined ? null : state.agents.find(agent => agent.agent_id === editingId) ?? null;

  const head = <div className="rosc-drawer__heading">
    <p className="rosc-drawer-eyebrow">SETTINGS / ROSC</p>
    <h2>Настройки ROSC</h2>
    <p>Пользователь, агенты, Workspace и локальное подключение</p>
  </div>;

  return <Drawer variant="settings" label="Настройки ROSC" closeLabel="Закрыть настройки" inert={false} onClose={onClose} head={head} fill>
    <div className="rosc-settings">
      <nav className="rosc-settings__nav" aria-label="Разделы настроек">
        <button type="button" className="rosc-settings__tab rosc-settings__tab--active" aria-current="page">Агенты и модели</button>
        {['Профиль Owner', 'Workspace и пауза', 'Авторизация', 'Статистика'].map(label =>
          <button key={label} type="button" className="rosc-settings__tab" disabled
            title="Не реализовано в этом срезе P016">{label}</button>)}
        <p className="rosc-hint rosc-settings__nav-hint">В этом срезе реализованы только «Агенты и модели». Остальные разделы отложены.</p>
      </nav>

      <div className="rosc-settings__content">
        <Feedback error={state.error} notice={state.notice} />
        <div className="rosc-settings__heading">
          <div>
            <h3>Глобальный Agent Registry</h3>
            <p className="rosc-hint">Регистрация → назначение роли в Project → отдельная активация</p>
          </div>
          <button type="button" className="rosc-btn rosc-btn--small rosc-btn--primary"
            onClick={() => setEditingId(null)}>＋ Добавить</button>
        </div>

        <div className="rosc-settings__hint">
          <strong>Готовность не проверена.</strong> Все записи читаются из Core; ни один CLI или LLM не вызывается.
          <strong> Declared capabilities ≠ подтверждённая доступность модели.</strong>
        </div>

        {state.status === 'failed' && state.agents.length === 0
          ? <div className="rosc-state rosc-state--failed"><p>Не удалось загрузить Agent Registry.
            <button type="button" className="rosc-btn rosc-btn--small" onClick={onRetry}>Повторить</button></p></div>
          : state.status === 'loading' && state.agents.length === 0
            ? <div className="rosc-state rosc-state--loading"><span className="rosc-spinner" aria-hidden="true" />Загрузка…</div>
            : state.agents.length === 0
              ? <div className="rosc-state"><strong>Нет Agent Definitions.</strong>
                <p>Сначала зарегистрируйте агента. Это не запускает Runtime.</p></div>
              : <div className="rosc-settings__agents">
                {state.agents.map(agent => <button type="button" className="rosc-settings__agent" key={agent.agent_id}
                  onClick={() => setEditingId(agent.agent_id)} aria-label={`Изменить ${agent.display_name}`}>
                  <span className="rosc-settings__avatar" aria-hidden="true">{agentIcon(agent.icon_label)}</span>
                  <span className="rosc-settings__agent-main">
                    <strong>{agent.display_name}</strong>
                    <p>{runtimeLabel(agent.runtime_adapter)} · {agent.provider_id} · {agent.model_id}</p>
                    <small>Capabilities: {agent.capabilities.length} объявлено · версия {agent.version}</small>
                    <small>Назначения — в настройках проекта</small>
                  </span>
                  <span className="rosc-tag">Не проверен</span>
                </button>)}
              </div>}

        {state.status === 'ready' && <p className="rosc-hint">Всего Agent Definitions: {state.agents.length}</p>}

        {editingId !== undefined && <AgentEditor key={editingId ?? 'new'} agent={editing} busy={state.busy}
          projects={projects} onSave={onSaveAgent} onOpenProjectSettings={onOpenProjectSettings}
          onCancel={() => setEditingId(undefined)} />}

        <div className="rosc-settings__hint">
          Имя, иконка, Runtime, Provider, Model и capabilities — свойства Agent Definition. Роли Architect / Worker
          назначаются только внутри проекта. Регистрация и назначение не запускают сессию.
        </div>
      </div>
    </div>
  </Drawer>;
}
