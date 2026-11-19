import { createContext, lazy, Suspense, useCallback, useContext, useState } from 'react';
import type { ReactNode } from 'react';
import type { Project } from './types';
import type { ResearchKind } from './research-entry';

const SubmissionDialog = lazy(() => import('./task-dialog'));
interface SubmissionContext {
  openProject: (project: Project, kind?: ResearchKind) => void; pending: boolean; reveal: () => void;
}
const Context = createContext<SubmissionContext | null>(null);

export function TaskSubmissionProvider({ children }: { children: ReactNode }) {
  const [entry, setEntry] = useState<{ project: Project; kind: ResearchKind } | null>(null);
  const [visible, setVisible] = useState(false);
  const [pending, setPending] = useState(false);
  function close() { setVisible(false); if (!pending) setEntry(null); }
  function complete() { setVisible(false); setPending(false); setEntry(null); }
  const openProject = useCallback((project: Project, kind: ResearchKind = 'research') => {
    if (!pending) setEntry({ project, kind });
    setVisible(true);
  }, [pending]);
  return <Context.Provider value={{ pending, reveal: () => setVisible(true), openProject }}>
    {children}
    {entry && <Suspense fallback={null}><SubmissionDialog key={`${entry.project.id}:${entry.kind}`}
      project={entry.project} initialKind={entry.kind}
      open={visible} close={close} complete={complete} pendingChanged={setPending} /></Suspense>}
  </Context.Provider>;
}
export function useSubmission(): SubmissionContext {
  const value = useContext(Context);
  if (!value) throw new Error('Missing task submission provider');
  return value;
}
