import { createContext, useCallback, useContext, useLayoutEffect, useState } from 'react';
import type { ReactNode } from 'react';

const PageReady = createContext<(() => void) | null>(null);

export function usePageReady(ready: boolean) {
  const reveal = useContext(PageReady);
  useLayoutEffect(() => { if (ready) reveal?.(); }, [ready, reveal]);
}

export default function PageTransition({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const reveal = useCallback(() => setReady(true), []);
  return <PageReady.Provider value={reveal}>
    <div className={ready ? 'page-transition' : undefined} hidden={!ready}>{children}</div>
  </PageReady.Provider>;
}
