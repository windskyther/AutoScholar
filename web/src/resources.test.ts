import { afterEach, expect, it, vi } from 'vitest';
import { ApiClient } from './api';
import { isArtifact, isEvidence, isExperiment, safeExternalURL } from './resource-guards';
import type { ArtifactResource } from './types';

const content = new TextEncoder().encode('# 中文公开报告');
const sha = [...new Uint8Array(await crypto.subtle.digest('SHA-256', content))].map((byte) => byte.toString(16).padStart(2, '0')).join('');
const artifact: ArtifactResource = { id: 'artifact-a', task_id: 'child-a', experiment_id: 'experiment-a', type: 'report',
  path: 'reports/report.md', media_type: 'text/markdown; charset=utf-8', size_bytes: content.length, sha256: sha, created_at: '', previewable: true };
const binary = (bytes = content, digest = sha) => new Response(bytes, { headers: { 'content-type': artifact.media_type, 'x-content-sha256': digest } });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
afterEach(() => vi.unstubAllGlobals());

it('downloads only through bearer fetch and verifies UTF-8 bytes/hash', async () => {
  const fetcher = vi.fn().mockResolvedValue(binary()); vi.stubGlobal('fetch', fetcher);
  const blob = await new ApiClient('public-token').downloadArtifact('root-a', artifact);
  expect(await blob.text()).toBe('# 中文公开报告');
  expect(fetcher.mock.calls[0][0]).toBe('/api/workbench/tasks/root-a/resources/artifacts/artifact-a/content');
  expect(fetcher.mock.calls[0][1]).toMatchObject({ headers: { Authorization: 'Bearer public-token' }, credentials: 'omit', redirect: 'error' });
});
it.each(['hash', 'short', 'long', 'header'])('rejects corrupt %s without retry', async (kind) => {
  const bytes = kind === 'hash' ? new Uint8Array(content.length) : kind === 'short' ? content.slice(1)
    : kind === 'long' ? new Uint8Array(content.length + 1) : content;
  const fetcher = vi.fn().mockResolvedValue(binary(bytes, kind === 'header' ? '0'.repeat(64) : sha)); vi.stubGlobal('fetch', fetcher);
  await expect(new ApiClient('public-token').downloadArtifact('root-a', artifact)).rejects.toMatchObject({ code: 'artifact_integrity_failed' });
  expect(fetcher).toHaveBeenCalledTimes(1);
});
it.each([401, 403])('clears authentication on binary %i', async (status) => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({}, status)));
  const disconnected = vi.fn(); const api = new ApiClient('public-token', disconnected);
  await expect(api.downloadArtifact('root-a', artifact)).rejects.toMatchObject({ status });
  expect(api.connected).toBe(false); expect(disconnected).toHaveBeenCalledTimes(1);
});
it('rejects unsafe manifests and traversal before a request', async () => {
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
  for (const item of [{ ...artifact, path: '.env' }, { ...artifact, path: '../report.md' }, { ...artifact, size_bytes: 16777217 }, { ...artifact, id: '../x' }]) {
    await expect(new ApiClient('public-token').downloadArtifact('root-a', item)).rejects.toMatchObject({ code: 'artifact_manifest_invalid' });
  }
  expect(fetcher).not.toHaveBeenCalled(); expect(isArtifact(artifact)).toBe(true);
});
it('checks preview identity and digest', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ task_id: 'other', artifact_id: artifact.id, sha256: sha, text: 'x', truncated: false })));
  await expect(new ApiClient('public-token').previewArtifact('root-a', artifact)).rejects.toMatchObject({ code: 'invalid_response' });
});
it.each(['javascript:alert(1)', 'data:text/html,x', 'https://user:pass@example.org', 'https://example.org/\n'])('refuses unsafe source URL %s', (value) => {
  expect(safeExternalURL(value)).toBeNull();
});
it('accepts safe source URL, evidence and empty experimental measurements', () => {
  expect(safeExternalURL('https://example.org/paper')).toBe('https://example.org/paper');
  expect(isEvidence({ id: 'e', task_id: 'child', citation_key: 'S1', source_type: 'web', title: 'public', url: null,
    authors: [], year: null, claim: 'c', excerpt: '<script>x</script>', text_truncated: false, document_id: null, page: null, section: null })).toBe(true);
  expect(isExperiment({ id: 'e', task_id: 'child', name: 'public', status: 'pending', specification: null,
    runs: [], winner: null, accuracy_delta: null, error_code: null, created_at: '', started_at: null, finished_at: null })).toBe(true);
});
it('checks resource page root identity', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ task_id: 'other', items: [], total: 0, limit: 20, offset: 0 })));
  await expect(new ApiClient('public-token').artifacts('root-a', 1)).rejects.toMatchObject({ code: 'invalid_response' });
});
