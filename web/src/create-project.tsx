import { useRef, useState } from 'react';
import { Alert, Button, Form, Input, Modal } from 'antd';
import { useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { PlusOutlined } from '@ant-design/icons';
import { errorMessage, isWriteUncertain } from './api';
import { useConnection } from './connection';
import { projectDestination } from './research-entry';
import type { ResearchEntry } from './research-entry';

export default function CreateProject({ researchEntry }: { researchEntry?: ResearchEntry }) {
  const { api } = useConnection();
  const cache = useQueryClient();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const inFlight = useRef(false);
  const [form] = Form.useForm<{ name: string; description?: string }>();
  async function submit(values: { name: string; description?: string }) {
    if (!api || inFlight.current || isWriteUncertain(error)) return;
    inFlight.current = true; setBusy(true); setError(null);
    try {
      const project = await api.createProject(values.name, values.description ?? '');
      if (!api.connected) return;
      void cache.invalidateQueries({ queryKey: ['projects'] });
      setOpen(false); form.resetFields();
      navigate(projectDestination(project.id, researchEntry));
    } catch (failure) { setError(failure); }
    finally { inFlight.current = false; setBusy(false); }
  }
  return <>
    <Button type="primary" icon={<PlusOutlined aria-hidden="true" />} onClick={() => { setError(null); form.resetFields(); setOpen(true); }}>新建项目</Button>
    <Modal title="新建项目" open={open} mask={{ closable: false }} onCancel={() => { if (!busy) setOpen(false); }}
      closable={!busy} keyboard={!busy} footer={null}>
      <Form form={form} layout="vertical" onFinish={(values) => void submit(values)} disabled={busy}>
        <Form.Item name="name" label="项目名称" rules={[{ required: true, whitespace: true, max: 200, message: '填写 1–200 字符的项目名称' }]}>
          <Input maxLength={200} />
        </Form.Item>
        <Form.Item name="description" label="项目说明" rules={[{ max: 2000 }]}><Input.TextArea maxLength={2000} rows={3} /></Form.Item>
        {error !== null && <Alert type="error" showIcon title={errorMessage(error)}
          description={isWriteUncertain(error) ? '创建结果尚未确认，请关闭窗口并核对项目列表后再操作。' : undefined} />}
        <Button aria-label="创建项目" type="primary" htmlType="submit" loading={busy} disabled={isWriteUncertain(error)}>创建项目</Button>
      </Form>
    </Modal>
  </>;
}
