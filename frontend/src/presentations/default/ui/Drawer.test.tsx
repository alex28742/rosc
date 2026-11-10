import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Drawer } from './Drawer';
import { NewTaskView } from '../views/NewTaskView';

afterEach(cleanup);

const renderDrawer = (variant: 'project' | 'task' | 'new-task' | 'settings') => render(
  <Drawer variant={variant} label="Layer" closeLabel="Закрыть" inert={false} onClose={vi.fn()} head={<span>Head</span>}>
    Body
  </Drawer>,
);

describe('P036 drawer variants', () => {
  it('gives New Task its own scoped layer class and nested backdrop', () => {
    renderDrawer('new-task');
    const layer = screen.getByLabelText('Layer');
    expect(layer.className).toContain('rosc-drawer--new-task');
    expect(layer.className).toContain('new-task-drawer');
    expect(layer.className).not.toContain('rosc-drawer--task');
    expect(layer.parentElement?.className).toBe('rosc-backdrop--nested');
    expect(layer.querySelector('.rosc-edge-caption')?.textContent).toBe('TASK');
  });

  it('leaves the ordinary Task layer and the non-nested Project/Settings layers unchanged', () => {
    for (const variant of ['task', 'project', 'settings'] as const) {
      const { unmount } = renderDrawer(variant);
      const layer = screen.getByLabelText('Layer');
      expect(layer.className).toContain(`rosc-drawer--${variant}`);
      expect(layer.parentElement?.className).toBe(variant === 'task' ? 'rosc-backdrop--nested' : 'rosc-backdrop');
      unmount();
    }
  });

  it('uses the New Task variant from the view, so the shared Task geometry is never reused', () => {
    render(<NewTaskView projectName="ROSC Platform" feedback={{ error: '', notice: '' }} busy={false} inert={false}
      onSubmit={vi.fn()} onClose={vi.fn()} />);
    const layer = screen.getByLabelText('Новая Task');
    expect(layer.className).toContain('rosc-drawer--new-task');
    expect(layer.className).not.toContain('rosc-drawer--task');
    expect(layer.parentElement?.className).toBe('rosc-backdrop--nested');
  });
});
