import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, request } from './http';
import { repairProject } from './projects';
import { createTask } from './tasks';

const reply = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const stubFetch = (impl: () => Promise<Response>) => { const spy = vi.fn(impl); vi.stubGlobal('fetch', spy); return spy; };

afterEach(() => vi.unstubAllGlobals());

describe('request', () => {
  it('prefixes /api and sends JSON', async () => {
    const spy = stubFetch(async () => reply(200, [{ id: 'P0001' }]));
    expect(await request('/projects')).toEqual([{ id: 'P0001' }]);
    expect(spy).toHaveBeenCalledWith('/api/projects', { headers: { 'Content-Type': 'application/json' } });
  });

  it('reports a network failure with the unreachable-Core message', async () => {
    stubFetch(async () => { throw new TypeError('Failed to fetch'); });
    const error = await request('/projects').catch((e: ApiError) => e) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ kind: 'network', status: null });
    expect(error.message).toBe('Не удалось связаться с ядром ROSC. Проверьте, что backend запущен, и повторите.');
  });

  it('keeps the backend detail for 409 and marks it as manual-resolution conflict', async () => {
    stubFetch(async () => reply(409, { detail: 'Workspace manifest is larger than the limit; manual resolution is required.' }));
    const error = (await request('/x').catch((e: ApiError) => e)) as ApiError;
    expect(error).toMatchObject({ kind: 'conflict', status: 409, isConflict: true });
    expect(error.message).toBe('Workspace manifest is larger than the limit; manual resolution is required.');
  });

  it('keeps the uncertain-outcome detail for a post-replace 503', async () => {
    stubFetch(async () => reply(503, { detail: 'Workspace repair outcome is uncertain: fsync failed' }));
    const error = (await request('/x').catch((e: ApiError) => e)) as ApiError;
    expect(error).toMatchObject({ kind: 'server', status: 503, isServerFailure: true });
    expect(error.message).toBe('Workspace repair outcome is uncertain: fsync failed');
  });

  it('falls back to a status message when the body has no string detail', async () => {
    stubFetch(async () => new Response('<html>', { status: 502 }));
    expect(((await request('/x').catch((e: ApiError) => e)) as ApiError).message).toBe('Ядро ROSC недоступно или вернуло ошибку (HTTP 502). Проверьте, что backend запущен, и повторите.');
    stubFetch(async () => reply(422, { detail: [{ msg: 'x' }] }));
    expect(((await request('/x').catch((e: ApiError) => e)) as ApiError).message).toBe('HTTP 422');
  });
});

describe('operations', () => {
  it('repair always sends confirm_repair: true to the right endpoint', async () => {
    const spy = stubFetch(async () => reply(200, { id: 'P0001', repair: null }));
    await repairProject('P0001');
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/projects/P0001/repair-workspace');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ confirm_repair: true });
  });

  it('creates a Task Draft under its Project', async () => {
    const spy = stubFetch(async () => reply(201, { id: 'T0001' }));
    await createTask('P0002', { title: 'a', description: 'b', acceptance_criteria: 'c' });
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/projects/P0002/tasks');
    expect(JSON.parse(init.body as string)).toEqual({ title: 'a', description: 'b', acceptance_criteria: 'c' });
  });
});
