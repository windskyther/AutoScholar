import { Alert, Button } from 'antd';
import { Navigate, useLocation } from 'react-router-dom';
import { errorMessage } from './api';
import { useConnection } from './connection';

export default function ConnectPage() {
  const { api, checking, connectionError, connect } = useConnection();
  const location = useLocation();
  if (api) {
    const destination = (location.state as { from?: string } | null)?.from;
    return <Navigate to={destination === '/projects' || destination?.startsWith('/projects/') || destination === '/models' || destination === '/usage' ? destination : '/'} replace />;
  }
  if (checking) return null;
  return <main className="connection-screen"><section className="connection-panel">
    <div className="wordmark">AutoScholar</div><h1>连接研究工作台</h1>
    <p>本机后台暂时未连接，请确认一键启动器已运行。</p>
    <Alert type="error" showIcon title={errorMessage(connectionError)} />
    <Button type="primary" block onClick={() => void connect()}>重试连接</Button>
  </section></main>;
}
