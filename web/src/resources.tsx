import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Descriptions, Empty, Modal, Space, Table, Tabs, Tag } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { useConnection } from './auth';
import { errorMessage } from './api';
import { Loading, QueryError } from './components';
import { safeExternalURL } from './resource-guards';
import type { ArtifactResource, EvidenceResource, ExperimentResource, ModelRun } from './types';

function Evidence({ taskId }: { taskId: string }) {
  const { api } = useConnection(); const [page, setPage] = useState(1);
  const query = useQuery({ queryKey: ['resources', taskId, 'evidence', page],
    queryFn: ({ signal }) => api!.evidence(taskId, page, signal) });
  return query.isPending ? <Loading /> : query.isError ? <QueryError error={query.error} retry={() => void query.refetch()} />
    : <Table<EvidenceResource> rowKey="id" dataSource={query.data.items} scroll={{ x: 700 }} size="small"
      locale={{ emptyText: <Empty description="暂无检索证据。" /> }} columns={[
        { title: '引用', dataIndex: 'citation_key', width: 90 },
        { title: '来源与标题', render: (_, item) => <><Tag>{item.source_type}</Tag>{item.title}</> },
        { title: '所属任务', dataIndex: 'task_id', ellipsis: true },
      ]} expandable={{ expandIcon: ({ expanded, onExpand, record }) => <Button size="small"
        aria-label={expanded ? '收起证据' : '展开证据'} onClick={(event) => onExpand(record, event)}>{expanded ? '−' : '+'}</Button>, expandedRowRender: (item) => <>
        <p>{item.authors.join('、')} {item.year ?? ''}</p>
        <p>主张：{item.claim}</p><pre className="plain-output">{item.excerpt}</pre>
        {item.text_truncated && <Alert type="warning" title="证据文本已截断。" />}
        {item.document_id && <p>文档：{item.document_id} · 页码：{item.page ?? '未知'} · {item.section}</p>}
        {safeExternalURL(item.url) && <a href={item.url!} target="_blank" rel="noopener noreferrer">在外部网站查看来源（离开本地工作台）</a>}
      </> }} pagination={{ current: page, total: query.data.total, pageSize: 20, showSizeChanger: false, onChange: setPage }} />;
}
function Runs({ item }: { item: ExperimentResource }) {
  return <>
    {item.specification ? <Descriptions size="small" column={{ xs: 1, sm: 3 }} items={[
      { key: 'dataset', label: '数据集', children: item.specification.dataset },
      { key: 'seed', label: '种子', children: item.specification.seed },
      { key: 'epochs', label: 'Epoch', children: item.specification.epochs },
      { key: 'samples', label: '训练 / 测试样本', children: `${item.specification.train_samples} / ${item.specification.test_samples}` },
      { key: 'batch', label: 'Batch', children: item.specification.batch_size },
      { key: 'lr', label: '学习率', children: item.specification.learning_rate },
    ]} /> : <p>实验参数尚未就绪或格式不受支持。</p>}
    <Table<ModelRun> rowKey="model" dataSource={item.runs} size="small" pagination={false} scroll={{ x: 800 }}
      locale={{ emptyText: <Empty description="暂无可比较的实测指标，不使用模拟数据填补。" /> }} columns={[
        { title: '模型', dataIndex: 'model' },
        { title: '测试准确率', dataIndex: 'test_accuracy', render: (value: number) => `${(value * 100).toFixed(2)}%` },
        { title: '参数量', dataIndex: 'parameters' },
        { title: '耗时（秒）', dataIndex: 'duration_seconds', render: (value: number) => value.toFixed(2) },
        { title: '逐轮损失', dataIndex: 'train_loss', render: (values: number[]) => values.map((value) => value.toFixed(4)).join(' → ') },
        { title: '逐轮训练准确率', dataIndex: 'train_accuracy', render: (values: number[]) => values.map((value) => `${(value * 100).toFixed(2)}%`).join(' → ') },
      ]} />
    {item.accuracy_delta !== null && <p>CNN − MLP：{(item.accuracy_delta * 100).toFixed(2)} 个百分点；比较结果：{item.winner ?? '未提供'}</p>}
    <p className="usage-note">固定 MNIST 子集工程结果，不代表完整数据集基准。</p>
  </>;
}
function Experiments({ taskId }: { taskId: string }) {
  const { api } = useConnection(); const [page, setPage] = useState(1);
  const query = useQuery({ queryKey: ['resources', taskId, 'experiments', page],
    queryFn: ({ signal }) => api!.experiments(taskId, page, signal) });
  return query.isPending ? <Loading /> : query.isError ? <QueryError error={query.error} retry={() => void query.refetch()} />
    : <Table<ExperimentResource> rowKey="id" dataSource={query.data.items} scroll={{ x: 650 }} size="small"
      columns={[{ title: '实验', dataIndex: 'name' }, { title: '状态', dataIndex: 'status' },
        { title: '所属任务', dataIndex: 'task_id', ellipsis: true }, { title: '错误码', dataIndex: 'error_code' }]}
      expandable={{ expandIcon: ({ expanded, onExpand, record }) => <Button size="small"
        aria-label={expanded ? '收起实验' : '展开实验'} onClick={(event) => onExpand(record, event)}>{expanded ? '−' : '+'}</Button>,
        expandedRowRender: (item) => <Runs item={item} /> }}
      pagination={{ current: page, total: query.data.total, pageSize: 20, showSizeChanger: false, onChange: setPage }} />;
}
function Files({ taskId }: { taskId: string }) {
  const { api } = useConnection(); const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<ArtifactResource | null>(null);
  const [text, setText] = useState(''); const [image, setImage] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false); const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); const pending = useRef<AbortController | null>(null);
  const urls = useRef(new Set<string>()); const downloads = useRef(new Set<ReturnType<typeof setTimeout>>());
  const query = useQuery({ queryKey: ['resources', taskId, 'artifacts', page],
    queryFn: ({ signal }) => api!.artifacts(taskId, page, signal) });
  function revoke() { for (const url of urls.current) URL.revokeObjectURL(url); urls.current.clear(); }
  useEffect(() => () => { pending.current?.abort(); for (const timer of downloads.current) clearTimeout(timer); revoke(); }, []);
  function close() { pending.current?.abort(); pending.current = null; setBusy(false); setSelected(null); setImage(null); setText(''); revoke(); }
  async function open(item: ArtifactResource) {
    if (pending.current) return;
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setError(null); setSelected(item); setText(''); setImage(null); setTruncated(false);
    try {
      if (item.type === 'plot') {
        const blob = await api!.downloadArtifact(taskId, item, controller.signal);
        if (controller.signal.aborted) return;
        const url = URL.createObjectURL(blob); urls.current.add(url); setImage(url);
      } else {
        const value = await api!.previewArtifact(taskId, item, controller.signal);
        if (controller.signal.aborted) return;
        setText(value.text); setTruncated(value.truncated);
      }
    } catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
    finally { if (pending.current === controller) { pending.current = null; setBusy(false); } }
  }
  async function download(item: ArtifactResource) {
    if (pending.current) return;
    const controller = new AbortController(); pending.current = controller; setBusy(true); setError(null);
    try {
      const blob = await api!.downloadArtifact(taskId, item, controller.signal);
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob); urls.current.add(url);
      const anchor = document.createElement('a'); anchor.href = url;
      anchor.download = `${item.task_id}-${item.id}-${item.path.split('/').at(-1)}`;
      anchor.click();
      const timer = setTimeout(() => { URL.revokeObjectURL(url); urls.current.delete(url); downloads.current.delete(timer); }, 1000);
      downloads.current.add(timer);
    } catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)); }
    finally { if (pending.current === controller) { pending.current = null; setBusy(false); } }
  }
  return <>
    <Alert type="info" title="只列出登记产物，不提供任意路径或整个工作目录导出。报告为纯文本预览；下载前校验大小和 SHA-256。" />
    {error && !selected && <Alert type="error" title={error} />}
    {query.isPending ? <Loading /> : query.isError ? <QueryError error={query.error} retry={() => void query.refetch()} />
      : <Table<ArtifactResource> rowKey="id" dataSource={query.data.items} scroll={{ x: 950 }} size="small" columns={[
        { title: '登记路径', dataIndex: 'path' }, { title: '所属任务', dataIndex: 'task_id', ellipsis: true },
        { title: '实验编号', dataIndex: 'experiment_id', ellipsis: true },
        { title: '大小（字节）', dataIndex: 'size_bytes' },
        { title: '操作', render: (_, item) => <Space>
          {item.previewable && <Button disabled={busy} onClick={() => void open(item)}>预览</Button>}
          <Button disabled={busy} onClick={() => void download(item)}>{item.type === 'report' ? '导出报告' : '下载产物'}</Button>
        </Space> },
      ]} pagination={{ current: page, total: query.data.total, pageSize: 20, showSizeChanger: false, onChange: setPage }} />}
    <Modal open={selected !== null} title={selected?.path} onCancel={close} footer={<Button onClick={close}>关闭预览</Button>}
      styles={{ body: { maxHeight: '70dvh', overflow: 'auto' } }} destroyOnHidden>
      {selected && <p className="identifier">SHA-256：{selected.sha256}</p>}
      {busy ? <Loading /> : error ? <Alert type="error" title={error} /> : image
        ? <img src={image} alt="实验曲线" style={{ maxWidth: '100%' }} /> : <pre className="plain-output">{text}</pre>}
      {truncated && <Alert type="warning" title="预览超过 256 KiB，已截断；导出可取得完整登记产物。" />}
    </Modal>
  </>;
}
export default function Resources({ taskId }: { taskId: string }) {
  return <section aria-label="任务资源"><h2>任务资源</h2><Tabs destroyOnHidden items={[
    { key: 'evidence', label: '证据与引用', children: <Evidence taskId={taskId} /> },
    { key: 'experiments', label: '实验对比', children: <Experiments taskId={taskId} /> },
    { key: 'files', label: '文件与报告', children: <Files taskId={taskId} /> },
  ]} /></section>;
}
