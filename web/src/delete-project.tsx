import { useRef, useState } from 'react';
import { Alert, Button, Input, Modal, Space } from 'antd';
import { DeleteOutlined } from '@ant-design/icons';
import { useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { ApiError, errorMessage, isWriteUncertain } from './api';
import { useConnection } from './connection';
import { useSubmission } from './task-submission';
import type { Project } from './types';

export default function DeleteProject({ project, onDeleted }: { project: Project; onDeleted?: () => void }) {
  const { api, session } = useConnection();
  const submission = useSubmission();
  const cache = useQueryClient();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [confirmation, setConfirmation] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [uncertain, setUncertain] = useState(false);
  const inFlight = useRef(false);
  async function finish() {
    if (!api?.connected) return;
    await cache.cancelQueries({ predicate: (query) => query.queryKey.includes(project.id) });
    cache.removeQueries({ predicate: (query) => query.queryKey.includes(project.id) });
    setOpen(false);
    navigate('/projects', { replace: true });
    onDeleted?.();
    await cache.invalidateQueries({ queryKey: ['projects'] });
  }
  async function remove() {
    if (!api || inFlight.current || uncertain || submission.pending || confirmation !== project.name) return;
    inFlight.current = true; setBusy(true); setError(null);
    try { await api.deleteProject(project.id); await finish(); }
    catch (failure) { setError(failure); setUncertain(isWriteUncertain(failure)); }
    finally { inFlight.current = false; setBusy(false); }
  }
  async function verify() {
    if (!api || inFlight.current) return;
    inFlight.current = true; setBusy(true);
    try { await api.project(project.id); setUncertain(false); setError(null); }
    catch (failure) {
      if (failure instanceof ApiError && failure.status === 404) await finish();
      else setError(failure);
    } finally { inFlight.current = false; setBusy(false); }
  }
  if (!session?.capabilities.project_deletion) return null;
  return <>
    <Button danger icon={<DeleteOutlined />} aria-label={`删除项目 ${project.name}`}
      disabled={!!submission.pending} onClick={() => { setOpen(true); setConfirmation(''); setError(null); setUncertain(false); }}>删除项目</Button>
    <Modal title="删除项目" open={open} footer={null} mask={{ closable: false }} closable={!busy} keyboard={!busy}
      onCancel={() => { if (!busy) setOpen(false); }}>
      <p>删除后，项目及关联任务、文档将从工作台移除，无法在界面中恢复。</p>
      <p>请输入项目名称 <strong>{project.name}</strong> 以确认：</p>
      <Input aria-label="输入项目名称以确认删除" value={confirmation} disabled={busy || uncertain}
        onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" />
      {error !== null && <Alert type="error" showIcon title={errorMessage(error)} />}
      {uncertain && <Alert type="warning" title="删除结果尚未确认，请先核对结果。" />}
      <Space className="filters">
        <Button disabled={busy} onClick={() => setOpen(false)}>取消</Button>
        {uncertain ? <Button disabled={busy} aria-busy={busy} onClick={() => void verify()}>核对删除结果</Button>
          : <Button type="primary" danger loading={busy} disabled={confirmation !== project.name || !!submission.pending}
            onClick={() => void remove()}>确认删除项目</Button>}
      </Space>
    </Modal>
  </>;
}
