import { useRef, useState } from 'react';
import { Alert, Breadcrumb, Button, Collapse, Empty, Form, Input, Modal, Select, Space, Table, Tag } from 'antd';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { errorMessage } from './api';
import { useConnection } from './connection';
import { dateLabel, Loading, PageHeading, QueryError } from './components';
import { usePageReady } from './page-transition';
import NumberSlider from './number-slider';
import type { BankClaim, EvidencePolicy } from './evidence-types';

const statuses = { ready: '已审查', pending: '待复核', withdrawn: '来源不可用' };
const access: Record<string, string> = { summary_only: '摘要', extracted_text: '正文片段', document_excerpt: '项目文档片段' };
const safeLink = (url: string | null) => url && (/^https?:\/\//i.test(url) || url.startsWith('/api/workbench/projects/')) ? url : null;

export default function EvidenceBank() {
  const projectId = useParams().projectId!;
  const { api, session } = useConnection();
  const cache = useQueryClient();
  const [page, setPage] = useState(1);
  const [q, setQuery] = useState('');
  const [status, setStatus] = useState('all');
  const [kind, setKind] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const [cursor, setCursor] = useState<string | null>(null);
  const [modal, contextHolder] = Modal.useModal();
  const inFlight = useRef(false);
  const enabled = !!session?.capabilities.project_evidence;
  const project = useQuery({ queryKey: ['project', projectId], queryFn: ({ signal }) => api!.project(projectId, signal) });
  const bank = useQuery({ queryKey: ['evidence-bank', projectId, page, q, status, kind], enabled: enabled && project.isSuccess,
    queryFn: ({ signal }) => api!.projectEvidence(projectId, page, q, status, kind, signal), refetchInterval: 15000 });
  usePageReady(!project.isPending && (!enabled || !bank.isPending || project.isError));
  async function act(action: () => Promise<unknown>) {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(null);
    try { await action(); void cache.invalidateQueries({ queryKey: ['evidence-bank', projectId] }); }
    catch (failure) { setError(failure); }
    finally { inFlight.current = false; setBusy(false); }
  }
  function toggle(item: BankClaim) {
    modal.confirm({ title: item.enabled ? '停止在后续研究中使用这条结论？' : '恢复使用这条结论？',
      content: item.enabled ? '已完成的研究报告会保留。后续任务将不再采用此条目及依赖它的成果。' : '恢复前会核对原始目标和资料是否仍可用。',
      okText: item.enabled ? '移除' : '恢复', cancelText: '取消',
      onOk: () => act(() => api!.evidenceState(projectId, item.id, !item.enabled)) });
  }
  function importHistory() {
    modal.confirm({ title: '整理本项目已有研究成果？', content: '在本机读取历史记录。缺少完整审查记录的成果标为待复核；不会调用模型或外部检索服务。',
      okText: '开始整理', cancelText: '取消', onOk: () => act(async () => {
        const result = await api!.importEvidence(projectId, cursor);
        setCursor(result.next_cursor);
        setNotice(`已排队 ${result.queued_tasks} 个目标，整理 ${result.pending_claims} 条待复核结论。${result.next_cursor ? '本批已处理 50 个目标，可继续整理下一批。' : ''}`);
      }) });
  }
  if (project.isPending) return <Loading />;
  if (project.isError) return <QueryError error={project.error} retry={() => void project.refetch()} />;
  if (!enabled) return <Alert type="info" title="项目证据库暂不可用。" />;
  return <>
    {contextHolder}
    <Breadcrumb items={[{ title: <Link to="/">项目</Link> }, { title: <Link to={`/projects/${encodeURIComponent(projectId)}`}>{project.data.name}</Link> }, { title: '证据库' }]} />
    <PageHeading title="项目证据库" subtitle={project.data.name} refresh={() => void bank.refetch()} />
    <p>研究目标通过审查后，在这里积累结论与原文依据。新目标会先查找相关成果，再补充需要的资料。</p>
    <Space className="filters" wrap>
      <Input.Search aria-label="搜索项目证据" placeholder="搜索论点或研究目标" maxLength={200} allowClear onSearch={v => { setQuery(v.trim()); setPage(1); }} />
      <Select aria-label="证据状态" value={status} style={{ width: 150 }} onChange={v => { setStatus(v); setPage(1); }} options={[
        { value: 'all', label: '全部状态' }, ...Object.entries(statuses).map(([value, label]) => ({ value, label: value === 'withdrawn' ? '已移除' : label })),
      ]} />
      <Select aria-label="证据来源类型" placeholder="全部来源" allowClear value={kind} style={{ width: 150 }} onChange={v => { setKind(v); setPage(1); }} options={[
        { value: 'web', label: '网页' }, { value: 'paper', label: '论文' }, { value: 'document', label: '项目文档' },
      ]} />
      <Button loading={busy} disabled={busy} onClick={importHistory}>{cursor ? '继续整理下一批' : '整理历史成果'}</Button>
    </Space>
    {error !== null && <Alert type="error" title={errorMessage(error)} />}
    {notice && <Alert type="info" title={notice} />}
    {bank.isPending ? <Loading /> : bank.isError ? <QueryError error={bank.error} retry={() => void bank.refetch()} /> : <>
      <p>匹配 {bank.data.total} 条结论</p>
      <Table<BankClaim> rowKey="id" dataSource={bank.data.items} size="small" scroll={{ x: 850 }}
        locale={{ emptyText: <Empty description="暂无匹配成果。完成研究目标，或整理已有成果后，可在这里查看。" /> }} columns={[
          { title: '结论', dataIndex: 'text', width: 340 },
          { title: '贡献目标', render: (_, item) => <Link to={`/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(item.task_id)}`}>{item.objective}</Link> },
          { title: '状态', render: (_, item) => <Tag>{item.enabled ? statuses[item.status] : '已移除'}</Tag> },
          { title: '采用次数', dataIndex: 'reuse_count' },
          { title: '操作', render: (_, item) => <Button disabled={busy} onClick={() => toggle(item)}>{item.enabled ? '移除' : '恢复'}</Button> },
        ]} expandable={{ expandedRowRender: item => <div>{item.sources.map((source, index) => {
          const href = safeLink(source.url);
          return <article key={index}><strong>{href ? <a href={href} target="_blank" rel="noreferrer">{source.title}</a> : source.title}</strong>
            <p>{access[source.availability] ?? '资料片段'}{source.page ? ` · 第 ${source.page} 页` : ''}{source.truncated ? ' · 有截断' : ''}</p>
            <p>发表日期：{source.published_at ?? '未确认'} · 取得时间：{source.fetched_at ? dateLabel(source.fetched_at) : '未记录'}</p>
            {source.recheck_due && <Tag color="orange">需要重新核验</Tag>}
            <blockquote>{source.quote}</blockquote></article>;
        })}</div> }} pagination={{ current: page, pageSize: 20, total: bank.data.total, showSizeChanger: false, onChange: setPage }} />
    </>}
    <Collapse items={[{ key: 'policy', label: '资料复核期限', children: <Policy projectId={projectId} /> }]} />
  </>;
}

function Policy({ projectId }: { projectId: string }) {
  const { api } = useConnection(); const cache = useQueryClient();
  const query = useQuery({ queryKey: ['evidence-policy', projectId], queryFn: ({ signal }) => api!.evidencePolicy(projectId, signal) });
  const [busy, setBusy] = useState(false); const [error, setError] = useState<unknown>(null);
  if (query.isPending) return <Loading />;
  if (query.isError) return <QueryError error={query.error} retry={() => void query.refetch()} />;
  return <Form<EvidencePolicy> layout="vertical" initialValues={query.data} disabled={busy} onFinish={async values => {
    if (busy) return; setBusy(true); setError(null);
    try { await api!.saveEvidencePolicy(projectId, values); void cache.invalidateQueries({ queryKey: ['evidence-policy', projectId] }); void cache.invalidateQueries({ queryKey: ['evidence-bank', projectId] }); }
    catch (failure) { setError(failure); } finally { setBusy(false); }
  }}>
    <p>超过期限提示重新核验；最新研究目标仍会补充新检索。复用旧资料不会刷新其取得时间。</p>
    <Form.Item name="web_days" label="网页复核间隔（天）" rules={[{ required: true, type: 'integer', min: 1, max: 365 }]}><NumberSlider min={1} max={365} label="网页复核间隔（天）" /></Form.Item>
    <Form.Item name="paper_days" label="论文复核间隔（天）" rules={[{ required: true, type: 'integer', min: 1, max: 365 }]}><NumberSlider min={1} max={365} label="论文复核间隔（天）" /></Form.Item>
    {error !== null && <Alert type="error" title={errorMessage(error)} />}
    <Button type="primary" htmlType="submit" loading={busy}>保存复核期限</Button>
  </Form>;
}
