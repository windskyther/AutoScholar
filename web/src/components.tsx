import { Alert, Button, Space } from 'antd';
import type { ReactNode } from 'react';
import { errorMessage } from './api';
import type { TaskStatus } from './types';

export const statusLabels: Record<TaskStatus, string> = {
  running: '运行中', succeeded: '已完成', partial: '部分完成', failed: '失败',
  budget_exceeded: '预算耗尽', queued: '已排队', pause_requested: '暂停请求中', paused: '已暂停',
  awaiting_approval: '等待审批', cancel_requested: '取消请求中', cancelled: '已取消',
  recovery_required: '需要人工检查',
};
type StatusTone = 'neutral' | 'success' | 'info' | 'warning' | 'danger';
const taskStatusTones: Partial<Record<TaskStatus, StatusTone>> = {
  succeeded: 'success', running: 'info', partial: 'warning', awaiting_approval: 'warning',
  failed: 'danger', budget_exceeded: 'danger', recovery_required: 'danger',
};

export function StatusBadge({ tone = 'neutral', children }: { tone?: StatusTone; children: ReactNode }) {
  return <span className="status-badge" data-tone={tone}>{children}</span>;
}
export function Status({ value }: { value: TaskStatus }) {
  return <StatusBadge tone={taskStatusTones[value]}>{statusLabels[value] ?? value}</StatusBadge>;
}
export function QueryError({ error, retry }: { error: unknown; retry: () => void }) {
  return <Alert type="error" showIcon title={errorMessage(error)}
    action={<Button onClick={retry}>手动重试查询</Button>} />;
}
export function PageHeading({ title, subtitle }: { title: string; subtitle?: string }) {
  return <div className="page-heading"><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div>;
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
