import { Typography } from 'antd';
import { Link } from 'react-router-dom';
import { useConnection } from './connection';
import { usePageReady } from './page-transition';

export default function Settings() {
  const { session } = useConnection();
  usePageReady(true);
  return <section style={{ maxWidth: 720, margin: '2rem auto' }}>
    <Typography.Title level={2}>工作台设置</Typography.Title>
    <Typography.Title level={3}>模型连接</Typography.Title>
    <Typography.Paragraph>{session?.capabilities.llm_configured ? '已配置模型连接。' : '尚未配置模型连接。'}</Typography.Paragraph>
    <Link to="/models">管理模型连接</Link>
  </section>;
}
