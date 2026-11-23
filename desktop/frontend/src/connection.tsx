import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ApiClient, ApiError } from './api';
import type { Session } from './types';

interface Connection {
  api: ApiClient | null; session: Session | null;
  checking: boolean; connectionError: unknown;
  connect: (signal?: AbortSignal) => Promise<void>;
}
const Context = createContext<Connection | null>(null);

export function ConnectionProvider({ children }: { children: ReactNode }) {
  const [api, setApi] = useState<ApiClient | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [checking, setChecking] = useState(true);
  const [connectionError, setConnectionError] = useState<unknown>(null);
  const generation = useRef(0);
  const active = useRef<ApiClient | null>(null);
  const cache = useQueryClient();
  const connect = useCallback(async (signal?: AbortSignal) => {
    const attempt = ++generation.current;
    const candidate = new ApiClient(() => {
      if (generation.current !== attempt) return;
      generation.current += 1;
      active.current?.clear(); active.current = null;
      void cache.cancelQueries(); cache.clear(); setApi(null); setSession(null);
      setChecking(false); setConnectionError(new ApiError('local_access_denied', 403));
    });
    setChecking(true);
    try {
      const result = await candidate.session(signal);
      if (generation.current !== attempt || signal?.aborted) { candidate.clear(); return; }
      active.current?.clear(); active.current = candidate;
      cache.clear(); setApi(candidate); setSession(result); setConnectionError(null);
    } catch (error) {
      candidate.clear();
      if (generation.current === attempt && !signal?.aborted) setConnectionError(error);
    } finally { if (generation.current === attempt) setChecking(false); }
  }, [cache]);
  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => void connect(controller.signal), 0);
    return () => { window.clearTimeout(timer); controller.abort(); generation.current += 1; active.current?.clear(); };
  }, [connect]);
  return <Context.Provider value={{ api, session, checking, connectionError, connect }}>{children}</Context.Provider>;
}
export function useConnection(): Connection {
  const value = useContext(Context);
  if (!value) throw new Error('Missing connection provider');
  return value;
}
