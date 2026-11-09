import { useState } from 'react';
import { Button, Empty, Pagination } from 'antd';
import { ArrowUpOutlined, FolderOpenOutlined, ReloadOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useConnection } from './connection';
import { dateLabel, Loading, QueryError } from './components';
import CreateProject from './create-project';
import DeleteProject from './delete-project';
import { usePageReady } from './page-transition';

function updated(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '时间未知' : date.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' });
}

export default function ProjectsPage() {
  const { api } = useConnection();
  const [page, setPage] = useState(1);
  const query = useQuery({ queryKey: ['projects', page], queryFn: ({ signal }) => api!.projects(page, signal) });
  usePageReady(!query.isPending);
  return <div className="projects-overview">
    <div className="projects-intro">
      <div><h1>项目</h1><p>从一个问题开始，让每次探索都有迹可循。</p></div>
      <div className="projects-actions">
        <Button icon={<ReloadOutlined />} onClick={() => void query.refetch()}>刷新快照</Button>
        <CreateProject />
      </div>
    </div>
    {query.isPending ? <Loading /> : query.isError ? <QueryError error={query.error} retry={() => void query.refetch()} /> : <>
      <div className="collection-heading"><h2>研究空间</h2><span>{query.data.total} 个项目</span></div>
      {query.data.items.length ? <div className="project-grid">{query.data.items.map(project => <article className="project-card" key={project.id}>
        <div className="project-card-top"><span className="project-folder" aria-hidden="true"><FolderOpenOutlined /></span>
          <div className="project-card-action"><DeleteProject project={project} onDeleted={() => setPage(1)} /></div>
        </div>
        <h2 className="project-card-title"><Link to={`/projects/${encodeURIComponent(project.id)}`}>{project.name}</Link></h2>
        <p className="project-description">{project.description || '为这个项目留一个想探索的问题。'}</p>
        <div className="project-card-footer"><time dateTime={project.updated_at} title={dateLabel(project.updated_at)}>更新于 {updated(project.updated_at)}</time>
          <ArrowUpOutlined className="project-open-icon" aria-hidden="true" /></div>
      </article>)}</div> : <div className="projects-empty"><Empty description="暂无项目，点击“新建项目”开始。" /></div>}
      {query.data.total > 20 && <Pagination className="project-pagination" current={page} pageSize={20} total={query.data.total} showSizeChanger={false} onChange={setPage} />}
    </>}
  </div>;
}
