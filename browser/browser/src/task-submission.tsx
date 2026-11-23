import { createContext, lazy, Suspense, useCallback, useContext, useState } from 'react';
import type { ReactNode } from 'react';
import type { LinkedTask, Project } from './types';
import type { ResearchKind } from './research-entry';

const SubmissionDialog = lazy(() => import('./task-dialog'));
type SubmissionProject = Pick<Project, 'id' | 'name'>;
export interface ContinuationDraft {
  previous: LinkedTask;
  references: LinkedTask[];
  modelProfileId: string | null;
}
interface SubmissionContext {
  openProject: (project: SubmissionProject, kind?: ResearchKind, continuation?: ContinuationDraft) => void;
  pending: boolean; reveal: () => void;
}
const Context = createContext<SubmissionContext | null>(null);

export function TaskSubmissionProvider({ children }: { children: ReactNode }) {
  const [entry, setEntry] = useState<{ project: SubmissionProject; kind: ResearchKind; continuation?: ContinuationDraft } | null>(null);
  const [visible, setVisible] = useState(false);
  const [pending, setPending] = useState(false);
  function close() { setVisible(false); if (!pending) setEntry(null); }
  function complete() { setVisible(false); setPending(false); setEntry(null); }
  const openProject = useCallback((project: SubmissionProject, kind: ResearchKind = 'research', continuation?: ContinuationDraft) => {
    if (!pending) setEntry({ project, kind, continuation });
    setVisible(true);
  }, [pending]);
  return <Context.Provider value={{ pending, reveal: () => setVisible(true), openProject }}>
    {children}
    {entry && <Suspense fallback={null}><SubmissionDialog key={`${entry.project.id}:${entry.kind}:${entry.continuation?.previous.task_id ?? ''}`}
      project={entry.project} initialKind={entry.kind} continuation={entry.continuation}
      open={visible} close={close} complete={complete} pendingChanged={setPending} /></Suspense>}
  </Context.Provider>;
}
export function useSubmission(): SubmissionContext {
  const value = useContext(Context);
  if (!value) throw new Error('Missing task submission provider');
  return value;
}
