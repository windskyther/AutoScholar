import { useState } from 'react';
import { Alert, Collapse, Empty, Table, Tag } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { useConnection } from './connection';
import { Loading, QueryError } from './components';

import { qualityPhases, verdicts, access } from './quality-types';
import type { Assessment, QualitySource } from './quality-types';

function Review({ item }: { item: Assessment | null }) {
  return item ? <><Tag>{verdicts[item.verdict]}</Tag><p>{item.reason}</p>
    {item.quote && <blockquote>{item.quote}</blockquote>}</> : <>尚未完成</>;
}
function QualityDetails({ taskId, running }: { taskId: string; running: boolean }) {
  const { api } = useConnection();
  const query = useQuery({ queryKey: ['quality', taskId], queryFn: ({ signal }) => api!.quality(taskId, signal),
    refetchInterval: running ? 5000 : false });
  if (query.isPending) return <Loading />;
  if (query.isError) return <QueryError error={query.error} retry={() => void query.refetch()} />;
  const data = query.data;
  return <>
    <Alert type={data.accepted ? 'success' : 'info'} title={data.accepted ? '结论已通过资料审查' : qualityPhases[data.phase]}
      description={`候选资料 ${data.sources.length} 项 · 正文提取 ${data.reads} 项 · 修订 ${data.rounds} 轮 · 补充检索 ${data.supplemental_queries} 次`} />
    <p>审查用于核对依据与局限，仍可能遗漏错误。仅有摘要的来源会单独标明。</p>
    <Table<QualitySource> rowKey="reference" dataSource={data.sources} size="small" pagination={{ pageSize: 10 }} scroll={{ x: 800 }} columns={[
      { title: '来源', render: (_, item) => <><Tag>{item.reference}</Tag>{item.url && /^https?:\/\//i.test(item.url)
        ? <a href={item.url} target="_blank" rel="noreferrer">{item.title}</a> : item.title}
        <p>{item.published_at ?? '发表日期未确认'} · {access[item.availability]}{item.truncated ? ' · 有截断' : ''}</p></> },
      { title: '初筛', render: (_, item) => <Review item={item.initial} /> },
      { title: '复筛', render: (_, item) => <Review item={item.refined} /> },
    ]} />
    {data.reviews.length ? data.reviews.map(r => <div key={r.round}>
      <strong>第 {r.round + 1} 次审稿 · {r.decision === 'accept' ? '通过' : r.decision === 'insufficient' ? '证据不足' : '需要修订'}</strong>
      {r.issues.map((issue, index) => <div key={index}><blockquote>{issue.draft_quote}</blockquote><p>{issue.reason}</p></div>)}
    </div>) : <Empty description="尚无审稿记录" />}
  </>;
}
export default function ResearchQuality({ taskId, running }: { taskId: string; running: boolean }) {
  const [open, setOpen] = useState(false);
  return <Collapse className="quality-audit" activeKey={open ? ['quality'] : []} onChange={keys => setOpen(keys.length > 0)}
    items={[{ key: 'quality', label: '资料筛选与审稿', children: open ? <QualityDetails taskId={taskId} running={running} /> : null }]} />;
}
