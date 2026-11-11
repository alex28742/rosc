// The single HTTP client. Everything under /api goes through `request`; nothing else may call fetch.

export type ApiErrorKind = 'network' | 'conflict' | 'server' | 'client';

export class ApiError extends Error {
  constructor(message: string, readonly kind: ApiErrorKind, readonly status: number | null, readonly code: string | null = null) {
    super(message);
    this.name = 'ApiError';
  }

  /** The backend refused with 5xx: for a repair this means the outcome may be uncertain, never a success. */
  get isServerFailure(): boolean { return this.kind === 'server'; }

  /** 409: the manifest needs manual resolution; retrying will not help. */
  get isConflict(): boolean { return this.kind === 'conflict'; }
}

const NETWORK_MESSAGE = 'Не удалось связаться с ядром ROSC. Проверьте, что backend запущен, и повторите.';

function kindFor(status: number): ApiErrorKind {
  if (status === 409) return 'conflict';
  return status >= 500 ? 'server' : 'client';
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } });
  } catch {
    throw new ApiError(NETWORK_MESSAGE, 'network', null);
  }
  if (!response.ok) {
    let code: string | null = null;
    let detail = response.status >= 500
      ? `Ядро ROSC недоступно или вернуло ошибку (HTTP ${response.status}). Проверьте, что backend запущен, и повторите.`
      : `HTTP ${response.status}`;
    try {
      const json = await response.json();
      if (typeof json.detail === 'string') detail = json.detail;
      else if (json.detail && typeof json.detail === 'object' && typeof json.detail.message === 'string') {
        detail = json.detail.message;
        code = typeof json.detail.code === 'string' ? json.detail.code : null;
      }
    } catch { /* keep the status-based message */ }
    throw new ApiError(detail, kindFor(response.status), response.status, code);
  }
  return response.json() as Promise<T>;
}

export function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
}

/** `PATCH` is only used for the version-guarded full Agent metadata replacement. */
export function patch<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: 'PATCH', body: JSON.stringify(body) });
}

export function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
