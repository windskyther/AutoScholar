import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient, isWriteUncertain } from './api';
import type { TaskSubmission } from './types';

const time = '2026-10-06T00:00:00Z';
const document = { id: 'doc-a', project_id: 'project-a', original_filename: 'paper.pdf', title: '公开 PDF',
  content_type: 'application/pdf', sha256: 'a'.repeat(64), size_bytes: 12, status: 'queued', page_count: null,
  chunk_count: 0, embedding_model: null, index_version: 0, error_code: null, error_message: null,
  created_at: time, updated_at: time };
const payload: TaskSubmission = { objective: '比较公开模型', mode: 'autonomous', project_id: 'project-a',
  document_ids: [], retrieval_mode: 'hybrid_rerank', research_sources: ['web'], budget: {
    steps: 20, replans: 3, model_calls: 60, tool_calls: 50, search_queries: 20, code_repairs: 3,
    training_runs: 4, sandbox_runs: 40, total_tokens: 1000, wall_seconds: 1800,
  } };
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
}
afterEach(() => vi.unstubAllGlobals());

describe('explicit browser writes', () => {
  it('creates projects using authenticated JSON without automatic retries', async () => {
    const fetcher = vi.fn().mockResolvedValue(json({ id: 'project-a', name: '中文项目', description: null, created_at: time, updated_at: time }, 201));
    vi.stubGlobal('fetch', fetcher);
    await new ApiClient('test-token').createProject(' 中文项目 ', ' ');
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][1]).toMatchObject({ method: 'POST', credentials: 'omit', cache: 'no-store' });
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ name: '中文项目', description: null });
  });
  it('uploads a multipart PDF without overriding the browser boundary', async () => {
    const fetcher = vi.fn().mockResolvedValue(json(document, 202));
    vi.stubGlobal('fetch', fetcher);
    const file = new File(['%PDF-1.4\npublic'], 'paper.pdf', { type: 'application/pdf' });
    await new ApiClient('test-token').uploadPDF('project-a', file, ' 中文标题 ', 1024);
    const options = fetcher.mock.calls[0][1];
    expect(options.method).toBe('POST');
    expect(options.body).toBeInstanceOf(FormData);
    expect(options.body.get('title')).toBe('中文标题');
    expect(options.headers['Content-Type']).toBeUndefined();
    expect(options.headers.Authorization).toBe('Bearer test-token');
  });
  it.each([
    [new File(['secret'], '.env'), 'unsupported_document_type'],
    [new File(['plain text'], 'bad.pdf', { type: 'application/pdf' }), 'invalid_pdf'],
    [new File(['%PDF-' + 'x'.repeat(1024)], 'large.pdf', { type: 'application/pdf' }), 'document_too_large'],
    [new File(['%PDF-1.4'], 'wrong.pdf', { type: 'text/html' }), 'unsupported_document_type'],
  ])('rejects invalid upload locally without a request', async (file, code) => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    await expect(new ApiClient('test-token').uploadPDF('project-a', file as File, '', 1024)).rejects.toMatchObject({ code });
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('rejects documents belonging to another project before rendering', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ items: [{ ...document, project_id: 'foreign' }], total: 1, limit: 20, offset: 0 })));
    await expect(new ApiClient('test-token').documents('project-a', 1)).rejects.toMatchObject({ code: 'invalid_response' });
  });
  it('handles the empty asynchronous delete response', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 202 }));
    vi.stubGlobal('fetch', fetcher);
    await expect(new ApiClient('test-token').deleteDocument('project-a', 'doc-a')).resolves.toBeUndefined();
    expect(fetcher.mock.calls[0][0]).toBe('/api/workbench/projects/project-a/documents/doc-a');
    expect(fetcher.mock.calls[0][1].method).toBe('DELETE');
  });
  it('preserves the original idempotency key and body on explicit task retry', async () => {
    const fetcher = vi.fn().mockRejectedValueOnce(new Error('connection lost')).mockResolvedValueOnce(json({
      task_id: 'task-a', status: 'queued', created: false, status_url: '/workbench/agent/tasks/task-a',
    }, 202));
    vi.stubGlobal('fetch', fetcher);
    const client = new ApiClient('test-token');
    await expect(client.submitTask(payload, 'original-key')).rejects.toMatchObject({ code: 'write_result_unknown' });
    expect(fetcher).toHaveBeenCalledTimes(1);
    await expect(client.submitTask(payload, 'original-key')).resolves.toMatchObject({ task_id: 'task-a', created: false });
    expect(fetcher.mock.calls[0][1].headers['Idempotency-Key']).toBe('original-key');
    expect(fetcher.mock.calls[1][1].headers['Idempotency-Key']).toBe('original-key');
    expect(fetcher.mock.calls[0][1].body).toBe(fetcher.mock.calls[1][1].body);
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual(payload);
  });
  it.each([{}, { task_id: 'task-a' }])('treats invalid success receipts as uncertain, not safe failure', async (value) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(value, 202)));
    await expect(new ApiClient('test-token').submitTask(payload, 'key')).rejects.toMatchObject({ code: 'write_result_unknown' });
  });
  it('marks server failures as uncertain without automatic re-execution', async () => {
    const fetcher = vi.fn().mockResolvedValue(json({ error: { code: 'internal_error' } }, 500));
    vi.stubGlobal('fetch', fetcher);
    try { await new ApiClient('test-token').submitTask(payload, 'key'); throw new Error('Expected error'); }
    catch (error) { expect(isWriteUncertain(error)).toBe(true); }
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('rejects invalid idempotency headers before network access', async () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    await expect(new ApiClient('test-token').submitTask(payload, 'bad\nkey')).rejects.toMatchObject({ code: 'request_validation_error' });
    expect(fetcher).not.toHaveBeenCalled();
  });
});
