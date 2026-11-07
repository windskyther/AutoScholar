import { lazy, Suspense } from 'react';
import { Alert, Button, Layout, Menu } from 'antd';
import { ApiOutlined, BarChartOutlined, FolderOutlined } from '@ant-design/icons';
import { Link, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import { useConnection } from './connection';
import ConnectPage from './connect';
import PageTransition from './page-transition';
import { TaskSubmissionProvider, useSubmission } from './task-submission';

const ProjectsPage = lazy(() => import('./pages').then((module) => ({ default: module.ProjectsPage })));
const ProjectPage = lazy(() => import('./pages').then((module) => ({ default: module.ProjectPage })));
const TaskPage = lazy(() => import('./pages').then((module) => ({ default: module.TaskPage })));
const ModelsPage = lazy(() => import('./models-page'));
const UsagePage = lazy(() => import('./usage-page'));

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
  if (!api) return <Navigate to="/connect" state={{ from: location.pathname }} replace />;
  return <Layout className="workbench-shell">
    <Layout.Sider width={212} breakpoint="lg" collapsedWidth={0} className="sidebar" theme="light">
      <Link to="/" className="wordmark sidebar-brand">AutoScholar</Link>
      <div className="sidebar-label">研究工作台</div>
      <Menu mode="inline" selectedKeys={[location.pathname === '/models' ? 'models' : location.pathname === '/usage' ? 'usage' : 'projects']} items={[
        { key: 'projects', icon: <FolderOutlined />, label: <Link to="/">项目与任务</Link> },
        { key: 'models', icon: <ApiOutlined />, label: <Link to="/models">模型管理</Link> },
        { key: 'usage', icon: <BarChartOutlined />, label: <Link to="/usage">用量统计</Link> },
      ]} />
    </Layout.Sider>
    <Layout>
      <Layout.Content className="app-content">
        {submission.pending && <Alert type="warning" title="有一笔任务提交结果未确认，不要直接新建另一轮。"
          action={<Button onClick={submission.reveal}>核查原提交</Button>} />}
        <PageTransition key={location.pathname}><Suspense fallback={null}><Outlet /></Suspense></PageTransition>
      </Layout.Content>
    </Layout>
  </Layout>;
}
export default function App() {
  return <Routes>
    <Route path="/connect" element={<ConnectPage />} />
    <Route element={<Shell />}>
      <Route index element={<ProjectsPage />} />
      <Route path="/models" element={<ModelsPage />} />
      <Route path="/usage" element={<UsagePage />} />
      <Route path="/projects/:projectId" element={<ProjectPage />} />
      <Route path="/projects/:projectId/tasks/:taskId" element={<TaskPage />} />
    </Route>
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes>;
}
