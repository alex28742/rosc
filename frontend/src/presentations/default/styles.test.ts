import { describe, expect, it } from 'vitest';
// Vitest stubs CSS module contents (even with `?raw`), so these guards read the package's actual
// stylesheet bytes from disk. The frontend deliberately has no @types/node; only the test runner
// provides this API.
// @ts-expect-error -- Node API available to Vitest, not part of the browser-facing source
import { readFileSync } from 'node:fs';

// Style-source guards for the presentation package. The approved v0.6 visual system must arrive as
// readable, package-owned CSS in one scoped token layer, not as inline styles or a palette in JSX.
// The checks read the real files, so a missing, moved or unscoped stylesheet fails here instead of
// passing silently. (Colour/structure is verified in browser evidence.)

type Rule = { header: string; declarations: string[]; rules: Rule[] };

/** Minimal brace-aware reader: rule headers, own declarations and nested rules (media/keyframes). */
function parseRules(css: string): Rule[] {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, '');
  let i = 0;
  const block = (): { rules: Rule[]; declarations: string[]; end: number } => {
    const rules: Rule[] = [];
    const declarations: string[] = [];
    let buffer = '';
    while (i < text.length) {
      const char = text[i]!;
      if (char === '{') {
        i += 1;
        const header = buffer.trim().replace(/\s+/g, ' ');
        buffer = '';
        const inner = block();
        rules.push({ header, declarations: inner.declarations, rules: inner.rules });
        i = inner.end;
      } else if (char === ';') {
        i += 1;
        const declaration = buffer.trim();
        if (declaration) declarations.push(declaration);
        buffer = '';
      } else if (char === '}') {
        i += 1;
        const declaration = buffer.trim();
        if (declaration) declarations.push(declaration);
        return { rules, declarations, end: i };
      } else {
        buffer += char;
        i += 1;
      }
    }
    return { rules, declarations, end: i };
  };
  return block().rules;
}

/** Every comma-separated selector written in a sheet; at-rule headers and keyframe steps excluded. */
function selectorsOf(rules: Rule[], insideKeyframes = false): string[] {
  return rules.flatMap(rule => {
    const isAtRule = rule.header.startsWith('@');
    const own = isAtRule || insideKeyframes ? [] : rule.header.split(',').map(part => part.trim()).filter(Boolean);
    return [...own, ...selectorsOf(rule.rules, rule.header.startsWith('@keyframes'))];
  });
}

function declarationsOf(rules: Rule[], prefix: string): { selector: string; declaration: string }[] {
  return rules.flatMap(rule => [
    ...rule.declarations.filter(declaration => declaration.startsWith(prefix)).map(declaration => ({ selector: rule.header, declaration })),
    ...declarationsOf(rule.rules, prefix),
  ]);
}

const PARTIALS = ['base.css', 'dashboard.css', 'index.css', 'modal.css', 'shell.css', 'tokens.css', 'work-area.css'];
// Allowlist for the whole `default` package: any CSS file that appears anywhere else in the package
// (a returning legacy ./style.css, a stray ./views/rogue.css, ...) must fail this guard.
const ALLOWED_CSS = PARTIALS.map(name => `./styles/${name}`).sort();
/** Every `.css` anywhere under the package, as glob keys relative to this test file. */
const packageCssFiles: string[] = Object.keys(import.meta.glob('./**/*.css')).sort();

const stylesDir = (() => {
  // The Vitest runner executes with the frontend package as its working directory (`npm test`).
  // @ts-expect-error -- Node global provided by the runner, not part of the browser-facing source
  const root: string = process.cwd();
  return `${root}/src/presentations/default/styles`;
})();
const cssFiles: string[] = packageCssFiles.filter(path => path.startsWith('./styles/')).map(path => path.slice('./styles/'.length));
const sheets = cssFiles.map(name => {
  const css = readFileSync(`${stylesDir}/${name}`, 'utf8');
  return { name, selectors: selectorsOf(parseRules(css)), rules: parseRules(css) };
});
const modules = Object.entries(import.meta.glob('./**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true }) as Record<string, string>)
  .filter(([path]) => !path.includes('.test.'));

describe('presentation styling', () => {
  it('inventories every stylesheet in the package and rejects CSS outside ./styles/', () => {
    expect(packageCssFiles.length).toBeGreaterThan(0);
    expect(packageCssFiles, 'the legacy one-line package sheet is gone').not.toContain('./style.css');
    expect(packageCssFiles, 'every package stylesheet lives in ./styles/').toEqual(ALLOWED_CSS);
    expect(cssFiles).toEqual(PARTIALS);
  });

  it('loads the partials once, in token-first order, from the package entry point', () => {
    const entry = readFileSync(`${stylesDir}/index.css`, 'utf8');
    const imported = [...entry.matchAll(/@import\s+'\.\/([^']+\.css)';/g)].map(match => match[1]);
    expect(imported).toEqual(['tokens.css', 'base.css', 'shell.css', 'dashboard.css', 'work-area.css', 'modal.css']);
    const index = modules.find(([path]) => path === './index.ts')?.[1] ?? '';
    expect(index).toMatch(/^import '\.\/styles\/index\.css';$/m);
  });

  it('scopes every package selector, so no rule leaks onto a bare class or the global root', () => {
    const unscoped = sheets.flatMap(({ name, selectors }) => selectors.filter(selector => !selector.startsWith('.rosc-')).map(selector => `${name}: ${selector}`));
    expect(unscoped).toEqual([]);
    const rootScoped = sheets.flatMap(({ name, selectors }) => selectors.filter(selector => /(^|[\s>+~]):root\b/.test(selector)).map(selector => `${name}: ${selector}`));
    expect(rootScoped, 'tokens belong to .rosc-app, not :root').toEqual([]);
  });

  it('keeps the dialog backdrop selector scoped while .modal-back stays a DOM hook', () => {
    const backdrop = sheets.flatMap(({ name, selectors }) => selectors.filter(selector => selector.includes('modal-back')).map(selector => ({ name, selector })));
    expect(backdrop.length).toBeGreaterThan(0);
    const bare = backdrop.filter(({ selector }) => !selector.startsWith('.rosc-app'));
    expect(bare, 'a bare .modal-back also styles anything outside the package').toEqual([]);
    expect(backdrop.some(({ selector }) => selector === '.rosc-app .modal-back'), 'the backdrop rule itself is scoped').toBe(true);
  });

  it('declares the token vocabulary on .rosc-app only', () => {
    const tokens = sheets.flatMap(({ rules }) => declarationsOf(rules, '--rosc-'));
    expect(tokens.length).toBeGreaterThan(20);
    expect([...new Set(tokens.map(token => token.selector))]).toEqual(['.rosc-app']);
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
