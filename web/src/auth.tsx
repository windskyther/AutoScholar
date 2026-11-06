import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ApiClient, ApiError } from './api';
import type { Session } from './types';

interface Connection {
  api: ApiClient | null; session: Session | null;
  mode: 'checking' | 'automatic' | 'manual'; connectionError: unknown;
  connect: (token: string | null, signal?: AbortSignal) => Promise<void>; disconnect: () => void;
}
const Context = createContext<Connection | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [api, setApi] = useState<ApiClient | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [mode, setMode] = useState<Connection['mode']>('checking');
  const [connectionError, setConnectionError] = useState<unknown>(null);
  const generation = useRef(0);
  const active = useRef<ApiClient | null>(null);
  const cache = useQueryClient();
  const disconnect = useCallback(() => {
    generation.current += 1;
    active.current?.clear();
    active.current = null;
    void cache.cancelQueries();
    cache.clear();
    setApi(null);
    setSession(null);
    setConnectionError(new ApiError('network_unavailable'));
  }, [cache]);
  const connect = useCallback(async (token: string | null, signal?: AbortSignal) => {
    const attempt = ++generation.current;
    const candidate = new ApiClient(token, () => {
      if (generation.current === attempt) disconnect();
    });
    try {
      const result = await candidate.session(signal);
      if (generation.current !== attempt) { candidate.clear(); return; }
      active.current?.clear();
      active.current = candidate;
      cache.clear();
      setApi(candidate);
      setSession(result);
      setConnectionError(null);
    } catch (error) {
      candidate.clear();
      if (token === null && error instanceof ApiError && error.code === 'experiment_auth_required') {
        throw new ApiError('local_proxy_auth_failed', error.status);
      }
      throw error;
    }
  }, [cache, disconnect]);
  useEffect(() => {
    const controller = new AbortController();
    async function initialize() {
      try {
        const response = await fetch('/api/local-access', { cache: 'no-store', credentials: 'omit', redirect: 'error',
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]) });
        const local = response.ok && response.headers.get('content-type')?.includes('application/json')
          && (await response.json() as { mode?: string }).mode === 'local_proxy';
        if (controller.signal.aborted) return;
        if (!local) { setMode('manual'); return; }
        setMode('automatic');
        await connect(null, controller.signal);
      } catch (failure) {
        if (!controller.signal.aborted) { setMode('automatic'); setConnectionError(failure); }
      }
    }
    void initialize();
    return () => { controller.abort(); generation.current += 1; };
  }, [connect]);
  return <Context.Provider value={{ api, session, mode, connectionError, connect, disconnect }}>{children}</Context.Provider>;
}
export function useConnection(): Connection {
  const value = useContext(Context);
  if (!value) throw new Error('Missing connection provider');
  return value;
}
