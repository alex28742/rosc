import { describe, expect, it, vi } from 'vitest';
import { hostGuard, hostGuardPlugin, isAllowedHostAuthority } from '../hostGuard';

const VALID = ['localhost', 'LOCALHOST', 'LocalHost:5173', '127.0.0.1', '127.0.0.1:8000', '[::1]', '[::1]:8000',
  'localhost:1', 'localhost:65535', 'localhost:4173'];
const INVALID = ['evil.example', 'localhost.evil.example', 'localhost.', '[::1]evil', '[::1]:8000evil', '[::1', '::1', '::1:8000',
  '[::1]:', 'localhost:abc', 'localhost:65536', 'localhost:0', 'localhost:', 'localhost:80garbage', 'localhost:-1', 'localhost:+80',
  'localhost:8000:evil', '127.0.0.1:65536', '127.0.0.1.evil.example', '0.0.0.0', '*', '*.localhost', '', ' ', ' localhost',
  'localhost ', 'local host', 'localhost\t', 'localhost\r\n', 'localhost%2f', 'user@localhost', 'localhost/path', 'localhost?x',
  'localhost#f', 'localhost,evil.example', 'localhost, localhost', '[0:0:0:0:0:0:0:1]', '[::]', '[::ffff:127.0.0.1]', 'localhost:١٢٣',
  'localhost:000000'];

function run(rawHeaders: string[]) {
  const res = { statusCode: 200, setHeader: vi.fn(), end: vi.fn() };
  const next = vi.fn();
  hostGuard({ headers: {}, rawHeaders }, res, next);
  return { res, next };
}

describe('original Host authority guard', () => {
  it.each(VALID)('accepts %j', (host) => {
    expect(isAllowedHostAuthority(host)).toBe(true);
    const { next, res } = run(['Host', host]);
    expect(next).toHaveBeenCalledOnce();
    expect(res.end).not.toHaveBeenCalled();
  });

  it.each(INVALID)('rejects %j without forwarding', (host) => {
    expect(isAllowedHostAuthority(host)).toBe(false);
    const { next, res } = run(['Host', host]);
    expect(next).not.toHaveBeenCalled();
    expect(res.statusCode).toBe(403);
  });

  it('rejects missing, duplicate and non-string hosts', () => {
    for (const raw of [[], ['Accept', '*/*'], ['Host', 'localhost', 'host', 'localhost'], ['Host', 'localhost', 'HOST', 'evil.example'],
      ['host', 'evil.example', 'Host', 'localhost']]) {
      const { next, res } = run(raw);
      expect(next).not.toHaveBeenCalled();
      expect(res.statusCode).toBe(403);
    }
    expect(isAllowedHostAuthority(undefined)).toBe(false);
    expect(isAllowedHostAuthority(['localhost'])).toBe(false);
  });

  it('registers the guard directly on both dev and preview middleware stacks', () => {
    const plugin = hostGuardPlugin();
    for (const hook of [plugin.configureServer, plugin.configurePreviewServer]) {
      const use = vi.fn();
      expect(hook({ middlewares: { use } })).toBeUndefined();
      expect(use).toHaveBeenCalledWith(hostGuard);
    }
  });
});
