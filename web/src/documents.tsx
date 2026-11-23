import { useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { DeleteOutlined, EllipsisOutlined, ReloadOutlined } from '@ant-design/icons';
import { Alert, Button, Dropdown, Form, Input, Modal, Space } from 'antd';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { errorMessage, isWriteUncertain } from './api';
import { useConnection } from './connection';
import { dateLabel, QueryError, StatusBadge } from './components';
import type { DocumentStatus, PDFDocument } from './types';
import { documentRefreshInterval } from './snapshot-refresh';
import WorkbenchTable from './workbench-table';

const labels: Record<DocumentStatus, string> = { queued: '等待处理', processing: '处理中', ready: '已就绪', failed: '处理失败', deleting: '等待删除' };
export function DocumentState({ value }: { value: DocumentStatus }) {
  const tone = value === 'ready' ? 'success' : value === 'failed' ? 'danger' : value === 'processing' ? 'info' : 'neutral';
  return <StatusBadge tone={tone}>{labels[value]}</StatusBadge>;
}

const documentActions = {
  retry: { label: '重试', title: '确认重试文档处理？', okText: '确认重试', warning: '后台可能产生 Embedding API 费用。' },
  reindex: { label: '重建索引', title: '确认重建此 PDF 的索引？', okText: '确认重建', warning: '后台可能调用已配置的付费 Embedding API，并在处理期间暂时不能检索此文档。' },
  delete: { label: '删除', title: '确认删除此 PDF？', okText: '确认删除', warning: '将异步删除该文档及其索引，不能在工作台撤销。' },
};
type DocumentAction = keyof typeof documentActions;

function DocumentMenu({ title, processAction, disabled, onAction }: {
  title: string; processAction: 'retry' | 'reindex'; disabled: boolean; onAction: (kind: DocumentAction) => void;
}) {
  const [open, setOpen] = useState(false);
  return <Dropdown placement="bottomRight" trigger={['click']} autoFocus disabled={disabled} open={open} onOpenChange={setOpen}
    classNames={{ root: 'table-action-menu' }} menu={{ items: [
      { key: processAction, label: documentActions[processAction].label, icon: <ReloadOutlined aria-hidden="true" />, onClick: () => onAction(processAction) },
      { type: 'divider' },
      { key: 'delete', label: documentActions.delete.label, icon: <DeleteOutlined aria-hidden="true" />, danger: true, onClick: () => onAction('delete') },
    ] }}>
    <Button className="table-action-trigger" type="text" size="small" disabled={disabled}
      aria-haspopup="menu" aria-expanded={open} aria-label={`文档操作 ${title}`} title="文档操作"
      icon={<EllipsisOutlined aria-hidden="true" />} />
  </Dropdown>;
}

export default function Documents({ projectId, analysisAction, analysisNotice }: {
  projectId: string; analysisAction?: ReactNode; analysisNotice?: ReactNode;
}) {
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
    refetchInterval: ({ state }) => documentRefreshInterval(state.data) });
  const processingLocally = Boolean(window.__autoscholarSession && query.data?.items.some(
    doc => doc.status === 'queued' || doc.status === 'processing'));
  const modelCache = useQuery({ queryKey: ['desktop-model-cache'], enabled: processingLocally,
    refetchInterval: processingLocally ? 5000 : false, queryFn: async ({ signal }) => {
      const result = await fetch('/api/desktop/model-cache', { signal,
        headers: { 'X-AutoScholar-Session': window.__autoscholarSession ?? '' } });
      if (!result.ok) throw new Error('model_cache_unavailable');
      return (await result.json()).bytes as number;
    } });
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
    } catch (failure) { setError(failure); if (isWriteUncertain(failure)) refresh(); }
    finally { inFlight.current = false; setBusy(false); }
  }
  async function action(document: PDFDocument, kind: DocumentAction) {
    if (!api || inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(null); setNotice('');
    try {
      if (kind === 'delete') await api.deleteDocument(projectId, document.id);
      else await api.documentAction(projectId, document.id, kind);
      if (api.connected) { setNotice(kind === 'delete' ? '删除已排队，等待后台清理。' : '文档处理已排队。'); refresh(); }
    } catch (failure) { setError(failure); refresh(); }
    finally { inFlight.current = false; setBusy(false); }
  }
  function confirm(document: PDFDocument, kind: DocumentAction) {
    const { title, warning, okText } = documentActions[kind];
    modal.confirm({ title, content: <><p>{document.title}</p><p>{warning}</p></>,
      okText, cancelText: '取消', okButtonProps: { danger: kind === 'delete' },
      onOk: () => action(document, kind) });
  }
  return <section id="project-pdfs" className="document-section">
    {contextHolder}
    <div className="section-heading"><h2>PDF 知识库</h2><Space wrap>
      <Button disabled={busy} onClick={() => { setError(null); setFile(null); form.resetFields(); setOpen(true); }}>上传 PDF</Button>
      {analysisAction}
    </Space></div>
    {analysisNotice}
    {processingLocally && modelCache.data !== undefined && <p role="status">
      正在本机处理 PDF。首次索引会下载检索模型，模型缓存已占用 {(modelCache.data / 1048576).toFixed(0)} MiB。
    </p>}
    {notice && <Alert type="success" showIcon title={notice} />}
    {error !== null && !open && <Alert type="error" showIcon title={errorMessage(error)}
      description={isWriteUncertain(error) ? '结果未确认，请等待文档列表更新后核查。' : undefined} />}
    {query.isPending ? null : !query.data ? <QueryError error={query.error} retry={() => void query.refetch()} />
      : <WorkbenchTable<PDFDocument> rowKey="id" dataSource={query.data.items} scroll={{ x: 900 }} emptyMessage="暂无 PDF 文档。"
        columns={[
          { title: '文档', dataIndex: 'title', className: 'table-primary', ellipsis: true },
          { title: '状态', dataIndex: 'status', width: 115, render: (value: DocumentStatus) => <DocumentState value={value} /> },
          { title: '页 / 分块', width: 120, align: 'right', render: (_, item) => `${item.page_count ?? '—'} / ${item.chunk_count}` },
          { title: '大小', width: 100, align: 'right', render: (_, item) => `${(item.size_bytes / 1024).toFixed(1)} KiB` },
          { title: '错误编号', dataIndex: 'error_code', width: 175, render: (value: string | null) => value || '—' },
          { title: '更新时间', dataIndex: 'updated_at', className: 'table-secondary', width: 185, render: dateLabel },
          { title: '操作', width: 80, align: 'right', render: (_, item) => {
            if (item.status !== 'ready' && item.status !== 'failed') return null;
            return <DocumentMenu title={item.title} processAction={item.status === 'ready' ? 'reindex' : 'retry'}
              disabled={busy} onAction={(kind) => confirm(item, kind)} />;
          } },
        ]} pagination={{ current: page, pageSize: 20, total: query.data.total, onChange: setPage }} />}
    <Modal title="上传 PDF" open={open} mask={{ closable: false }} closable={!busy} keyboard={!busy}
      onCancel={() => { if (!busy) { setOpen(false); setError(null); } }} footer={null}>
      <Alert type="warning" showIcon title="文件会发送到本地 API 并交给后台索引"
        description="仅支持 PDF；远程 Embedding 可能产生费用。" />
      <Form form={form} layout="vertical" disabled={busy} onFinish={(values) => void upload(values)}>
        <Form.Item label={`PDF 文件（最多 ${(maxBytes / 1048576).toFixed(1)} MiB）`} required>
          <input key={String(open)} aria-label="PDF 文件" type="file" accept=".pdf,application/pdf" disabled={busy}
            onChange={(event) => { setFile(event.target.files?.[0] ?? null); setError(null); }} />
        </Form.Item>
        <Form.Item label="文档标题（可选）" name="title" rules={[{ max: 500 }]}><Input maxLength={500} /></Form.Item>
        {error !== null && <Alert type="error" showIcon title={file ? errorMessage(error) : '请选择 PDF 文件。'}
          description={isWriteUncertain(error) ? '请关闭窗口，核对列表中的文档后再操作。' : undefined} />}
        <Button type="primary" htmlType="submit" loading={busy} disabled={!file || isWriteUncertain(error)}>确认上传并索引</Button>
      </Form>
    </Modal>
  </section>;
}
