import type { ArtifactPreview, ArtifactResource, EvidenceResource, ExperimentResource } from './types';
import { isSpecification } from './guards';

const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value: unknown, max = 8192): value is string => typeof value === 'string' && value.length <= max;
const nullable = (value: unknown) => value === null || text(value);
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const nonnegative = (value: unknown): value is number => finite(value) && value >= 0;
const identity = (value: Record<string, unknown>) => text(value.id, 64) && text(value.task_id, 64);
export function safeExternalURL(value: unknown): string | null {
  if (!text(value, 2048) || /[\s\x00-\x1f]/.test(value)) return null;
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? value : null;
  } catch { return null; }
}
export function isEvidence(value: unknown): value is EvidenceResource {
  return object(value) && identity(value) && text(value.citation_key, 16) && text(value.source_type)
    && text(value.title, 2048) && (value.url === null || safeExternalURL(value.url) !== null)
    && Array.isArray(value.authors) && value.authors.length <= 30 && value.authors.every((author) => text(author, 200))
    && (value.year === null || finite(value.year)) && text(value.claim) && text(value.excerpt)
    && typeof value.text_truncated === 'boolean' && nullable(value.document_id) && nullable(value.section)
    && (value.page === null || nonnegative(value.page));
}
export function isExperiment(value: unknown): value is ExperimentResource {
  if (!object(value) || !identity(value) || !text(value.name, 200) || !text(value.status)
    || !(value.specification === null || isSpecification(value.specification)) || !Array.isArray(value.runs) || value.runs.length > 2) return false;
  return value.runs.every((run) => object(run) && ['mlp', 'cnn'].includes(String(run.model))
    && nonnegative(run.test_accuracy) && run.test_accuracy <= 1 && nonnegative(run.parameters) && nonnegative(run.duration_seconds)
    && Array.isArray(run.train_loss) && run.train_loss.length >= 1 && run.train_loss.length <= 10 && run.train_loss.every(nonnegative)
    && Array.isArray(run.train_accuracy) && run.train_accuracy.length === run.train_loss.length
    && run.train_accuracy.every((item) => nonnegative(item) && item <= 1))
    && [null, 'mlp', 'cnn', 'tie'].includes(value.winner as string | null)
    && (value.accuracy_delta === null || (finite(value.accuracy_delta) && Math.abs(value.accuracy_delta) <= 1))
    && nullable(value.error_code) && text(value.created_at) && nullable(value.started_at) && nullable(value.finished_at);
}
export const registeredPaths = new Set(['outputs/raw_metrics.json', 'outputs/metrics.json', 'outputs/experiment.json',
  'outputs/loss.png', 'outputs/accuracy.png', 'reports/report.md', 'checkpoints/mlp.pt', 'checkpoints/cnn.pt', 'logs/stdout.log', 'logs/stderr.log']);
export function isArtifact(value: unknown): value is ArtifactResource {
  return object(value) && identity(value) && text(value.experiment_id, 64) && text(value.type)
    && text(value.path) && registeredPaths.has(value.path) && text(value.media_type, 100)
    && nonnegative(value.size_bytes) && Number.isSafeInteger(value.size_bytes) && value.size_bytes <= 16777216
    && text(value.sha256, 64) && /^[0-9a-f]{64}$/.test(value.sha256) && text(value.created_at)
    && typeof value.previewable === 'boolean';
}
export function isPreview(value: unknown): value is ArtifactPreview {
  return object(value) && text(value.task_id, 64) && text(value.artifact_id, 64)
    && text(value.sha256, 64) && /^[0-9a-f]{64}$/.test(value.sha256)
    && text(value.text, 262144) && typeof value.truncated === 'boolean';
}
