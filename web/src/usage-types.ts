export interface UsageCounts {
  calls: number; input_tokens: number; output_tokens: number; total_tokens: number;
  unknown_usage_calls: number; failed_calls: number;
  cached_input_tokens?: number | null; uncached_input_tokens?: number | null;
  cache_known_calls?: number; unknown_cache_calls?: number; cache_known_input_tokens?: number;
  cache_hit_rate?: number | null;
  estimated_cny?: number | null; estimated_usd?: number | null;
  cost_known_calls?: number; unknown_cost_calls?: number;
}
export interface UsageGroup extends UsageCounts { id: string; label: string }
export interface UsageDay extends UsageCounts { date: string }
export interface UsageSummary {
  group_by: 'api' | 'model'; days: 0 | 7 | 30 | 90; utc_offset_minutes: number;
  recorded_since: string | null; trend_from: string; totals: UsageCounts;
  groups: UsageGroup[]; daily: UsageDay[];
  options: { apis: { id: string; name: string }[]; models: string[] }; has_more_groups: boolean;
  stages?: UsageGroup[];
}
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const counts = (value: unknown): value is UsageCounts => record(value)
  && ['calls', 'input_tokens', 'output_tokens', 'total_tokens', 'unknown_usage_calls', 'failed_calls']
    .every((key) => Number.isSafeInteger(value[key]) && Number(value[key]) >= 0)
  && Number(value.unknown_usage_calls) <= Number(value.calls) && Number(value.failed_calls) <= Number(value.calls)
  && ['estimated_cny', 'estimated_usd'].every(k => value[k] == null || typeof value[k] === 'number' && Number.isFinite(value[k]) && Number(value[k]) >= 0)
  && ['cost_known_calls', 'unknown_cost_calls'].every(k => value[k] === undefined || Number.isSafeInteger(value[k]) && Number(value[k]) >= 0);
const cacheCounts = (value: UsageCounts) => ['cached_input_tokens', 'uncached_input_tokens']
  .every((key) => { const item = (value as unknown as Record<string, unknown>)[key];
    return item === undefined || item === null || Number.isSafeInteger(item) && Number(item) >= 0; })
  && ['cache_known_calls', 'unknown_cache_calls', 'cache_known_input_tokens'].every((key) => {
    const item = (value as unknown as Record<string, unknown>)[key];
    return item === undefined || Number.isSafeInteger(item) && Number(item) >= 0;
  }) && (value.cache_hit_rate === undefined || value.cache_hit_rate === null
    || Number.isFinite(value.cache_hit_rate) && value.cache_hit_rate >= 0 && value.cache_hit_rate <= 1);
export function isUsageSummary(value: unknown): value is UsageSummary {
  if (!record(value) || (value.group_by !== 'api' && value.group_by !== 'model')
    || typeof value.days !== 'number' || ![0, 7, 30, 90].includes(value.days)) return false;
  return value.utc_offset_minutes === 480 && (value.recorded_since === null || typeof value.recorded_since === 'string')
    && typeof value.trend_from === 'string' && counts(value.totals) && cacheCounts(value.totals)
    && Array.isArray(value.groups) && value.groups.length <= 500
    && value.groups.every((item) => counts(item) && cacheCounts(item) && record(item) && typeof item.id === 'string' && typeof item.label === 'string')
    && Array.isArray(value.daily) && value.daily.length === (value.days || 90)
    && value.daily.every((item) => counts(item) && record(item) && typeof item.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(item.date))
    && record(value.options) && Array.isArray(value.options.apis) && value.options.apis.length <= 500
    && value.options.apis.every((item) => record(item) && typeof item.id === 'string' && typeof item.name === 'string')
    && Array.isArray(value.options.models) && value.options.models.length <= 500 && value.options.models.every((item) => typeof item === 'string')
    && typeof value.has_more_groups === 'boolean'
    && (value.stages === undefined || Array.isArray(value.stages) && value.stages.length <= 10
      && value.stages.every((item) => counts(item) && cacheCounts(item) && record(item) && typeof item.id === 'string' && typeof item.label === 'string'));
}
