import { useState } from 'react';
import { Button, Empty, Pagination } from 'antd';
import { ArrowUpOutlined, FolderOpenOutlined, ReloadOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Link, useLocation } from 'react-router-dom';
import { useConnection } from './connection';
import { dateLabel, QueryError } from './components';
import CreateProject from './create-project';
import DeleteProject from './delete-project';
import { usePageReady } from './page-transition';
import { projectDestination, readResearchEntry, researchEntryLabels } from './research-entry';

function updated(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '时间未知' : date.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' });
}

export default function ProjectsPage() {
  const { api } = useConnection();
  const entry = readResearchEntry(useLocation().search);
  const [page, setPage] = useState(1);
  const query = useQuery({ queryKey: ['projects', page], queryFn: ({ signal }) => api!.projects(page, signal) });
  usePageReady(!query.isPending);
  return <div className="projects-overview">
    <div className="projects-intro">
      <div><h1>{entry ? `${researchEntryLabels[entry]} · 选择项目` : '项目'}</h1></div>
      <div className="projects-actions">
        <Button icon={<ReloadOutlined />} onClick={() => void query.refetch()}>刷新快照</Button>
        <CreateProject researchEntry={entry} />
      </div>
    </div>
    {query.isPending ? null : query.isError ? <QueryError error={query.error} retry={() => void query.refetch()} /> : <>
      <div className="collection-heading"><h2>研究空间</h2><span>{query.data.total} 个项目</span></div>
      {query.data.items.length ? <div className="project-grid">{query.data.items.map(project => <article className="project-card" key={project.id}>
        <div className="project-card-top"><span className="project-folder" aria-hidden="true"><FolderOpenOutlined /></span>
          <div className="project-card-action"><DeleteProject project={project} onDeleted={() => setPage(1)} /></div>
        </div>
        <h2 className="project-card-title"><Link to={projectDestination(project.id, entry)}>{project.name}</Link></h2>
        <p className="project-description">{project.description}</p>
        <div className="project-card-footer"><time dateTime={project.updated_at} title={dateLabel(project.updated_at)}>更新于 {updated(project.updated_at)}</time>
          <ArrowUpOutlined className="project-open-icon" aria-hidden="true" /></div>
      </article>)}</div> : <div className="projects-empty"><Empty description="暂无项目" /></div>}
      {query.data.total > 20 && <Pagination className="project-pagination" current={page} pageSize={20} total={query.data.total} showSizeChanger={false} onChange={setPage} />}
    </>}
  </div>;
}
