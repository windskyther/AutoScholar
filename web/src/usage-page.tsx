import { useState } from 'react';
import { Alert, Collapse, Empty, Segmented, Select, Table } from 'antd';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useConnection } from './connection';
import { Loading, Metric, PageHeading, QueryError } from './components';
import type { UsageDay, UsageGroup } from './usage-types';
import { usePageReady } from './page-transition';

const format = (value: number) => value.toLocaleString('zh-CN');
const short = (value: number) => new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
const reported = (value: number | null | undefined) => value == null ? '未提供' : format(value);
const money = (value: number | null | undefined) => value == null ? '未提供'
  : value > 0 && value < 0.00000001 ? '<0.00000001' : value.toLocaleString('zh-CN', { maximumFractionDigits: 8 });
const stageLabels: Record<string, string> = { planning: '研究规划', screening: '资料初筛',
  reading: '精读资料', refining: '独立复筛', writing: '综合写作', review: '结论审稿', other: '其他调用' };

function Trend({ daily, metric }: { daily: UsageDay[]; metric: 'total_tokens' | 'calls' }) {
  const maximum = Math.max(1, ...daily.map((day) => day[metric]));
  const points = daily.map((day, index) => `${index / Math.max(1, daily.length - 1) * 880 + 10},${190 - day[metric] / maximum * 170}`).join(' ');
  return <>
    <div className="usage-plot">
      <div className="usage-y-axis" aria-hidden="true"><span>{short(maximum)}</span><span>{short(maximum / 2)}</span><span>0</span></div>
      <svg viewBox="0 0 900 200" preserveAspectRatio="none" role="img" aria-label={`每日${metric === 'calls' ? '调用次数' : '已确认 Token'}趋势`}>
        <title>每日用量；可展开下方的每日明细查看准确数值。</title>
        {[20, 105, 190].map((y) => <line key={y} x1="0" x2="900" y1={y} y2={y} stroke="#dce5e1" strokeDasharray="3 5" />)}
        <polygon points={`10,190 ${points} 890,190`} fill="#1f7165" fillOpacity="0.08" />
        <polyline points={points} fill="none" stroke="#1f7165" strokeWidth="2" vectorEffect="non-scaling-stroke" />
        {daily.map((day, index) => <rect key={day.date} x={index / daily.length * 900} y="0" width={900 / daily.length} height="200" fill="transparent">
          <title>{`${day.date}：${format(day.total_tokens)} 已确认 Tokens；${format(day.calls)} 次调用；${format(day.unknown_usage_calls)} 次用量未确认`}</title>
        </rect>)}
      </svg>
    </div>
    <div className="usage-x-axis" aria-hidden="true"><span>{daily[0]?.date}</span><span>{daily[Math.floor(daily.length / 2)]?.date}</span><span>{daily.at(-1)?.date}</span></div>
  </>;
}

