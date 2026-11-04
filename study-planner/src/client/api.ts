import { useSyncExternalStore } from 'react';
import type { PublicState } from '../shared/types';

let state: PublicState | null = null;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export function useAppState(): PublicState | null {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => state,
  );
}

export async function api<T = PublicState>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.error ?? `${res.status} ${res.statusText}`);
  if (json && typeof json === 'object' && 'sessions' in json && 'settings' in json) {
    state = json as PublicState;
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
  setTimeout(() => {
    toasts = toasts.filter((x) => x.id !== t.id);
    toastListeners.forEach((l) => l());
  }, kind === 'error' ? 7000 : 3500);
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
    toast((e as Error).message, 'error');
    return undefined;
  }
}
