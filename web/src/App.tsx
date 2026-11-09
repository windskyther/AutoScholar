import { lazy, Suspense, useLayoutEffect } from 'react';
import { Alert, Button } from 'antd';
import { Link, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import { useConnection } from './connection';
import ConnectPage from './connect';
import PageTransition from './page-transition';
import { TaskSubmissionProvider, useSubmission } from './task-submission';
import WorkspaceDock from './workspace-dock';

const HomePage = lazy(() => import('./home-page'));
const ProjectsPage = lazy(() => import('./projects-page'));
const ProjectPage = lazy(() => import('./pages').then((module) => ({ default: module.ProjectPage })));
const TaskPage = lazy(() => import('./pages').then((module) => ({ default: module.TaskPage })));
const ModelsPage = lazy(() => import('./models-page'));
const UsagePage = lazy(() => import('./usage-page'));
const EvidenceBank = lazy(() => import('./evidence-bank'));

function Shell() {
  const { api } = useConnection();
  const location = useLocation();
  if (!api) return <Navigate to="/connect" state={{ from: location.pathname }} replace />;
  return <TaskSubmissionProvider><WorkspaceLayout /></TaskSubmissionProvider>;
}
function WorkspaceLayout() {
  const { api } = useConnection();
  const submission = useSubmission();
  const location = useLocation();
  useLayoutEffect(() => { window.scrollTo(0, 0); }, [location.pathname]);
  if (!api) return <Navigate to="/connect" state={{ from: location.pathname }} replace />;
  return <div className="workbench-shell">
    <a className="skip-link" href="#workspace-content">跳至内容</a>
    <header className="workspace-header"><Link to="/" className="wordmark workspace-brand">AutoScholar</Link><WorkspaceDock /></header>
      <main className="app-content" id="workspace-content" tabIndex={-1}>
        {submission.pending && <Alert type="warning" title="有一笔任务提交结果未确认，不要直接新建另一轮。"
          action={<Button onClick={submission.reveal}>核查原提交</Button>} />}
        <PageTransition key={location.pathname}><Suspense fallback={null}><Outlet /></Suspense></PageTransition>
      </main>
  </div>;
}
export default function App() {
  return <div className="app-scene"><div className="geometric-backdrop" aria-hidden="true" /><Routes>
    <Route path="/connect" element={<ConnectPage />} />
    <Route element={<Shell />}>
      <Route index element={<HomePage />} />
      <Route path="/projects" element={<ProjectsPage />} />
      <Route path="/models" element={<ModelsPage />} />
      <Route path="/usage" element={<UsagePage />} />
      <Route path="/projects/:projectId" element={<ProjectPage />} />
      <Route path="/projects/:projectId/evidence" element={<EvidenceBank />} />
      <Route path="/projects/:projectId/tasks/:taskId" element={<TaskPage />} />
    </Route>
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes></div>;
}
