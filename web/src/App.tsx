import { lazy, Suspense } from 'react';
import { Alert, Button, Layout, Menu, Modal, Space, Tag } from 'antd';
import { DisconnectOutlined, FolderOutlined } from '@ant-design/icons';
import { Link, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import { useConnection } from './auth';
import ConnectPage from './connect';
import { Loading } from './components';
import { TaskSubmissionProvider, useSubmission } from './task-submission';

const ProjectsPage = lazy(() => import('./pages').then((module) => ({ default: module.ProjectsPage })));
const ProjectPage = lazy(() => import('./pages').then((module) => ({ default: module.ProjectPage })));
const TaskPage = lazy(() => import('./pages').then((module) => ({ default: module.TaskPage })));

function Shell() {
  const { api } = useConnection();
  const location = useLocation();
  if (!api) return <Navigate to="/connect" state={{ from: location.pathname }} replace />;
  return <TaskSubmissionProvider><WorkspaceLayout /></TaskSubmissionProvider>;
}
function WorkspaceLayout() {
  const { api, session, disconnect } = useConnection();
  const submission = useSubmission();
  const [modal, contextHolder] = Modal.useModal();
  const location = useLocation();
  if (!api) return <Navigate to="/connect" state={{ from: location.pathname }} replace />;
  return <Layout className="workbench-shell">
    {contextHolder}
    <Layout.Sider width={212} breakpoint="lg" collapsedWidth={0} className="sidebar" theme="light">
      <Link to="/" className="wordmark sidebar-brand">AutoScholar</Link>
      <div className="sidebar-label">研究工作台</div>
      <Menu mode="inline" selectedKeys={['projects']} items={[
        { key: 'projects', icon: <FolderOutlined />, label: <Link to="/">项目与任务</Link> },
      ]} />
      <div className="sidebar-foot">Phase 9C · 本地研究工作台</div>
    </Layout.Sider>
    <Layout>
      <Layout.Header className="app-header"><Space><Tag color="success">已连接</Tag>
        <span>API {session?.api_version}</span></Space>
        <Button icon={<DisconnectOutlined />} onClick={() => {
          if (!submission.pending) disconnect();
          else modal.confirm({ title: '还有一笔结果未确认的提交', content: '断开连接会丢失内存中的提交编号和表单。请先记录编号并核查任务列表。',
            okText: '确认断开', cancelText: '返回核查', onOk: disconnect });
        }}>断开连接</Button>
      </Layout.Header>
      <Layout.Content className="app-content"><Suspense fallback={<Loading />}>
        {submission.pending && <Alert type="warning" title="有一笔任务提交结果未确认，不要直接新建另一轮。"
          action={<Button onClick={submission.reveal}>核查原提交</Button>} />}
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
