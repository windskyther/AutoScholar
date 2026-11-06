import { useRef, useState } from 'react';
import { Alert, Button, Modal, Space, Tooltip } from 'antd';
import { DeleteOutlined } from '@ant-design/icons';
import { useQueryClient } from '@tanstack/react-query';
import { ApiError, errorMessage, isWriteUncertain } from './api';
import { useConnection } from './auth';
import type { Task } from './types';

export default function DeleteTask({ task, projectId, onDeleted }: { task: Task; projectId: string; onDeleted: () => void }) {
  const { api, session } = useConnection(); const cache = useQueryClient();
  const [open, setOpen] = useState(false); const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null); const [uncertain, setUncertain] = useState(false);
  const inFlight = useRef(false);
  const removable = ['succeeded', 'partial', 'failed', 'budget_exceeded', 'cancelled'].includes(task.status);
  async function finish() {
    if (!api?.connected) return;
    await cache.cancelQueries({ predicate: (query) => query.queryKey.includes(task.task_id) });
    cache.removeQueries({ predicate: (query) => query.queryKey.includes(task.task_id) });
    setOpen(false); onDeleted();
    await cache.invalidateQueries({ queryKey: ['tasks', projectId] });
  }
  async function remove() {
    if (!api || inFlight.current || uncertain) return;
    inFlight.current = true; setBusy(true); setError(null);
    try { await api.deleteTask(projectId, task.task_id); await finish(); }
    catch (failure) { setError(failure); setUncertain(isWriteUncertain(failure)); }
    finally { inFlight.current = false; setBusy(false); }
  }
  async function verify() {
    if (!api || inFlight.current) return;
    inFlight.current = true; setBusy(true);
    try { await api.overview(task.task_id, 1); setUncertain(false); setError(null); }
    catch (failure) { if (failure instanceof ApiError && failure.status === 404) await finish(); else setError(failure); }
    finally { inFlight.current = false; setBusy(false); }
  }
  if (!session?.capabilities.task_deletion) return null;
  return <><Tooltip title={removable ? undefined : '请先取消任务，或等待任务结束后再删除。'}>
    <Button danger icon={<DeleteOutlined />} disabled={!removable} aria-label={`删除研究目标 ${task.objective}`}
      onClick={() => { setOpen(true); if (!uncertain) setError(null); }}>删除</Button></Tooltip>
    <Modal title="删除研究目标" open={open} footer={null} mask={{ closable: false }} closable={!busy} keyboard={!busy}
      onCancel={() => { if (!busy) setOpen(false); }}>
      <p>确定删除研究目标 <strong>{task.objective}</strong>？</p>
      <p>删除后，该目标及关联输出、资源将从工作台移除。项目和其他目标不受影响。</p>
      {error !== null && <Alert type="error" title={errorMessage(error)} />}
      {uncertain && <Alert type="warning" title="删除结果尚未确认，请先核对结果。" />}
      <Space className="filters"><Button disabled={busy} onClick={() => setOpen(false)}>取消</Button>
        {uncertain ? <Button key="verify" aria-label="核对删除结果" loading={busy} onClick={() => void verify()}>核对删除结果</Button>
          : <Button key="remove" aria-label="删除此目标" type="primary" danger loading={busy} onClick={() => void remove()}>删除此目标</Button>}
      </Space>
    </Modal>
  </>;
}
