import { lazy, Suspense } from 'react';
import { Button, Layout, Menu, Space, Tag } from 'antd';
import { DisconnectOutlined, FolderOutlined } from '@ant-design/icons';
import { Link, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import { useConnection } from './auth';
import ConnectPage from './connect';
import { Loading } from './components';

const ProjectsPage = lazy(() => import('./pages').then((module) => ({ default: module.ProjectsPage })));
const ProjectPage = lazy(() => import('./pages').then((module) => ({ default: module.ProjectPage })));
const TaskPage = lazy(() => import('./pages').then((module) => ({ default: module.TaskPage })));

function Shell() {
  const { api, session, disconnect } = useConnection();
  const location = useLocation();
  if (!api) return <Navigate to="/connect" state={{ from: location.pathname }} replace />;
  return <Layout className="workbench-shell">
    <Layout.Sider width={212} breakpoint="lg" collapsedWidth={0} className="sidebar" theme="light">
      <Link to="/" className="wordmark sidebar-brand">AutoScholar</Link>
      <div className="sidebar-label">研究工作台</div>
      <Menu mode="inline" selectedKeys={['projects']} items={[
        { key: 'projects', icon: <FolderOutlined />, label: <Link to="/">项目与任务</Link> },
      ]} />
      <div className="sidebar-foot">Phase 9B · 本地只读查询</div>
    </Layout.Sider>
    <Layout>
      <Layout.Header className="app-header"><Space><Tag color="success">已连接</Tag>
        <span>API {session?.api_version}</span></Space>
        <Button icon={<DisconnectOutlined />} onClick={disconnect}>断开连接</Button>
      </Layout.Header>
      <Layout.Content className="app-content"><Suspense fallback={<Loading />}>
        <Outlet key={location.pathname} />
      </Suspense></Layout.Content>
    </Layout>
  </Layout>;
}
export default function App() {
  return <Routes>
    <Route path="/connect" element={<ConnectPage />} />
    <Route element={<Shell />}>
      <Route index element={<ProjectsPage />} />
      <Route path="/projects/:projectId" element={<ProjectPage />} />
      <Route path="/projects/:projectId/tasks/:taskId" element={<TaskPage />} />
    </Route>
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes>;
}
