import { useState } from 'react';
import { Alert, Breadcrumb, Button, Descriptions, Empty, Input, Select, Space, Table, Tag } from 'antd';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ApiError } from './api';
import { useConnection } from './auth';
import { dateLabel, Loading, Metric, PageHeading, QueryError, Status, statusLabels } from './components';
import type { Project, Task, TaskStatus } from './types';
import CreateProject from './create-project';
import Documents from './documents';
import { useSubmission } from './task-submission';
import Events from './events';
import Controls from './controls';
import Resources from './resources';
import DeleteProject from './delete-project';

export function ProjectsPage() {
  const { api } = useConnection();
  const [page, setPage] = useState(1);
  const query = useQuery({ queryKey: ['projects', page], queryFn: ({ signal }) => api!.projects(page, signal) });
  return <>
    <PageHeading title="项目" refresh={() => void query.refetch()} />
    <div className="filters"><CreateProject /></div>
    {query.isPending ? <Loading /> : query.isError ? <QueryError error={query.error} retry={() => void query.refetch()} />
      : <Table<Project> rowKey="id" dataSource={query.data.items} scroll={{ x: 650 }}
        locale={{ emptyText: <Empty description="暂无项目，点击“新建项目”开始。" /> }}
        columns={[
          { title: '项目', dataIndex: 'name', render: (name: string, item) => <Link to={`/projects/${encodeURIComponent(item.id)}`}>{name}</Link> },
          { title: '说明', dataIndex: 'description', ellipsis: true, render: (value: string | null) => value || '未填写' },
          { title: '更新时间', dataIndex: 'updated_at', width: 210, render: dateLabel },
          { title: '操作', width: 150, render: (_, item) => <DeleteProject project={item} onDeleted={() => setPage(1)} /> },
        ]} pagination={{ current: page, pageSize: 20, total: query.data.total,
                          showSizeChanger: false, onChange: setPage }} />}
  </>;
}

export function ProjectPage() {
  const projectId = useParams().projectId!;
  const { api, session } = useConnection();
  const submission = useSubmission();
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<TaskStatus | undefined>();
  const [search, setSearch] = useState('');
  const project = useQuery({ queryKey: ['project', projectId], queryFn: ({ signal }) => api!.project(projectId, signal) });
  const tasks = useQuery({ queryKey: ['tasks', projectId, page, status, search],
    queryFn: ({ signal }) => api!.tasks(projectId, page, status, search, signal), enabled: project.isSuccess });
  if (project.isPending) return <Loading />;
  if (project.isError) return <QueryError error={project.error} retry={() => void project.refetch()} />;
  return <>
    <Breadcrumb items={[{ title: <Link to="/">项目</Link> }, { title: project.data.name }]} />
    <PageHeading title={project.data.name} subtitle={project.data.description || '该项目尚未填写说明。'} refresh={() => void tasks.refetch()} />
    <div className="section-heading"><h2>研究任务</h2><Space><DeleteProject project={project.data} /><Button type="primary"
      disabled={!session?.capabilities.llm_configured || !session.capabilities.budget_limits}
      onClick={() => submission.openProject(project.data)}>创建研究任务</Button></Space></div>
    {!session?.capabilities.llm_configured && <Alert type="warning" title="研究服务尚未配置，暂时无法创建任务。" />}
    {!session?.capabilities.budget_limits && <Alert type="warning" title="暂时无法读取任务配置，请稍后刷新。" />}
    <Space className="filters" wrap>
      <Input.Search placeholder="搜索研究目标" aria-label="搜索研究目标" allowClear maxLength={200}
        onSearch={(value) => { setSearch(value.trim()); setPage(1); }} style={{ width: 280 }} />
      <Select<TaskStatus> placeholder="全部状态" aria-label="任务状态" allowClear style={{ width: 170 }} value={status}
        options={Object.entries(statusLabels).map(([value, label]) => ({ value: value as TaskStatus, label }))}
        onChange={(value) => { setStatus(value); setPage(1); }} />
    </Space>
    {tasks.isPending ? <Loading /> : tasks.isError ? <QueryError error={tasks.error} retry={() => void tasks.refetch()} />
      : <Table<Task> rowKey="task_id" dataSource={tasks.data.items} scroll={{ x: 850 }}
        locale={{ emptyText: <Empty description="没有符合条件的研究任务。" /> }} columns={[
          { title: '研究目标', dataIndex: 'objective', ellipsis: true, render: (value: string, item) =>
            <Link to={`/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(item.task_id)}`}>{value}</Link> },
          { title: '模式', dataIndex: 'mode', width: 120 },
          { title: '状态', dataIndex: 'status', width: 140, render: (value: TaskStatus) => <Status value={value} /> },
          { title: 'Tokens', width: 100, render: (_, item) => (item.metrics.total_tokens ?? 0).toLocaleString('zh-CN') },
          { title: '更新时间', dataIndex: 'updated_at', width: 210, render: dateLabel },
        ]} pagination={{ current: page, pageSize: 20, total: tasks.data.total, showSizeChanger: false, onChange: setPage }} />}
    <Documents projectId={projectId} />
  </>;
}

