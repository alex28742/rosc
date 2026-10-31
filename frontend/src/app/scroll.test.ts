import { afterEach, describe, expect, it } from 'vitest';
import { ScrollMemory } from './scroll';

const region = () => { const el = document.createElement('div'); document.body.append(el); return el; };
const scrollTo = (el: HTMLElement, top: number) => { el.scrollTop = top; el.dispatchEvent(new Event('scroll')); };

afterEach(() => { document.body.innerHTML = ''; });

describe('ScrollMemory', () => {
  it('restores a region that was closed and mounted again', () => {
    const memory = new ScrollMemory();
    const first = region();
    memory.region('project:P1')(first);
    scrollTo(first, 240);
    memory.region('project:P1')(null);
    const second = region();
    memory.region('project:P1')(second);
    expect(second.scrollTop).toBe(240);
  });

  it('keeps regions independent and hands out a stable ref per key', () => {
    const memory = new ScrollMemory();
    const a = region(), b = region();
    memory.region('a')(a); memory.region('b')(b);
    scrollTo(a, 10); scrollTo(b, 99);
    expect([memory.read('a'), memory.read('b'), memory.read('c')]).toEqual([10, 99, 0]);
    expect(memory.region('a')).toBe(memory.region('a'));
  });

  it('re-applies the saved position when content arrives late, until the user scrolls', async () => {
    const memory = new ScrollMemory();
    const first = region();
    memory.region('p')(first);
    scrollTo(first, 300);
    memory.region('p')(null);
    const el = region();
    memory.region('p')(el);
    el.scrollTop = 0; // clamped while the list was still empty
    el.append(document.createElement('p'));
    await Promise.resolve();
    expect(el.scrollTop).toBe(300);
    scrollTo(el, 50); // user takes over
    el.append(document.createElement('p'));
    await Promise.resolve();
    expect(el.scrollTop).toBe(50);
  });
});
