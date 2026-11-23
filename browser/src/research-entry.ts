import type { TaskSubmission } from './types';

export type ResearchKind = NonNullable<TaskSubmission['workflow_kind']>;
export type ResearchEntry = ResearchKind | 'evidence';

export const researchLabels: Record<ResearchKind, string> = {
  research: '资料调研',
  literature_review: '论文综述',
  comparison: '技术对比',
  research_proposal: '研究方案',
  document_analysis: '项目文档分析',
};

export const researchEntryLabels: Record<ResearchEntry, string> = {
  ...researchLabels, evidence: '项目证据库',
};

export function readResearchEntry(search: string): ResearchEntry | undefined {
  const value = new URLSearchParams(search).get('research');
  return value && Object.hasOwn(researchEntryLabels, value) ? value as ResearchEntry : undefined;
}

export function projectDestination(projectId: string, entry?: ResearchEntry) {
  const path = `/projects/${encodeURIComponent(projectId)}`;
  if (entry === 'evidence') return `${path}/evidence`;
  return entry ? `${path}?research=${entry}` : path;
}
