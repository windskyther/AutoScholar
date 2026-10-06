import { useState } from 'react';
import { Alert, Button, Form, Input } from 'antd';
import { Navigate, useLocation } from 'react-router-dom';
import { errorMessage } from './api';
import { useConnection } from './auth';

export default function ConnectPage() {
  const { api, connect } = useConnection();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [form] = Form.useForm<{ token: string }>();
  const location = useLocation();
  if (api) {
    const destination = (location.state as { from?: string } | null)?.from;
    return <Navigate to={destination?.startsWith('/projects/') ? destination : '/'} replace />;
  }
  async function submit({ token }: { token: string }) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try { await connect(token.trim()); form.resetFields(); }
    catch (failure) { setError(failure); }
    finally { setBusy(false); }
  }
  return <main className="connection-screen"><section className="connection-panel">
    <div className="wordmark">AutoScholar</div>
    <h1>连接研究工作台</h1>
    <p>查看项目、任务计划与执行结果。当前为本地单操作者模式。</p>
    <Alert type="info" showIcon title="使用操作者 Token，不是模型供应商 API Key"
      description="填写服务端 EXPERIMENT_API_TOKEN。仅保存在浏览器内存，刷新后需重新连接。" />
    <Form form={form} layout="vertical" onFinish={(values) => void submit(values)} autoComplete="off">
      <Form.Item label="操作者 Token" name="token" rules={[{ required: true, whitespace: true, message: '请输入操作者 Token' }]}>
        <Input.Password autoComplete="off" placeholder="EXPERIMENT_API_TOKEN" maxLength={4096} disabled={busy} />
      </Form.Item>
      {error !== null && <Alert type="error" showIcon title={errorMessage(error)} />}
      <Button type="primary" htmlType="submit" loading={busy} block>连接本地 API</Button>
    </Form>
    <div className="connection-footer">API 地址：同源 /api/workbench · 本次连接不会调用付费 API</div>
  </section></main>;
}
