import { useEffect, useState } from 'react';
import { Select } from 'antd';
import { useQuery } from '@tanstack/react-query';
import { useConnection } from './connection';
import { statusLabels } from './components';
import type { LinkedTask } from './types';

interface ReferenceOption {
  value: string;
  label: string;
  title: string;
  status: LinkedTask['status'];
}

export default function TaskReferencePicker({ projectId, exclude = [], value = [], onChange, id, disabled }: {
  projectId: string; exclude?: string[]; value?: LinkedTask[]; onChange?: (items: LinkedTask[]) => void;
  id?: string; disabled?: boolean;
}) {
  const { api } = useConnection();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  useEffect(() => {
    const delay = window.setTimeout(() => setQuery(search.trim().slice(0, 200)), 200);
    return () => window.clearTimeout(delay);
  }, [search]);
  const options = useQuery({
    queryKey: ['task-reference-options', projectId, query],
    queryFn: ({ signal }) => api!.referenceTasks(projectId, query, signal),
    enabled: !!api && open,
  });
  const eligible = (options.data ?? []).filter(item => !exclude.includes(item.task_id));
  const byId = new Map([...value, ...eligible].map(item => [item.task_id, item]));
  return <Select<{ value: string; label: string }[], ReferenceOption> id={id} aria-label="关联任务" mode="multiple" showSearch labelInValue
    style={{ width: '100%' }} maxCount={5} maxTagCount="responsive" disabled={disabled} placeholder="搜索并选择参考任务"
    value={value.map(item => ({ value: item.task_id, label: item.objective }))}
    filterOption={false} onSearch={setSearch} open={open} onOpenChange={setOpen}
    onSelect={() => { setSearch(''); setOpen(false); }}
    notFoundContent={options.isError ? '暂时无法读取任务' : options.isPending ? '正在读取任务…' : '暂无可关联的任务'}
    options={eligible.map(item => ({
      value: item.task_id,
      label: item.objective,
      title: item.objective,
      status: item.status,
    }))}
    optionRender={({ data }) => <div className="task-reference-option"><span>{data.label}</span><small>{statusLabels[data.status]}</small></div>}
    onChange={items => onChange?.(items.flatMap(({ value: taskId }) => {
      const task = byId.get(taskId);
      return task ? [task] : [];
    }))} />;
}
