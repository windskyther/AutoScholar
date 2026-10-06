import { createContext, useContext, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { ApiClient } from './api';
import type { Session } from './types';

interface Connection {
  api: ApiClient | null; session: Session | null;
  connect: (token: string) => Promise<void>; disconnect: () => void;
}
const Context = createContext<Connection | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [api, setApi] = useState<ApiClient | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const generation = useRef(0);
  const active = useRef<ApiClient | null>(null);
  const cache = useQueryClient();
  function disconnect() {
    generation.current += 1;
    active.current?.clear();
    active.current = null;
    void cache.cancelQueries();
    cache.clear();
    setApi(null);
    setSession(null);
  }
  async function connect(token: string) {
    const attempt = ++generation.current;
    const candidate = new ApiClient(token, () => {
      if (generation.current === attempt) disconnect();
    });
    try {
      const result = await candidate.session();
      if (generation.current !== attempt) { candidate.clear(); return; }
      active.current?.clear();
      active.current = candidate;
      cache.clear();
      setApi(candidate);
      setSession(result);
    } catch (error) { candidate.clear(); throw error; }
  }
  return <Context.Provider value={{ api, session, connect, disconnect }}>{children}</Context.Provider>;
}
export function useConnection(): Connection {
  const value = useContext(Context);
  if (!value) throw new Error('Missing connection provider');
  return value;
}
