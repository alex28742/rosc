import type { Task } from '../../../core/models';

/**
 * P058: the human-facing Project-local Task label ("№5"). The global technical ID (T0105) is never shown as if it were
 * the local number; when the ordinal is not loaded yet a neutral placeholder is shown, never a fabricated number.
 */
export function taskNumberLabel(task: Pick<Task, 'project_number'> | null | undefined): string {
  const n = task?.project_number;
  return typeof n === 'number' && Number.isInteger(n) && n > 0 ? `№${n}` : '№—';
}
