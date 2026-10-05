// Cliente HTTP da API do gateway. Base `/api/v1`, JSON em snake_case.
// A sessão (JWT) vai no cabeçalho Authorization; um 401 encerra a sessão e
// leva para o login.

import { clearSession, getToken } from '../lib/session';

export const API_BASE = '/api/v1';

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly errors: string[];

  constructor(status: number, detail: string, errors: string[] = []) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.errors = errors;
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

export type QueryValue = string | number | boolean | null | undefined;
export type Query = Record<string, QueryValue>;

export interface RequestOptions {
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
  /** Rota pública (GET /config, login): não envia a sessão nem trata 401. */
  anonymous?: boolean;
}

const STATUS_MESSAGES: Record<number, string> = {
  400: 'Requisição inválida.',
  401: 'Sua sessão expirou. Entre novamente.',
  403: 'Você não tem permissão para esta ação.',
  404: 'Recurso não encontrado.',
  409: 'Conflito com o estado atual do recurso.',
  422: 'Dados inválidos.',
  429: 'Muitas requisições. Aguarde um instante.',
  500: 'Erro interno da API.',
  502: 'O provedor ou a API está indisponível no momento.',
  503: 'A API está indisponível no momento.',
  504: 'A API demorou demais para responder.',
};

export function toSearchParams(query?: Query): URLSearchParams {
  const params = new URLSearchParams();
  if (!query) return params;
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue;
    params.set(key, String(value));
  }
  return params;
}

export function buildUrl(path: string, query?: Query): string {
  const qs = toSearchParams(query).toString();
  return `${API_BASE}${path}${qs ? `?${qs}` : ''}`;
}

let unauthorizedHandled = false;

export function handleUnauthorized(): void {
  if (unauthorizedHandled) return;
  unauthorizedHandled = true;
  clearSession();
  const here = window.location.pathname + window.location.search;
  const next = here && !here.startsWith('/login') ? `?next=${encodeURIComponent(here)}` : '';
  window.location.assign(`/login${next}`);
}

/** Converte a resposta de erro (formato do console ou da OpenAI) em ApiError. */
export async function toApiError(res: Response): Promise<ApiError> {
  const fallback = STATUS_MESSAGES[res.status] ?? `Erro ${res.status}.`;
  let payload: unknown = null;
  try {
    const text = await res.text();
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (payload && typeof payload === 'object') {
    const body = payload as { detail?: unknown; errors?: unknown; error?: { message?: unknown } };
    const errors = Array.isArray(body.errors) ? body.errors.map(String) : [];
    if (typeof body.detail === 'string') return new ApiError(res.status, body.detail, errors);
    if (body.error && typeof body.error.message === 'string') return new ApiError(res.status, body.error.message);
    return new ApiError(res.status, fallback, errors);
  }
  return new ApiError(res.status, fallback);
}

export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (!opts.anonymous) Object.assign(headers, authHeaders());
  let body: BodyInit | undefined;
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }
  let res: Response;
  try {
    res = await fetch(buildUrl(path, opts.query), { method, headers, body, signal: opts.signal });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError(0, 'Não foi possível conectar à API. Verifique a rede e tente de novo.');
  }
  if (res.status === 401 && !opts.anonymous) handleUnauthorized();
  if (!res.ok) throw await toApiError(res);
  if (res.status === 204) return undefined as T;
  const type = res.headers.get('content-type') ?? '';
  if (type.includes('json')) return (await res.json()) as T;
  return (await res.text()) as unknown as T;
}

export const api = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) => request<T>('GET', path, { query, signal }),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, { body: body ?? {} }),
  patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, { body }),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, { body }),
  delete: <T = void>(path: string) => request<T>('DELETE', path),
};

/** Baixa um arquivo autenticado (CSV de uso): fetch com a sessão → Blob → link temporário. */
export async function downloadFile(path: string, query: Query | undefined, filename: string): Promise<void> {
  let res: Response;
  try {
    res = await fetch(buildUrl(path, query), { headers: authHeaders() });
  } catch {
    throw new ApiError(0, 'Não foi possível conectar à API.');
  }
  if (res.status === 401) handleUnauthorized();
  if (!res.ok) throw await toApiError(res);
  const disposition = res.headers.get('content-disposition');
  const match = disposition?.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  if (match?.[1]) filename = decodeURIComponent(match[1]);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
