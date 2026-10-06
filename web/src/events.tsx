import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Empty, Space, Table, Tag } from 'antd';
import { useQueryClient } from '@tanstack/react-query';
import { ApiError, errorMessage } from './api';
import { useConnection } from './auth';
import { dateLabel, Loading } from './components';
import type { EventPage, StreamMessage, WorkflowEvent } from './types';

const labels: Record<string, string> = { submitted: '任务已提交', claimed: 'Worker 已领取', unit_started: '执行单元开始',
  checkpoint_saved: '检查点已保存', budget_saved: '预算已更新', call_started: '外部调用开始', call_finished: '外部调用结束',
  call_reconciled: '调用回执已核对', recovery_verified: '恢复已核验', checkpoint_rejected: '检查点校验失败',
  approval_requested: '等待人工审批', approval_decided: '审批已决定', approval_consumed: '审批已消费',
  pause: '请求暂停', resume: '请求恢复', cancel: '请求取消', memory_retrieved: '已检索 Memory',
  memory_enabled_changed: 'Memory 启用状态改变', experience_recorded: '已记录经验', other: '其他事件' };
const states = { loading: '加载事件记录', connecting: '连接事件流', live: '事件流已连接', reconnecting: '断线后补齐事件',
  complete: '任务已结束，事件已同步', unsupported: '历史任务无持久化实时事件', error: '事件连接已停止' };

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, ms);
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
  });
}

export default function Events({ taskId, projectId }: { taskId: string; projectId: string }) {
  const { api } = useConnection(); const cache = useQueryClient();
  const [reload, setReload] = useState(0);
  const [state, setState] = useState<keyof typeof states>('loading');
  const [events, setEvents] = useState<WorkflowEvent[]>([]);
  const [history, setHistory] = useState<EventPage | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [historyError, setHistoryError] = useState<unknown>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const historyBusy = useRef(false);
  useEffect(() => {
    if (!api) return;
    const active = new AbortController(); controller.current = active;
    const signal = active.signal;
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    let cursor = 0; let failures = 0;
    setState('loading'); setEvents([]); setHistory(null); setError(null); setHistoryError(null); setLoadingHistory(false); historyBusy.current = false;
    function refresh() {
      if (refreshTimer !== undefined) return;
      refreshTimer = setTimeout(() => {
        refreshTimer = undefined;
        if (!signal.aborted) {
          void cache.invalidateQueries({ queryKey: ['overview', projectId, taskId] });
          void cache.invalidateQueries({ queryKey: ['tasks', projectId] });
        }
      }, 500);
    }
    async function run() {
      try {
        const first = await api!.events(taskId, undefined, signal);
        if (signal.aborted) return;
        cursor = first.next_cursor; setEvents(first.items);
        if (!first.durable) { setState('unsupported'); return; }
        while (!signal.aborted) {
          setState(failures ? 'reconnecting' : 'connecting');
          let ending: Extract<StreamMessage, { type: 'end' }> | undefined;
          try {
            await api!.stream(taskId, cursor, signal, (message) => {
              if (message.type === 'ready') { setState('live'); refresh(); }
              else if (message.type === 'end') { ending = message; refresh(); }
              else {
                if (message.event.sequence <= cursor) return; // At-least-once transport, once-only rendering.
                if (message.event.sequence !== cursor + 1) throw new ApiError('event_sequence_gap');
                cursor = message.event.sequence; failures = 0;
                setEvents((previous) => [...previous, message.event].slice(-200)); refresh();
              }
            });
            if (signal.aborted) return;
            if (ending?.reason === 'terminal' || ending?.reason === 'unsupported') {
              setState(ending.reason === 'terminal' ? 'complete' : 'unsupported'); return;
            }
            if (ending?.reason === 'rotate') { failures = 0; await delay(1000, signal); continue; }
            throw new ApiError('network_unavailable');
          } catch (failure) {
            if (signal.aborted || !api!.connected) return;
            const retryable = failure instanceof ApiError && (failure.status >= 500 || failure.code === 'network_unavailable');
            if (!retryable || ++failures > 5) throw failure;
            setState('reconnecting');
            await delay(Math.min(1000 * 2 ** (failures - 1), 10000), signal);
          }
        }
      } catch (failure) { if (!signal.aborted && api!.connected) { setError(failure); setState('error'); } }
    }
    void run();
    return () => { active.abort(); if (refreshTimer !== undefined) clearTimeout(refreshTimer); };
  }, [api, cache, projectId, taskId, reload]);
  const visible = history?.items ?? events;
  const earliest = visible[0]?.sequence ?? 0;
  async function older() {
    const signal = controller.current?.signal;
    if (!api || !signal || signal.aborted || historyBusy.current || earliest <= 1) return;
    historyBusy.current = true; setLoadingHistory(true); setHistoryError(null);
    try {
      const page = await api.events(taskId, earliest, signal);
      if (!signal.aborted && api.connected) setHistory(page);
    } catch (failure) { if (!signal.aborted && api.connected) setHistoryError(failure); }
    finally { if (!signal.aborted) { historyBusy.current = false; setLoadingHistory(false); } }
  }
  return <section className="event-section">
    <div className="section-heading"><h2>执行事件</h2><Space wrap>
      <Tag color={state === 'live' ? 'success' : state === 'error' ? 'error' : 'default'}>{states[state]}</Tag>
      <Button onClick={() => setReload((value) => value + 1)}>重新加载记录</Button>
    </Space></div>
    <p>这里只读订阅已持久化的工作流事件，不是模型逐 Token 输出。断线按原序号补齐，最多连续自动重连 5 次；刷新或重连不会重新执行任务。</p>
    {error !== null && <Alert type="warning" showIcon title={errorMessage(error)} description="任务仍由后台管理，可手动刷新快照；重新加载记录会重新读取最近事件，不会提交任务。" />}
    {historyError !== null && <Alert type="error" title={errorMessage(historyError)} />}
    {state === 'loading' ? <Loading /> : <>
      <Space wrap className="event-navigation"><Button disabled={earliest <= 1} loading={loadingHistory} onClick={() => void older()}>更早记录</Button>
        <Button disabled={!history} onClick={() => setHistory(null)}>返回最新</Button>
        <span>{history ? '正在浏览历史页，最新事件仍在后台接收。' : '最近事件视图最多保留 200 项；更早记录按页读取。'}</span></Space>
      <Table<WorkflowEvent> key={history ? 'history-' + history.next_cursor : 'live'} rowKey="sequence" dataSource={[...visible].reverse()} size="small" scroll={{ x: 620 }} locale={{ emptyText: <Empty description="暂无持久化事件。" /> }} columns={[
        { title: '序号', dataIndex: 'sequence', width: 85, render: (value) => `#${value}` },
        { title: '事件', dataIndex: 'kind', width: 190, render: (value: string) => labels[value] },
        { title: '安全摘要', dataIndex: 'payload', render: (value: WorkflowEvent['payload']) => <span className="event-payload">{Object.keys(value).length ? JSON.stringify(value) : '—'}</span> },
        { title: '时间', dataIndex: 'created_at', width: 190, render: dateLabel },
      ]} pagination={{ pageSize: 20, showSizeChanger: false }} />
    </>}
  </section>;
}
