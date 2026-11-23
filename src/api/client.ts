import type { ActorRole } from '../shared/types';

export interface AuthHeadersConfig {
  tenantId: string;
  actorId: string;
  actorName: string;
  actorRole: ActorRole;
}

let currentAuth: AuthHeadersConfig = {
  tenantId: 'tenant-wasp-ops',
  actorId: 'usr-jlee',
  actorName: 'J. Lee',
  actorRole: 'supervisor',
};

export function setClientAuth(next: Partial<AuthHeadersConfig>) {
  currentAuth = { ...currentAuth, ...next };
}

export function getClientAuth(): AuthHeadersConfig {
  return currentAuth;
}

export class ApiError extends Error {
  public readonly status: number;
  public readonly code: string;
  public readonly correlationId?: string;
  public readonly retryable?: boolean;

  constructor(status: number, code: string, message: string, correlationId?: string, retryable?: boolean) {
    super(message);
    this.status = status;
    this.code = code;
    this.correlationId = correlationId;
    this.retryable = retryable;
  }
}

export function makeIdempotencyKey(prefix: string): string {
  const rand = Math.random().toString(36).slice(2, 10);
  return `${prefix}_${Date.now().toString(36)}_${rand}`;
}

export async function apiRequest<T>(
  path: string,
  options: {
    method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
    body?: unknown;
    signal?: AbortSignal;
    timeoutMs?: number;
  } = {},
): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? 12000);

  if (options.signal) {
    options.signal.addEventListener('abort', () => controller.abort(), { once: true });
  }

  try {
    const res = await fetch(`/api/v1${path}`, {
      method: options.method ?? 'GET',
      headers: {
        'Content-Type': 'application/json',
        'X-Tenant-Id': currentAuth.tenantId,
        'X-Actor-Id': currentAuth.actorId,
        'X-Actor-Name': currentAuth.actorName,
        'X-Actor-Role': currentAuth.actorRole,
      },
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      signal: controller.signal,
    });

    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = data?.error ?? {};
      throw new ApiError(
        res.status,
        err.code || `HTTP_${res.status}`,
        err.message || `Request failed with status ${res.status}`,
        err.correlation_id,
        err.retryable,
      );
    }
    return data as T;
  } finally {
    clearTimeout(timeout);
  }
}
