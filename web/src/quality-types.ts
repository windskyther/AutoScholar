export const qualityPhases: Record<string, string> = {
  collect: '整理候选资料', screen: '初步筛选', read: '读取重点来源', refine: '独立复筛',
  write: '综合写作', check: '审查结论', revise: '定向修订', supplement: '补充检索', done: '审查完成',
};
export const verdicts: Record<string, string> = { keep: '采用', background: '背景参考', reject: '排除' };
export const access: Record<string, string> = {
  summary_only: '仅有摘要', extracted_text: '已读取正文片段', document_excerpt: '项目文档片段',
};
export interface Assessment {
  source_id: string; verdict: string; relevance: number; time_fit: string; reason: string; quote: string;
}
export interface QualitySource {
  reference: string; title: string; url: string | null; published_at: string | null;
  availability: string; truncated: boolean; read_attempted: boolean;
  initial: Assessment | null; refined: Assessment | null;
}
export interface QualityAudit {
  depth: 'quick' | 'standard' | 'deep'; phase: string; accepted: boolean;
  reads: number; rounds: number; supplemental_queries: number; sources: QualitySource[];
  reviews: { round: number; decision: string; issues: { draft_quote: string; reason: string }[] }[];
}
export function isQualityAudit(value: unknown): value is QualityAudit {
  const obj = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v);
  const count = (v: unknown) => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
  const nullable = (v: unknown) => v === null || typeof v === 'string';
  const assessment = (v: unknown) => v === null || (obj(v) && typeof v.source_id === 'string'
    && typeof v.reason === 'string' && typeof v.quote === 'string' && typeof v.time_fit === 'string'
    && typeof v.relevance === 'number' && v.relevance >= 0 && v.relevance <= 1
    && typeof v.verdict === 'string' && Object.hasOwn(verdicts, v.verdict));
  return obj(value) && ['quick', 'standard', 'deep'].includes(String(value.depth))
    && typeof value.phase === 'string' && Object.hasOwn(qualityPhases, value.phase) && typeof value.accepted === 'boolean'
    && count(value.reads) && count(value.rounds) && count(value.supplemental_queries)
    && Array.isArray(value.sources) && value.sources.length <= 40 && value.sources.every(s => obj(s)
      && typeof s.reference === 'string' && /^E[1-9]\d*$/.test(s.reference) && typeof s.title === 'string'
      && nullable(s.url) && nullable(s.published_at) && typeof s.availability === 'string' && Object.hasOwn(access, s.availability)
      && typeof s.truncated === 'boolean' && typeof s.read_attempted === 'boolean'
      && assessment(s.initial) && assessment(s.refined))
    && Array.isArray(value.reviews) && value.reviews.length <= 3 && value.reviews.every(r => obj(r)
      && count(r.round) && ['accept', 'revise', 'insufficient'].includes(String(r.decision))
      && Array.isArray(r.issues) && r.issues.length <= 16 && r.issues.every(i => obj(i)
        && typeof i.draft_quote === 'string' && typeof i.reason === 'string'));
}