export function TaskPage() {
  const { projectId, taskId } = useParams();
  const { api, session } = useConnection();
  const [page, setPage] = useState(1);
  const query = useQuery({ queryKey: ['overview', projectId, taskId, page], queryFn: async ({ signal }) => {
    const value = await api!.overview(taskId!, page, signal);
    if (value.task.project_id !== projectId) throw new ApiError('workbench_task_not_found', 404);
    return value;
  } });
  if (query.isPending) return <Loading />;
  if (query.isError) return <QueryError error={query.error} retry={() => void query.refetch()} />;
  const data = query.data;
  const usage = data.execution?.budget_used ?? data.task.metrics;
  const limits = data.execution?.budget_limits;
  return <>
    <Breadcrumb items={[{ title: <Link to="/">项目</Link> },
      { title: <Link to={`/projects/${encodeURIComponent(projectId!)}`}>项目任务</Link> }, { title: '任务详情' }]} />
    <PageHeading title="任务详情" subtitle={data.task.objective} refresh={() => void query.refetch()} />
    <Space wrap data-testid="task-status"><Status value={data.execution?.status ?? data.task.status} />
      <Tag>{data.task.mode}</Tag><span className="identifier">{data.task.task_id}</span></Space>
    {session?.capabilities.task_controls && <Controls key={taskId} taskId={taskId!} projectId={projectId!} />}
    <div className="metrics-grid">
      <Metric label="模型调用" value={usage.model_calls ?? 0} limit={limits?.model_calls} />
      <Metric label="总 Tokens" value={usage.total_tokens ?? 0} limit={limits?.total_tokens} />
      <Metric label="检索调用" value={usage.search_queries ?? 0} limit={limits?.search_queries} />
      <Metric label="训练次数" value={usage.training_runs ?? 0} limit={limits?.training_runs} />
    </div>
    <Descriptions bordered size="small" column={{ xs: 1, sm: 2, lg: 3 }} items={[
      { key: 'stage', label: '执行阶段', children: data.execution?.stage || '暂无执行信息' },
      { key: 'plan', label: '计划版本', children: data.current_plan?.version ?? '尚未生成' },
      { key: 'cost', label: '金额', children: '暂不可估算' },
      { key: 'evidence', label: '证据', children: data.resources.evidence },
      { key: 'experiments', label: '实验', children: data.resources.experiments },
      { key: 'artifacts', label: '产物', children: data.resources.artifacts },
    ]} />
    {data.execution?.pending_call_count ? <Alert type="warning" showIcon title={`有 ${data.execution.pending_call_count} 个未完成调用，需要检查，不会自动重放。`} /> : null}
    <h2>当前计划</h2>
    {data.current_plan ? <ol className="plan-list">{data.current_plan.plan.steps.map((step) =>
      <li key={step.id}><Tag>{step.type}</Tag><strong>{step.id}</strong><p>{step.description}</p>
        <span>依赖：{step.dependencies.join('、') || '无'}</span></li>)}
    </ol> : <Empty description="尚未生成结构化计划。" />}
    <h2>子任务与历史尝试</h2>
    <Table<Task> rowKey="task_id" dataSource={data.children.items} scroll={{ x: 700 }} size="small" columns={[
      { title: '任务编号', dataIndex: 'task_id', ellipsis: true },
      { title: '模式', dataIndex: 'mode', width: 120 },
      { title: '状态', dataIndex: 'status', width: 140, render: (value: TaskStatus) => <Status value={value} /> },
      { title: '创建时间', dataIndex: 'created_at', width: 210, render: dateLabel },
    ]} pagination={{ current: page, pageSize: 20, total: data.children.total, showSizeChanger: false, onChange: setPage }} />
    <h2>任务输出</h2>
    {data.answer_truncated && <Alert type="warning" title="输出超过大小上限，下方仅展示部分内容。" />}
    {data.answer ? <pre className="plain-output">{data.answer}</pre> : <Empty description="任务尚未产生最终输出。" />}
    {session?.capabilities.task_streaming && <Events taskId={taskId!} projectId={projectId!} />}
    {session?.capabilities.resource_browser && <Resources key={taskId} taskId={taskId!} />}
  </>;
}
