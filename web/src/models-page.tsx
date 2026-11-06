import { useRef, useState } from 'react';
import { Alert, Button, Form, Input, Modal, Select, Space, Table, Tag } from 'antd';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useConnection } from './auth';
import { errorMessage, isWriteUncertain } from './api';
import { dateLabel, Loading, PageHeading, QueryError } from './components';
import type { ModelProfile, ModelProfileInput } from './model-types';

export default function ModelsPage() {
  const { api } = useConnection();
  const cache = useQueryClient();
  const catalog = useQuery({ queryKey: ['models'], queryFn: ({ signal }) => api!.models(signal) });
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [uncertain, setUncertain] = useState(false);
  const [form] = Form.useForm<ModelProfileInput>();
  const draft = useRef<ModelProfileInput | null>(null);
  const inFlight = useRef(false);
  async function save(values?: ModelProfileInput) {
    if (!api || inFlight.current) return;
    if (values) draft.current = { ...values, id: crypto.randomUUID(), name: values.name.trim(), model: values.model.trim() };
    if (!draft.current) return;
    inFlight.current = true; setBusy(true); setError(null);
    try {
      await api.createModel(draft.current);
      if (!api.connected) return;
      draft.current = null; form.resetFields(); setOpen(false); setUncertain(false);
      void cache.invalidateQueries({ queryKey: ['models'] });
    } catch (failure) { setError(failure); setUncertain(isWriteUncertain(failure)); }
    finally { inFlight.current = false; setBusy(false); }
  }
  async function toggle(item: ModelProfile) {
    if (!api || inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(null);
    try { await api.modelStatus(item.id, !item.enabled); void catalog.refetch(); }
    catch (failure) { setError(failure); void catalog.refetch(); }
    finally { inFlight.current = false; setBusy(false); }
  }
  function close() {
    if (busy) return;
    form.resetFields(); draft.current = null; setUncertain(false); setError(null); setOpen(false);
    void catalog.refetch();
  }
  return <>
    <PageHeading title="模型管理" refresh={() => void catalog.refetch()} />
    <p>保存常用的模型连接，新建任务时选择使用哪一组。支持原生工具调用的 OpenAI 兼容接口。</p>
    <div className="filters"><Button type="primary" onClick={() => { setError(null); setOpen(true); }}>添加模型</Button></div>
    {error !== null && !open && <Alert type="error" title={errorMessage(error)} />}
    {catalog.isPending ? <Loading /> : catalog.isError ? <QueryError error={catalog.error} retry={() => void catalog.refetch()} /> : <>
      {catalog.data.default_model && <p>默认模型：{catalog.data.default_model.model}（来自本机配置）</p>}
      <Table<ModelProfile> rowKey="id" dataSource={catalog.data.items} scroll={{ x: 850 }} pagination={{ pageSize: 10 }}
        locale={{ emptyText: '还没有保存模型，点击“添加模型”开始。' }} columns={[
          { title: '名称', dataIndex: 'name' }, { title: '模型', dataIndex: 'model' },
          { title: '接口地址', dataIndex: 'base_url', ellipsis: true },
          { title: '状态', render: (_, item) => <Tag color={item.enabled ? 'success' : 'default'}>{item.enabled ? '可选用' : '已停用'}</Tag> },
          { title: '添加时间', dataIndex: 'created_at', render: dateLabel },
          { title: '操作', render: (_, item) => <Button disabled={busy} onClick={() => void toggle(item)}>{item.enabled ? '停用' : '启用'}</Button> },
        ]} />
      <p>停用后不再用于新任务，已有任务继续使用原配置。API Key 保存后不会回显；更换密钥时请添加一组新配置。</p>
    </>}
    <Modal title="添加模型" open={open} onCancel={close} footer={null} mask={{ closable: false }} closable={!busy} keyboard={!busy}>
      <Form form={form} layout="vertical" disabled={busy || uncertain} onFinish={(values) => void save(values)} initialValues={{ compatibility_profile: 'auto' }}>
        <Form.Item name="name" label="配置名称" rules={[{ required: true, whitespace: true, max: 200 }]}><Input placeholder="例如：论文阅读模型" maxLength={200} /></Form.Item>
        <Form.Item name="base_url" label="API 接口地址" rules={[{ required: true, type: 'url' }]}><Input placeholder="https://服务地址/v1" maxLength={2048} autoComplete="off" /></Form.Item>
        <Form.Item name="model" label="模型名称" rules={[{ required: true, whitespace: true, max: 200 }]}><Input placeholder="填写服务商提供的模型 ID" maxLength={200} /></Form.Item>
        <Form.Item name="api_key" label="API Key" rules={[{ required: true, pattern: /^[\x21-\x7e]{1,4096}$/, message: '填写完整密钥，不含空格和换行' }]}><Input.Password autoComplete="new-password" maxLength={4096} /></Form.Item>
        <Form.Item name="compatibility_profile" label="接口兼容方式"><Select options={[
          { value: 'auto', label: '自动识别' }, { value: 'generic', label: '标准兼容接口' }, { value: 'deepseek', label: 'DeepSeek 兼容网关' },
        ]} /></Form.Item>
        <p>保存只记录配置，不会发送模型请求或产生 API 费用。</p>
        {error !== null && <Alert type="error" title={errorMessage(error)} description={uncertain ? '保存结果未确认，可使用原配置重试，或关闭后刷新列表核对。' : undefined} />}
        <Space>
          {!uncertain && <Button type="primary" htmlType="submit" loading={busy}>保存模型</Button>}
          {uncertain && <Button type="primary" loading={busy} disabled={busy} onClick={() => void save()}>确认 / 重试保存</Button>}
          <Button disabled={busy} onClick={close}>关闭</Button>
        </Space>
      </Form>
    </Modal>
  </>;
}
