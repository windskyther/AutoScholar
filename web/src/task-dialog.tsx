import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Checkbox, Collapse, Form, Input, InputNumber, Modal, Select, Space, Table } from 'antd';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { ApiError, errorMessage, isWriteUncertain } from './api';
import { useConnection } from './auth';
import { Loading, QueryError } from './components';
import { DocumentState } from './documents';
import type { BudgetLimits, PDFDocument, Project, TaskSubmission } from './types';

interface Values {
  objective: string; budget: BudgetLimits; sources: ('web' | 'paper')[];
  documentMode: 'all' | 'none' | 'selected'; retrieval: TaskSubmission['retrieval_mode']; consent: boolean;
}
const fields: { key: keyof BudgetLimits; label: string; min: number; max: number }[] = [
  { key: 'total_tokens', label: '总 Token 预算', min: 1, max: 1000000 },
  { key: 'model_calls', label: '模型调用上限', min: 1, max: 300 },
  { key: 'search_queries', label: '检索调用上限', min: 1, max: 100 },
  { key: 'training_runs', label: '训练次数上限', min: 1, max: 20 },
  { key: 'steps', label: '计划步骤上限', min: 1, max: 100 },
  { key: 'replans', label: '重规划上限', min: 0, max: 10 },
  { key: 'tool_calls', label: '工具调用上限', min: 1, max: 500 },
  { key: 'code_repairs', label: '代码修复上限', min: 0, max: 10 },
  { key: 'sandbox_runs', label: '沙箱次数上限', min: 1, max: 200 },
  { key: 'wall_seconds', label: '活跃执行时间（秒）', min: 1, max: 7200 },
];

