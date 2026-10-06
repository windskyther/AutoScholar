import { Alert, Button, Space, Spin, Tag } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import { ApiError, errorMessage } from './api';
import type { TaskStatus } from './types';

export const statusLabels: Record<TaskStatus, string> = {
  running: '运行中', succeeded: '已完成', partial: '部分完成', failed: '失败',
  budget_exceeded: '预算耗尽', queued: '已排队', pause_requested: '暂停请求中', paused: '已暂停',
  awaiting_approval: '等待审批', cancel_requested: '取消请求中', cancelled: '已取消',
  recovery_required: '需要人工检查',
};
export function Status({ value }: { value: TaskStatus }) {
  const color = value === 'succeeded' ? 'success' : value === 'running' ? 'processing'
    : ['failed', 'budget_exceeded', 'recovery_required'].includes(value) ? 'error' : 'default';
  return <Tag color={color}>{statusLabels[value] ?? value}</Tag>;
}
export function QueryError({ error, retry }: { error: unknown; retry: () => void }) {
  const requestId = error instanceof ApiError ? error.requestId : null;
  return <Alert type="error" showIcon title={errorMessage(error)}
    description={requestId ? `请求编号：${requestId}` : undefined}
    action={<Button onClick={retry}>手动重试查询</Button>} />;
}
export function Loading() { return <div className="loading" role="status"><Spin /> 正在读取数据</div>; }
export function PageHeading({ title, subtitle, refresh }: {
  title: string; subtitle: string; refresh?: () => void;
}) {
  return <div className="page-heading"><div><h1>{title}</h1><p>{subtitle}</p></div>
    {refresh && <Button icon={<ReloadOutlined />} onClick={refresh}>刷新快照</Button>}</div>;
}
export function Metric({ label, value, limit }: { label: string; value: number; limit?: number }) {
  return <div className="metric"><span>{label}</span><Space align="baseline">
    <strong>{value.toLocaleString('zh-CN')}</strong>{limit !== undefined && <span>/ {limit.toLocaleString('zh-CN')}</span>}
  </Space></div>;
}
export function dateLabel(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '时间未知' : date.toLocaleString('zh-CN');
}
