import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { EngineNotice, EngineNoticeKind } from '../../../app/presentation/contracts';
import { EngineNoticeStack } from './EngineNoticeStack';

const notice = (key: string, kind: EngineNoticeKind, over: Partial<EngineNotice> = {}): EngineNotice => ({
  key, kind, kicker: 'ROSC · СИСТЕМА', title: `Заголовок ${key}`, description: `Описание ${key}`, ...over,
});

afterEach(cleanup);

describe('Engine notice stack', () => {
  it('renders all four approved kinds with status/alert roles and a labelled close button', () => {
    render(<EngineNoticeStack notices={[
      notice('progress', 'progress'), notice('success', 'success'), notice('warning', 'warning'),
    ]} onDismiss={vi.fn()} />);
    const statuses = screen.getAllByRole('status').map(node => node.textContent).join(' ');
    expect(statuses).toContain('Заголовок progress');
    expect(statuses).toContain('Заголовок success');
    expect(statuses).toContain('Заголовок warning');
    expect(document.querySelector('.rosc-engine-notice__spin')).toBeTruthy();
    expect(screen.getAllByLabelText('Закрыть уведомление')).toHaveLength(3);

    cleanup();
    render(<EngineNoticeStack notices={[notice('error', 'error')]} onDismiss={vi.fn()} />);
    expect(screen.getByRole('alert').textContent).toContain('Заголовок error');
  });

  it('shows at most three cards, newest first, and never the removed demo panel', () => {
    const { container } = render(<EngineNoticeStack notices={[
      notice('first', 'success'), notice('second', 'success'), notice('third', 'success'), notice('fourth', 'success'),
    ]} onDismiss={vi.fn()} />);
    const titles = [...container.querySelectorAll('.rosc-engine-notice__title')].map(node => node.textContent);
    expect(titles).toEqual(['Заголовок fourth', 'Заголовок third', 'Заголовок second']);
    expect(container.querySelector('#uxNoticeStack')).toBeNull();
    expect(container.querySelector('.ux-demo-panel')).toBeNull();
    expect(screen.queryByText(/демо|сценари/i)).toBeNull();
  });

  it('reports the dismiss key of the card the Owner closes', () => {
    const onDismiss = vi.fn();
    render(<EngineNoticeStack notices={[notice('mcp', 'warning')]} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByLabelText('Закрыть уведомление'));
    expect(onDismiss).toHaveBeenCalledWith('mcp');
  });

  it('renders nothing when there is no Engine notice', () => {
    const { container } = render(<EngineNoticeStack notices={[]} onDismiss={vi.fn()} />);
    expect(container.querySelector('.rosc-engine-notice-stack')).toBeNull();
  });

  it('supports the P029 onboarding card as a UI-only type, without inventing a live signal', () => {
    render(<EngineNoticeStack notices={[notice('onboarding', 'success', {
      kicker: 'NEOPLATFORM · ARCHITECT', title: 'Architect готов к работе',
      description: 'Начальное знакомство с проектом завершено. Можно продолжать диалог.',
    })]} onDismiss={vi.fn()} />);
    expect(screen.getByText('NEOPLATFORM · ARCHITECT')).toBeTruthy();
    expect(screen.getByText('Architect готов к работе')).toBeTruthy();
  });
});
