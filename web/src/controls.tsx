import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Descriptions, Empty, Form, Input, InputNumber, Modal, Space, Table, Tag } from 'antd';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, errorMessage, isWriteUncertain } from './api';
import { useConnection } from './auth';
import { dateLabel, Loading, QueryError, Status } from './components';
import type { Approval, ApprovalAction, ControlAction, ExpectedState, ExperimentSpecification } from './types';

const controlLabels: Record<ControlAction, string> = { pause: '暂停任务', resume: '恢复任务', cancel: '取消任务' };
const decisionLabels: Record<ApprovalAction, string> = { approve: '批准', reject: '拒绝', modify: '修改参数' };
const approvalLabels = { pending: '待审批', approved: '已批准，未消费', rejected: '已拒绝', expired: '已过期', superseded: '已被新参数取代', consumed: '已消费' };
type Dialog = { expected: ExpectedState } & (
  { kind: 'control'; action: ControlAction } | { kind: 'approval'; action: ApprovalAction | 'view'; approval: Approval }
);
type Values = ExperimentSpecification & { reason: string };
const terminal = new Set(['succeeded', 'partial', 'failed', 'budget_exceeded', 'cancelled']);

export default function Controls({ taskId, projectId }: { taskId: string; projectId: string }) {
  const { api } = useConnection(); const cache = useQueryClient();
  const [page, setPage] = useState(1);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false); const mounted = useRef(true);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const [form] = Form.useForm<Values>();
  const [modal, contextHolder] = Modal.useModal();
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const controls = useQuery({ queryKey: ['controls', taskId], queryFn: ({ signal }) => api!.controls(taskId, signal),
    refetchInterval: (query) => query.state.data?.expected && !terminal.has(query.state.data.status) ? 3000 : false });
  const approvals = useQuery({ queryKey: ['approvals', taskId, page], queryFn: ({ signal }) => api!.approvals(taskId, page, signal),
    enabled: !!controls.data?.expected,
    refetchInterval: controls.data?.status === 'awaiting_approval' ? 3000 : false });
  const uncertain = isWriteUncertain(error);
  const stale = dialog && (controls.isError || !controls.data?.expected
    || JSON.stringify(dialog.expected) !== JSON.stringify(controls.data.expected)
    || (dialog.kind === 'approval' && dialog.action === 'approve' && Date.parse(dialog.approval.expires_at) <= Date.now()));
  function refresh() {
    void cache.invalidateQueries({ queryKey: ['controls', taskId] });
    void cache.invalidateQueries({ queryKey: ['approvals', taskId] });
    void cache.invalidateQueries({ queryKey: ['overview', projectId, taskId] });
    void cache.invalidateQueries({ queryKey: ['tasks', projectId] });
  }
  function openControl(action: ControlAction) {
    if (inFlight.current || uncertain || !controls.data?.expected || !controls.data.actions.includes(action)) return;
    form.resetFields(); setError(null); setNotice('');
    setDialog({ kind: 'control', action, expected: { ...controls.data.expected } });
  }
  function openApproval(approval: Approval, action: ApprovalAction | 'view') {
    if (inFlight.current || uncertain || !controls.data?.expected || (action !== 'view' && !approval.actions.includes(action))) return;
    form.resetFields(); form.setFieldsValue({ ...approval.specification, reason: '' });
    setError(null); setNotice('');
    setDialog({ kind: 'approval', action, approval, expected: { ...controls.data.expected } });
  }
  async function submit(values: Values) {
    if (!api || !dialog || inFlight.current || uncertain || (dialog.kind === 'approval' && dialog.action === 'view')) return;
    if (stale) { setError(new ApiError('workbench_state_stale', 409)); refresh(); return; }
    const pending = dialog;
    inFlight.current = true; setBusy(true); setError(null);
    try {
      if (pending.kind === 'control') await api.controlTask(taskId, pending.action, pending.expected);
      else {
        if (pending.action === 'view') return;
        const original = pending.approval.specification;
        await api.decideApproval(taskId, pending.approval.id, {
          action: pending.action, operation_sha256: pending.approval.operation_sha256,
          reason: values.reason ?? '', expected: pending.expected,
          ...(pending.action === 'modify' ? { specification: {
            ...original, name: values.name, seed: values.seed, epochs: values.epochs,
            batch_size: values.batch_size, learning_rate: values.learning_rate,
            train_samples: values.train_samples, test_samples: values.test_samples,
          } } : {}),
        });
      }
      if (!mounted.current || !api.connected) return;
      setNotice(pending.kind === 'approval'
        ? pending.action === 'reject' ? '审批已拒绝，任务仍等待处理；可修改参数或取消任务。'
          : pending.action === 'modify' ? '已创建新计划版本并暂停。单独恢复后会重新验证代码和审批，既有预算用量不会清零。'
            : '审批已批准，任务保持暂停；需单独恢复，批准本身不会开始训练。'
        : pending.action === 'resume' ? '恢复请求已保存，Worker 可能继续调用付费 API；实际状态以快照为准。'
          : pending.action === 'pause' ? '暂停请求已保存。正在执行的单元可能先收尾，不代表立即停止或免除已产生费用。'
            : '取消请求已保存。正在执行的单元可能先收尾，已产生费用不能撤销。');
      setDialog(null); refresh();
    } catch (failure) {
      if (mounted.current && api.connected) { setError(failure); setDialog(null); refresh(); }
    } finally { inFlight.current = false; if (mounted.current) setBusy(false); }
  }
  async function inspect() {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true);
    try {
      const state = await controls.refetch(); const review = await approvals.refetch();
      if (state.isError || review.isError || !api?.connected || !mounted.current) return;
      modal.confirm({ title: '确认已核查当前任务及审批状态？',
        content: '解除操作锁只允许你重新选择操作，不会重复发送上一笔请求。请核对任务编号、状态和审批决定；有疑问时保持锁定。',
        okText: '已核查，解除操作锁', cancelText: '保持锁定',
        onOk: () => { if (mounted.current && api.connected) { setError(null); refresh(); } } });
    } finally { inFlight.current = false; if (mounted.current) setBusy(false); }
  }
  return <section className="control-section">
    {contextHolder}
    <div className="section-heading"><h2>任务控制与审批</h2><Button onClick={refresh}>刷新控制状态</Button></div>
    <p>状态每 3 秒只读核查。所有变更都需要确认，不自动重试；审批通过与恢复执行是两次独立操作。</p>
    {notice && <Alert type="success" showIcon title={notice} />}
    {error !== null && <Alert type="error" showIcon title={errorMessage(error)}
      description={uncertain ? '写入结果未确认，已锁定变更操作。请核查最新任务及审批记录后手动解锁；不会自动重发。' : '未自动重试，请刷新后重新选择操作。'}
      action={uncertain ? <Button loading={busy} onClick={() => void inspect()}>核查并解锁</Button> : undefined} />}
    {controls.isPending ? <Loading /> : controls.isError ? <QueryError error={controls.error} retry={refresh} />
      : !controls.data.expected ? <Alert type="info" title="历史非持久化任务仅支持查看，不支持控制或审批。" /> : <>
        <Space wrap><Status value={controls.data.status} />
          {(['pause', 'resume', 'cancel'] as const).map((action) => <Button key={action} danger={action === 'cancel'}
            disabled={busy || uncertain || !controls.data.actions.includes(action)}
            onClick={() => openControl(action)}>{controlLabels[action]}</Button>)}
        </Space>
        {controls.data.status === 'recovery_required' && <Alert className="snapshot-note" type="warning" showIcon
          title="此任务需要核查不确定调用，不能直接恢复。" description="本模块不提供强制重放。请使用既有后端恢复核查流程处理回执；也可明确取消任务。" />}
        {approvals.isPending ? <Loading /> : approvals.isError ? <QueryError error={approvals.error} retry={() => void approvals.refetch()} />
          : <Table<Approval> className="approval-table" rowKey="id" dataSource={approvals.data.items} scroll={{ x: 800 }} size="small"
            locale={{ emptyText: <Empty description="暂无人工审批记录。" /> }} columns={[
              { title: '审批', dataIndex: 'step_id', render: (value: string, item) => <><span>{value}</span><br /><span className="identifier">{item.id}</span></> },
              { title: '计划版本', dataIndex: 'plan_version', width: 100 },
              { title: '状态', dataIndex: 'status', width: 170, render: (value: Approval['status']) => <Tag>{approvalLabels[value]}</Tag> },
              { title: '过期时间', dataIndex: 'expires_at', width: 200, render: dateLabel },
              { title: '操作', width: 250, render: (_, item) => <Space wrap>
                <Button size="small" onClick={() => openApproval(item, item.actions[0] ?? 'view')}
                  disabled={busy || uncertain}>{item.actions.length ? '审阅与决定' : '查看审批详情'}</Button>
              </Space> },
            ]} pagination={{ current: page, pageSize: 20, total: approvals.data.total, showSizeChanger: false, onChange: setPage }} />}
      </>}
    <Modal open={!!dialog} title={dialog?.kind === 'control' ? `确认${controlLabels[dialog.action]}？` : '审阅实验审批'}
      width={720} footer={null} mask={{ closable: false }} closable={!busy} keyboard={!busy}
      styles={{ body: { maxHeight: '70dvh', overflowY: 'auto' } }}
      onCancel={() => { if (!busy) { setDialog(null); setError(null); } }}>
      {dialog && <>
        <p className="identifier">任务：{taskId} · 检查点 #{dialog.expected.checkpoint_sequence} · 事件 #{dialog.expected.event_sequence}</p>
        {stale && <Alert type="warning" title="状态或有效期已变化，请关闭窗口并刷新后重新审阅。" />}
        {dialog.kind === 'control' ? <Alert type={dialog.action === 'resume' ? 'warning' : 'info'} showIcon
          title={dialog.action === 'resume' ? '恢复后可能产生 LLM、检索等 API 费用及本地训练记录。'
            : dialog.action === 'pause' ? '暂停是协作式请求，当前单元可能继续收尾。'
              : '取消不能在工作台撤销；当前单元可能继续收尾，已产生费用不能退回。'} /> : <>
          <Alert type="warning" showIcon title="风险级别 3 · MNIST CPU 实验"
            description="计算量单位 = epochs × train_samples × 2 个模型，不是金额。批准不会自动恢复；修改会创建新计划版本，并使相关代码、实验及下游结果失效。" />
          <Descriptions size="small" bordered column={2} items={[
            { key: 'version', label: '计划 / 步骤', children: `v${dialog.approval.plan_version} / ${dialog.approval.step_id}` },
            { key: 'cost', label: '当前计算量单位', children: dialog.approval.cost_units.toLocaleString('zh-CN') },
            { key: 'status', label: '审批状态', children: approvalLabels[dialog.approval.status] },
            { key: 'expiry', label: '有效期至', children: dateLabel(dialog.approval.expires_at) },
          ]} />
          <p className="identifier">操作指纹：{dialog.approval.operation_sha256}</p>
          <p>审批说明：{dialog.approval.reason || '未填写'}</p>
          <details><summary>原实验参数与共享预算上限</summary><pre className="plain-output">{JSON.stringify({ specification: dialog.approval.specification, budget_limits: dialog.approval.budget_limits }, null, 2)}</pre></details>
          <Space wrap className="approval-options">{dialog.approval.actions.map((action) => <Button key={action}
            type={dialog.action === action ? 'primary' : 'default'} danger={action === 'reject'} disabled={busy || !!stale}
            onClick={() => setDialog({ ...dialog, action })}>{decisionLabels[action]}</Button>)}</Space>
        </>}
        <Form form={form} layout="vertical" disabled={busy} onFinish={(values) => void submit(values)}>
          {dialog.kind === 'approval' && <>
            {dialog.action === 'modify' && <div className="budget-grid">
              <Form.Item name="name" label="实验名称" rules={[{ required: true, pattern: /^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$/ }]}><Input maxLength={200} /></Form.Item>
              <Form.Item name="seed" label="随机种子" rules={[{ required: true }]}><InputNumber min={0} max={4294967295} precision={0} /></Form.Item>
              <Form.Item name="epochs" label="训练轮数" rules={[{ required: true }]}><InputNumber min={1} max={10} precision={0} /></Form.Item>
              <Form.Item name="batch_size" label="批量大小" rules={[{ required: true }]}><InputNumber min={8} max={256} precision={0} /></Form.Item>
              <Form.Item name="learning_rate" label="学习率" rules={[{ required: true }, { type: 'number', min: Number.MIN_VALUE, max: 1 }]}><InputNumber min={0.000000001} max={1} /></Form.Item>
              <Form.Item name="train_samples" label="训练样本数" rules={[{ required: true }]}><InputNumber min={128} max={60000} precision={0} /></Form.Item>
              <Form.Item name="test_samples" label="测试样本数" rules={[{ required: true }]}><InputNumber min={128} max={10000} precision={0} /></Form.Item>
            </div>}
            {dialog.action === 'modify' && <Form.Item noStyle shouldUpdate>
              {({ getFieldValue }) => <Alert type="info" title={`修改后计算量单位：${
                Number.isFinite(getFieldValue('epochs') * getFieldValue('train_samples'))
                  ? (getFieldValue('epochs') * getFieldValue('train_samples') * 2).toLocaleString('zh-CN') : '待填写'
              }（原 ${dialog.approval.cost_units.toLocaleString('zh-CN')}）`}
                description="数据集 MNIST、模型顺序 MLP/CNN、主指标和 CPU 设备不变；共享预算上限不会扩大，用量不会清零。" />}
            </Form.Item>}
            <Form.Item name="reason" label="决定理由（可选，不要填写密钥）" rules={[{ max: 2000 }]}><Input.TextArea rows={2} maxLength={2000} /></Form.Item>
          </>}
          <Space wrap><Button type="primary" danger={dialog.action === 'cancel' || dialog.action === 'reject'} htmlType="submit" loading={busy} disabled={!!stale || uncertain || dialog.action === 'view'}>
            {dialog.kind === 'control' ? `确认${controlLabels[dialog.action]}` : dialog.action === 'modify' ? '确认修改并暂停' : dialog.action === 'approve' ? '确认批准并暂停' : dialog.action === 'view' ? '仅查看' : '确认拒绝'}
          </Button><Button disabled={busy} onClick={() => { setDialog(null); refresh(); }}>关闭并刷新</Button></Space>
        </Form>
      </>}
    </Modal>
  </section>;
}
