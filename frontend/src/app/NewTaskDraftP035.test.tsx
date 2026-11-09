import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Feedback as FeedbackState } from '../app/presentation/contracts';
import { NewTaskView } from '../presentations/default/views/NewTaskView';

const feedback = (over: Partial<FeedbackState> = {}): FeedbackState => ({ error: '', notice: '', ...over });

const renderView = (over: { busy?: boolean; feedback?: FeedbackState } = {}) => {
  const onSubmit = vi.fn();
  const onClose = vi.fn();
  const view = (state: { busy?: boolean; feedback?: FeedbackState }) => <NewTaskView
    projectName="ROSC Platform"
    feedback={state.feedback ?? feedback()}
    busy={state.busy ?? false}
    inert={false}
    onSubmit={onSubmit}
    onClose={onClose}
  />;
  const rendered = render(view(over));
  return { ...rendered, onSubmit, onClose, view };
};

const drawer = () => screen.getByLabelText('Новая Task');

afterEach(cleanup);

describe('P035 New Task Draft panel', () => {
  it('shows the prototype header, informational Draft chip and introduction', () => {
    renderView();
    const panel = drawer();
    expect(within(panel).getByText('PROJECT / НОВАЯ ЗАДАЧА · DRAFT')).toBeTruthy();
    expect(within(panel).getByRole('heading', { name: 'Новая задача' })).toBeTruthy();
    expect(within(panel).getByText('ROSC Platform · создание Task не запускает Worker')).toBeTruthy();
    const chip = within(panel).getByText('Черновик · без Prompt / Run');
    expect(chip.tagName).toBe('SPAN');
    expect(chip.getAttribute('role')).toBeNull();
    expect(within(panel).getByRole('heading', { name: 'Постановка новой задачи' })).toBeTruthy();
    expect(within(panel).getByText('Owner и Architect пользуются одним Core-инструментом создания задачи.')).toBeTruthy();
    expect(within(panel).getByText('Создание Task Draft не создаёт Prompt и не запускает агентскую сессию.')).toBeTruthy();
  });

  it('uses the approved labels, required indicators, maxlength and placeholders', () => {
    renderView();
    const title = within(drawer()).getByLabelText('Название задачи *') as HTMLInputElement;
    const description = within(drawer()).getByLabelText('Описание и контекст *') as HTMLTextAreaElement;
    const criteria = within(drawer()).getByLabelText('Критерии приёмки *') as HTMLTextAreaElement;

    expect(title.required).toBe(true);
    expect(title.maxLength).toBe(160);
    expect(title.getAttribute('placeholder')).toBe('Например, Добавить проверку статусов API');
    expect(description.required).toBe(true);
    expect(description.getAttribute('placeholder')).toBe('Что требуется реализовать, ограничения и исходные решения');
    expect(criteria.required).toBe(true);
    expect(criteria.getAttribute('placeholder')).toBe('Как Worker, Architect и Owner проверят результат');
    expect(within(drawer()).getByText(/Критерии станут общим источником проверки/)).toBeTruthy();
  });

  it('renders the attachment affordance disabled, without inventing an upload path', () => {
    renderView();
    const attach = within(drawer()).getByRole('button', { name: 'Прикрепить файлы' }) as HTMLButtonElement;
    expect(attach.disabled).toBe(true);
    expect(attach.getAttribute('title')).toBe('Прикрепление файлов пока недоступно');
    expect(attach.getAttribute('aria-describedby')).toBe('rosc-new-task-attach-hint');
    expect(drawer().querySelector('input[type="file"]')).toBeNull();
    expect(within(drawer()).getByText(/ROSC ещё не поддерживает вложения/)).toBeTruthy();
  });

  it('closes on Cancel and on the disabled attachment without submitting', () => {
    const { onSubmit, onClose } = renderView();
    fireEvent.click(within(drawer()).getByRole('button', { name: 'Отмена' }));
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(within(drawer()).getByRole('button', { name: 'Прикрепить файлы' }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('submits exactly the three Core Task Draft values through the existing callback', () => {
    const { onSubmit } = renderView();
    fireEvent.change(within(drawer()).getByLabelText('Название задачи *'), { target: { value: 'New task' } });
    fireEvent.change(within(drawer()).getByLabelText('Описание и контекст *'), { target: { value: 'Context' } });
    fireEvent.change(within(drawer()).getByLabelText('Критерии приёмки *'), { target: { value: 'Reviewed' } });
    fireEvent.submit(drawer().querySelector('form')!);
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({ title: 'New task', description: 'Context', acceptance_criteria: 'Reviewed' });
  });

  it('blocks a duplicate submit while busy and shows the saving label', () => {
    const { onSubmit } = renderView({ busy: true });
    const submit = within(drawer()).getByRole('button', { name: 'Создание Task Draft…' }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    expect(submit.getAttribute('aria-busy')).toBe('true');
    fireEvent.submit(drawer().querySelector('form')!);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('shows a Core error without losing the values already typed', () => {
    const { rerender, view } = renderView();
    fireEvent.change(within(drawer()).getByLabelText('Название задачи *'), { target: { value: 'Keep me' } });
    rerender(view({ feedback: feedback({ error: 'Не удалось создать Task Draft.' }) }));
    expect(screen.getByRole('alert').textContent).toBe('Не удалось создать Task Draft.');
    expect((within(drawer()).getByLabelText('Название задачи *') as HTMLInputElement).value).toBe('Keep me');
  });
});