export default function TaskDialog({ project, open, close, complete, pendingChanged }: {
  project: Project; open: boolean; close: () => void; complete: () => void; pendingChanged: (value: boolean) => void;
}) {
  const { api, session } = useConnection();
  const cache = useQueryClient();
  const navigate = useNavigate();
  const [form] = Form.useForm<Values>();
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const draft = useRef<{ key: string; payload: TaskSubmission } | null>(null);
  const inFlight = useRef(false);
  const documentMode = Form.useWatch('documentMode', form) ?? 'all';
  const ceiling = session?.capabilities.budget_limits;
  const docs = useQuery({ queryKey: ['documents', project.id, page], queryFn: ({ signal }) => api!.documents(project.id, page, signal),
    enabled: open && documentMode === 'selected' });
  useEffect(() => {
    if (!busy && !uncertain) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [busy, uncertain]);
  async function send() {
    if (!api || !draft.current || inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(null);
    try {
      const result = await api.submitTask(draft.current.payload, draft.current.key);
      if (!api.connected) return;
      void cache.invalidateQueries({ queryKey: ['tasks', project.id] });
      complete(); navigate(`/projects/${encodeURIComponent(project.id)}/tasks/${encodeURIComponent(result.task_id)}`);
    } catch (failure) {
      setError(failure);
      const unknown = isWriteUncertain(failure);
      setUncertain(unknown); pendingChanged(unknown);
      if (!unknown) draft.current = null;
    } finally { inFlight.current = false; setBusy(false); }
  }
  async function submit(values: Values) {
    if (inFlight.current || uncertain || !values.consent || !ceiling) return;
    if (values.documentMode === 'selected' && (selected.length === 0 || selected.length > 100)) {
      setError(new ApiError('documents_selection_required', 422)); return;
    }
    draft.current = { key: crypto.randomUUID(), payload: structuredClone({
      objective: values.objective.trim(), mode: 'autonomous', project_id: project.id,
      document_ids: values.documentMode === 'all' ? null : values.documentMode === 'none' ? [] : selected,
      retrieval_mode: values.retrieval, research_sources: values.sources, budget: values.budget,
    }) };
    await send();
  }
  return <Modal title={`提交研究任务 · ${project.name}`} open={open} width={820} mask={{ closable: false }}
    keyboard={!busy} closable={!busy} onCancel={() => { if (!busy) close(); }} footer={null}>
    <Alert type="warning" showIcon title="提交后由 Worker 自动执行，可能产生 LLM、检索和训练费用"
      description="预算是资源上限，不是金额估价；最后一次模型响应可能越过 Token 阈值。这里只支持持久化 autonomous 模式，不会自动重试或重跑整轮。" />
    <Form form={form} layout="vertical" disabled={busy || uncertain} onFinish={(values) => void submit(values)} initialValues={{
      budget: ceiling, sources: session?.capabilities.web_search_configured ? ['web'] : ['paper'],
      documentMode: 'all', retrieval: 'hybrid_rerank', consent: false,
    }}>
      <Form.Item name="objective" label="研究目标" rules={[{ required: true, whitespace: true, max: 10000, message: '填写 1–10000 字符的研究目标' }]}>
        <Input.TextArea rows={3} maxLength={10000} showCount />
      </Form.Item>
      <Form.Item name="sources" label="检索来源" rules={[{ required: true, type: 'array', min: 1, max: 2 }]}>
        <Select mode="multiple" options={[{ value: 'web', label: 'Tavily Web', disabled: !session?.capabilities.web_search_configured },
          { value: 'paper', label: 'Semantic Scholar 论文（无密钥时匿名）' }]} />
      </Form.Item>
      {!session?.capabilities.web_search_configured && <p>Tavily 未配置；匿名论文检索可能受到限流。</p>}
      <Space className="task-options" wrap>
        <Form.Item name="documentMode" label="项目 PDF 范围"><Select style={{ width: 210 }} options={[
          { value: 'all', label: '全部已就绪 PDF' }, { value: 'none', label: '不使用 PDF' }, { value: 'selected', label: '选择已就绪 PDF' },
        ]} /></Form.Item>
        <Form.Item name="retrieval" label="检索方式"><Select style={{ width: 210 }} options={[
          { value: 'hybrid_rerank', label: '混合检索 + 重排' }, { value: 'hybrid', label: '混合检索' },
          { value: 'dense', label: '向量检索' }, { value: 'sparse', label: '稀疏检索' },
        ]} /></Form.Item>
      </Space>
      {documentMode === 'selected' && <>
        <p>已选择 {selected.length} 项；支持跨页选择，最多 100 项。提交时服务端再次核对归属与就绪状态。</p>
        {docs.isPending ? <Loading /> : docs.isError ? <QueryError error={docs.error} retry={() => void docs.refetch()} />
          : <Table<PDFDocument> rowKey="id" size="small" dataSource={docs.data.items} scroll={{ x: 400 }} columns={[
            { title: '文档', dataIndex: 'title' }, { title: '状态', dataIndex: 'status', render: (value) => <DocumentState value={value} /> },
          ]} rowSelection={{ selectedRowKeys: selected, preserveSelectedRowKeys: true,
            getCheckboxProps: (item) => ({ disabled: busy || uncertain || item.status !== 'ready' || (selected.length >= 100 && !selected.includes(item.id)) }),
            onChange: (keys) => setSelected(keys.map(String)) }}
            pagination={{ current: page, pageSize: 20, total: docs.data.total, showSizeChanger: false, onChange: setPage }} />}
      </>}
      <Collapse className="budget-settings" defaultActiveKey={['budget']} items={[{ key: 'budget', label: '预算上限（不超过服务端配置）', children:
        <div className="budget-grid">{fields.map((field) => <Form.Item key={field.key} name={['budget', field.key]} label={field.label}
          rules={[{ required: true, type: 'integer', min: field.min, max: Math.min(ceiling?.[field.key] ?? field.max, field.max) }]}>
          <InputNumber min={field.min} max={Math.min(ceiling?.[field.key] ?? field.max, field.max)} precision={0} style={{ width: '100%' }} />
        </Form.Item>)}</div> }]} />
      <Form.Item name="consent" valuePropName="checked" rules={[{ validator: (_, value) => value ? Promise.resolve() : Promise.reject(new Error('请确认自动执行及可能产生费用')) }]}>
        <Checkbox>我确认将此研究目标交给 Worker 自动执行，并接受可能产生的 API 费用。</Checkbox>
      </Form.Item>
      {error !== null && <Alert type="error" showIcon title={errorMessage(error)}
        description={uncertain ? '请勿新建另一轮。请求内容已锁定，可使用下面的原编号确认/重试；关闭窗口会保留该请求，但刷新或断开连接将丢失内存状态，请先记录编号并核查项目任务。' : '请检查输入和文档状态，修改后再手动提交。'} />}
      {draft.current && <p className="identifier">提交编号：{draft.current.key}</p>}
      <Space wrap>
        {!uncertain && <Button type="primary" htmlType="submit" loading={busy} disabled={!ceiling || !session?.capabilities.llm_configured}>确认提交并自动执行</Button>}
        {uncertain && <Button type="primary" loading={busy} disabled={busy} onClick={() => void send()}>用原编号确认 / 重试</Button>}
        <Button disabled={busy} onClick={close}>{uncertain ? '保留请求并关闭' : '取消'}</Button>
      </Space>
    </Form>
  </Modal>;
}
