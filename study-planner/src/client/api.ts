import { useSyncExternalStore } from 'react';
import type { PublicState } from '../shared/types';
import type { LearnedCourse } from '../planner/learning';

export type AppState = PublicState & { learning: Record<string, LearnedCourse> };

/** `undefined` = still loading; `null` = signed out. */
let state: AppState | null | undefined = undefined;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export function useAppState(): AppState | null | undefined {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => state,
  );
}

export function setSignedOut() {
  state = null;
  emit();
}

const isState = (j: any): j is AppState => j && typeof j === 'object' && 'sessions' in j && 'settings' in j && 'user' in j;

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export async function api<T = AppState>(method: string, path: string, body?: unknown, opts: { raw?: Blob | ArrayBuffer | string; contentType?: string } = {}): Promise<T> {
  const headers: Record<string, string> = {};
  let payload: BodyInit | undefined;
  if (opts.raw !== undefined) {
    payload = opts.raw as BodyInit;
    headers['Content-Type'] = opts.contentType ?? 'application/octet-stream';
  } else if (body !== undefined) {
    payload = JSON.stringify(body);
    headers['Content-Type'] = 'application/json';
  }
  const res = await fetch(path, { method, headers, body: payload, credentials: 'same-origin' });
  const json = await res.json().catch(() => ({}));
  if (res.status === 401) {
    setSignedOut();
    throw new ApiError(json.error ?? 'Please sign in.', 401);
  }
  if (!res.ok) throw new ApiError(json.error ?? `${res.status} ${res.statusText}`, res.status);
  if (isState(json)) {
    state = json;
    emit();
  } else if (json && typeof json === 'object' && isState((json as any).state)) {
    state = (json as any).state;
    emit();
  }
  return json as T;
}

export const refresh = () => api('GET', '/api/state');

// Toasts
export interface Toast {
  id: number;
  text: string;
  kind: 'info' | 'error';
}
let toasts: Toast[] = [];
const toastListeners = new Set<() => void>();
export function toast(text: string, kind: Toast['kind'] = 'info') {
  const t = { id: Date.now() + Math.random(), text, kind };
  toasts = [...toasts, t];
  toastListeners.forEach((l) => l());
  setTimeout(
    () => {
      toasts = toasts.filter((x) => x.id !== t.id);
      toastListeners.forEach((l) => l());
    },
    kind === 'error' ? 7000 : 3500,
  );
}
export function useToasts(): Toast[] {
  return useSyncExternalStore(
    (l) => {
      toastListeners.add(l);
      return () => toastListeners.delete(l);
    },
    () => toasts,
  );
}

/** Runs an API call and reports failures as a toast. */
export async function act<T>(p: Promise<T>, ok?: string): Promise<T | undefined> {
  try {
    const r = await p;
    if (ok) toast(ok);
    return r;
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 401)) toast((e as Error).message, 'error');
    return undefined;
  }
}
