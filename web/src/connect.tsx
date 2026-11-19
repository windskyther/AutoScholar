import { Alert, Button } from 'antd';
import { Navigate, useLocation } from 'react-router-dom';
import { errorMessage } from './api';
import { useConnection } from './connection';
import { ThemeToggle } from './appearance';

export default function ConnectPage() {
  const { api, checking, connectionError, connect } = useConnection();
  const location = useLocation();
  if (api) {
    const destination = (location.state as { from?: string } | null)?.from;
    const path = destination?.split('?')[0];
    const isWorkspacePath = path === '/projects' || path?.startsWith('/projects/') || path === '/models' || path === '/usage';
    return <Navigate to={isWorkspacePath && destination ? destination : '/'} replace />;
  }
  if (checking) return null;
  return <main className="connection-screen"><ThemeToggle /><section className="connection-panel">
    <div className="wordmark">AutoScholar</div><h1>连接研究工作台</h1>
    <p>本机后台暂时未连接，请确认一键启动器已运行。</p>
    <Alert type="error" showIcon title={errorMessage(connectionError)} />
    <Button type="primary" block onClick={() => void connect()}>重试连接</Button>
  </section></main>;
}
