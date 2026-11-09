import { Alert, Collapse, Empty, Tag } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useConnection } from './connection';
import { Loading, QueryError } from './components';

const notes: Record<string, string> = {
  keyword_fallback: '语义索引暂不可用，本次使用关键词查找相关证据。',
  evidence_store_unavailable: '项目证据暂不可用，本次按常规检索执行。',
};
const labels: Record<string, string> = { selected: '待筛选', adopted: '已采用', excluded: '未采用' };

export default function EvidenceReuse({ taskId, projectId, running }: { taskId: string; projectId: string; running: boolean }) {
  const { api } = useConnection();
  const query = useQuery({ queryKey: ['evidence-reuse', taskId], queryFn: ({ signal }) => api!.evidenceReuse(taskId, signal),
    refetchInterval: running ? 5000 : false });
  return <Collapse items={[{ key: 'reuse', label: '项目证据复用', children:
    query.isPending ? <Loading /> : query.isError ? <QueryError error={query.error} retry={() => void query.refetch()} /> : <>
      <p>{query.data.enabled ? `找到 ${query.data.selected} 条相关结论，采用 ${query.data.adopted} 条。` : '本任务未启用项目证据复用。'}检索与读取调用：{query.data.retrieval_calls} 次。</p>
      {query.data.note && notes[query.data.note] && <Alert type="info" title={notes[query.data.note]} />}
      {query.data.items.length ? query.data.items.map((item, index) => <article key={index}>
        <Tag>{labels[item.status]}</Tag><p>{item.claim}</p>
        <p>来源目标：<Link to={`/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(item.source_task_id)}`}>{item.source_objective}</Link></p>
      </article>) : <Empty description="没有可复用的项目结论。" />}
    </>,
  }]} />;
}
