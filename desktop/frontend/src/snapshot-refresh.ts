import type { Page, PDFDocument, TaskStatus } from './types';

export const SNAPSHOT_REFRESH_MS = 15_000;
export const ACTIVE_TASK_REFRESH_MS = 5_000;
export const DOCUMENT_PROCESSING_REFRESH_MS = 3_000;

const terminal = new Set<TaskStatus>(['succeeded', 'partial', 'failed', 'budget_exceeded', 'cancelled']);

export function isActiveTask(status: TaskStatus) {
  return !terminal.has(status);
}

export function documentRefreshInterval(documents?: Page<PDFDocument>) {
  return documents?.items.some(item => item.status === 'queued' || item.status === 'processing' || item.status === 'deleting')
    ? DOCUMENT_PROCESSING_REFRESH_MS : SNAPSHOT_REFRESH_MS;
}
