import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { contextAccess, project, stubApi } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const panel = () => document.querySelector('aside[aria-label="Project Conversation"]') as HTMLElement;

beforeEach(() => at('/projects/P0001'));
afterEach(() => cleanup());

describe('Architect context access summary (P019)', () => {
  it('shows the four-tier matrix, read-only source and no disclosure by default; loading it changes nothing', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => [P1]) });
    render(<App api={api} />);
    expect(await screen.findByText('Доступ Architect к контексту')).toBeTruthy();
    expect(api.getContextAccess).toHaveBeenCalledWith('P0001');
    expect(api.setContextDisclosure).not.toHaveBeenCalled();
    expect(api.activateArchitect).not.toHaveBeenCalled();
    const matrix = within(panel()).getByLabelText('Матрица доступа');
    expect(within(matrix).getAllByRole('listitem')).toHaveLength(5);
    expect(within(matrix).getAllByText('только чтение')).toHaveLength(3);
    expect(within(matrix).getByText('чтение и запись')).toBeTruthy();
    expect(within(matrix).getByText('запрещено')).toBeTruthy();
    for (const box of within(panel()).getAllByRole('switch') as HTMLInputElement[]) expect(box.checked).toBe(false);
    expect(within(panel()).getByText(/По умолчанию ничего не передаётся/)).toBeTruthy();
    expect(within(panel()).getByText(/не публикуются и не синхронизируются автоматически/)).toBeTruthy();
  });

  it('enables one category only on an explicit Owner click and reflects the stored scope', async () => {
    const enabledScope = { category: 'project_source' as const, covered: 'all', enabled: true, updated_at: '2026-11-05T00:00:00Z' };
    const setContextDisclosure = vi.fn(async () => contextAccess({
      disclosure: { ...contextAccess().disclosure, scopes: [enabledScope] } }));
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), setContextDisclosure });
    render(<App api={api} />);
    const source = await screen.findByLabelText(/Исходники привязанного checkout/) as HTMLInputElement;
    fireEvent.click(source);
    await waitFor(() => expect(setContextDisclosure).toHaveBeenCalledWith('P0001', 'project_source', true));
    await waitFor(() => expect((within(panel()).getByLabelText(/Исходники привязанного checkout/) as HTMLInputElement).checked).toBe(true));
    expect((within(panel()).getByLabelText(/Документы других проектов/) as HTMLInputElement).checked).toBe(false);
  });

  it('says so when the tools are not connected to the live session and reports a failed summary', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => [P1]),
      getContextAccess: vi.fn(async () => contextAccess({ tools: { transport: 'inactive', names: [] } })) });
    render(<App api={api} />);
    expect(await screen.findByText(/активируйте Architect заново/)).toBeTruthy();
    cleanup();
    const failing = stubApi({ listProjects: vi.fn(async () => [P1]), getContextAccess: vi.fn(async () => { throw new Error('boom'); }) });
    render(<App api={failing} />);
    expect(await screen.findByText(/Не удалось получить сводку доступа Architect/)).toBeTruthy();
  });
});
