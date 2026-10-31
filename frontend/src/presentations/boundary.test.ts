import { describe, expect, it } from 'vitest';

// Presentation packages are view-only: they may use view contracts and Core *types*, never the API client,
// the network, the router or storage. Business rules (payload normalisation, approvals) stay outside.
const sources = import.meta.glob('./**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }) as Record<string, string>;
const files = Object.entries(sources).filter(([path]) => !path.endsWith('.test.ts') && !path.endsWith('.test.tsx'));

const FORBIDDEN: [string, RegExp][] = [
  ['imports core/api', /from\s+['"][^'"]*core\/api[^'"]*['"]/],
  ['imports the router or navigation', /from\s+['"][^'"]*app\/(route|navigation|useWorkspaceController|App)[^'"]*['"]/],
  ['calls fetch', /\bfetch\s*\(/],
  ['uses XMLHttpRequest/WebSocket/EventSource', /\b(XMLHttpRequest|WebSocket|EventSource)\b/],
  ['reads the location or history', /\b(location|history)\.|window\.(location|history)|popstate|pushState/],
  ['touches storage', /\b(localStorage|sessionStorage|indexedDB)\b/],
  ['imports a runtime value from core/models', /import\s+(?!type\b)\{[^}]*\}\s+from\s+['"][^'"]*core\/models['"]/],
];

describe('presentation boundary', () => {
  it('scans the default package', () => expect(files.length).toBeGreaterThan(5));

  it.each(FORBIDDEN)('no presentation file %s', (_label, pattern) => {
    const offenders = files.filter(([, source]) => pattern.test(source)).map(([path]) => path);
    expect(offenders).toEqual([]);
  });
});
