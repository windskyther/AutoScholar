import { useRef, useState } from 'react';
import { Alert, Button, Empty, Form, Input, Modal, Space, Table, Tag } from 'antd';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { errorMessage, isWriteUncertain } from './api';
import { useConnection } from './auth';
import { dateLabel, Loading, QueryError } from './components';
import type { DocumentStatus, PDFDocument } from './types';

const labels: Record<DocumentStatus, string> = { queued: '等待处理', processing: '处理中', ready: '已就绪', failed: '处理失败', deleting: '等待删除' };
export function DocumentState({ value }: { value: DocumentStatus }) {
  return <Tag color={value === 'ready' ? 'success' : value === 'failed' ? 'error' : 'default'}>{labels[value]}</Tag>;
}

export default function Documents({ projectId }: { projectId: string }) {
  const { api, session } = useConnection();
  const cache = useQueryClient();
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const inFlight = useRef(false);
  const [form] = Form.useForm<{ title?: string; consent: boolean }>();
  const [modal, contextHolder] = Modal.useModal();
  const maxBytes = Math.min(session?.capabilities.document_max_bytes ?? 52428800, 52428800);
  const query = useQuery({ queryKey: ['documents', projectId, page], queryFn: ({ signal }) => api!.documents(projectId, page, signal),
    refetchInterval: (state) => state.state.data?.items.some((item) => ['queued', 'processing', 'deleting'].includes(item.status)) ? 3000 : false });
  function refresh() { void cache.invalidateQueries({ queryKey: ['documents', projectId] }); }
  async function upload(values: { title?: string }) {
    if (!api || inFlight.current || isWriteUncertain(error)) return;
    if (!file) { setError(new Error('missing-file')); return; }
    inFlight.current = true; setBusy(true); setError(null);
    try {
      await api.uploadPDF(projectId, file, values.title ?? '', maxBytes);
      if (!api.connected) return;
      setOpen(false); setFile(null); form.resetFields(); setPage(1);
      setNotice('PDF 已保存并排队，尚未完成解析与索引。'); refresh();
    } catch (failure) { setError(failure); }
    finally { inFlight.current = false; setBusy(false); }
  }
  async function action(document: PDFDocument, kind: 'retry' | 'reindex' | 'delete') {
    if (!api || inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(null); setNotice('');
    try {
      if (kind === 'delete') await api.deleteDocument(projectId, document.id);
      else await api.documentAction(projectId, document.id, kind);
      if (api.connected) { setNotice(kind === 'delete' ? '删除已排队，等待后台清理。' : '文档处理已排队。'); refresh(); }
    } catch (failure) { setError(failure); refresh(); }
    finally { inFlight.current = false; setBusy(false); }
  }
  function confirm(document: PDFDocument, kind: 'reindex' | 'delete') {
    modal.confirm({ title: kind === 'delete' ? '确认删除此 PDF？' : '确认重建此 PDF 的索引？',
      content: <><p>{document.title}</p><p>{kind === 'delete' ? '将异步删除该文档及其索引，不能在工作台撤销。' : '后台可能调用已配置的付费 Embedding API，并在处理期间暂时不能检索此文档。'}</p></>,
      okText: kind === 'delete' ? '确认删除' : '确认重建', cancelText: '取消', okButtonProps: { danger: kind === 'delete' },
      onOk: () => action(document, kind) });
  }
  return <section className="document-section">
    {contextHolder}
    <div className="section-heading"><h2>PDF 知识库</h2><Space wrap>
      <Button onClick={() => void query.refetch()}>刷新文档</Button>
      <Button disabled={busy} onClick={() => { setError(null); setFile(null); form.resetFields(); setOpen(true); }}>上传 PDF</Button>
    </Space></div>
    <p>处理中每 3 秒只读刷新状态；仅“已就绪”文档能参与检索。后台处理需要 RAG Worker。</p>
    {notice && <Alert type="success" showIcon title={notice} />}
    {error !== null && !open && <Alert type="error" showIcon title={errorMessage(error)}
      description={isWriteUncertain(error) ? '结果未确认，请刷新文档状态核查，不会自动重复执行。' : undefined} />}
    {query.isPending ? <Loading /> : query.isError ? <QueryError error={query.error} retry={() => void query.refetch()} />
      : <Table<PDFDocument> rowKey="id" dataSource={query.data.items} scroll={{ x: 950 }} locale={{ emptyText: <Empty description="暂无 PDF 文档。" /> }}
        columns={[
          { title: '文档', dataIndex: 'title', ellipsis: true },
          { title: '状态', dataIndex: 'status', width: 115, render: (value: DocumentStatus) => <DocumentState value={value} /> },
          { title: '页 / 分块', width: 120, render: (_, item) => `${item.page_count ?? '—'} / ${item.chunk_count}` },
          { title: '大小', width: 100, render: (_, item) => `${(item.size_bytes / 1024).toFixed(1)} KiB` },
          { title: '错误编号', dataIndex: 'error_code', width: 175, render: (value: string | null) => value || '—' },
          { title: '更新时间', dataIndex: 'updated_at', width: 185, render: dateLabel },
          { title: '操作', width: 195, render: (_, item) => <Space>
            {item.status === 'failed' && <Button size="small" disabled={busy} onClick={() => {
              modal.confirm({ title: '确认重试文档处理？', content: '后台可能产生 Embedding API 费用。', okText: '确认重试', cancelText: '取消', onOk: () => action(item, 'retry') });
            }}>重试</Button>}
            {item.status === 'ready' && <Button size="small" disabled={busy} onClick={() => confirm(item, 'reindex')}>重建索引</Button>}
            {['ready', 'failed'].includes(item.status) && <Button size="small" danger disabled={busy} onClick={() => confirm(item, 'delete')}>删除</Button>}
          </Space> },
        ]} pagination={{ current: page, pageSize: 20, total: query.data.total, showSizeChanger: false, onChange: setPage }} />}
    <Modal title="上传 PDF" open={open} mask={{ closable: false }} closable={!busy} keyboard={!busy}
      onCancel={() => { if (!busy) { setOpen(false); setError(null); } }} footer={null}>
      <Alert type="warning" showIcon title="文件会发送到本地 API 并交给后台索引"
        description="只选择用于研究的 PDF，不要选择密钥、.env 或设计文档。配置远程 Embedding 时可能产生费用。" />
      <Form form={form} layout="vertical" disabled={busy} onFinish={(values) => void upload(values)}>
        <Form.Item label={`PDF 文件（最多 ${(maxBytes / 1048576).toFixed(1)} MiB）`} required>
          <input key={String(open)} aria-label="PDF 文件" type="file" accept=".pdf,application/pdf" disabled={busy}
            onChange={(event) => { setFile(event.target.files?.[0] ?? null); setError(null); }} />
        </Form.Item>
        <Form.Item label="文档标题（可选）" name="title" rules={[{ max: 500 }]}><Input maxLength={500} /></Form.Item>
        {error !== null && <Alert type="error" showIcon title={file ? errorMessage(error) : '请选择 PDF 文件。'}
          description={isWriteUncertain(error) ? '请关闭窗口并刷新文档列表核查，不要直接重复上传。' : undefined} />}
        <Button type="primary" htmlType="submit" loading={busy} disabled={!file || isWriteUncertain(error)}>确认上传并索引</Button>
      </Form>
    </Modal>
  </section>;
}
