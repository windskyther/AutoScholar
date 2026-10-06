import { createContext, lazy, Suspense, useContext, useState } from 'react';
import type { ReactNode } from 'react';
import type { Project } from './types';
import { Loading } from './components';

const SubmissionDialog = lazy(() => import('./task-dialog'));
interface SubmissionContext {
  openProject: (project: Project) => void; pending: boolean; reveal: () => void;
}
const Context = createContext<SubmissionContext | null>(null);

export function TaskSubmissionProvider({ children }: { children: ReactNode }) {
  const [project, setProject] = useState<Project | null>(null);
  const [visible, setVisible] = useState(false);
  const [pending, setPending] = useState(false);
  function close() { setVisible(false); if (!pending) setProject(null); }
  function complete() { setVisible(false); setPending(false); setProject(null); }
  return <Context.Provider value={{ pending, reveal: () => setVisible(true), openProject: (value) => {
    if (!pending) setProject(value);
    setVisible(true);
  } }}>
    {children}
    {project && <Suspense fallback={<Loading />}><SubmissionDialog key={project.id} project={project}
      open={visible} close={close} complete={complete} pendingChanged={setPending} /></Suspense>}
  </Context.Provider>;
}
export function useSubmission(): SubmissionContext {
  const value = useContext(Context);
  if (!value) throw new Error('Missing task submission provider');
  return value;
}
