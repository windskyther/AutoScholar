import { useState } from 'react';
import { Alert, Button, Modal, Space } from 'antd';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { errorMessage } from './api';
import { QueryError, Status } from './components';
import { useConnection } from './connection';
import DeleteTask from './delete-task';
import { ACTIVE_TASK_REFRESH_MS, isActiveTask, SNAPSHOT_REFRESH_MS } from './snapshot-refresh';
import TaskReferencePicker from './task-reference-picker';
import { useSubmission } from './task-submission';
import type { LinkedTask, Task, TaskLinks } from './types';
import WorkbenchTable from './workbench-table';

const pageSize = 10;

export default function TaskConnections({ projectId, task }: { projectId: string; task: Task }) {
  const { api, session } = useConnection();
  const { openProject, pending } = useSubmission();
  const cache = useQueryClient();
  const [page, setPage] = useState(1);
  const [draft, setDraft] = useState<LinkedTask[] | null>(null);
  const key = ['task-links', task.task_id];
  const query = useQuery({
    queryKey: [...key, page],
    queryFn: ({ signal }) => api!.taskLinks(task.task_id, page, signal),
    refetchInterval: ({ state }) => isActiveTask(task.status) ||
      state.data?.followups.items.some(item => isActiveTask(item.status))
      ? ACTIVE_TASK_REFRESH_MS : SNAPSHOT_REFRESH_MS,
  });
  const save = useMutation({
    mutationFn: (items: LinkedTask[]) => api!.saveTaskLinks(task.task_id, items.map(item => item.task_id)),
    onSuccess: async references => {
      await cache.cancelQueries({ queryKey: key });
      cache.setQueriesData<TaskLinks>({ queryKey: key }, value => value && { ...value, references });
      setDraft(null);
      await cache.invalidateQueries({ queryKey: key });
    },
  });
  if (!query.data) {
    return query.isError ? <QueryError error={query.error} retry={() => void query.refetch()} /> : null;
  }
  const links = query.data;
  const canCreate = (session?.capabilities.llm_configured || session?.capabilities.model_management) &&
    session.capabilities.budget_limits;
  const taskLink = (item: LinkedTask) => <Link
    to={`/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(item.task_id)}`}>{item.objective}</Link>;
  return <section className="task-connections" aria-label="任务关联与追加">
    <div className="section-heading"><h2>任务关联与追加</h2><Space>
      <Button onClick={() => { save.reset(); setDraft(links.references); }}>编辑关联</Button>
      {links.can_append && <Button type="primary" disabled={!canCreate || pending} onClick={() => openProject(
        { id: projectId, name: links.project_name }, links.workflow_kind ?? 'research',
        { previous: task, references: links.references, modelProfileId: links.model_profile_id },
      )}>追加任务</Button>}
    </Space></div>
    {links.previous && <p>上一轮 · {taskLink(links.previous)}</p>}
    {links.references.length > 0 && <><h3>关联任务</h3><ul>
      {links.references.map(item => <li key={item.task_id}>{taskLink(item)}</li>)}
    </ul></>}
    {links.followups.total > 0 && <><h3>追加记录</h3>
      <WorkbenchTable<LinkedTask> rowKey="task_id" dataSource={links.followups.items} size="small"
        columns={[
          { title: '追加指令', className: 'table-primary', render: (_, item) => taskLink(item) },
          { title: '状态', width: 120, render: (_, item) => <Status value={item.status} /> },
          ...(session?.capabilities.task_deletion ? [{ title: '操作', width: 105,
            render: (_: unknown, item: LinkedTask) => <DeleteTask task={item} projectId={projectId}
              onDeleted={() => setPage(1)} /> }] : []),
        ]} pagination={{ current: page, pageSize, total: links.followups.total, onChange: setPage }} />
    </>}
    <Modal title="编辑关联任务" open={draft !== null} onCancel={() => setDraft(null)}
      okText="保存" cancelText="取消" onOk={() => { if (draft) save.mutate(draft); }}
      confirmLoading={save.isPending} cancelButtonProps={{ disabled: save.isPending }}
      closable={!save.isPending} keyboard={!save.isPending} mask={{ closable: false }}>
      <p>选中的任务结果会用于后续追加。</p>
      <TaskReferencePicker projectId={projectId} exclude={links.previous ? [task.task_id, links.previous.task_id] : [task.task_id]}
        value={draft ?? []} onChange={setDraft} disabled={save.isPending} />
      {save.isError && <Alert type="error" title={errorMessage(save.error)} />}
    </Modal>
  </section>;
}
