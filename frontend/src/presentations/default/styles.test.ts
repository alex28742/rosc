import { describe, expect, it } from 'vitest';

// Style-source guards for the presentation package. The approved v0.6 visual system must arrive as
// readable, package-owned CSS in one token layer, not as inline styles or a palette in JSX.
// (Colour/structure content is verified in the browser evidence; these checks guard the shape.)
const sources = import.meta.glob('./**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }) as Record<string, string>;
const modules = Object.entries(sources).filter(([path]) => !path.includes('.test.'));
const cssFiles = Object.keys(import.meta.glob('./styles/*.css')).map(path => path.split('/').pop()!);

describe('presentation styling', () => {
  it('keeps the package stylesheet as a token layer plus focused partials', () => {
    expect(cssFiles.sort()).toEqual(['base.css', 'dashboard.css', 'index.css', 'modal.css', 'shell.css', 'tokens.css', 'work-area.css']);
  });

  it('has no leftover minified package sheet', () => {
    expect(Object.keys(sources).some(path => path.endsWith('/style.css'))).toBe(false);
  });

  it('keeps presentation modules free of inline styles and literal colours', () => {
    const offenders = modules.flatMap(([path, source]) => {
      const found: string[] = [];
      if (/style=\{/.test(source)) found.push(`${path}: inline style prop`);
      if (/#[0-9a-fA-F]{3,8}\b/.test(source)) found.push(`${path}: literal colour`);
      return found;
    });
    expect(offenders).toEqual([]);
  });

  it('reuses the shared UI primitives instead of re-implementing overlays per view', () => {
    const views = modules.filter(([path]) => path.includes('/views/'));
    expect(views.length).toBeGreaterThan(3);
    for (const [path, source] of views) {
      expect(source, `${path} must not style overlays on its own`).not.toMatch(/position:\s*fixed|boxShadow|borderRadius/);
    }
  });
});
