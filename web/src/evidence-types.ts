import type { Page } from './types';

export interface BankSource {
  title: string; url: string | null; kind: 'web' | 'paper' | 'document'; quote: string;
  published_at: string | null; fetched_at: string | null; availability: string;
  page: number | null; truncated: boolean; recheck_due: boolean;
}
export interface BankClaim {
  id: string; task_id: string; text: string; objective: string; enabled: boolean;
  status: 'ready' | 'pending' | 'withdrawn'; sources: BankSource[]; reuse_count: number; created_at: string;
}
export interface EvidencePolicy { web_days: number; paper_days: number }
export interface HistoryImport { queued_tasks: number; pending_claims: number; skipped_tasks: number; next_cursor: string | null }
export interface ReuseSummary {
  enabled: boolean; note: string | null; selected: number; adopted: number; retrieval_calls: number;
  items: { claim: string; source_objective: string; source_task_id: string; status: string; reason: string }[];
}
const obj = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v);
const count = (v: unknown) => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
const nullable = (v: unknown) => v === null || typeof v === 'string';
export function isBankClaim(v: unknown): v is BankClaim {
  return obj(v) && ['id', 'task_id', 'text', 'objective', 'created_at'].every(k => typeof v[k] === 'string')
    && typeof v.enabled === 'boolean' && ['ready', 'pending', 'withdrawn'].includes(String(v.status))
    && count(v.reuse_count) && Array.isArray(v.sources) && v.sources.length <= 6 && v.sources.every(s => obj(s)
      && ['title', 'quote', 'availability'].every(k => typeof s[k] === 'string')
      && ['web', 'paper', 'document'].includes(String(s.kind)) && nullable(s.url) && nullable(s.published_at)
      && nullable(s.fetched_at) && (s.page === null || count(s.page)) && typeof s.truncated === 'boolean' && typeof s.recheck_due === 'boolean');
}
export function isEvidencePolicy(v: unknown): v is EvidencePolicy {
  return obj(v) && ['web_days', 'paper_days'].every(k => count(v[k]) && Number(v[k]) >= 1 && Number(v[k]) <= 365);
}
export function isHistoryImport(v: unknown): v is HistoryImport {
  return obj(v) && ['queued_tasks', 'pending_claims', 'skipped_tasks'].every(k => count(v[k])) && nullable(v.next_cursor);
}
export function isReuse(v: unknown): v is ReuseSummary {
  return obj(v) && typeof v.enabled === 'boolean' && nullable(v.note)
    && ['selected', 'adopted', 'retrieval_calls'].every(k => count(v[k]))
    && Array.isArray(v.items) && v.items.length <= 12 && v.items.every(i => obj(i)
      && ['claim', 'source_objective', 'source_task_id', 'reason'].every(k => typeof i[k] === 'string')
      && ['selected', 'adopted', 'excluded'].includes(String(i.status)));
}
export type BankPage = Page<BankClaim>;
