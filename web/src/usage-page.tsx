import { useState } from 'react';
import { Alert, Collapse, Empty, Segmented, Select } from 'antd';
import type { TableColumnsType } from 'antd';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useConnection } from './connection';
import { Metric, PageHeading, QueryError } from './components';
import type { UsageCounts, UsageDay, UsageGroup } from './usage-types';
import { usePageReady } from './page-transition';
import { SNAPSHOT_REFRESH_MS } from './snapshot-refresh';
import WorkbenchTable from './workbench-table';

const format = (value: number) => value.toLocaleString('zh-CN');
const short = (value: number) => new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
const stageLabels: Record<string, string> = { planning: '研究规划', screening: '资料初筛',
  reading: '精读资料', refining: '独立复筛', writing: '综合写作', review: '结论审稿', other: '其他调用' };
const counts = [
  { title: '已确认 Tokens', dataIndex: 'total_tokens', align: 'right', render: format },
  { title: '输入 Tokens', dataIndex: 'input_tokens', align: 'right', render: format },
  { title: '输出 Tokens', dataIndex: 'output_tokens', align: 'right', render: format },
  { title: '调用次数', dataIndex: 'calls', align: 'right', render: format },
  { title: '用量未确认', dataIndex: 'unknown_usage_calls', align: 'right', render: format },
  { title: '失败响应', dataIndex: 'failed_calls', align: 'right', render: format },
] satisfies TableColumnsType<UsageCounts>;

function Trend({ daily, metric }: { daily: UsageDay[]; metric: 'total_tokens' | 'calls' }) {
  const maximum = Math.max(1, ...daily.map((day) => day[metric]));
  const points = daily.map((day, index) => `${index / Math.max(1, daily.length - 1) * 880 + 10},${190 - day[metric] / maximum * 170}`).join(' ');
  return <>
    <div className="usage-plot">
      <div className="usage-y-axis" aria-hidden="true"><span>{short(maximum)}</span><span>{short(maximum / 2)}</span><span>0</span></div>
      <svg viewBox="0 0 900 200" preserveAspectRatio="none" role="img" aria-label={`每日${metric === 'calls' ? '调用次数' : '已确认 Token'}趋势`}>
        <title>每日用量；可展开下方的每日明细查看准确数值。</title>
        {[20, 105, 190].map((y) => <line key={y} x1="0" x2="900" y1={y} y2={y} stroke="var(--border-soft)" strokeDasharray="3 5" />)}
        <polygon points={`10,190 ${points} 890,190`} fill="var(--ink)" fillOpacity="0.06" />
        <polyline points={points} fill="none" stroke="var(--ink)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
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
    placeholderData: keepPreviousData, refetchInterval: SNAPSHOT_REFRESH_MS });
  usePageReady(!query.isPending);
  const data = query.data;
  const maximum = Math.max(1, ...(data?.groups.map((item) => item[metric]) ?? []));
  return <>
    <PageHeading title="用量统计" />
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
    {query.isPending || query.isPlaceholderData ? null : !data ? <QueryError error={query.error} retry={() => void query.refetch()} /> : <>
      <div className="metrics-grid usage-metrics" data-testid="usage-totals">
        <Metric label="已确认 Tokens" value={data.totals.total_tokens} />
        <Metric label="输入 Tokens" value={data.totals.input_tokens} />
        <Metric label="输出 Tokens" value={data.totals.output_tokens} />
        <Metric label="调用次数" value={data.totals.calls} />
      </div>
      {!!data.stages?.length && <section className="usage-table-section" aria-label="研究阶段用量">
        <h2>研究阶段用量</h2>
        <WorkbenchTable<UsageGroup> rowKey="id" size="small" pagination={false} dataSource={data.stages} scroll={{ x: 800 }}
          columns={[{ title: '阶段', dataIndex: 'id', className: 'table-primary', render: (value: string) => stageLabels[value] ?? '其他调用' }, ...counts]} />
      </section>}
      {data.totals.unknown_usage_calls > 0 && <Alert showIcon type="warning" title={`${format(data.totals.unknown_usage_calls)} 次调用的用量尚未确认`}
        description="Token 合计只包含模型已返回的用量。进行中、中断或未返回用量的调用会单独标记，不会自动重发。" />}
      {data.totals.calls === 0 ? <Empty className="usage-empty" description="暂无调用记录" /> : <>
        <section className="usage-surface" aria-label="每日趋势">
          <div className="usage-section-heading"><div><h2>每日趋势</h2><p>{days === 0 ? '最近 90 天的趋势，汇总涵盖全部时间。' : `最近 ${days} 天`} · UTC+8</p></div>
            <Segmented aria-label="图表指标" value={metric} onChange={setMetric} options={[
              { value: 'total_tokens', label: 'Tokens' }, { value: 'calls', label: '调用次数' },
            ]} /></div>
          <Trend daily={data.daily} metric={metric} />
        </section>
        <section className="usage-table-section" aria-label="分组用量">
          <div className="usage-section-heading"><h2>{groupBy === 'api' ? 'API 配置用量分布' : '模型用量分布'}</h2></div>
          <div className="usage-ranking">{data.groups.slice(0, 8).map((item) => <button key={item.id} className="usage-rank-row" type="button"
            aria-label={`筛选${groupBy === 'api' ? ' API 配置' : '模型'} ${item.label}`}
            onClick={() => groupBy === 'api' ? setApiId(item.id) : setModel(item.id)}>
            <span className="usage-rank-name">{item.label}</span><span className="usage-rank-track"><span style={{ width: `${item[metric] / maximum * 100}%` }} /></span>
            <strong>{format(item[metric])}</strong>
          </button>)}</div>
          <WorkbenchTable<UsageGroup> rowKey="id" size="small" dataSource={data.groups} scroll={{ x: 800 }} pagination={{ pageSize: 10 }}
            columns={[{ title: groupBy === 'api' ? 'API 配置' : '模型', dataIndex: 'label', className: 'table-primary', width: 200, ellipsis: true }, ...counts]} />
          {data.has_more_groups && <p>展示用量最高的 500 项；上方合计仍包含全部分组。</p>}
        </section>
        <Collapse className="usage-daily" items={[{ key: 'daily', label: '查看每日明细', children: <WorkbenchTable<UsageDay> rowKey="date" size="small"
          dataSource={data.daily} scroll={{ x: 800 }} pagination={{ pageSize: 10 }}
          columns={[{ title: '日期（UTC+8）', dataIndex: 'date', className: 'table-primary' }, ...counts]} /> }]} />
      </>}
      <p className="usage-footnote">更新于 {new Date(query.dataUpdatedAt).toLocaleTimeString('zh-CN')}</p>
    </>}
  </>;
}
