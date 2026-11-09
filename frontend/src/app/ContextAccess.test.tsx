import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { contextAccess, project, stubApi } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const agentsCard = () => document.querySelector('section[aria-label="Агенты проекта"]') as HTMLElement;

beforeEach(() => at('/projects/P0001'));
afterEach(() => cleanup());

describe('Architect context access summary (P019)', () => {
  it('shows the four-tier matrix, read-only source and no toggles; loading it changes nothing', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => [P1]) });
    render(<App api={api} />);
    expect(await screen.findByText('Доступ Architect к контексту')).toBeTruthy();
    expect(api.getContextAccess).toHaveBeenCalledWith('P0001');
    expect(api.activateArchitect).not.toHaveBeenCalled();
    const matrix = within(agentsCard()).getByLabelText('Матрица доступа');
    expect(within(matrix).getAllByRole('listitem')).toHaveLength(5);
    expect(within(matrix).getAllByText('только чтение')).toHaveLength(3);
    expect(within(matrix).getByText('чтение и запись')).toBeTruthy();
    expect(within(matrix).getByText('запрещено')).toBeTruthy();
    expect(within(agentsCard()).queryAllByRole('switch')).toHaveLength(0);
    expect(within(agentsCard()).queryAllByRole('checkbox')).toHaveLength(0);
    expect(within(agentsCard()).getByText(/Провайдер\/модель: prov\/model-a/)).toBeTruthy();
    expect(within(agentsCard()).getByText(/могут отправляться/)).toBeTruthy();
    expect(within(agentsCard()).queryByText(/consent_required/)).toBeNull();
    expect(within(agentsCard()).getByText(/не публикуются и не синхронизируются автоматически/)).toBeTruthy();
  });

  it('says so when the tools are not connected to the live session and reports a failed summary', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => [P1]),
      getContextAccess: vi.fn(async () => contextAccess({ tools: { transport: 'inactive', names: [] } })) });
    render(<App api={api} />);
    expect(await screen.findByText(/восстанавливает подключение в той же сессии/)).toBeTruthy();
    cleanup();
    const failing = stubApi({ listProjects: vi.fn(async () => [P1]), getContextAccess: vi.fn(async () => { throw new Error('boom'); }) });
    render(<App api={failing} />);
    expect(await screen.findByText(/Не удалось получить сводку доступа Architect/)).toBeTruthy();
  });
});