export default function UsagePage() {
  const { api } = useConnection();
  const [groupBy, setGroupBy] = useState<'api' | 'model'>('api');
  const [days, setDays] = useState<0 | 7 | 30 | 90>(30);
  const [apiId, setApiId] = useState<string | undefined>();
  const [model, setModel] = useState<string | undefined>();
  const [metric, setMetric] = useState<'total_tokens' | 'calls'>('total_tokens');
  const query = useQuery({ queryKey: ['usage', groupBy, days, apiId, model],
    queryFn: ({ signal }) => api!.usage(groupBy, days, apiId, model, signal),
    placeholderData: keepPreviousData, refetchInterval: 15000 });
  usePageReady(!query.isPending);
  const data = query.data;
  const columns = [
    { title: '已确认 Tokens', dataIndex: 'total_tokens', render: format },
    { title: '输入 Tokens', dataIndex: 'input_tokens', render: format },
    { title: '输出 Tokens', dataIndex: 'output_tokens', render: format },
    { title: '调用次数', dataIndex: 'calls', render: format },
    { title: '用量未确认', dataIndex: 'unknown_usage_calls', render: format },
    { title: '失败响应', dataIndex: 'failed_calls', render: format },
    { title: '已报告命中输入', dataIndex: 'cached_input_tokens', render: reported },
    { title: '已报告未命中输入', dataIndex: 'uncached_input_tokens', render: reported },
    { title: '估算 CNY', dataIndex: 'estimated_cny', render: money },
    { title: '估算 USD', dataIndex: 'estimated_usd', render: money },
  ];
  const maximum = Math.max(1, ...(data?.groups.map((item) => item[metric]) ?? []));
  return <>
    <PageHeading title="用量统计" subtitle="查看各模型与 API 配置的实际调用及 Token 用量。" refresh={() => void query.refetch()} />
    <div className="usage-filters">
      <label>时间范围<Select aria-label="时间范围" value={days} onChange={setDays} options={[
        { value: 7, label: '最近 7 天' }, { value: 30, label: '最近 30 天' }, { value: 90, label: '最近 90 天' }, { value: 0, label: '全部时间' },
      ]} /></label>
      <label>API 配置<Select aria-label="API 配置" value={apiId} placeholder="全部 API" allowClear showSearch optionFilterProp="label" onChange={setApiId}
        options={data?.options.apis.map((item) => ({ value: item.id, label: item.name })) ?? []} /></label>
      <label>模型<Select aria-label="模型筛选" value={model} placeholder="全部模型" allowClear showSearch optionFilterProp="label" onChange={setModel}
        options={data?.options.models.map((item) => ({ value: item, label: item })) ?? []} /></label>
      <label>统计维度<Segmented aria-label="统计维度" value={groupBy} onChange={setGroupBy}
        options={[{ value: 'api', label: '按 API 配置' }, { value: 'model', label: '按模型' }]} /></label>
    </div>
    {query.isPending || query.isPlaceholderData ? <Loading /> : query.isError ? <QueryError error={query.error} retry={() => void query.refetch()} /> : data && <>
      <div className="metrics-grid usage-metrics" data-testid="usage-totals">
        <Metric label="已确认 Tokens" value={data.totals.total_tokens} />
        <Metric label="输入 Tokens" value={data.totals.input_tokens} />
        <Metric label="输出 Tokens" value={data.totals.output_tokens} />
        <Metric label="调用次数" value={data.totals.calls} />
      </div>
      <section className="usage-surface" aria-label="缓存复用">
        <h2>缓存复用</h2>
        <div className="metrics-grid">
          <div className="metric"><span>已报告命中输入</span><strong>{reported(data.totals.cached_input_tokens)}</strong></div>
          <div className="metric"><span>已报告未命中输入</span><strong>{reported(data.totals.uncached_input_tokens)}</strong></div>
          <div className="metric"><span>已报告输入的命中率</span><strong>{data.totals.cache_hit_rate == null ? '未提供' : `${(data.totals.cache_hit_rate * 100).toFixed(1)}%`}</strong></div>
          <div className="metric"><span>缓存用量未确认的调用</span><strong>{format(data.totals.unknown_cache_calls ?? data.totals.calls)}</strong></div>
        </div>
        <p>命中输入已包含在输入 Token 中，不重复计数。供应商未提供的缓存数据和旧记录显示为“未提供”；命中率仅计算已报告缓存明细的输入。</p>
      </section>
      <section className="usage-surface" aria-label="模型费用估算">
        <h2>模型费用估算</h2>
        <p>{data.totals.estimated_cny == null && data.totals.estimated_usd == null ? '暂不可估算' : [
          data.totals.estimated_cny == null ? null : `CNY ${money(data.totals.estimated_cny)}`,
          data.totals.estimated_usd == null ? null : `USD ${money(data.totals.estimated_usd)}`,
        ].filter(Boolean).join(' · ')}</p>
        <p>已估算 {data.totals.cost_known_calls ?? 0} 次；{data.totals.unknown_cost_calls ?? data.totals.calls} 次缺少价格或用量信息。
          按调用时保存的单价计算，各币种分别汇总；不包含检索费用，不代表服务商账单。</p>
      </section>
      {!!data.stages?.length && <section className="usage-surface" aria-label="研究阶段用量">
        <h2>研究阶段用量</h2>
        <Table<UsageGroup> rowKey="id" size="small" pagination={false} dataSource={data.stages} scroll={{ x: 1100 }}
          columns={[{ title: '阶段', dataIndex: 'id', render: (value: string) => stageLabels[value] ?? '其他调用' }, ...columns]} />
      </section>}
      {data.totals.unknown_usage_calls > 0 && <Alert showIcon type="warning" title={`${format(data.totals.unknown_usage_calls)} 次调用的用量尚未确认`}
        description="Token 合计只包含模型已返回的用量。进行中、中断或未返回用量的调用会单独标记，不会自动重发。" />}
      {data.totals.calls === 0 ? <Empty className="usage-empty" description={apiId || model || data.options.apis.length
        ? '当前筛选下没有调用记录，可调整时间范围、API 或模型。' : '暂无调用记录。执行研究任务后，这里会自动更新。'} /> : <>
        <section className="usage-surface" aria-label="每日趋势">
          <div className="usage-section-heading"><div><h2>每日趋势</h2><p>{days === 0 ? '最近 90 天的趋势，汇总涵盖全部时间。' : `最近 ${days} 天`} · UTC+8</p></div>
            <Segmented aria-label="图表指标" value={metric} onChange={setMetric} options={[
              { value: 'total_tokens', label: 'Tokens' }, { value: 'calls', label: '调用次数' },
            ]} /></div>
          <Trend daily={data.daily} metric={metric} />
        </section>
        <section className="usage-surface" aria-label="分组用量">
          <div className="usage-section-heading"><h2>{groupBy === 'api' ? 'API 配置用量分布' : '模型用量分布'}</h2></div>
          <div className="usage-ranking">{data.groups.slice(0, 8).map((item) => <button key={item.id} className="usage-rank-row" type="button"
            aria-label={`筛选${groupBy === 'api' ? ' API 配置' : '模型'} ${item.label}`}
            onClick={() => groupBy === 'api' ? setApiId(item.id) : setModel(item.id)}>
            <span className="usage-rank-name">{item.label}</span><span className="usage-rank-track"><span style={{ width: `${item[metric] / maximum * 100}%` }} /></span>
            <strong>{format(item[metric])}</strong>
          </button>)}</div>
          <Table<UsageGroup> rowKey="id" size="small" dataSource={data.groups} scroll={{ x: 950 }} pagination={{ pageSize: 10, showSizeChanger: false }}
            columns={[{ title: groupBy === 'api' ? 'API 配置' : '模型', dataIndex: 'label', width: 200, ellipsis: true }, ...columns]} />
          {data.has_more_groups && <p>展示用量最高的 500 项；上方合计仍包含全部分组。</p>}
        </section>
        <Collapse className="usage-daily" items={[{ key: 'daily', label: '查看每日明细', children: <Table<UsageDay> rowKey="date" size="small"
          dataSource={data.daily} scroll={{ x: 950 }} pagination={{ pageSize: 10, showSizeChanger: false }} columns={[{ title: '日期（UTC+8）', dataIndex: 'date' }, ...columns]} /> }]} />
      </>}
      <p className="usage-footnote">统计从启用用量记录后开始，旧任务仍可在任务详情查看。仅统计本工作台内的模型调用，不代表服务商账户账单。
        <span>更新于 {new Date(query.dataUpdatedAt).toLocaleTimeString('zh-CN')}</span></p>
    </>}
  </>;
}
